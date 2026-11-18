"""Separately frozen H12 v2 evaluator using the original shared budget."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import EvaluationProfile, canonical_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_benchmark import apply_public_amendments
from mgar_v3.quality_eval.h12_runner import (
    ARMS,
    DEV_ENVIRONMENT_V4,
    HOLDOUT_INFRA_V4,
    EvaluationRunner,
    profile_for,
)
from mgar_v3.quality_eval.h12_v2_freeze import (
    PUBLIC_RUNTIME_POLICY,
    development_protocol_metadata,
    load_frozen_benchmark,
    load_public_development_benchmark,
    public_assets_for_task,
    public_development_profile,
)
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root

REVISION = 'h12-benchmark-v2'


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BenchmarkV2Runner(EvaluationRunner):
    """New 12+12 first-sample matrix; old H00 and original scores stay intact."""

    def __init__(
        self,
        engine: Any,
        *,
        experiment_id: str,
        evidence: Path,
        freeze_path: Path,
        freeze_sha256: str,
        api_key: str | None,
        execute_live: bool = False,
        root: Path | None = None,
        public_development: bool = False,
    ) -> None:
        self.public_development = public_development
        base = root or repo_root()
        self.freeze = (
            load_public_development_benchmark(base, freeze_path, freeze_sha256)
            if public_development else load_frozen_benchmark(base, freeze_path, freeze_sha256)
        )
        options: dict[str, Any] = {}
        if public_development:
            options = {
                'environment_revision': DEV_ENVIRONMENT_V4,
                'public_plan_refs': self.freeze['public_plan_refs'],
                'public_probe_root': self.freeze['public_probe_root'],
                'public_readiness_ref': self.freeze['public_subset_readiness'],
                'full_dev_readiness_ref': self.freeze['full_dev_readiness'],
            }
            if execute_live and self.freeze['document']['phase'] != 'FROZEN':
                raise DomainError(
                    'H12_FULL_DEV_READINESS_REQUIRED',
                    'Preparation does not authorize model dispatch', 403,
                )
        super().__init__(
            engine, experiment_id=experiment_id, evidence=evidence,
            image_locks={}, api_key=api_key, root=root,
            execute_live=False, **options,
        )
        self.freeze_path = freeze_path
        self.freeze_sha256 = freeze_sha256
        self._dev_replacement_id = json.loads(
            self.freeze['assets']['proposal'].read_text()
        )['dev_replacement']
        self._image_maps = {
            split: json.loads(self.freeze['assets'][f'{split}_image_map'].read_text())
            for split in (('dev',) if public_development else ('dev', 'holdout'))
        }
        self.environment_revision = DEV_ENVIRONMENT_V4 if public_development else REVISION
        self.execute_live = execute_live
        self._validate_candidate_source()

    def _validate_candidate_source(self) -> None:
        if self._candidate_fingerprint() != self.freeze['candidate_source_sha256']:
            raise DomainError('H12_FROZEN_EXECUTION_CHANGED', 'Candidate source differs', 409)

    def _tasks(self, split: str) -> list[dict[str, Any]]:
        if getattr(self, 'public_development', False) and split != 'dev':
            raise DomainError(
                'H12_HISTORICAL_HOLDOUT_QUARANTINED',
                'This development candidate contains no held-out inputs', 409,
            )
        return cast(list[dict[str, Any]], self.freeze['tasks'][split])

    def _revision_for(self, task: Mapping[str, Any]) -> str:
        return DEV_ENVIRONMENT_V4 if task['split'] == 'dev' else HOLDOUT_INFRA_V4

    def _profile_for(self, task: Mapping[str, Any], arm: str) -> EvaluationProfile:
        if getattr(self, 'public_development', False):
            profile = public_development_profile(
                task, profile_for(task, self.experiment_id, arm,
                                  environment_revision=self._revision_for(task)),
                self._revision_for(task),
            )
            frozen = self._public_plan_for(task, profile.public_test_argv)
            profile = profile.model_copy(update={
                'public_probe': frozen.plan, 'verification_protocol': 'public-probe-v1',
            }) if frozen else profile
            return self._bind_regression_metadata(task, profile)
        return self._bind_regression_metadata(task, profile_for(
            task, self.experiment_id, arm, environment_revision=self._revision_for(task)))

    def _lock_for(self, task: Mapping[str, Any]) -> str | None:
        return cast(
            str | None,
            self._image_maps[task['split']].get(task['repository'] + '@' + task['base_commit']),
        )

    def _registry_for(self, task: Mapping[str, Any]) -> tuple[Path | None, str | None]:
        amended = self.freeze.get('amendment_registries', {}).get(task['task_id'])
        if amended is not None:
            return cast(tuple[Path, str], amended)
        if task['split'] == 'dev' and task['task_id'] != self._dev_replacement_id:
            return None, None
        item = self.freeze['assets'][f"{task['split']}_judges"]
        if getattr(self, 'public_development', False):
            expected = str(self.freeze['document']['dev_judges']['sha256'])
            if _sha(item) != expected:
                raise DomainError(
                    'H12_FROZEN_EXECUTION_CHANGED', 'Independent registry bytes changed', 409,
                )
            return item, expected
        return item, _sha(item)

    def _log_limit_for(self, task: Mapping[str, Any]) -> int:
        return 1048576

    def _after_materialize(self, task: Mapping[str, Any], target: Path) -> Path:
        from mgar_v3.quality_eval.h12_scope_amendment import validate_scope_basis

        validate_scope_basis(target, self.freeze.get('scope_bindings', {}).get(task['task_id']))
        public_assets = public_assets_for_task(self.root, dict(task))
        destinations: list[tuple[Path, bytes]] = []
        for name, raw in public_assets:
            path = target / name
            if (
                path.exists()
                or path.is_symlink()
                or not path.resolve().is_relative_to(target.resolve())
                or any(parent.is_symlink() for parent in path.parents
                       if parent == target or parent.is_relative_to(target))
            ):
                raise DomainError(
                    'H12_PUBLIC_ASSET_CONFLICT',
                    'Independent public asset target differs',
                    409,
                )
            destinations.append((path, raw))
        for path, raw in destinations:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        amendment = self.freeze['assets']['public_amendments']
        document = json.loads(amendment.read_text())
        if (task['repository'], task['base_commit']) == (
            document['repository'], document['base_commit']
        ):
            apply_public_amendments(
                target, amendment, _sha(amendment),
                repository=task['repository'], commit=task['base_commit'],
            )
        return target

    def _config(self) -> dict[str, Any]:
        from mgar_v3.quality_eval.h12_live import MODEL_OBSERVATION

        model = self.root / MODEL_OBSERVATION
        public_dev = getattr(self, 'public_development', False)
        splits = ('dev',) if public_dev else ('dev', 'holdout')
        config: dict[str, Any] = {
            'schema': 'harness-h12-experiment-v1',
            'profiles': [
                self._profile_for(task, arm).model_dump(mode='json')
                for split in splits
                for task in self._tasks(split)
                for arm in ARMS
            ],
            'frozen_catalog': h12.frozen_hashes(self.root),
            'model_observation_sha256': _sha(model),
            'arms': list(ARMS),
            'independent_registry_sha256': _sha(
                self.freeze['assets']['dev_judges' if public_dev else 'holdout_judges']
            ),
            'image_locks': {
                split: {
                    key: {'path': value, 'sha256': _sha(Path(value))}
                    for key, value in self._image_maps[split].items()
                }
                for split in splits
            },
            'candidate_source': self._candidate_fingerprint(),
            'environment_revision': self.environment_revision,
            'benchmark_freeze_sha256': self.freeze_sha256,
        }
        if public_dev:
            assert self.public_plan_refs is not None and self.public_probe_root is not None
            metadata, profiles_hash = development_protocol_metadata(
                self._tasks('dev'), self.public_plan_refs, self.public_probe_root,
            )
            if (
                metadata != self.freeze['document']['public_plans']
                or profiles_hash != self.freeze['document']['profiles_sha256']
                or canonical_hash([
                    {key: value for key, value in row.items() if key != 'experiment_id'}
                    for row in config['profiles']
                ]) != profiles_hash
                or metadata != self._public_plan_metadata(self._tasks('dev'))
            ):
                raise CatalogError('Public development profiles or source bindings changed')
            config.update({
                'public_development': True,
                'public_runtime_policy': dict(PUBLIC_RUNTIME_POLICY),
                'public_plans': metadata,
                'profiles_sha256': profiles_hash,
                'public_probe_root': str(self.public_probe_root.resolve()),
                'environment_manifest_sha256': self._environment_manifest_hash(),
                'public_readiness': self._public_readiness_metadata(),
                'full_dev_readiness': self._full_dev_readiness_metadata(),
            })
        return config

    def _public_readiness_metadata(self) -> dict[str, str] | None:
        reference = self.public_readiness_ref
        return {'path': str(reference[0].resolve()), 'sha256': reference[1]} if reference else None

    def _require_public_development_frozen(self) -> None:
        if self.freeze['document']['phase'] != 'FROZEN':
            raise DomainError(
                'H12_FULL_DEV_READINESS_REQUIRED',
                'A preparation binding cannot initialize or execute a paid experiment', 403,
            )
        if self.public_readiness_ref is None or self.full_dev_readiness_ref is None:
            raise DomainError(
                'H12_FULL_DEV_READINESS_REQUIRED',
                'Both pinned readiness reports are required', 403,
            )
        if (
            self.public_readiness_ref != self.freeze['public_subset_readiness']
            or self.full_dev_readiness_ref != self.freeze['full_dev_readiness']
            or self.public_plan_refs != self.freeze['public_plan_refs']
            or self.public_probe_root != self.freeze['public_probe_root']
        ):
            raise DomainError(
                'H12_FROZEN_EXECUTION_CHANGED',
                'Public development references differ from the frozen candidate', 409,
            )
        self.validate_public_readiness()
        self.validate_full_dev_readiness()

    def initialize(self) -> None:
        self._validate_candidate_source()
        if self.public_development:
            self._require_public_development_frozen()
        config = self._config()
        self._validate_semantic_freeze([
            task for split in (("dev",) if self.public_development else ("dev", "holdout"))
            for task in self._tasks(split)
        ])
        authorization_path = self.root / 'experiments/harness-v4/h12-authorization-v1.json'
        authorization = json.loads(authorization_path.read_text())
        if not authorization.get('allow_paid') or authorization.get('max_cost_micros') != 12000000:
            raise DomainError('H12_AUTHORIZATION_REQUIRED', 'Original shared approval differs', 403)
        approval = {
            'approved_by': self.engine.settings.local_actor,
            'allow_paid': True,
            'max_cost_micros': 12000000,
            'max_calls': 1536,
            'source_authorization_sha256': _sha(authorization_path),
        }
        self.store.create_experiment(self.experiment_id, config, approval)

    def _validate_frozen_execution(self) -> dict[str, Any]:
        self.freeze = (
            load_public_development_benchmark(self.root, self.freeze_path, self.freeze_sha256)
            if self.public_development
            else load_frozen_benchmark(self.root, self.freeze_path, self.freeze_sha256)
        )
        self._validate_candidate_source()
        if self.public_development:
            self._require_public_development_frozen()
        current = self._config()
        stored = self.store.get_experiment(self.experiment_id)['config']
        if stored != current:
            raise DomainError('H12_FROZEN_EXECUTION_CHANGED', 'Frozen experiment differs', 409)
        return dict(stored)

    def _live_policy(self, split: str) -> None:
        if not self.execute_live or split not in {'dev', 'holdout'}:
            raise DomainError('H12_LIVE_EXECUTION_REQUIRED', 'Paid dispatch is not enabled', 403)
        if getattr(self, 'public_development', False) and split != 'dev':
            raise DomainError(
                'H12_HISTORICAL_HOLDOUT_QUARANTINED',
                'Public development freeze cannot authorize held-out execution', 409,
            )
        self._validate_frozen_execution()
        if split == 'holdout':
            records = []
            for task in self._tasks('dev'):
                path = self.evidence / 'candidate/dev' / task['task_id'] / 'score.json'
                if not path.is_file():
                    raise DomainError(
                        'H12_DEV_THRESHOLD_REQUIRED', 'Dev scores are incomplete', 409
                    )
                records.append(json.loads(path.read_text()))
            if not h12.score_records(records, 'dev', expected_tasks=self._tasks('dev'))[
                'threshold_met'
            ]:
                raise DomainError('H12_DEV_THRESHOLD_REQUIRED', 'Dev threshold is unmet', 409)

    def _require_dev_readiness(self) -> None:
        self._validate_frozen_execution()
        if self.public_development:
            # _validate_frozen_execution already rechecked both pinned reports.
            return
        record = json.loads(self.freeze['assets']['dev_readiness'].read_text())
        if (
            record.get('status') != 'DRAFT_READY'
            or record.get('ready_count') != 12
            or record.get('candidate_source_sha256_after') != self._candidate_fingerprint()
        ):
            raise DomainError('H12_DEV_EXECUTION_NOT_READY', 'Frozen dev readiness differs', 409)
