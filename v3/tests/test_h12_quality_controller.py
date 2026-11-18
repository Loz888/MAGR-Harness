"""Synthetic independent controller boundaries; these are not quality samples."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_h12_v2_public_development import bound, bridge, put, sha  # noqa: F401

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval.h12_full_dev_readiness import validate_full_report
from mgar_v3.quality_eval.h12_quality_runner import (
    QualityRunner,
    load_quality_freeze,
    write_quality_freeze,
)
from mgar_v3.quality_eval.h12_runner import HOLDOUT_INFRA_V4
from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
from mgar_v3.quality_eval.harness_v4 import CatalogError


@pytest.fixture
def quality(bound):  # noqa: F811
    root = bound.root
    anchor = json.loads(bound.anchor.read_bytes())
    def ref(path):
        return {'path': str(path.relative_to(root)), 'sha256': sha(path.read_bytes())}
    public = root / '.local/h12/independent-holdout-v2-20261113/public-assets/test_public.py'
    public.parent.mkdir(parents=True)
    public.write_bytes(b'# Synthetic public feature fixture\n')
    tasks = [{**copy.deepcopy(row), 'split': 'holdout', 'task_id': f'independent-{i}',
              'public_assets': [{'source': str(public.relative_to(root)),
                                 'target': 'tests/test_independent.py',
                                 'sha256': sha(public.read_bytes())}]}
             for i, row in enumerate(bound.tasks)]
    catalog = root / 'private/holdout.jsonl'
    catalog.write_text(''.join(json.dumps(row) + '\n' for row in tasks))
    (root / 'experiments/harness-v4/tasks/holdout.jsonl').write_text('')
    registry = root / 'private/holdout-registry.json'
    registry.write_bytes(b'opaque synthetic registry')
    anchor['candidate_source_sha256'] = 'a' * 64  # Historic source is provenance only.
    for split in ('dev', 'holdout'):
        report = put(root / f'private/old-{split}-readiness.json', {
            'ready_count': 12, 'denominator': 12,
            'candidate_source_sha256_before': 'a' * 64,
            'candidate_source_sha256_after': 'a' * 64,
            'status': 'DRAFT_READY' if split == 'dev' else 'READY',
        })
        anchor[f'{split}_readiness'] = ref(report)
    controls = put(root / 'private/controls.json', {
        'status': 'PASS', 'passed_tasks': 13,
        'task_ids': ['toy-replacement'] + [row['task_id'] for row in tasks],
    })
    anchor.update(holdout_catalog=ref(catalog), holdout_judges=ref(registry),
                  holdout_image_map=anchor['dev_image_map'], judge_controls=ref(controls))
    bound.anchor.write_text(json.dumps(anchor))
    put(root / f'experiments/harness-v4/{HOLDOUT_INFRA_V4}.json',
        {'revision': HOLDOUT_INFRA_V4})
    dev_path, dev = bridge(bound, sealed=True)
    path = root / 'frozen/quality-preparation.json'
    value = write_quality_freeze(root, path, development=(dev_path, sha(dev_path.read_bytes())),
                                 holdout_image_map=dev['assets']['dev_image_map'])
    runner = QualityRunner(SimpleNamespace(harness=None), experiment_id='quality-synthetic',
                           evidence=root / 'quality-evidence', freeze_path=path,
                           freeze_sha256=sha(path.read_bytes()), api_key=None, root=root)
    return SimpleNamespace(bound=bound, path=path, value=value, runner=runner)


def test_quality_has_same_dev_plans_and_independent_profile_budget(quality, monkeypatch):
    value = quality.runner
    monkeypatch.setattr(value, '_source', lambda _: quality.bound.workspace)
    monkeypatch.setattr(value, '_materialize_into',
                        lambda task, source, target: Path(shutil.copytree(source, target)))
    assert len(value._tasks('dev')) == len(value._tasks('holdout')) == 12
    assert not value._development_only()
    config = value._config()
    assert len(config['profiles']) == 48
    assert config['quality_protocol']['shared_budget_micros'] == 12000000
    assert config['quality_protocol']['thresholds'] == {
        'dev': 10, 'holdout': 9, 'denominator_each': 12}
    assert value._profile_for(value._tasks('dev')[0], 'candidate').public_probe is not None
    holdout = value._tasks('holdout')[0]
    profile = value._profile_for(holdout, 'candidate')
    assert profile.public_probe is None and profile.resources.max_model_calls == 20
    assert profile.sandbox_scratch_mb == 256
    assert value._registry_for(holdout)[0].name == 'holdout-registry.json'


def test_full_quality_matrix_initializes_in_original_pg_store_and_shares_budget(
    quality, monkeypatch, engine
):
    value = quality.runner
    value.engine, value.store = engine, engine.harness
    monkeypatch.setattr(value, '_source', lambda _: quality.bound.workspace)
    monkeypatch.setattr(value, '_materialize_into',
                        lambda task, source, target: Path(shutil.copytree(source, target)))
    # Only isolate physical readiness here (validated with real Broker in the
    # other tests). Exercise the production initialize/config/Engine SQL path.
    monkeypatch.setattr(value, '_require_public_development_frozen', lambda: None)
    value.initialize()
    first = engine.harness.get_experiment(value.experiment_id)
    assert first['config']['environment_revision'] == 'h12-benchmark-v2'
    assert first['config']['public_development'] is False
    assert len(first['config']['profiles']) == 48
    value.experiment_id = 'quality-synthetic-second'
    value.initialize()
    second = engine.harness.get_experiment(value.experiment_id)
    assert first['profile_hash'] == second['profile_hash']
    assert first['limit_micros'] == second['limit_micros'] == 12000000
    assert first['used_calls'] == second['used_calls'] == 0


def test_quality_preparation_cannot_send_or_initialize(quality):
    with pytest.raises(DomainError) as caught:
        QualityRunner(SimpleNamespace(harness=None), experiment_id='no-calls',
                      evidence=quality.bound.root / 'forbidden', freeze_path=quality.path,
                      freeze_sha256=sha(quality.path.read_bytes()), api_key='never-used',
                      execute_live=True, root=quality.bound.root)
    assert caught.value.code == 'H12_HOLDOUT_READINESS_REQUIRED'
    # Even fake FROZEN development metadata is not accepted as physical evidence.
    with pytest.raises(CatalogError):
        quality.runner.initialize()


def test_both_zero_model_preparations_may_run_before_sealing_but_never_grant_calls(quality):
    dev, _ = bridge(quality.bound)
    root = quality.bound.root
    path = root / 'frozen/early-quality-preparation.json'
    image_map = quality.value['dev']['assets']['dev_image_map']
    write_quality_freeze(root, path, development=(dev, sha(dev.read_bytes())),
                         holdout_image_map=image_map)
    value = QualityRunner(SimpleNamespace(harness=None), experiment_id='zero-model-only',
                          evidence=root / 'early-evidence', freeze_path=path,
                          freeze_sha256=sha(path.read_bytes()), api_key=None, root=root)
    with pytest.raises(DomainError):
        value.initialize()
    ready = quality.value['independent']['assets']['holdout_readiness']
    with pytest.raises(CatalogError, match='complete frozen development'):
        write_quality_freeze(root, root / 'frozen/invalid-early-seal.json',
                             development=(dev, sha(dev.read_bytes())),
                             holdout_image_map=image_map,
                             readiness=(ready, sha(ready.read_bytes())))


@pytest.mark.parametrize('field', ['candidate_source_sha256', 'thresholds',
                                  'holdout_profiles_sha256', 'holdout_image_locks'])
def test_quality_changed_bindings_rejected_before_engine(quality, field):
    document = json.loads(quality.path.read_bytes())
    document[field] = 'different'
    quality.path.write_text(json.dumps(document))
    with pytest.raises(CatalogError):
        load_quality_freeze(quality.bound.root, quality.path, sha(quality.path.read_bytes()))


def test_historic_holdout_readiness_is_not_current_execution_proof(quality):
    root = quality.bound.root
    old = quality.value['independent']['assets']['holdout_readiness']
    path = root / 'frozen/quality-with-old-readiness.json'
    ref = quality.value['document']['public_dev_freeze']
    write_quality_freeze(root, path, development=(root / ref['path'], ref['sha256']),
                         holdout_image_map=quality.value['dev']['assets']['dev_image_map'],
                         readiness=(old, sha(old.read_bytes())))
    runner = QualityRunner(SimpleNamespace(harness=None), experiment_id='old-receipts',
                           evidence=root / 'old-evidence', freeze_path=path,
                           freeze_sha256=sha(path.read_bytes()), api_key=None, root=root)
    with pytest.raises(CatalogError):
        validate_full_report(json.loads(old.read_bytes()), old.parent, bindings={},
                             candidate_source='c' * 64, split='holdout')
    with pytest.raises(CatalogError):
        runner._require_public_development_frozen()


@pytest.mark.parametrize('mode', [
    'missing', 'nine', 'wrong-experiment', 'unknown', 'forged', 'ten',
])
def test_holdout_requires_complete_current_development_gate(quality, monkeypatch, mode):
    runner = quality.runner
    # Isolate the gate logic from readiness, exercised separately above and in
    # the real Broker test. No provider or real quality sample is used here.
    monkeypatch.setattr(BenchmarkV2Runner, '_live_policy', lambda *a: None)
    monkeypatch.setattr(runner, 'sample_usage', lambda p: {
        'run_id': 'synthetic-run', 'tree_hash': 'b' * 64, 'engine_status': 'succeeded',
        'unknown_calls': 1 if mode == 'unknown' else 0,
    } if mode != 'forged' else None)
    tasks = runner._tasks('dev')
    for i, task in enumerate(tasks):
        if mode == 'missing' and i == 11:
            continue
        put(runner.evidence / 'candidate/dev' / task['task_id'] / 'score.json', {
            'experiment_id': 'other' if mode == 'wrong-experiment' else runner.experiment_id,
            'task_id': task['task_id'], 'split': 'dev', 'type': task['type'], 'arm': 'candidate',
            'status': 'PASS' if i < (9 if mode == 'nine' else 10) else 'FAIL',
            'raw_runtime_status': 'completed', 'judge_status': 'PASS',
            'final_tree': 'b' * 64, 'judge_tree': 'b' * 64, 'cost_micros': 0,
            'regression_evidence_known': True, 'new_regression_failures': 0,
            'run_id': 'synthetic-run',
        })
    if mode == 'ten':
        runner._live_policy('holdout')
    else:
        with pytest.raises(DomainError) as caught:
            runner._live_policy('holdout')
        assert caught.value.code == 'H12_DEV_THRESHOLD_REQUIRED'


def test_independent_readiness_uses_real_broker_and_cannot_be_relabelled(tmp_path):
    from test_h12_full_dev_readiness import LOCK, ROOT, ToyRunner

    class IndependentToy(ToyRunner):
        def _tasks(self, split):
            assert split == 'holdout'
            return self.tasks

        def _revision_for(self, task):
            return HOLDOUT_INFRA_V4

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'test_smoke.py').write_text(
        'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
    lock = json.loads(LOCK.read_bytes())
    runtime = 'python-' + '.'.join(lock['python_version'].split()[1].split('.')[:2])
    tasks = [dict(task_id=f'independent-toy-{i}', split='holdout', type='feature',
                  language='python', repository='example/public', base_commit='a' * 40,
                  goal='Synthetic public feature', scope=['feature.py'],
                  resources=dict(max_model_calls=20, max_tool_calls=80, max_cost_micros=500000,
                                 deadline_seconds=60, max_context_tokens=32000,
                                 max_repair_rounds=6),
                  environment=dict(runtime=runtime,
                                   verify=['python3 -B -m unittest -q test_smoke']),
                  acceptance=dict(regression=['python3 -B -m unittest -q test_smoke']))
             for i in range(12)]
    runner = IndependentToy(SimpleNamespace(harness=None), experiment_id='synthetic-readiness',
                            evidence=tmp_path / 'controller', root=ROOT, api_key=None,
                            image_locks={'example/public@' + 'a' * 40: str(LOCK)})
    runner.tasks, runner.source = tasks, source
    report = runner._prepare_split_readiness('holdout', tmp_path / 'proof')
    assert report['status'] == 'FULL_HOLDOUT_READY' and report['paid_model_calls'] == 0
    bindings = runner._readiness_bindings('holdout')
    validate_full_report(report, tmp_path / 'proof', bindings=bindings,
                         candidate_source='c' * 64, split='holdout')
    with pytest.raises(CatalogError):
        validate_full_report(report, tmp_path / 'proof', bindings=bindings,
                             candidate_source='c' * 64)
    with pytest.raises(CatalogError):
        validate_full_report(report, tmp_path / 'proof', bindings=bindings,
                             candidate_source='d' * 64, split='holdout')
    receipt = next((tmp_path / 'proof').glob('*/runtime-receipt.json'))
    receipt.write_bytes(receipt.read_bytes() + b' ')
    with pytest.raises(CatalogError):
        validate_full_report(report, tmp_path / 'proof', bindings=bindings,
                             candidate_source='c' * 64, split='holdout')
