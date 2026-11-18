"""Hash-bound H12 v2 benchmark inputs; drafts never authorize model calls."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mgar_v3.harness.contracts import EvaluationProfile, canonical_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_public_plan import load_public_plan
from mgar_v3.quality_eval.harness_v4 import CatalogError, load_jsonl, load_tasks, validate_task

PUBLIC_DEV_SCHEMA = 'h12-benchmark-v2-public-dev-v1'
AMENDED_DEV_SCHEMA = 'h12-benchmark-v2-public-dev-v2'
SCOPED_DEV_SCHEMA = 'h12-benchmark-v2-public-dev-scope-v1'
PUBLIC_RUNTIME_POLICY = {
    'revision': 'h12-public-runtime-v1',
    'scratch_mb': 256,
    'readonly_pytest_cache': '/scratch/pytest-cache',
}
_DEV_ASSETS = ('dev_catalog', 'dev_judges', 'dev_image_map', 'public_amendments', 'proposal')
_THRESHOLDS = {'dev': 10, 'holdout': 9, 'denominator_each': 12}


def public_assets_for_task(root: Path, task: dict[str, Any]) -> list[tuple[str, bytes]]:
    """Validate separately curated public files before any v2 dispatch."""
    if task['split'] != 'holdout':
        if task.get('public_assets'):
            raise CatalogError('Development tasks cannot add independent public assets')
        return []
    rows = task.get('public_assets')
    if not isinstance(rows, list) or not rows:
        raise CatalogError('Independent public assets are missing')
    private_root = root / '.local/h12/independent-holdout-v2-20261113/public-assets'
    if not private_root.is_dir() or private_root.is_symlink():
        raise CatalogError('Independent public asset root is unavailable')
    result: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogError('Independent public asset entry is invalid')
        name, target, expected = row.get('source'), row.get('target'), row.get('sha256')
        if (
            not isinstance(name, str)
            or not isinstance(target, str)
            or not isinstance(expected, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or not name.startswith('.local/h12/independent-holdout-v2-20261113/public-assets/')
            or not name.removeprefix('.local/h12/independent-holdout-v2-20261113/public-assets/')
            or '..' in Path(name).parts
            or '\\' in name
            or not h12._safe_path(target)
            or target in seen
        ):
            raise CatalogError('Independent public asset path or hash is invalid')
        seen.add(target)
        source = root / name
        if (
            any(parent.is_symlink() for parent in (source, *source.parents)
                if parent == root or parent.is_relative_to(root))
            or not source.resolve().is_relative_to(private_root.resolve())
            or not source.is_file()
        ):
            raise CatalogError('Independent public asset source is unsafe')
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise CatalogError('Independent public asset hash differs')
        result.append((target, raw))
    return result


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset(root: Path, entry: dict[str, str]) -> Path:
    name, expected = entry.get('path'), entry.get('sha256')
    if (
        not isinstance(name, str)
        or not isinstance(expected, str)
        or not re.fullmatch(r'[0-9a-f]{64}', expected)
    ):
        raise CatalogError('Benchmark asset identity is incomplete')
    target = root / name
    if (
        target.is_symlink()
        or not target.resolve().is_relative_to(root.resolve())
        or not target.is_file()
        or _sha(target) != expected
    ):
        raise CatalogError('Benchmark asset differs from frozen hash or scope')
    return target


def load_frozen_benchmark(root: Path, manifest: Path, expected_sha256: str) -> dict[str, Any]:
    """Validate a separate v2 identity before any Engine or provider dispatch."""
    if (
        manifest.is_symlink()
        or not manifest.resolve().is_relative_to(root.resolve())
        or not manifest.is_file()
        or _sha(manifest) != expected_sha256
    ):
        raise CatalogError('Benchmark freeze manifest differs')
    document = json.loads(manifest.read_text())
    if (
        document.get('schema') != 'h12-benchmark-v2-freeze-v1'
        or document.get('status') != 'FROZEN'
        or document.get('revision') != 'h12-benchmark-v2'
        or document.get('thresholds') != {'dev': 10, 'holdout': 9, 'denominator_each': 12}
        or document.get('shared_budget_micros') != 12_000_000
        or document.get('old_h00_hashes') != h12.frozen_hashes(root)
    ):
        raise CatalogError('Benchmark has not been independently frozen')
    assets = {
        name: _asset(root, document[name])
        for name in (
            'dev_catalog', 'holdout_catalog', 'dev_judges', 'holdout_judges',
            'dev_image_map', 'holdout_image_map', 'public_amendments',
            'dev_readiness', 'holdout_readiness', 'judge_controls', 'proposal',
        )
    }
    proposal = json.loads(assets['proposal'].read_text())
    if (
        proposal.get('revision') != 'h12-benchmark-v2'
        or proposal.get('status') != 'DRAFT_NOT_FROZEN'
        or proposal.get('dev_catalog_sha256') != _sha(assets['dev_catalog'])
        or proposal.get('dev_registry_sha256') != _sha(assets['dev_judges'])
        or proposal.get('public_amendments_sha256') != _sha(assets['public_amendments'])
    ):
        raise CatalogError('Frozen benchmark differs from reviewed draft inputs')
    catalogs = {
        split: load_jsonl(assets[f'{split}_catalog']) for split in ('dev', 'holdout')
    }
    old_ids = {task['task_id'] for split in ('dev', 'holdout') for task in load_tasks(split, root)}
    all_new_ids: set[str] = set()
    for split, tasks in catalogs.items():
        if len(tasks) != 12 or len({task['task_id'] for task in tasks}) != 12:
            raise CatalogError('Each benchmark split requires twelve distinct tasks')
        for task in tasks:
            if validate_task(task, split):
                raise CatalogError('Frozen benchmark task is invalid')
            if task['task_id'] in all_new_ids:
                raise CatalogError('Benchmark split task IDs overlap')
            all_new_ids.add(task['task_id'])
    # Eleven unchanged development cases intentionally retain their old IDs;
    # the replacement and all independent held-out cases must be new.
    dev_ids = {task['task_id'] for task in catalogs['dev']}
    old_dev = {task['task_id']: task for task in load_tasks('dev', root)}
    old_dev_ids = set(old_dev)
    if len(dev_ids & old_dev_ids) != 11 or any(
        task['task_id'] in old_ids for task in catalogs['holdout']
    ):
        raise CatalogError('Benchmark replacement or independent holdout differs')
    if any(task != old_dev[task['task_id']] for task in catalogs['dev']
           if task['task_id'] in old_dev):
        raise CatalogError('Unchanged development task differs from H00')
    for task in catalogs['holdout']:
        public_assets_for_task(root, task)
    for split in ('dev', 'holdout'):
        ready = json.loads(assets[f'{split}_readiness'].read_text())
        candidate_sha = document.get('candidate_source_sha256')
        if (
            ready.get('ready_count') != 12
            or ready.get('denominator') != 12
            or ready.get('candidate_source_sha256_before') != candidate_sha
            or ready.get('candidate_source_sha256_after') != candidate_sha
            or ready.get('status') not in (
                {'DRAFT_READY'} if split == 'dev' else {'READY', 'COMPLETE'}
            )
        ):
            raise CatalogError('Frozen readiness belongs to another source or is incomplete')
        image_map = json.loads(assets[f'{split}_image_map'].read_text())
        if not isinstance(image_map, dict) or any(
            task['repository'] + '@' + task['base_commit'] not in image_map
            for task in catalogs[split]
        ):
            raise CatalogError('Frozen project environments are incomplete')
    controls = json.loads(assets['judge_controls'].read_text())
    changed_ids = (dev_ids - old_dev_ids) | {
        task['task_id'] for task in catalogs['holdout']
    }
    if (
        controls.get('status') != 'PASS'
        or controls.get('passed_tasks') != 13
        or len(controls.get('task_ids', ())) != 13
        or set(controls.get('task_ids', ())) != changed_ids
    ):
        raise CatalogError('Judge positive and negative controls are incomplete')
    return {'manifest_sha256': expected_sha256, 'assets': assets, 'tasks': catalogs,
            'candidate_source_sha256': document['candidate_source_sha256']}


def _public_asset(root: Path, entry: Any) -> Path:
    if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}:
        raise CatalogError('Public development reference is incomplete')
    name = entry['path']
    if (
        not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
        or '\\' in name or not name
    ):
        raise CatalogError('Public development reference is outside its root')
    target = root / name
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise CatalogError('Public development reference cannot traverse symlinks')
    return _asset(root, entry)


def _reference(root: Path, path: Path, expected: str | None = None) -> dict[str, str]:
    try:
        name = str(path.absolute().relative_to(root.absolute()))
    except ValueError:
        raise CatalogError(
            'Public development reference must be inside the evaluator root',
        ) from None
    result = {'path': name, 'sha256': expected or _sha(path)}
    _public_asset(root, result)
    return result


def _public_document(root: Path, path: Path, expected: str) -> dict[str, Any]:
    path = _public_asset(root, _reference(root, path, expected))
    # Duplicate identity fields must not become an alternate interpretation.
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in rows:
            if name in result:
                raise CatalogError('Duplicate development freeze field')
            result[name] = value
        return result
    try:
        raw = path.read_bytes()
        if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != expected:
            raise CatalogError('Development freeze changed while reading its bound bytes')
        document = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise CatalogError('Public development freeze is not valid JSON') from None
    if not isinstance(document, dict):
        raise CatalogError('Public development freeze must be an object')
    return document


def _dev_anchor(root: Path, reference: dict[str, str]) -> tuple[dict[str, Any], dict[str, Path]]:
    """Inspect old freeze metadata, never open its held-out catalogs or judges."""
    document = _public_document(root, _public_asset(root, reference), reference['sha256'])
    if (
        document.get('schema') != 'h12-benchmark-v2-freeze-v1'
        or document.get('status') != 'FROZEN'
        or document.get('revision') != 'h12-benchmark-v2'
        or document.get('thresholds') != _THRESHOLDS
        or document.get('shared_budget_micros') != 12_000_000
        or document.get('old_h00_hashes') != h12.frozen_hashes(root)
        or document['old_h00_hashes'].get('status') != 'PASS'
    ):
        raise CatalogError('Development anchor has not been independently frozen')
    return document, {name: _public_asset(root, document.get(name)) for name in _DEV_ASSETS}


def _development_tasks(root: Path, assets: Mapping[str, Path]) -> list[dict[str, Any]]:
    tasks = load_jsonl(assets['dev_catalog'])
    if len(tasks) != 12 or len({task.get('task_id') for task in tasks}) != 12 or any(
        validate_task(task, 'dev') for task in tasks
    ):
        raise CatalogError('Development freeze requires twelve valid distinct development tasks')
    old = {task['task_id']: task for task in load_tasks('dev', root)}
    unchanged = [task for task in tasks if task['task_id'] in old]
    if len(unchanged) != 11 or any(task != old[task['task_id']] for task in unchanged):
        raise CatalogError('Development replacement differs from the original frozen benchmark')
    proposal = json.loads(assets['proposal'].read_bytes())
    replacement = {task['task_id'] for task in tasks} - set(old)
    if (
        proposal.get('revision') != 'h12-benchmark-v2'
        or proposal.get('status') != 'DRAFT_NOT_FROZEN'
        or proposal.get('dev_catalog_sha256') != _sha(assets['dev_catalog'])
        or proposal.get('dev_registry_sha256') != _sha(assets['dev_judges'])
        or proposal.get('public_amendments_sha256') != _sha(assets['public_amendments'])
        or replacement != {proposal.get('dev_replacement')}
    ):
        raise CatalogError('Development inputs differ from the reviewed replacement')
    return tasks


def _relocated_image_map(original: Path, selected: Path) -> dict[str, str]:
    before, after = json.loads(original.read_bytes()), json.loads(selected.read_bytes())
    if (
        not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after)
        or any(not isinstance(value, str) for value in (*before.values(), *after.values()))
        or any(_sha(Path(before[key])) != _sha(Path(after[key])) for key in before)
    ):
        raise CatalogError('Relocated image map must preserve every original exact lock byte')
    return after


def public_development_profile(
    task: Mapping[str, Any], profile: EvaluationProfile, revision: str,
) -> EvaluationProfile:
    """Keep primary tests and an independent compile gate in the new branch."""
    from mgar_v3.quality_eval.h12_full_dev_readiness import command_plan
    from mgar_v3.quality_eval.h12_public_regression import public_test_argv
    from mgar_v3.quality_eval.h12_runner import profile_for

    profile = profile.model_copy(update={
        'public_test_argv': public_test_argv(task, profile.public_test_argv),
    })
    if profile.task_kind == 'refactor':
        # Refactors preserve the task's declared regression contract. Historic
        # environment.verify selectors can name obsolete file layouts.
        regression = profile_for(
            {**task, 'environment': {**task.get('environment', {}), 'verify': []}},
            profile.experiment_id, profile.arm, environment_revision=revision,
        ).public_test_argv
        if not regression:
            raise CatalogError('A public refactor needs its declared fixed regression command')
        profile = profile.model_copy(update={'public_test_argv': regression})
    commands = command_plan(task, profile, revision)
    tests = [tuple(row['argv']) for row in commands if row['kind'] == 'tests']
    builds = [tuple(row['argv']) for row in commands if row['kind'] == 'build']
    if not tests or len(builds) > 1:
        raise CatalogError('Public candidate requires real tests and at most one fixed build gate')
    primary = profile.public_test_argv if profile.public_test_argv in tests else tests[0]
    return profile.model_copy(update={
        'public_test_argv': primary, 'public_build_argv': builds[0] if builds else (),
        'sandbox_scratch_mb': 256,
    })


def development_protocol_metadata(
    tasks: list[dict[str, Any]], references: Mapping[str, tuple[Path, str]], asset_root: Path,
) -> tuple[dict[str, Any], str]:
    """Bind every profile field while allowing an experiment's separate identity."""
    from mgar_v3.quality_eval.h12_runner import ARMS, DEV_ENVIRONMENT_V4, profile_for

    if not references or not set(references).issubset({task['task_id'] for task in tasks}):
        raise CatalogError('Public plans must explicitly name a nonempty development subset')
    metadata, profiles = {}, []
    for task in tasks:
        initial = profile_for(task, 'public-dev-freeze', 'candidate',
                              environment_revision=DEV_ENVIRONMENT_V4)
        initial = public_development_profile(task, initial, DEV_ENVIRONMENT_V4)
        reference = references.get(task['task_id'])
        frozen = load_public_plan(
            reference[0], reference[1], task=task, asset_root=asset_root,
            regression_argv=initial.public_test_argv,
        ) if reference else None
        if frozen is not None:
            metadata[task['task_id']] = frozen.freeze_metadata()
        for arm in ARMS:
            profile = profile_for(task, 'public-dev-freeze', arm,
                                  environment_revision=DEV_ENVIRONMENT_V4,
                                  public_probe=frozen.plan if frozen else None)
            profile = public_development_profile(task, profile, DEV_ENVIRONMENT_V4)
            value = profile.model_dump(mode='json')
            value.pop('experiment_id')
            profiles.append(value)
    return metadata, canonical_hash(profiles)


def load_public_development_benchmark(
    root: Path, manifest: Path, expected_sha256: str,
) -> dict[str, Any]:
    """Validate the new dev-only bridge without loading any holdout input.

    Original H00 bytes are only hashed opaquely, as in the historic protocol.
    PREPARATION permits zero-model readiness only, never an experiment or dispatch.
    """
    from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4

    document = _public_document(root, manifest, expected_sha256)
    if (
        document.get('schema') not in {PUBLIC_DEV_SCHEMA, AMENDED_DEV_SCHEMA, SCOPED_DEV_SCHEMA}
        or document.get('phase') not in {'PUBLIC_PREPARATION', 'FROZEN'}
        or document.get('revision') != 'h12-benchmark-v2'
        or document.get('scope') != 'dev'
        or document.get('public_runtime_policy') != PUBLIC_RUNTIME_POLICY
        or document.get('thresholds') != _THRESHOLDS
        or document.get('shared_budget_micros') != 12_000_000
        or document.get('old_h00_hashes') != h12.frozen_hashes(root)
        or document['old_h00_hashes'].get('status') != 'PASS'
        or not re.fullmatch(r'[0-9a-f]{64}', str(document.get('candidate_source_sha256')))
    ):
        raise CatalogError('Public development candidate has not been explicitly bound')
    anchor_ref = document.get('benchmark_anchor')
    _public_asset(root, anchor_ref)
    assert isinstance(anchor_ref, dict)
    anchor, assets = _dev_anchor(root, anchor_ref)
    if any(document.get(name) != anchor[name] for name in _DEV_ASSETS if name != 'dev_image_map'):
        raise CatalogError('Public development inputs differ from the frozen benchmark anchor')
    relocated = _public_asset(root, document.get('dev_image_map'))
    image_map = _relocated_image_map(assets['dev_image_map'], relocated)
    assets['dev_image_map'] = relocated
    tasks = _development_tasks(root, assets)
    original_tasks = tasks
    amendment_fields: dict[str, Any] = {}
    amendment_ref = document.get('development_amendment')
    if (document['schema'] != SCOPED_DEV_SCHEMA
            and (document['schema'] == AMENDED_DEV_SCHEMA) != (amendment_ref is not None)):
        raise CatalogError('Development amendment requires its explicit versioned protocol')
    if amendment_ref is not None:
        from mgar_v3.quality_eval.h12_dev_amendment import load_amendment

        amended = load_amendment(root, amendment_ref, tasks)
        amendment_fields = {'base_tasks': tasks, 'amendment_registries': amended['registries']}
        tasks = amended['tasks']
    scope_ref = document.get('development_scope_amendment')
    if (document['schema'] == SCOPED_DEV_SCHEMA) != (scope_ref is not None):
        raise CatalogError('Scope amendment requires its separate versioned protocol')
    if scope_ref is not None:
        from mgar_v3.quality_eval.h12_scope_amendment import load_scope_amendment

        scoped = load_scope_amendment(root, scope_ref, tasks)
        amendment_fields.update(base_tasks=original_tasks, scope_bindings=scoped['bindings'])
        tasks = scoped['tasks']
    if not isinstance(image_map, dict) or any(
        not isinstance(image_map.get(task['repository'] + '@' + task['base_commit']), str)
        for task in tasks
    ):
        raise CatalogError('Public development image map is incomplete')
    image_locks = {
        key: _reference(root, Path(value)) for key, value in image_map.items()
    }
    if document.get('image_locks') != image_locks:
        raise CatalogError('Public development image locks changed')
    environment = root / f'experiments/harness-v4/{DEV_ENVIRONMENT_V4}.json'
    if document.get('environment_manifest') != _reference(root, environment):
        raise CatalogError('Public development environment manifest changed')
    asset_name = document.get('public_probe_root')
    if (
        not isinstance(asset_name, str) or Path(asset_name).is_absolute()
        or '..' in Path(asset_name).parts or '\\' in asset_name or not asset_name
    ):
        raise CatalogError('Public development asset root is invalid')
    asset_root = root / asset_name
    if not asset_root.is_dir() or any(p.is_symlink() for p in (asset_root, *asset_root.parents)):
        raise CatalogError('Public development asset root is unavailable')
    entries = document.get('public_plan_refs')
    if not isinstance(entries, dict) or not entries:
        raise CatalogError('Public development plans must be explicitly frozen')
    references = {
        task_id: (_public_asset(root, entry), entry['sha256'])
        for task_id, entry in entries.items()
    }
    metadata, profiles_hash = development_protocol_metadata(tasks, references, asset_root)
    if document.get('public_plans') != metadata or document.get('profiles_sha256') != profiles_hash:
        raise CatalogError('Public development profiles or behavior assets changed')
    reports: dict[str, tuple[Path, str] | None] = {}
    for name in ('public_subset_readiness', 'full_dev_readiness'):
        entry = document.get(name)
        reports[name] = (_public_asset(root, entry), entry['sha256']) if entry is not None else None
    if (
        document['phase'] == 'FROZEN' and any(value is None for value in reports.values())
        or document['phase'] == 'PUBLIC_PREPARATION'
        and any(value is not None for value in reports.values())
    ):
        raise CatalogError('Preparation and frozen readiness references cannot be mixed')
    return {
        'manifest_sha256': expected_sha256, 'assets': assets, 'tasks': {'dev': tasks},
        'candidate_source_sha256': document['candidate_source_sha256'],
        'document': document, 'public_plan_refs': references, 'public_probe_root': asset_root,
        **reports, **amendment_fields,
    }


def write_public_development_freeze(
    root: Path, output: Path, *, benchmark: tuple[Path, str], candidate_source_sha256: str,
    public_plan_refs: Mapping[str, tuple[Path, str]], public_probe_root: Path,
    public_readiness_ref: tuple[Path, str] | None = None,
    full_dev_readiness_ref: tuple[Path, str] | None = None,
    dev_image_map: Path | None = None,
    development_amendment: tuple[Path, str] | None = None,
    development_scope_amendment: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    """Create a new immutable bridge; no Engine, budget write, Judge or model."""
    from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4, private_json

    anchor_ref = _reference(root, benchmark[0], benchmark[1])
    anchor, assets = _dev_anchor(root, anchor_ref)
    tasks = _development_tasks(root, assets)
    amendment_ref = None
    if development_amendment is not None:
        from mgar_v3.quality_eval.h12_dev_amendment import load_amendment

        amendment_ref = _reference(root, *development_amendment)
        tasks = load_amendment(root, amendment_ref, tasks)['tasks']
    scope_ref = None
    if development_scope_amendment is not None:
        from mgar_v3.quality_eval.h12_scope_amendment import load_scope_amendment

        scope_ref = _reference(root, *development_scope_amendment)
        tasks = load_scope_amendment(root, scope_ref, tasks)['tasks']
    metadata, profiles_hash = development_protocol_metadata(
        tasks, public_plan_refs, public_probe_root,
    )
    if (public_readiness_ref is None) != (full_dev_readiness_ref is None):
        raise CatalogError('Both executed readiness reports are required when sealing a candidate')
    try:
        asset_name = str(public_probe_root.absolute().relative_to(root.absolute()))
    except ValueError:
        raise CatalogError(
            'Public probe assets must stay inside the isolated evaluator root',
        ) from None
    selected_image_map = dev_image_map or assets['dev_image_map']
    image_map = _relocated_image_map(assets['dev_image_map'], selected_image_map)
    document = {
        'schema': SCOPED_DEV_SCHEMA if scope_ref is not None else (
            AMENDED_DEV_SCHEMA if amendment_ref is not None else PUBLIC_DEV_SCHEMA),
        'phase': 'FROZEN' if full_dev_readiness_ref is not None else 'PUBLIC_PREPARATION',
        'revision': 'h12-benchmark-v2', 'scope': 'dev', 'thresholds': _THRESHOLDS,
        'public_runtime_policy': dict(PUBLIC_RUNTIME_POLICY),
        'shared_budget_micros': 12_000_000, 'old_h00_hashes': h12.frozen_hashes(root),
        'benchmark_anchor': anchor_ref, 'candidate_source_sha256': candidate_source_sha256,
        **{name: anchor[name] for name in _DEV_ASSETS},
        'dev_image_map': _reference(root, selected_image_map),
        'image_locks': {key: _reference(root, Path(value)) for key, value in image_map.items()},
        'environment_manifest': _reference(
            root, root / f'experiments/harness-v4/{DEV_ENVIRONMENT_V4}.json',
        ),
        'public_probe_root': asset_name,
        'public_plan_refs': {
            key: _reference(root, value[0], value[1]) for key, value in public_plan_refs.items()
        },
        'public_plans': metadata, 'profiles_sha256': profiles_hash,
        'public_subset_readiness': _reference(root, *public_readiness_ref)
        if public_readiness_ref is not None else None,
        'full_dev_readiness': _reference(root, *full_dev_readiness_ref)
        if full_dev_readiness_ref is not None else None,
    }
    if amendment_ref is not None:
        document['development_amendment'] = amendment_ref
    if scope_ref is not None:
        document['development_scope_amendment'] = scope_ref
    private_json(output, document)
    return load_public_development_benchmark(root, output, _sha(output))
