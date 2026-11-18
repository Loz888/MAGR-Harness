"""Synthetic contract fixtures; actual five Broker controls are recorded separately."""

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_h12_quality_controller import quality  # noqa: F401
from test_h12_v2_public_development import bound, put, sha  # noqa: F401

from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.quality_eval.h12_dev_amendment import load_amendment
from mgar_v3.quality_eval.h12_quality_runner import QualityRunner, write_quality_freeze
from mgar_v3.quality_eval.h12_v2_freeze import (
    AMENDED_DEV_SCHEMA,
    load_public_development_benchmark,
    write_public_development_freeze,
)
from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
from mgar_v3.quality_eval.harness_v4 import CatalogError, judge_sha256


def fixture(b):
    root = b.root
    def ref(path):
        return {'path': str(path.relative_to(root)), 'sha256': sha(path.read_bytes())}
    original = b.tasks[0]
    task = copy.deepcopy(original)
    task.update(task_id='toy-behavior-corrected', judge={
        'version': 'behavior-v2', 'kind': 'registered_behavior',
        'pass_rule': 'Assert public behavior only',
    })
    task['judge_sha256'] = judge_sha256(task)
    check = root / 'amendment/check.py'
    check.parent.mkdir()
    check.write_text('# Controlled fixture, not a real quality Judge\n')
    registry = put(root / 'amendment/registry.json', {
        'schema': 'mgar-h12-judge-implementation-v1', 'tasks': [{
            'task_id': task['task_id'], 'original_judge_sha256': task['judge_sha256'],
            'files': [{'source': check.name, 'target': 'verification/check.py',
                       'sha256': sha(check.read_bytes())}],
            'command': ['python3', '-m', 'pytest', 'verification/check.py'],
            'baseline_oracle': False,
        }],
    })
    cases = []
    for index, kind in enumerate(['baseline', 'positive', 'positive', 'negative', 'negative']):
        directory = root / 'amendment' / f'control-{index}'
        directory.mkdir()
        log = directory / 'command-0.log'
        log.write_text('1 passed in 0.1s\n' if kind == 'positive'
                       else 'FAILED verification/check.py::test_behavior\n1 failed in 0.1s\n')
        result = put(directory / 'result.json', {
            'task_id': task['task_id'], 'status': 'FAIL' if kind == 'negative' else 'PASS',
            'phase': 'baseline' if kind == 'baseline' else 'final',
            'baseline_reproduced': kind == 'baseline',
            'judge_sha256': task['judge_sha256'], 'tree_hash': str(index) * 64,
            'hashes': {'verification/check.py': sha(check.read_bytes())},
            'receipts': [{
                'argv': ['python3', '-m', 'pytest', 'verification/check.py'],
                'private_log': str(log), 'log_sha256': sha(log.read_bytes()),
                'complete': True, 'state': 'exited' if kind == 'positive' else 'failed',
                'exit_code': 0 if kind == 'positive' else 1,
                'provenance': {'operation_id': 'synthetic', 'lease_id': 'synthetic',
                    'lease_hash': 'd' * 64, 'grant_hash': 'e' * 64, 'actor': 'h12-verifier',
                    'repository': task['repository'], 'workspace_id': 'synthetic',
                    'execution_epoch': 1, 'read_paths': [''], 'write_paths': [],
                    'protected_paths': ['.git', '.mgar', '.env']},
            }],
        })
        cases.append({'kind': kind, 'result': ref(result)})
    controls = put(root / 'amendment/controls.json', {
        'schema': 'h12-development-judge-controls-v1', 'status': 'PASS',
        'task_id': task['task_id'], 'judge_sha256': task['judge_sha256'],
        'registry_sha256': sha(registry.read_bytes()), 'cases': cases,
        'paid_calls': 0, 'model_workspace_modified': False,
    })
    amendment = put(root / 'amendment/amendment.json', {
        'schema': 'h12-development-judge-amendment-v1', 'status': 'CONTROLLED',
        'base_tasks_sha256': canonical_hash(b.tasks), 'registry': ref(registry),
        'replacements': [{'original_task_id': original['task_id'],
                          'original_task_sha256': canonical_hash(original),
                          'reason': 'UNDOCUMENTED_IMPLEMENTATION_DEPENDENCY',
                          'task': task, 'controls': ref(controls)}],
    })
    old_plan = json.loads(b.plans[original['task_id']][0].read_bytes())
    plan = put(root / 'amendment/plan.json', {**old_plan, 'task_id': task['task_id']})
    return SimpleNamespace(root=root, task=task, amendment=amendment, controls=controls,
                           registry=registry, ref=ref, plans={task['task_id']: (plan, sha(
                               plan.read_bytes()))})


def amended_bridge(b, f):
    path = b.root / 'frozen/amended-public.json'
    value = write_public_development_freeze(
        b.root, path, benchmark=(b.anchor, sha(b.anchor.read_bytes())),
        candidate_source_sha256='c' * 64, public_plan_refs=f.plans,
        public_probe_root=b.asset_root,
        development_amendment=(f.amendment, sha(f.amendment.read_bytes())),
    )
    return path, value


def test_versioned_amendment_preserves_original_and_routes_only_new_judge(bound):  # noqa: F811
    f = fixture(bound)
    before = copy.deepcopy(bound.tasks)
    path, value = amended_bridge(bound, f)
    assert value['document']['schema'] == AMENDED_DEV_SCHEMA
    assert value['base_tasks'] == before and bound.tasks == before
    assert len(value['tasks']['dev']) == 12
    assert value['tasks']['dev'][0] == f.task
    runner = BenchmarkV2Runner(SimpleNamespace(harness=None), experiment_id='test-amendment',
        evidence=bound.root / 'evidence', freeze_path=path, freeze_sha256=sha(path.read_bytes()),
        api_key=None, root=bound.root, public_development=True)
    assert runner._registry_for(f.task) == (f.registry, sha(f.registry.read_bytes()))
    assert runner._registry_for(before[1]) == (None, None)
    assert runner._profile_for(f.task, 'candidate').resources.max_model_calls == 20
    with pytest.raises(Exception) as exc:
        runner._require_public_development_frozen()
    assert getattr(exc.value, 'code', '') == 'H12_FULL_DEV_READINESS_REQUIRED'


@pytest.mark.parametrize('field,value', [
    ('goal', 'An easier goal that changes the requested behavior.'),
    ('scope', ['']), ('base_commit', 'b' * 40),
    ('resources', {'max_model_calls': 100}), ('task_id', 'toy-1'),
])
def test_judge_correction_cannot_relax_task_or_budget(bound, field, value):  # noqa: F811
    f = fixture(bound)
    doc = json.loads(f.amendment.read_bytes())
    target = doc['replacements'][0]['task']
    if field == 'resources':
        target[field].update(value)
    else:
        target[field] = value
    put(f.amendment, doc)
    with pytest.raises(CatalogError):
        load_amendment(bound.root, f.ref(f.amendment), bound.tasks)


@pytest.mark.parametrize('change', [
    'log', 'missing-negative', 'false-outcome', 'paid', 'model-tree',
])
def test_controls_revalidate_physical_reports_and_zero_model_scope(bound, change):  # noqa: F811
    f = fixture(bound)
    controls = json.loads(f.controls.read_bytes())
    if change == 'log':
        (f.controls.parent / 'control-1/command-0.log').write_text('truncated')
    elif change == 'missing-negative':
        controls['cases'].pop()
    elif change == 'paid':
        controls['paid_calls'] = 1
    elif change == 'model-tree':
        controls['model_workspace_modified'] = True
    else:
        case = controls['cases'][-1]
        result_path = bound.root / case['result']['path']
        result = json.loads(result_path.read_bytes())
        result['status'] = 'PASS'
        put(result_path, result)
        case['result'] = f.ref(result_path)
    put(f.controls, controls)
    doc = json.loads(f.amendment.read_bytes())
    doc['replacements'][0]['controls'] = f.ref(f.controls)
    put(f.amendment, doc)
    with pytest.raises(CatalogError):
        load_amendment(bound.root, f.ref(f.amendment), bound.tasks)


def test_old_protocol_cannot_silently_accept_an_amendment(bound):  # noqa: F811
    f = fixture(bound)
    path, _ = amended_bridge(bound, f)
    doc = json.loads(path.read_bytes())
    doc['schema'] = 'h12-benchmark-v2-public-dev-v1'
    put(path, doc)
    with pytest.raises(CatalogError):
        load_public_development_benchmark(bound.root, path, sha(path.read_bytes()))


def test_full_quality_keeps_unseen_holdout_and_amended_dev_config(quality, monkeypatch):  # noqa: F811
    b = quality.bound
    f = fixture(b)
    path, value = amended_bridge(b, f)
    target = b.root / 'frozen/amended-quality.json'
    write_quality_freeze(b.root, target, development=(path, sha(path.read_bytes())),
                        holdout_image_map=value['assets']['dev_image_map'])
    runner = QualityRunner(SimpleNamespace(harness=None), experiment_id='amended-quality',
        evidence=b.root / 'amended-evidence', freeze_path=target,
        freeze_sha256=sha(target.read_bytes()), api_key=None, root=b.root)
    monkeypatch.setattr(runner, '_source', lambda _: b.workspace)
    monkeypatch.setattr(runner, '_materialize_into',
                        lambda task, source, target: Path(shutil.copytree(source, target)))
    assert runner._tasks('holdout') == quality.runner._tasks('holdout')
    config = runner._config()
    assert len(config['profiles']) == 48
    assert {row['task_id'] for row in config['profiles'] if row['split'] == 'dev'} == {
        f.task['task_id'], *[task['task_id'] for task in b.tasks[1:]]}
