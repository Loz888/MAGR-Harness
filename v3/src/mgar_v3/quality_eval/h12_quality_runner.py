"""Explicit 12+12 quality candidate; the historical dev-only freeze stays closed.

The original independent benchmark supplies task identities, never readiness for
new source. All current public command receipts must be physically re-established.
No private task text or Judge implementation is emitted by this controller.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_full_dev_readiness import validate_full_report
from mgar_v3.quality_eval.h12_public_readiness import read_evidence_json
from mgar_v3.quality_eval.h12_runner import ARMS, HOLDOUT_INFRA_V4, private_json, profile_for
from mgar_v3.quality_eval.h12_v2_freeze import (
    _public_asset,
    _public_document,
    _reference,
    _relocated_image_map,
    load_frozen_benchmark,
    load_public_development_benchmark,
    public_development_profile,
)
from mgar_v3.quality_eval.h12_v2_runner import REVISION, BenchmarkV2Runner, _sha
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root

SCHEMA = "h12-public-quality-candidate-v1"
COMPLETE_SCHEMA = "h12-public-quality-complete-matrix-v1"


def _complete_authorization(root: Path, reference: dict[str, str]) -> dict[str, Any]:
    document = _public_document(root, _public_asset(root, reference), reference['sha256'])
    if (document.get('schema') != 'h12-complete-matrix-authorization-v1'
            or document.get('status') != 'USER_REQUESTED'
            or document.get('mode') != 'complete-matrix-diagnostic-v1'
            or document.get('cases') != {'dev': 12, 'holdout': 12}
            or document.get('thresholds') != {'dev': 10, 'holdout': 9, 'denominator_each': 12}
            or document.get('shared_budget_micros') != 12_000_000
            or document.get('thinking') != {'type': 'enabled'}
            or document.get('reasoning_effort') != 'max'
            or document.get('allow_unknown_retry') is not False
            or document.get('allow_publication') is not False
            or document.get('stop_on_known_task_failure') is not False
            or document.get('holdout_disposition') != 'QUARANTINE_AFTER_REPORT'):
        raise CatalogError('Complete matrix needs its explicit bounded diagnostic protocol')
    return document


def _profiles(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        public_development_profile(
            task, profile_for(task, "quality-freeze", arm,
                              environment_revision=HOLDOUT_INFRA_V4), HOLDOUT_INFRA_V4,
        ).model_dump(mode="json", exclude={"experiment_id"})
        for task in tasks for arm in ARMS
    ]


def _inputs(root: Path, reference: dict[str, str], image_ref: dict[str, str]) -> dict[str, Any]:
    dev = load_public_development_benchmark(
        root, _public_asset(root, reference), reference["sha256"],
    )
    anchor = dev["document"]["benchmark_anchor"]
    independent = load_frozen_benchmark(root, _public_asset(root, anchor), anchor["sha256"])
    if independent["tasks"]["dev"] != dev.get("base_tasks", dev["tasks"]["dev"]):
        raise CatalogError("Independent benchmark development identities differ")
    if {task['task_id'] for task in independent['tasks']['holdout']} & {
        task['task_id'] for task in dev['tasks']['dev']
    }:
        raise CatalogError("Amended development and independent held-out identities overlap")
    image_map = _relocated_image_map(
        independent["assets"]["holdout_image_map"], _public_asset(root, image_ref),
    )
    metadata = {
        "candidate_source_sha256": dev["candidate_source_sha256"],
        "benchmark_anchor": anchor,
        "thresholds": {"dev": 10, "holdout": 9, "denominator_each": 12},
        "shared_budget_micros": 12_000_000,
        "holdout_profiles_sha256": canonical_hash(_profiles(independent["tasks"]["holdout"])),
        "holdout_image_locks": {
            key: _reference(root, Path(value)) for key, value in image_map.items()
        },
        "holdout_environment": _reference(
            root, root / f"experiments/harness-v4/{HOLDOUT_INFRA_V4}.json",
        ),
    }
    return {"dev": dev, "independent": independent, "image_map": image_map, "metadata": metadata}


def load_quality_freeze(root: Path, path: Path, expected: str) -> dict[str, Any]:
    document = _public_document(root, path, expected)
    if document.get("schema") not in {SCHEMA, COMPLETE_SCHEMA} or document.get("phase") not in {
        "HOLDOUT_PREPARATION", "FROZEN",
    }:
        raise CatalogError("Quality candidate is not explicitly bound")
    complete_ref = document.get('complete_matrix_authorization')
    if (document['schema'] == COMPLETE_SCHEMA) != (complete_ref is not None):
        raise CatalogError('Complete matrix cannot be enabled on the historical quality protocol')
    if complete_ref is not None:
        _complete_authorization(root, complete_ref)
    inputs = _inputs(root, document["public_dev_freeze"], document["holdout_image_map"])
    if any(document.get(key) != value for key, value in inputs["metadata"].items()):
        raise CatalogError("Quality candidate source, threshold or held-out protocol changed")
    readiness = document.get("holdout_readiness")
    if (document["phase"] == "FROZEN") != (readiness is not None):
        raise CatalogError("Quality preparation cannot grant paid execution")
    if document["phase"] == "FROZEN" and inputs["dev"]["document"]["phase"] != "FROZEN":
        raise CatalogError("Paid quality execution needs the complete frozen development candidate")
    reference = (_public_asset(root, readiness), readiness["sha256"]) if readiness else None
    return {**inputs, "document": document, "holdout_readiness": reference}


def write_quality_freeze(
    root: Path, output: Path, *, development: tuple[Path, str], holdout_image_map: Path,
    readiness: tuple[Path, str] | None = None,
    complete_matrix_authorization: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    dev_ref = _reference(root, *development)
    image_ref = _reference(root, holdout_image_map)
    inputs = _inputs(root, dev_ref, image_ref)
    complete_ref = None
    if complete_matrix_authorization is not None:
        complete_ref = _reference(root, *complete_matrix_authorization)
        _complete_authorization(root, complete_ref)
    document = {
        "schema": COMPLETE_SCHEMA if complete_ref else SCHEMA,
        "phase": "FROZEN" if readiness else "HOLDOUT_PREPARATION",
        "public_dev_freeze": dev_ref, "holdout_image_map": image_ref,
        **inputs["metadata"],
        "holdout_readiness": _reference(root, *readiness) if readiness else None,
    }
    if complete_ref is not None:
        document['complete_matrix_authorization'] = complete_ref
    private_json(output, document)
    return load_quality_freeze(root, output, _sha(output))


class QualityRunner(BenchmarkV2Runner):
    """A separately frozen candidate, including the independent unseen holdout."""

    def __init__(
        self, engine: Any, *, experiment_id: str, evidence: Path, freeze_path: Path,
        freeze_sha256: str, api_key: str | None, execute_live: bool = False,
        root: Path | None = None,
    ) -> None:
        base = root or repo_root()
        self.quality = load_quality_freeze(base, freeze_path, freeze_sha256)
        self.quality_path, self.quality_sha256 = freeze_path, freeze_sha256
        if execute_live and self.quality["document"]["phase"] != "FROZEN":
            raise DomainError("H12_HOLDOUT_READINESS_REQUIRED", "Preparation grants no calls", 403)
        ref = self.quality["document"]["public_dev_freeze"]
        super().__init__(
            engine, experiment_id=experiment_id, evidence=evidence,
            freeze_path=base / ref["path"], freeze_sha256=ref["sha256"], api_key=api_key,
            execute_live=execute_live, root=base, public_development=True,
        )
        self._image_maps["holdout"] = self.quality["image_map"]

    def _development_only(self) -> bool:
        return False

    def _tasks(self, split: str) -> list[dict[str, Any]]:
        if split == "holdout":
            return list(self.quality["independent"]["tasks"][split])
        return super()._tasks(split)

    def _registry_for(self, task: Mapping[str, Any]) -> tuple[Path | None, str | None]:
        if task["split"] == "holdout":
            path = self.quality["independent"]["assets"]["holdout_judges"]
            return path, _sha(path)
        return super()._registry_for(task)

    def prepare_holdout_readiness(self, evidence: Path) -> dict[str, Any]:
        # No Engine, private Judge execution or provider key is needed here.
        return self._prepare_split_readiness("holdout", evidence)

    def validate_holdout_readiness(self) -> dict[str, Any]:
        ref = self.quality["holdout_readiness"]
        if ref is None:
            raise DomainError("H12_HOLDOUT_READINESS_REQUIRED", "Held-out proof missing", 403)
        report = read_evidence_json(*ref)
        validate_full_report(
            report, ref[0].parent, bindings=self._readiness_bindings("holdout"),
            candidate_source=self._candidate_fingerprint(), split="holdout",
        )
        return report

    def _require_public_development_frozen(self) -> None:
        super()._require_public_development_frozen()
        current = load_quality_freeze(self.root, self.quality_path, self.quality_sha256)
        if current != self.quality or current["document"]["phase"] != "FROZEN":
            raise DomainError("H12_HOLDOUT_READINESS_REQUIRED", "Quality binding differs", 409)
        self.validate_holdout_readiness()

    def _config(self) -> dict[str, Any]:
        config = super()._config()
        profiles = [self._profile_for(task, arm).model_dump(mode="json")
                    for task in self._tasks("holdout") for arm in ARMS]
        if canonical_hash([
            {key: value for key, value in row.items() if key != "experiment_id"}
            for row in profiles
        ]) != self.quality["document"]["holdout_profiles_sha256"]:
            raise CatalogError("Actual held-out runtime profiles differ from the frozen protocol")
        config["profiles"].extend(profiles)
        config.update({
            "environment_revision": REVISION,
            "public_development": False,
            "quality_freeze_sha256": self.quality_sha256,
            "quality_protocol": self.quality["document"],
            "independent_registry_sha256": _sha(
                self.quality["independent"]["assets"]["holdout_judges"],
            ),
        })
        return config

    def _live_policy(self, split: str) -> None:
        if split not in {"dev", "holdout"}:
            raise DomainError("H12_LIVE_EXECUTION_REQUIRED", "Unknown quality split", 403)
        # This validates live permission, exact source, all receipts, stored
        # experiment identity and the original shared authorization via parent.
        super()._live_policy("dev")
        if split == "holdout":
            records = []
            complete = self.quality['document']['schema'] == COMPLETE_SCHEMA
            for task in self._tasks("dev"):
                path = self.evidence / "candidate/dev" / task["task_id"] / "score.json"
                if not path.is_file():
                    raise DomainError("H12_DEV_THRESHOLD_REQUIRED", "Dev scores incomplete", 409)
                row = json.loads(path.read_bytes())
                if (row.get("experiment_id") != self.experiment_id
                        or row.get("arm") != "candidate" or row.get('split') != 'dev'
                        or row.get('task_id') != task['task_id']):
                    raise DomainError("H12_DEV_THRESHOLD_REQUIRED", "Score belongs elsewhere", 409)
                if row.get("status") == "PASS":
                    usage = self.sample_usage(self._profile_for(task, "candidate"))
                    if not usage or usage["unknown_calls"] or (
                        usage["run_id"] != row.get("run_id")
                        or usage["tree_hash"] != row.get("final_tree")
                        or usage["engine_status"] != "succeeded"
                    ):
                        raise DomainError(
                            "H12_DEV_THRESHOLD_REQUIRED", "Passing score lacks Engine facts", 409,
                        )
                if complete:
                    self._complete_case_known(task, row)
                records.append(row)
            summary = h12.score_records(records, "dev", expected_tasks=self._tasks("dev"))
            if complete and any(summary.get(key) for key in (
                'unknown_count', 'unauthorized_actions', 'unknown_blind_retries'
            )):
                raise DomainError('H12_COMPLETE_MATRIX_UNSAFE', 'Diagnostic safety stop', 409)
            if not complete and not summary['threshold_met']:
                raise DomainError("H12_DEV_THRESHOLD_REQUIRED", "Dev quality threshold unmet", 409)

    def _complete_case_known(self, task: dict[str, Any], row: dict[str, Any]) -> None:
        if (row.get('usage_known') is not True or row.get('reserved_micros') != 0
                or row.get('status') not in {'PASS', 'FAIL', 'NOT_RUN'}):
            raise DomainError('H12_COMPLETE_MATRIX_USAGE_REQUIRED',
                              'Diagnostic continuation requires known usage', 409)
        usage = self.sample_usage(self._profile_for(task, 'candidate'))
        if row.get('status') == 'NOT_RUN' and not row.get('run_id'):
            valid = usage is None and row.get('model_calls', 0) == row.get('cost_micros') == 0
        else:
            valid = bool(usage and usage['unknown_calls'] == 0
                and usage['run_id'] == row.get('run_id')
                and usage['tree_hash'] == row.get('final_tree')
                and usage['engine_status'] in {'succeeded', 'failed', 'cancelled'}
                and usage['model_calls'] == row.get('model_calls')
                and usage['settled_cost_micros'] == row.get('settled_cost_micros'))
        if not valid:
            raise DomainError('H12_COMPLETE_MATRIX_USAGE_REQUIRED',
                              'Diagnostic score lacks the original Engine usage facts', 409)
