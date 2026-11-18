"""Dev-only controller binding tests; neither fixtures nor metadata prove quality."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_public_plan import PUBLIC_ROLES
from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4, profile_for
from mgar_v3.quality_eval.h12_v2_freeze import (
    PUBLIC_RUNTIME_POLICY,
    development_protocol_metadata,
    load_public_development_benchmark,
    write_public_development_freeze,
)
from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
from mgar_v3.quality_eval.harness_v4 import CatalogError, judge_sha256


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def bound(tmp_path, monkeypatch):
    root = tmp_path / 'controller'
    workspace = tmp_path / 'original'
    workspace.mkdir()
    (workspace / 'source.py').write_bytes(b'value = 1\n')
    judge = {'version': 'synthetic', 'pass_rule': 'independently checked after editing'}
    task = {
        'schema': 'mgar-hv4-task-v1', 'task_id': 'toy-0', 'split': 'dev',
        'type': 'bugfix', 'composition': 'bugfix', 'repository': 'example/public',
        'source_url': 'https://example.invalid/public', 'base_commit': 'a' * 40,
        'fix_commit': None, 'language': 'python',
        'goal': 'Preserve the declared public toy behavior.',
        'scope': ['source.py'], 'judge': judge, 'judge_status': 'implemented',
        'judge_sha256': judge_sha256({'judge': judge}),
        'resources': {'max_model_calls': 20, 'max_tool_calls': 80, 'max_cost_micros': 500000,
                      'deadline_seconds': 300, 'max_context_tokens': 32000,
                      'max_repair_rounds': 6},
        'environment': {'platform': 'linux', 'runtime': 'python3', 'prepare': [],
                        'verify': ['python3 -m pytest tests/test_public.py'], 'network': 'offline'},
        'acceptance': {'baseline': ['public original'], 'required': ['declared behavior'],
                       'regression': ['python3 -m pytest tests/test_public.py']},
    }
    original = [{**copy.deepcopy(task), 'task_id': f'toy-{index}'} for index in range(12)]
    tasks = copy.deepcopy(original)
    tasks[-1]['task_id'] = 'toy-replacement'
    dev = root / 'public/dev.jsonl'
    dev.parent.mkdir(parents=True)
    dev.write_text(''.join(json.dumps(row) + '\n' for row in tasks))
    old = root / 'experiments/harness-v4/tasks/dev.jsonl'
    old.parent.mkdir(parents=True)
    old.write_text(''.join(json.dumps(row) + '\n' for row in original))
    registry = root / 'private/dev-registry.json'
    registry.parent.mkdir()
    registry.write_bytes(b'opaque registry identity; never a model input')
    amendment = put(root / 'public/amendment.json', {
        'repository': 'another/public', 'base_commit': 'b' * 40,
    })
    lock = put(root / 'locks/python.json', {'image': 'synthetic-image'})
    image_map = put(root / 'public/images.json', {'example/public@' + 'a' * 40: str(lock)})
    proposal = put(root / 'public/proposal.json', {
        'revision': 'h12-benchmark-v2', 'status': 'DRAFT_NOT_FROZEN',
        'dev_catalog_sha256': sha(dev.read_bytes()),
        'dev_registry_sha256': sha(registry.read_bytes()),
        'public_amendments_sha256': sha(amendment.read_bytes()),
        'dev_replacement': 'toy-replacement',
    })
    frozen_h00 = {'status': 'PASS', 'opaque_frozen_h00': 'unchanged'}
    monkeypatch.setattr(h12, 'frozen_hashes', lambda _: frozen_h00)
    def ref(path):
        return {'path': str(path.relative_to(root)), 'sha256': sha(path.read_bytes())}
    anchor = put(root / 'frozen/original-v2.json', {
        'schema': 'h12-benchmark-v2-freeze-v1', 'status': 'FROZEN',
        'revision': 'h12-benchmark-v2', 'thresholds': {'dev': 10, 'holdout': 9,
                                                     'denominator_each': 12},
        'shared_budget_micros': 12000000, 'old_h00_hashes': frozen_h00,
        'dev_catalog': ref(dev), 'dev_judges': ref(registry), 'dev_image_map': ref(image_map),
        'public_amendments': ref(amendment), 'proposal': ref(proposal),
        # No such files exist. The new bridge must not select, stat or open them.
        'holdout_catalog': {'path': 'do-not-read/held-out', 'sha256': 'f' * 64},
        'holdout_judges': {'path': 'do-not-read/private', 'sha256': 'e' * 64},
    })
    put(root / f'experiments/harness-v4/{DEV_ENVIRONMENT_V4}.json',
        {'revision': DEV_ENVIRONMENT_V4})
    put(root / 'experiments/harness-v4/h12-model-v2.json', {'model': 'synthetic'})
    put(root / 'experiments/harness-v4/h12-authorization-v1.json', {
        'allow_paid': True, 'max_cost_micros': 12000000,
    })
    asset_root = root / 'public/probes'
    raw = b'# Independently authored synthetic public behavior check.\n'
    asset = asset_root / sha(raw) / 'probe.py'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(raw)
    first = tasks[0]
    manifest = put(root / 'public/plan.json', {
        'schema': 'h12-public-plan-v1', 'status': 'FROZEN', 'task_id': first['task_id'],
        'repository': first['repository'], 'base_commit': first['base_commit'],
        'public_goal_sha256': sha(first['goal'].encode()),
        'resources_sha256': canonical_hash(first['resources']),
        'original_tree_sha256': _tree_hash(_scan(workspace)[0]), 'roles': PUBLIC_ROLES,
        'regression_argv': list(profile_for(first, 'synthetic', 'candidate',
                                          environment_revision=DEV_ENVIRONMENT_V4).public_test_argv),
        'public_basis': [{'path': 'source.py',
                          'sha256': sha((workspace / 'source.py').read_bytes())}],
        'probe': {'plan_id': 'toy-public', 'asset_sha256': sha(raw), 'case_ids': ['behavior']},
    })
    plans = {first['task_id']: (manifest, sha(manifest.read_bytes()))}
    monkeypatch.setattr(BenchmarkV2Runner, '_candidate_fingerprint', lambda _: 'c' * 64)
    return SimpleNamespace(root=root, workspace=workspace, tasks=tasks, anchor=anchor,
                           plans=plans, asset_root=asset_root, asset=asset, lock=lock)


def bridge(bound, *, sealed=False):
    subset = put(bound.root / 'reports/subset.json', {'status': 'PUBLIC_SUBSET_READY'})
    full = put(bound.root / 'reports/full.json', {'status': 'FULL_DEV_READY'})
    path = bound.root / ('frozen/sealed.json' if sealed else 'frozen/preparation.json')
    result = write_public_development_freeze(
        bound.root, path, benchmark=(bound.anchor, sha(bound.anchor.read_bytes())),
        candidate_source_sha256='c' * 64, public_plan_refs=bound.plans,
        public_probe_root=bound.asset_root,
        public_readiness_ref=(subset, sha(subset.read_bytes())) if sealed else None,
        full_dev_readiness_ref=(full, sha(full.read_bytes())) if sealed else None,
    )
    return path, result


def runner(bound, path, *, live=False):
    return BenchmarkV2Runner(
        SimpleNamespace(harness=None), experiment_id='new-synthetic-sample',
        evidence=bound.root / 'runtime-evidence', freeze_path=path,
        freeze_sha256=sha(path.read_bytes()), api_key=None, root=bound.root,
        public_development=True, execute_live=live,
    )


def test_dev_bridge_never_loads_absent_holdout_and_preserves_profiles_and_budgets(bound):
    path, result = bridge(bound)
    value = runner(bound, path)
    assert set(result['tasks']) == {'dev'} and len(value._tasks('dev')) == 12
    first, unlisted = value._tasks('dev')[:2]
    opted = value._profile_for(first, 'candidate')
    assert opted.verification_protocol == 'public-probe-v1' and opted.public_probe is not None
    assert value._profile_for(unlisted, 'candidate').verification_protocol == 'legacy'
    old = profile_for(first, 'another-identity', 'candidate',
                      environment_revision=DEV_ENVIRONMENT_V4)
    assert opted.resources == old.resources and opted.resources.max_model_calls == 20
    assert opted.write_paths == old.write_paths and opted.public_test_argv == old.public_test_argv
    assert value._registry_for(first) == (None, None)
    assert value._registry_for(value._tasks('dev')[-1])[0] == result['assets']['dev_judges']
    with pytest.raises(DomainError, match='held-out'):
        value._tasks('holdout')


def test_preparation_cannot_initialize_or_send_even_with_live_flag(bound):
    path, _ = bridge(bound)
    value = runner(bound, path)
    with pytest.raises(DomainError) as caught:
        value.initialize()
    assert caught.value.code == 'H12_FULL_DEV_READINESS_REQUIRED'
    with pytest.raises(DomainError) as caught:
        runner(bound, path, live=True)
    assert caught.value.code == 'H12_FULL_DEV_READINESS_REQUIRED'


@pytest.mark.parametrize('kind', [
    'asset', 'plan', 'image_lock', 'environment', 'profile', 'report',
])
def test_changed_frozen_input_is_rejected_before_dispatch(bound, kind):
    path, result = bridge(bound, sealed=True)
    if kind == 'asset':
        bound.asset.write_bytes(b'changed')
    elif kind == 'plan':
        bound.plans['toy-0'][0].write_bytes(b'changed')
    elif kind == 'image_lock':
        bound.lock.write_bytes(b'changed')
    elif kind == 'environment':
        (bound.root / f'experiments/harness-v4/{DEV_ENVIRONMENT_V4}.json').write_bytes(b'changed')
    elif kind == 'report':
        result['full_dev_readiness'][0].write_bytes(b'changed')
    else:
        document = json.loads(path.read_bytes())
        document['profiles_sha256'] = 'd' * 64
        path.write_text(json.dumps(document))
    with pytest.raises(CatalogError):
        runner(bound, path, live=True)


def test_sealed_report_refs_are_not_treated_as_executed_readiness(bound):
    path, _ = bridge(bound, sealed=True)
    value = runner(bound, path, live=True)
    # The synthetic fixture pins report bytes only. Its empty receipt set is not evidence.
    with pytest.raises(CatalogError):
        value._require_public_development_frozen()
    with pytest.raises(DomainError) as caught:
        value._live_policy('holdout')
    assert caught.value.code == 'H12_HISTORICAL_HOLDOUT_QUARANTINED'


def test_changed_candidate_and_missing_ready_report_fail_without_engine(bound, monkeypatch):
    path, _ = bridge(bound, sealed=True)
    monkeypatch.setattr(BenchmarkV2Runner, '_candidate_fingerprint', lambda _: 'd' * 64)
    with pytest.raises(DomainError) as caught:
        runner(bound, path)
    assert caught.value.code == 'H12_FROZEN_EXECUTION_CHANGED'
    document = json.loads(path.read_bytes())
    document['full_dev_readiness'] = None
    path.write_text(json.dumps(document))
    with pytest.raises(CatalogError, match='cannot be mixed'):
        load_public_development_benchmark(bound.root, path, sha(path.read_bytes()))


def test_frozen_config_rechecks_both_reports_and_original_tree_before_store(bound, monkeypatch):
    path, result = bridge(bound, sealed=True)
    value = runner(bound, path)
    observed = []
    monkeypatch.setattr(value, 'validate_public_readiness', lambda: observed.append('subset'))
    monkeypatch.setattr(value, 'validate_full_dev_readiness', lambda: observed.append('full'))
    monkeypatch.setattr(value, '_source', lambda _: bound.workspace)
    monkeypatch.setattr(value, '_materialize_into',
                        lambda task, source, target: Path(shutil.copytree(source, target)))
    saved = {}
    value.engine.settings = SimpleNamespace(local_actor='test-controller')
    value.store = SimpleNamespace(
        create_experiment=lambda identity, config, approval: saved.update(config=config,
                                                                         approval=approval),
        get_experiment=lambda identity: saved,
    )
    value.initialize()
    assert observed == ['subset', 'full']
    assert len(saved['config']['profiles']) == 24
    assert all(profile['split'] == 'dev' for profile in saved['config']['profiles'])
    assert saved['config']['full_dev_readiness']['sha256'] == result['full_dev_readiness'][1]
    assert saved['approval']['max_cost_micros'] == 12000000
    value._validate_frozen_execution()
    assert observed == ['subset', 'full', 'subset', 'full']
    # Replacing a valid pinned reference in memory cannot silently rebind the candidate.
    value.full_dev_readiness_ref = value.public_readiness_ref
    with pytest.raises(DomainError) as caught:
        value._validate_frozen_execution()
    assert caught.value.code == 'H12_FROZEN_EXECUTION_CHANGED'


def test_cli_readiness_uses_no_settings_or_engine(monkeypatch, tmp_path, capsys):
    path = Path(__file__).resolve().parents[1] / 'scripts/h12_eval.py'
    spec = importlib.util.spec_from_file_location('test_h12_public_dev_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        pytest.fail('Preparation must not initialize Engine, read settings or obtain a key')
    monkeypatch.setattr(module, 'load_settings', forbidden)
    monkeypatch.setattr(module, 'Engine', forbidden)
    def prepare(*args, **kwargs):
        assert kwargs['api_key'] is None and kwargs['execute_live'] is False
        assert kwargs['public_development'] is True and args[0].harness is None
        return SimpleNamespace(prepare_full_dev_readiness=lambda _: {'status': 'NOT_RUN'})
    monkeypatch.setattr('mgar_v3.quality_eval.h12_v2_runner.BenchmarkV2Runner', prepare)
    monkeypatch.setattr('sys.argv', [
        'h12_eval.py', '--public-dev-freeze', str(tmp_path / 'binding'),
        '--public-dev-freeze-sha256', 'a' * 64,
        '--prepare-full-dev-readiness', '--out', str(tmp_path / 'report'),
    ])
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)['paid_calls'] == 0


def test_relocated_root_local_image_map_preserves_exact_original_lock_bytes(bound):
    relocated = bound.root / 'relocated/lock.json'
    relocated.parent.mkdir()
    relocated.write_bytes(bound.lock.read_bytes())
    image_map = put(bound.root / 'relocated/images.json', {
        'example/public@' + 'a' * 40: str(relocated),
    })
    common = {
        'benchmark': (bound.anchor, sha(bound.anchor.read_bytes())),
        'candidate_source_sha256': 'c' * 64, 'public_plan_refs': bound.plans,
        'public_probe_root': bound.asset_root, 'dev_image_map': image_map,
    }
    path = bound.root / 'frozen/relocated.json'
    frozen = write_public_development_freeze(bound.root, path, **common)
    assert frozen['assets']['dev_image_map'] == image_map
    value = runner(bound, path)
    assert value._lock_for(bound.tasks[0]) == str(relocated)
    assert sha(relocated.read_bytes()) == sha(bound.lock.read_bytes())
    relocated.write_bytes(b'a different image environment')
    with pytest.raises(CatalogError, match='original exact lock byte'):
        write_public_development_freeze(bound.root, bound.root / 'frozen/unsafe.json', **common)
    assert not (bound.root / 'frozen/unsafe.json').exists()


def test_legacy_v2_keeps_original_loader_and_config_representation(bound, monkeypatch):
    path, prepared = bridge(bound)
    held_registry = bound.root / 'private/synthetic-sealed-registry.json'
    held_registry.write_bytes(b'synthetic opaque legacy registry')
    legacy = {
        **prepared, 'assets': {**prepared['assets'], 'holdout_judges': held_registry,
                              'holdout_image_map': prepared['assets']['dev_image_map']},
        'tasks': {**prepared['tasks'], 'holdout': []},
    }
    seen = []
    def old_loader(*args):
        seen.append('legacy')
        return legacy
    def forbidden(*args):
        pytest.fail('A historic freeze must not implicitly enable public development plans')
    monkeypatch.setattr('mgar_v3.quality_eval.h12_v2_runner.load_frozen_benchmark', old_loader)
    monkeypatch.setattr('mgar_v3.quality_eval.h12_v2_runner.load_public_development_benchmark',
                        forbidden)
    value = BenchmarkV2Runner(
        SimpleNamespace(harness=None), experiment_id='historic-synthetic',
        evidence=bound.root / 'legacy-evidence', freeze_path=path,
        freeze_sha256=sha(path.read_bytes()), api_key=None, root=bound.root,
    )
    assert seen == ['legacy'] and value.public_development is False
    assert value.public_plan_refs is None and value.public_readiness_ref is None
    assert value.full_dev_readiness_ref is None
    config = value._config()
    assert config['environment_revision'] == 'h12-benchmark-v2'
    assert set(config['image_locks']) == {'dev', 'holdout'}
    assert not {'public_plans', 'public_readiness', 'full_dev_readiness',
                'public_development', 'profiles_sha256'}.intersection(config)
    assert all('verification_protocol' not in profile for profile in config['profiles'])


def test_cli_real_constructor_keeps_readiness_output_uncreated(bound, monkeypatch, capsys):
    binding, _ = bridge(bound)
    script = Path(__file__).resolve().parents[1] / 'scripts/h12_eval.py'
    spec = importlib.util.spec_from_file_location('test_h12_cli_readiness_directory', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        pytest.fail('Readiness must use no Engine or database settings')
    monkeypatch.setattr(module, 'load_settings', forbidden)
    monkeypatch.setattr(module, 'Engine', forbidden)
    def construct(*args, **kwargs):
        return BenchmarkV2Runner(*args, **kwargs, root=bound.root)
    monkeypatch.setattr('mgar_v3.quality_eval.h12_v2_runner.BenchmarkV2Runner', construct)
    output = bound.root / 'readiness-result'
    def prepare(self, target):
        assert self.store is None
        assert self.evidence != target and self.evidence.is_dir()
        assert target == output and not target.exists()
        target.mkdir(parents=True, exist_ok=False)
        return {'status': 'NOT_RUN'}
    monkeypatch.setattr(BenchmarkV2Runner, 'prepare_full_dev_readiness', prepare)
    monkeypatch.setattr('sys.argv', [
        'h12_eval.py', '--public-dev-freeze', str(binding),
        '--public-dev-freeze-sha256', sha(binding.read_bytes()),
        '--prepare-full-dev-readiness', '--out', str(output),
    ])
    assert module.main() == 1
    assert output.is_dir() and json.loads(capsys.readouterr().out)['paid_calls'] == 0


def test_post_constructor_registry_change_cannot_rebind_independent_scoring(bound):
    path, frozen = bridge(bound)
    value = runner(bound, path)
    replacement = value._tasks('dev')[-1]
    registry, expected = value._registry_for(replacement)
    assert registry == frozen['assets']['dev_judges']
    assert expected == frozen['document']['dev_judges']['sha256']
    registry.write_bytes(b'changed after candidate was prepared')
    with pytest.raises(DomainError) as caught:
        value._registry_for(replacement)
    assert caught.value.code == 'H12_FROZEN_EXECUTION_CHANGED'


def test_public_runtime_policy_binds_scratch_without_changing_legacy_or_budgets(bound, monkeypatch):
    path, frozen = bridge(bound)
    value = runner(bound, path)
    assert frozen['document']['public_runtime_policy'] == {
        'revision': 'h12-public-runtime-v1', 'scratch_mb': 256,
        'readonly_pytest_cache': '/scratch/pytest-cache',
    } == PUBLIC_RUNTIME_POLICY
    for task in value._tasks('dev'):
        current = value._profile_for(task, 'candidate')
        legacy = profile_for(task, 'other-identity', 'candidate',
                             environment_revision=DEV_ENVIRONMENT_V4)
        assert current.sandbox_scratch_mb == 256 and legacy.sandbox_scratch_mb == 64
        assert 'sandbox_scratch_mb' not in legacy.model_dump(mode='json')
        assert current.model_dump(mode='json')['sandbox_scratch_mb'] == 256
        assert current.resources == legacy.resources and current.resources.max_model_calls == 20
    monkeypatch.setattr(value, '_source', lambda _: bound.workspace)
    monkeypatch.setattr(value, '_materialize_into',
                        lambda task, source, target: Path(shutil.copytree(source, target)))
    config = value._config()
    assert config['public_runtime_policy'] == PUBLIC_RUNTIME_POLICY
    assert all(row['sandbox_scratch_mb'] == 256 for row in config['profiles'])
    original = path.read_bytes()
    for key, replacement in [('scratch_mb', 64), ('revision', 'another-runtime'),
                             ('readonly_pytest_cache', '/tmp/another-cache')]:
        changed = json.loads(original)
        changed['public_runtime_policy'][key] = replacement
        path.write_text(json.dumps(changed))
        with pytest.raises(CatalogError):
            runner(bound, path, live=True)
    path.write_bytes(original)


def test_public_candidate_keeps_build_and_tests_separate_and_rejects_profile_drift(
    bound, monkeypatch,
):
    feature = copy.deepcopy(bound.tasks[1])
    feature.update({
        'type': 'feature', 'composition': 'feature', 'language': 'typescript',
        'scope': ['source.ts'],
        'environment': {**feature['environment'],
                        'verify': ['npx tsc --noEmit', 'npm test']},
        'acceptance': {**feature['acceptance'], 'regression': ['npm test']},
    })
    bound.tasks[1] = feature
    # Synthetic revised and original catalogs retain eleven identical cases.
    # Only the declared command classification changes; no code solution is supplied.
    anchor = json.loads(bound.anchor.read_bytes())
    dev = bound.root / anchor['dev_catalog']['path']
    dev.write_text(''.join(json.dumps(row) + '\n' for row in bound.tasks))
    old = bound.root / 'experiments/harness-v4/tasks/dev.jsonl'
    rows = [json.loads(line) for line in old.read_text().splitlines()]
    rows[1] = feature
    old.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    proposal_path = bound.root / anchor['proposal']['path']
    proposal = json.loads(proposal_path.read_bytes())
    proposal['dev_catalog_sha256'] = sha(dev.read_bytes())
    put(proposal_path, proposal)
    anchor['dev_catalog']['sha256'] = sha(dev.read_bytes())
    anchor['proposal']['sha256'] = sha(proposal_path.read_bytes())
    put(bound.anchor, anchor)

    path, _ = bridge(bound)
    value = runner(bound, path)
    legacy = profile_for(feature, 'original-identity', 'candidate',
                         environment_revision=DEV_ENVIRONMENT_V4)
    current = value._profile_for(feature, 'candidate')
    assert legacy.public_test_argv == ('node', '/node_modules/typescript/bin/tsc', '--noEmit')
    assert legacy.public_build_argv == ()
    assert 'public_build_argv' not in legacy.model_dump(mode='json')
    assert current.public_test_argv == ('npm', 'test')
    assert current.public_build_argv == legacy.public_test_argv
    assert current.resources == legacy.resources and current.resources.max_model_calls == 20
    monkeypatch.setattr(value, '_source', lambda _: bound.workspace)
    monkeypatch.setattr(value, '_materialize_into',
                        lambda task, source, target: Path(shutil.copytree(source, target)))
    config = value._config()
    selected = [row for row in config['profiles'] if row['task_id'] == feature['task_id']]
    assert len(selected) == 2
    assert all(row['public_build_argv'] == list(legacy.public_test_argv) for row in selected)
    assert all(row['public_test_argv'] == ['npm', 'test'] for row in selected)

    original_profile_for = value._profile_for
    def changed_profile(task, arm):
        result = original_profile_for(task, arm)
        return result.model_copy(update={'public_build_argv': ()}) if (
            task['task_id'] == feature['task_id']
        ) else result
    monkeypatch.setattr(value, '_profile_for', changed_profile)
    with pytest.raises(CatalogError, match='profiles or source bindings changed'):
        value._config()
    document = json.loads(path.read_bytes())
    document['profiles_sha256'] = 'd' * 64
    path.write_text(json.dumps(document))
    with pytest.raises(CatalogError, match='profiles or behavior assets changed'):
        runner(bound, path, live=True)


def test_public_refactor_uses_declared_full_regression_and_matching_profile_metadata():
    from mgar_v3.quality_eval.h12_full_dev_readiness import command_plan
    from mgar_v3.quality_eval.h12_runner import ARMS
    from mgar_v3.quality_eval.harness_v4 import load_jsonl

    root = Path(__file__).resolve().parents[1]
    tasks = load_jsonl(root / 'experiments/harness-v4/benchmark-v2/dev.jsonl')
    public_root = root / 'experiments/harness-v4/public-plans-v1'
    entries = json.loads((public_root / 'public-plan-map.json').read_bytes())
    references = {key: (root / row['path'], row['sha256']) for key, row in entries.items()}
    # No controller constructor, Engine, source materialization or command execution.
    value = object.__new__(BenchmarkV2Runner)
    value.public_development = True
    value.experiment_id = 'public-refactor-metadata-check'
    value.public_plan_refs, value.public_probe_root = references, public_root / 'assets'
    selected = []
    for task in tasks:
        if task['type'] != 'refactor':
            continue
        original = profile_for(task, value.experiment_id, 'candidate',
                               environment_revision=DEV_ENVIRONMENT_V4)
        declared = profile_for(
            {**task, 'environment': {**task['environment'], 'verify': []}},
            value.experiment_id, 'candidate', environment_revision=DEV_ENVIRONMENT_V4,
        )
        current = value._profile_for(task, 'candidate')
        assert original.public_test_argv[-1] == 'tests/test_utils.py'
        assert declared.public_test_argv == ('python3', '-B', '-m', 'pytest', 'tests/')
        assert current.public_test_argv == declared.public_test_argv
        assert current.resources == original.resources
        assert current.resources.max_model_calls == original.resources.max_model_calls
        commands = [row for row in command_plan(task, current, DEV_ENVIRONMENT_V4)
                    if row['kind'] in {'tests', 'build'}]
        from mgar_v3.quality_eval.h12_judge import public_regression_commands

        assert commands == [
            {'role': 'public_01', 'kind': 'tests', 'argv': list(declared.public_test_argv)},
            {'role': 'verifier_public_02', 'kind': 'tests',
             'argv': public_regression_commands(task, DEV_ENVIRONMENT_V4)[0]},
        ]
        selected.append(task['task_id'])
    assert set(selected) == {
        'hv4-dev-click-style-refactor', 'hv4-dev-requests-encoding-refactor',
    }
    _, expected_hash = development_protocol_metadata(tasks, references, public_root / 'assets')
    actual = [value._profile_for(task, arm).model_dump(mode='json', exclude={'experiment_id'})
              for task in tasks for arm in ARMS]
    assert len(actual) == 24 and canonical_hash(actual) == expected_hash
