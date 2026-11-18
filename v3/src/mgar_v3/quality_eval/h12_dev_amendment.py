"""Explicit development-Judge corrections; no task/resource or holdout relaxation."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from mgar_v3.harness.contracts import ExecutionProvenance, canonical_hash
from mgar_v3.harness.project_commands import parse_tests, test_project_kind
from mgar_v3.quality_eval.h12_public_readiness import read_evidence_json
from mgar_v3.quality_eval.harness_v4 import CatalogError, validate_task


def load_amendment(
    root: Path, reference: dict[str, str], tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    from mgar_v3.quality_eval.h12_v2_freeze import _public_asset

    def document(ref: dict[str, str]) -> tuple[Path, dict[str, Any]]:
        path = _public_asset(root, ref)
        return path, read_evidence_json(path, ref['sha256'])

    _, amendment = document(reference)
    if (amendment.get('schema') != 'h12-development-judge-amendment-v1'
            or amendment.get('status') != 'CONTROLLED'
            or amendment.get('base_tasks_sha256') != canonical_hash(tasks)
            or not isinstance(amendment.get('replacements'), list)
            or not 1 <= len(amendment['replacements']) <= 12):
        raise CatalogError('Development amendment is not bound to its original tasks')
    registry_path, registry = document(amendment['registry'])
    if registry.get('schema') != 'mgar-h12-judge-implementation-v1':
        raise CatalogError('Development amendment registry schema differs')
    old = {task['task_id']: task for task in tasks}
    replacements: dict[str, dict[str, Any]] = {}
    registries: dict[str, tuple[Path, str]] = {}
    new_ids: set[str] = set()
    for change in amendment['replacements']:
        original = old.get(change.get('original_task_id'))
        task = change.get('task')
        if (original is None or original['task_id'] in replacements
                or not isinstance(task, dict) or validate_task(task, 'dev')
                or task['task_id'] in old or task['task_id'] in new_ids
                or change.get('original_task_sha256') != canonical_hash(original)
                or change.get('reason') != 'UNDOCUMENTED_IMPLEMENTATION_DEPENDENCY'):
            raise CatalogError('Development amendment replacement identity differs')
        # A Judge correction cannot simplify the goal, change repository/tree,
        # expand editable scope or mint a larger per-task resource allowance.
        allowed = {'task_id', 'judge', 'judge_sha256', 'judge_status'}
        if ({k: v for k, v in task.items() if k not in allowed}
                != {k: v for k, v in original.items() if k not in allowed}
                or task['judge_status'] != 'implemented'
                or task['judge'].get('hidden_tests')
                or task['judge'].get('reference_task') != original['judge'].get('reference_task')):
            raise CatalogError('Development Judge correction changed the public task contract')
        matches = [row for row in registry.get('tasks', [])
                   if row.get('task_id') == task['task_id']]
        if len(matches) != 1 or matches[0].get('original_judge_sha256') != task['judge_sha256']:
            raise CatalogError('Amended Judge does not match the replacement specification')
        row = matches[0]
        hashes = {}
        for entry in row.get('files', []):
            path = registry_path.parent / entry['source']
            if not path.resolve().is_relative_to(registry_path.parent.resolve()):
                raise CatalogError('Amended Judge file escaped its registry')
            _public_asset(root, {'path': str(path.relative_to(root)), 'sha256': entry['sha256']})
            hashes[entry['target']] = entry['sha256']
        if not hashes:
            raise CatalogError('Amended Judge has no hash-bound checks')
        _, controls = document(change['controls'])
        cases = controls.get('cases', [])
        kinds = Counter(case.get('kind') for case in cases)
        if (controls.get('schema') != 'h12-development-judge-controls-v1'
                or controls.get('status') != 'PASS'
                or controls.get('task_id') != task['task_id']
                or controls.get('judge_sha256') != task['judge_sha256']
                or controls.get('registry_sha256') != amendment['registry']['sha256']
                or controls.get('paid_calls') != 0
                or controls.get('model_workspace_modified') is not False
                or kinds['baseline'] != 1 or kinds['positive'] < 2 or kinds['negative'] < 2
                or set(kinds) != {'baseline', 'positive', 'negative'}):
            raise CatalogError('Amended Judge lacks baseline and independent behavior controls')
        trees: set[str] = set()
        for case in cases:
            path, result = document(case['result'])
            baseline = case['kind'] == 'baseline'
            expected = 'FAIL' if case['kind'] == 'negative' else 'PASS'
            receipts = result.get('receipts', [])
            if (result.get('task_id') != task['task_id'] or result.get('status') != expected
                    or result.get('phase') != ('baseline' if baseline else 'final')
                    or result.get('judge_sha256') != task['judge_sha256']
                    or result.get('hashes') != hashes or not receipts
                    or not result.get('tree_hash') or result['tree_hash'] in trees
                    or baseline and result.get('baseline_reproduced') is not True):
                raise CatalogError('Amended Judge control outcome is not physically established')
            trees.add(result['tree_hash'])
            observed_failure = False
            for receipt in receipts:
                log = Path(receipt['private_log'])
                if not log.resolve().is_relative_to(path.parent.resolve()):
                    raise CatalogError('Control log is outside its evidence directory')
                _public_asset(root, {'path': str(log.relative_to(root)),
                                     'sha256': receipt['log_sha256']})
                raw = log.read_bytes()
                if hashlib.sha256(raw).hexdigest() != receipt['log_sha256']:
                    raise CatalogError('Control log changed')
                status, ran, failed, _, _ = parse_tests(
                    test_project_kind(receipt['argv'], task['language']),
                    raw.decode(), receipt['exit_code'])
                provenance = ExecutionProvenance.model_validate(receipt.get('provenance'))
                if (receipt.get('complete') is not True or ran <= 0
                        or receipt.get('state') not in {'exited', 'failed'}
                        or receipt.get('exit_code') not in {0, 1}
                        or status not in {'passed', 'failed'}
                        or provenance.actor != 'h12-verifier' or provenance.write_paths
                        or provenance.read_paths != ('',)
                        or provenance.repository != task['repository'].lower()):
                    raise CatalogError('Control execution is incomplete or not a real test report')
                observed_failure |= failed > 0
            if observed_failure != (case['kind'] != 'positive'):
                raise CatalogError('Control status differs from its raw assertion outcomes')
        replacements[original['task_id']] = task
        new_ids.add(task['task_id'])
        registries[task['task_id']] = (registry_path, amendment['registry']['sha256'])
    if {row.get('task_id') for row in registry.get('tasks', [])} != new_ids:
        raise CatalogError('Amendment registry contains an unreviewed task')
    return {'tasks': [replacements.get(task['task_id'], task) for task in tasks],
            'registries': registries, 'document': amendment}
