"""Frozen public tests across languages; target absence is not a missing toolchain."""

import hashlib
import json
from pathlib import Path

import pytest

from mgar_v3.quality_eval.h12_full_dev_readiness import _runtime_matches, execute_task, task_binding
from mgar_v3.quality_eval.h12_public_regression import (
    declared_assertion_failure,
    missing_public_go_target,
    public_test_argv,
)
from mgar_v3.quality_eval.h12_runner import HOLDOUT_INFRA_V4, profile_for
from mgar_v3.quality_eval.h12_v2_freeze import public_development_profile


@pytest.mark.parametrize('change,expected', [
    ('', True), ('foreign', False), ('unlisted', False), ('count', False), ('dependency', False),
])
def test_go_red_requires_actual_frozen_test_location(change, expected):
    output = ('=== RUN   TestPublic\n    public_test.go:8: expected declared behavior\n'
              '--- FAIL: TestPublic (0.00s)\nFAIL\nFAIL example/public 0.001s\n')
    if change == 'foreign':
        output = output.replace('public_test.go', 'foreign_test.go')
    if change == 'dependency':
        output += 'ImportError: missing tool dependency\n'
    assert declared_assertion_failure(
        output, 2 if change == 'count' else 1,
        [] if change == 'unlisted' else ['public_test.go'], language='go',
    ) is expected


@pytest.mark.parametrize('message,path,expected', [
    ('undefined: NewPublicAPI', 'public_test.go', True),
    ('value.NewAPI undefined (type *Value has no field or method NewAPI)', 'public_test.go', True),
    ('undefined: InternalBug', 'implementation.go', False),
    ('syntax error: unexpected token', 'public_test.go', False),
    ('cannot find package dependency', 'public_test.go', False),
])
def test_feature_compiler_baseline_is_limited_to_frozen_public_missing_symbols(
    message, path, expected
):
    text = f'# example/public\n./{path}:8:9: {message}\nFAIL example/public [build failed]\n'
    assert missing_public_go_target(text, ['public_test.go']) is expected
    assert not missing_public_go_target(text + '\nFAIL example/public [setup failed]\n',
                                        ['public_test.go'])


@pytest.mark.parametrize('path,summary,scopes,expected', [
    ('/workspace/pkg/utils.py', True, ['pkg/utils.py'], True),
    ('/workspace/pkg/utils.py', True, [], False),
    ('/usr/lib/python/dependency.py', True, ['pkg'], False),
    ('/workspace/foreign.py', True, ['pkg'], False),
    ('/workspace/pkg/../foreign.py', True, ['pkg'], False),
    ('/workspace/pkg/utils.py', False, ['pkg'], False),
])
def test_feature_missing_python_export_needs_existing_project_module_and_test_failure(
    path, summary, scopes, expected
):
    output = f"E   ImportError: cannot import name 'new_api' from 'pkg.utils' ({path})\n"
    if summary:
        output += 'FAILED tests/public.py::test_api - ImportError: cannot import name\n'
    assert declared_assertion_failure(output, 1, ['tests/public.py'],
                                      missing_export_scopes=scopes) is expected
    assert not declared_assertion_failure(
        output + '\nERROR collecting tests/public.py\n', 1, ['tests/public.py'],
        missing_export_scopes=scopes,
    )


def test_only_explicit_immutable_standalone_node_script_uses_real_test_runner():
    task = {'language': 'javascript', 'public_assets': [{'target': 'test/public.js'}]}
    assert public_test_argv(task, ['node', 'test/public.js']) == (
        'node', '--test', 'test/public.js')
    for argv in [('node', 'app.js'), ('node', '-e', '1'), ('node', 'test/public.js', '--argument'),
                 ('node', '--test', 'test/public.js')]:
        assert public_test_argv(task, argv) == argv


@pytest.mark.parametrize('revision,repo,observed,expected', [
    (HOLDOUT_INFRA_V4, 'expressjs/express', 'v18.10.0', True),
    ('h12-dev-environment-v3', 'expressjs/express', 'v18.10.0', False),
    (HOLDOUT_INFRA_V4, 'another/project', 'v18.10.0', False),
    (HOLDOUT_INFRA_V4, 'expressjs/express', 'v18.10.1', False),
])
def test_independent_node_override_stays_bound_to_exact_project_and_version(
    revision, repo, observed, expected
):
    binding = {'task': {'language': 'javascript', 'repository': repo},
               'image': {'versions': {'node_version': 'v18.10.0'}},
               'declared_runtime': 'node-20', 'environment_revision': revision}
    assert _runtime_matches(observed, binding, probe=False) is expected


@pytest.mark.parametrize('foreign,expected', [(False, 'READY'), (True, 'NOT_RUN')])
def test_real_node_file_runner_keeps_assertions_but_rejects_missing_dependencies(
    tmp_path, monkeypatch, foreign, expected
):
    root = Path(__file__).resolve().parents[1]
    images = json.loads((root / '.local/h12/public-dev-20261113/image-map.json').read_bytes())
    lock_path = Path(next(v for k, v in images.items() if k.startswith('expressjs/express@')))
    lock = json.loads(lock_path.read_bytes())
    workspace = tmp_path / 'source'
    (workspace / 'test').mkdir(parents=True)
    script = workspace / 'test/public.js'
    script.write_text("require('unavailable_public_dependency');\n" if foreign else
                      "require('node:assert').strictEqual(2, 3);\n")
    task = dict(task_id='node-public-format', split='holdout', type='feature',
                language='javascript', repository='example/public', base_commit='a' * 40,
                goal='Synthetic new behavior', scope=['app.js'],
                resources=dict(max_model_calls=20, max_tool_calls=80, max_cost_micros=500000,
                               deadline_seconds=60, max_context_tokens=32000, max_repair_rounds=6),
                environment=dict(runtime='node-' + lock['node_version'].split('.')[0][1:],
                                 verify=['node test/public.js']),
                acceptance=dict(regression=['node test/public.js']),
                public_assets=[{'target': 'test/public.js',
                                'sha256': hashlib.sha256(script.read_bytes()).hexdigest()}])
    profile = public_development_profile(
        task, profile_for(task, 'synthetic-format', 'candidate',
                          environment_revision=HOLDOUT_INFRA_V4), HOLDOUT_INFRA_V4,
    )
    binding = task_binding(task, profile, workspace, lock_path, HOLDOUT_INFRA_V4, None,
                           split='holdout')
    result = execute_task(workspace, binding=binding, public=None, evidence=tmp_path / 'proof')
    assert result['status'] == expected
    assert result['command_facts']['public_01']['tests_run'] == 1
    assert result['command_facts']['public_01']['tests_failed'] == 1
    from mgar_v3.quality_eval import h12_judge as judge

    monkeypatch.setattr(judge, 'prepare_judge', lambda *args, **kwargs: {
        'files': [], 'commands': judge.public_regression_commands(task, HOLDOUT_INFRA_V4),
        'baseline_oracle': False,
    })
    task['judge_sha256'] = 'a' * 64
    environment = judge.verify(
        workspace, task, lock_path, tmp_path / 'environment', phase='environment',
        environment_revision=HOLDOUT_INFRA_V4,
    )
    assert environment['status'] == ('NOT_RUN' if foreign else 'PASS'), environment
    if not foreign:
        assert environment['baseline_test_status'] == 'FAIL'
        # Original red is preparation evidence only, never final success.
        final = judge.verify(
            workspace, task, lock_path, tmp_path / 'final', sealed=True,
            frozen_tests=judge.test_hashes(workspace), environment_revision=HOLDOUT_INFRA_V4,
        )
        assert final['status'] == 'FAIL'
