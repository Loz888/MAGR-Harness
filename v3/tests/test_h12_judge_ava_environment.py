"""Frozen runner options and infrastructure errors cannot change behavioral checks."""

import pytest
from test_harness_v4_h12_judge import _task

from mgar_v3.harness.diagnostics import failure_diagnosis
from mgar_v3.quality_eval import h12_judge as judge
from mgar_v3.quality_eval.harness_v4 import CatalogError

ENTRY = '/node_modules/ava/entrypoints/cli.mjs'
CONFIG = '--config=/opt/h12/ava-config.mjs'
REVISION = 'h12-dev-environment-v4'


def test_typescript_verifier_also_uses_the_immutable_dependency_entrypoint(tmp_path):
    bundle = judge.prepare_judge(_task('hv4-dev-got-header-value'), tmp_path / 'private')
    assert bundle['commands'][-1] == ['node', '/node_modules/typescript/bin/tsc', '--noEmit']


@pytest.mark.parametrize('prefix', [['node', ENTRY], ['node', '--import', 'tsx/esm', ENTRY]])
def test_direct_private_runner_retains_selectors_and_uses_frozen_settings(prefix):
    original = [*prefix, 'test/behavior.ts', '--match=*edge*']
    commands = [original, ['node', 'scripts/other.js', ENTRY], ['npm', 'test']]
    result = judge._environment_commands(commands, 'sindresorhus/got', REVISION)
    assert result[0] == [*prefix, CONFIG, 'test/behavior.ts', '--match=*edge*']
    assert result[1] == commands[1]  # An argument mentioning AVA is not its entrypoint.
    assert result[2] == ['npm', 'test', '--', CONFIG]
    assert commands[0] == original and CONFIG not in original
    assert judge._environment_commands(result, 'sindresorhus/got', REVISION) == result


@pytest.mark.parametrize('repository,revision', [
    ('example/other', REVISION), ('sindresorhus/got', 'h12-original'),
])
def test_old_or_unrelated_authority_does_not_acquire_new_runner_settings(repository, revision):
    commands = [['node', ENTRY, 'test/behavior.ts'], ['npm', 'test']]
    assert judge._environment_commands(commands, repository, revision) == commands


def test_private_explicit_configuration_cannot_be_silently_overridden():
    with pytest.raises(CatalogError, match='conflicts'):
        judge._environment_commands(
            [['node', ENTRY, '--config=custom.mjs', 'test/behavior.ts']],
            'sindresorhus/got', REVISION,
        )


CACHE_ERROR = (
    "  ✔ behavior\n  ✘ Internal error\n"
    "  Error: ENOENT: no such file or directory, mkdir '/workspace/node_modules/.cache/ava'\n"
    "      at Object.mkdirSync (node:fs:1386:26)\n"
    "      at Api._createCacheDir (file:///node_modules/ava/lib/api.js:338:6)\n"
    "  1 test passed\n"
)


def test_passing_assertion_followed_by_real_runner_error_is_not_a_behavioral_fail():
    receipt = {'argv': ['node', ENTRY], 'state': 'failed', 'exit_code': 1, 'complete': True}
    result = failure_diagnosis(receipt, CACHE_ERROR)
    assert result['cause'] == 'TEST_RUNNER_CACHE_UNAVAILABLE'
    assert result['certainty'] == 'observed'
    assert result['legacy_environment_reason'] == 'DEPENDENCY_OR_LOG_UNAVAILABLE'
    # Neither a successful exit nor a truncated log can establish this diagnosis.
    assert failure_diagnosis(receipt | {'state': 'exited', 'exit_code': 0}, CACHE_ERROR)[
        'cause'] == 'NONE'
    assert failure_diagnosis(receipt | {'complete': False}, CACHE_ERROR)[
        'cause'] == 'LOG_INCOMPLETE'


@pytest.mark.parametrize('output', [
    "AssertionError: expected \"Error: ENOENT: mkdir '/workspace/node_modules/.cache/ava'\"\n"
    "  1 test failed\n",
    "Error: ENOENT: mkdir '/workspace/application-output'\n"
    "    at handler (file:///workspace/server.js:10:1)\n",
    CACHE_ERROR.replace('at Api._createCacheDir', 'at application.handler'),
])
def test_quoted_messages_or_application_filesystem_errors_remain_unclassified(output):
    result = failure_diagnosis(
        {'argv': ['node', ENTRY], 'state': 'failed', 'exit_code': 1, 'complete': True}, output,
    )
    assert result['cause'] == 'TEST_ASSERTION_OR_COMMAND_FAILURE'
    assert result['legacy_environment_reason'] is None
