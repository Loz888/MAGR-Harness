"""Explicit full diagnostics preserve old gates, facts and resource boundaries."""
# ruff: noqa: F811 -- pytest fixtures imported from the shared controller fixture.

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_h12_quality_controller import quality  # noqa: F401
from test_h12_v2_public_development import bound, put, sha  # noqa: F401

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_quality_runner import (
    COMPLETE_SCHEMA,
    QualityRunner,
    load_quality_freeze,
    write_quality_freeze,
)
from mgar_v3.quality_eval.h12_scope_amendment import load_scope_amendment, validate_scope_basis
from mgar_v3.quality_eval.h12_v2_freeze import (
    SCOPED_DEV_SCHEMA,
    load_public_development_benchmark,
    write_public_development_freeze,
)
from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
from mgar_v3.quality_eval.harness_v4 import CatalogError


def scope_document(b):
    source = b.root / 'scope/public-implementation.py'
    source.parent.mkdir()
    source.write_text('def behavior():\n    return 1\n')
    def ref(path):
        return {'path': str(path.relative_to(b.root)), 'sha256': sha(path.read_bytes())}
    original = b.tasks[0]
    document = put(b.root / 'scope/amendment.json', {
        'schema': 'h12-development-scope-amendment-v1', 'status': 'REVIEWED',
        'base_tasks_sha256': canonical_hash(b.tasks),
        'changes': [{'task_id': original['task_id'],
            'original_task_sha256': canonical_hash(original), 'original_scope': original['scope'],
            'reason': 'PUBLIC_IMPLEMENTATION_OUTSIDE_FROZEN_SCOPE',
            'implementation_path': 'implementation.py', 'replacement_scope': ['implementation.py'],
            'symbol': 'behavior', 'public_source': ref(source)}],
    })
    return document, source, ref


def test_scope_correction_has_distinct_freeze_and_only_changes_the_implementation_path(bound):
    doc, source, ref = scope_document(bound)
    originals = copy.deepcopy(bound.tasks)
    path = bound.root / 'scope/frozen.json'
    result = write_public_development_freeze(bound.root, path,
        benchmark=(bound.anchor, sha(bound.anchor.read_bytes())), candidate_source_sha256='c' * 64,
        public_plan_refs=bound.plans, public_probe_root=bound.asset_root,
        development_scope_amendment=(doc, sha(doc.read_bytes())))
    assert result['document']['schema'] == SCOPED_DEV_SCHEMA
    assert result['base_tasks'] == originals == bound.tasks
    target = result['tasks']['dev'][0]
    assert target['scope'] == ['implementation.py']
    assert {k: v for k, v in target.items() if k != 'scope'} == {
        k: v for k, v in originals[0].items() if k != 'scope'}
    assert len(result['tasks']['dev']) == 12
    assert result['tasks']['dev'][1:] == originals[1:]
    runner = BenchmarkV2Runner(SimpleNamespace(harness=None), experiment_id='scope-proof',
        evidence=bound.root / 'scope-proof', freeze_path=path,
        freeze_sha256=sha(path.read_bytes()), api_key=None, root=bound.root,
        public_development=True)
    profile = runner._profile_for(target, 'candidate')
    assert profile.write_paths == ('implementation.py',)
    assert profile.resources.max_model_calls == originals[0]['resources']['max_model_calls']
    physical = bound.root / 'materialized'
    physical.mkdir()
    (physical / 'implementation.py').write_bytes(source.read_bytes())
    validate_scope_basis(physical, result['scope_bindings'][target['task_id']])
    runner._after_materialize(target, physical)
    (physical / 'implementation.py').write_text('def behavior(): return 2\n')
    with pytest.raises(CatalogError, match='original repository tree'):
        runner._after_materialize(target, physical)
    raw = json.loads(path.read_bytes())
    raw['schema'] = 'h12-benchmark-v2-public-dev-v1'
    put(path, raw)
    with pytest.raises(CatalogError):
        load_public_development_benchmark(bound.root, path, sha(path.read_bytes()))


@pytest.mark.parametrize('change', ['folder', 'test', 'metadata', 'wrong-hash', 'wrong-symbol',
                                    'already-granted', 'foreign-task'])
def test_scope_correction_rejects_unbound_or_broad_permissions(bound, change):
    doc, source, ref = scope_document(bound)
    raw = json.loads(doc.read_bytes())
    item = raw['changes'][0]
    if change in {'folder', 'test', 'metadata', 'already-granted'}:
        path = {'folder': 'src', 'test': 'tests/implementation.py',
                'metadata': '.mgar/implementation.py', 'already-granted': 'source.py'}[change]
        item.update(implementation_path=path, replacement_scope=[path])
    elif change == 'wrong-hash':
        source.write_text('def behavior(): return 3\n')
    elif change == 'wrong-symbol':
        item['symbol'] = 'unrelated'
    else:
        item['task_id'] = 'another-task'
    put(doc, raw)
    with pytest.raises(CatalogError):
        load_scope_amendment(bound.root, ref(doc), bound.tasks)


def complete_runner(q):
    root = q.bound.root
    auth = put(root / 'diagnostic/authorization.json', {
        'schema': 'h12-complete-matrix-authorization-v1', 'status': 'USER_REQUESTED',
        'mode': 'complete-matrix-diagnostic-v1', 'cases': {'dev': 12, 'holdout': 12},
        'thresholds': {'dev': 10, 'holdout': 9, 'denominator_each': 12},
        'shared_budget_micros': 12000000, 'thinking': {'type': 'enabled'},
        'reasoning_effort': 'max', 'allow_unknown_retry': False, 'allow_publication': False,
        'stop_on_known_task_failure': False, 'holdout_disposition': 'QUARANTINE_AFTER_REPORT',
    })
    path = root / 'diagnostic/quality.json'
    dev = q.value['document']['public_dev_freeze']
    write_quality_freeze(root, path, development=(root / dev['path'], dev['sha256']),
        holdout_image_map=q.value['dev']['assets']['dev_image_map'],
        complete_matrix_authorization=(auth, sha(auth.read_bytes())))
    runner = QualityRunner(SimpleNamespace(harness=None), experiment_id='complete-diagnostic',
        evidence=root / 'diagnostic/evidence', freeze_path=path,
        freeze_sha256=sha(path.read_bytes()),
        api_key=None, root=root)
    return runner, path, auth


@pytest.mark.parametrize('mode', ['known-failures', 'missing', 'unknown', 'reservation',
                                  'foreign-run', 'forged-cost', 'missing-runtime', 'wrong-task'])
def test_full_diagnostics_continue_after_known_failures_but_need_every_original_pg_fact(
    quality, monkeypatch, mode
):
    runner, path, auth = complete_runner(quality)
    assert runner.quality['document']['schema'] == COMPLETE_SCHEMA
    monkeypatch.setattr(BenchmarkV2Runner, '_live_policy', lambda *a: None)
    def usage(profile):
        if mode == 'missing-runtime':
            return None
        return {'run_id': 'foreign' if mode == 'foreign-run' else 'run-' + profile.task_id,
            'tree_hash': 'b' * 64, 'engine_status': 'failed', 'unknown_calls': 0,
            'model_calls': 1, 'settled_cost_micros': 7 if mode == 'forged-cost' else 5}
    monkeypatch.setattr(runner, 'sample_usage', usage)
    rows = []
    for index, task in enumerate(runner._tasks('dev')):
        if mode == 'missing' and index == 11:
            continue
        row = {'experiment_id': runner.experiment_id, 'task_id': task['task_id'],
            'split': 'dev', 'type': task['type'], 'arm': 'candidate', 'status': 'FAIL',
            'raw_runtime_status': 'budget_exhausted', 'judge_status': 'FAIL',
            'final_tree': 'b' * 64, 'judge_tree': 'b' * 64, 'cost_micros': 5,
            'settled_cost_micros': 5, 'model_calls': 1, 'usage_known': mode != 'unknown',
            'reserved_micros': 1 if mode == 'reservation' else 0,
            'regression_evidence_known': True, 'new_regression_failures': 0,
            'run_id': 'run-' + task['task_id']}
        if mode == 'wrong-task':
            row['task_id'] = 'another-task'
        put(runner.evidence / 'candidate/dev' / task['task_id'] / 'score.json', row)
        rows.append(row)
    if mode == 'known-failures':
        runner._live_policy('holdout')
        # Execution coverage does not turn failures into passing acceptance.
        summary = h12.score_records(rows, 'dev', expected_tasks=runner._tasks('dev'))
        assert not summary['threshold_met']
    else:
        with pytest.raises(DomainError):
            runner._live_policy('holdout')


@pytest.mark.parametrize('field,value', [
    ('shared_budget_micros', 13000000), ('allow_unknown_retry', True),
    ('allow_publication', True), ('stop_on_known_task_failure', True),
    ('reasoning_effort', 'high'), ('cases', {'dev': 12, 'holdout': 11}),
])
def test_complete_mode_needs_its_exact_explicit_protocol(quality, field, value):
    runner, path, auth = complete_runner(quality)
    raw = json.loads(auth.read_bytes())
    raw[field] = value
    put(auth, raw)
    doc = json.loads(path.read_bytes())
    doc['complete_matrix_authorization']['sha256'] = sha(auth.read_bytes())
    put(path, doc)
    with pytest.raises(CatalogError):
        load_quality_freeze(quality.bound.root, path, sha(path.read_bytes()))


def test_complete_mode_cannot_be_attached_to_old_protocol_or_skip_readiness(quality):
    runner, path, auth = complete_runner(quality)
    with pytest.raises(DomainError, match='Preparation grants no calls'):
        QualityRunner(SimpleNamespace(harness=None), experiment_id='blocked',
            evidence=quality.bound.root / 'no-calls', freeze_path=path,
            freeze_sha256=sha(path.read_bytes()), api_key='never-used', execute_live=True,
            root=quality.bound.root)
    raw = json.loads(path.read_bytes())
    raw['schema'] = 'h12-public-quality-candidate-v1'
    put(path, raw)
    with pytest.raises(CatalogError):
        load_quality_freeze(quality.bound.root, path, sha(path.read_bytes()))


def test_complete_mode_uses_original_pg_shared_budget_and_no_provider_calls(
    quality, monkeypatch, engine
):
    runner, path, auth = complete_runner(quality)
    runners = (quality.runner, runner)
    for item in runners:
        item.engine, item.store = engine, engine.harness
        monkeypatch.setattr(item, '_source', lambda _: quality.bound.workspace)
        monkeypatch.setattr(item, '_materialize_into',
                            lambda task, source, target: Path(shutil.copytree(source, target)))
        monkeypatch.setattr(item, '_require_public_development_frozen', lambda: None)
        item.initialize()
    old = engine.harness.get_experiment(quality.runner.experiment_id)
    new = engine.harness.get_experiment(runner.experiment_id)
    assert old['profile_hash'] == new['profile_hash']
    assert old['limit_micros'] == new['limit_micros'] == 12000000
    assert old['used_calls'] == new['used_calls'] == 0
    assert new['config']['quality_protocol']['schema'] == COMPLETE_SCHEMA
