"""Measure actual final-tree regressions, independently of candidate completion."""

import copy

import pytest
from test_harness_v4_h12_runner import _runner

from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_regression_metrics import regression_metrics
from mgar_v3.quality_eval.h12_runner import profile_for

ARGV = ['python3', '-B', '-m', 'pytest', 'tests/']


def receipt(tree, failed, **changes):
    return dict(receipt_id='c' * 64, operation_id='task:tests', purpose='test',
                status='failed' if failed else 'passed', argv=ARGV, cwd='.',
                exit_code=1 if failed else 0, stdout='', stderr='', duration_seconds=1,
                tree_hash=tree, log_sha256='d' * 64, environment_hash='e' * 64,
                tests_run=617, tests_failed=failed) | changes


def outcome():
    return {'status': 'budget_exhausted', 'tree_before': 'a' * 64, 'tree_after': 'b' * 64,
            'receipts': [receipt('a' * 64, 0), receipt('b' * 64, 4)]}


@pytest.mark.parametrize('status', ['completed', 'budget_exhausted', 'blocked', 'incomplete'])
def test_four_regressions_are_counted_for_every_terminal_state(status):
    result = outcome() | {'status': status}
    measured = regression_metrics(result, ARGV)
    assert measured['new_regression_failures'] == 4
    assert measured['regression_evidence_known']


@pytest.mark.parametrize('change', [
    {'tree_hash': 'f' * 64}, {'argv': ['python3', 'another_test.py']},
    {'purpose': 'build'}, {'cwd': 'other'}, {'tests_run': 0, 'tests_failed': 0},
    {'status': 'error', 'exit_code': 2}, {'status': 'passed'},
])
def test_missing_or_unverified_final_regression_never_becomes_zero(change):
    result = outcome()
    result['receipts'][-1].update(change)
    measured = regression_metrics(result, ARGV)
    assert measured['new_regression_failures'] is None
    assert not measured['regression_evidence_known']


def test_latest_failure_or_error_cannot_reuse_earlier_green_receipt():
    result = outcome()
    result['receipts'].insert(1, receipt('b' * 64, 0))
    assert regression_metrics(result, ARGV)['new_regression_failures'] == 4
    result['receipts'].append(receipt('b' * 64, 0, status='error', exit_code=2))
    assert regression_metrics(result, ARGV)['new_regression_failures'] is None


def test_baseline_must_be_green_before_regression_can_be_attributed():
    result = outcome()
    result['receipts'][0] = receipt('a' * 64, 4)
    assert regression_metrics(result, ARGV)['new_regression_failures'] is None


@pytest.mark.parametrize('final_names,expected', [
    (['test_existing'], 0), (['test_new'], 1), (['test_existing', 'test_new'], 1),
])
def test_red_baseline_compares_failure_identities_not_counts(final_names, expected):
    result = outcome()
    result['receipts'] = [
        receipt('a' * 64, 1, stdout='FAILED tests/test_api.py::test_existing - AssertionError\n'),
        receipt('b' * 64, len(final_names), stdout=''.join(
            f'FAILED tests/test_api.py::{name} - AssertionError\n' for name in final_names)),
    ]
    measured = regression_metrics(result, ARGV)
    assert measured['new_regression_failures'] == expected
    assert measured['regression_evidence_known']


@pytest.mark.parametrize('change', [
    {'stdout': 'FAILED tests/test_api.py::test_existing - AssertionError\n', 'tests_failed': 2},
    {'stdout': 'FAILED tests/test_api.py::test_existing - AssertionError\n' * 2,
     'tests_failed': 2},
    {'tests_run': 616},
])
def test_red_comparison_rejects_partial_duplicate_or_changed_coverage(change):
    result = outcome()
    log = 'FAILED tests/test_api.py::test_existing - AssertionError\n'
    result['receipts'] = [receipt('a' * 64, 1, stdout=log),
                          receipt('b' * 64, 1, stdout=log) | change]
    assert regression_metrics(result, ARGV)['new_regression_failures'] is None


def test_reproduced_bug_fixed_to_green_has_zero_new_regressions():
    result = outcome()
    result['receipts'] = [receipt('a' * 64, 1), receipt('b' * 64, 0)]
    measured = regression_metrics(result, ARGV)
    assert measured['new_regression_failures'] == 0 and measured['regression_evidence_known']


def test_final_green_proves_zero_failures_after_feature_compile_baseline():
    result = outcome()
    result['receipts'] = [receipt('a' * 64, 0, status='error', tests_run=0),
                          receipt('b' * 64, 0)]
    measured = regression_metrics(result, ARGV)
    assert measured['new_regression_failures'] == 0 and measured['regression_evidence_known']
    assert measured['regression_baseline_receipt'] is None
    assert measured['regression_baseline_verified'] is False


def test_scoring_integration_does_not_gate_counts_on_completion(tmp_path):
    runner = _runner(tmp_path)
    runner.store.get_experiment = lambda _: {'usage': {'unknown_calls': 0}, 'reserved_micros': 0}
    task = h12.load_frozen_tasks('dev')[0]
    profile = profile_for(task, 'synthetic', 'candidate').model_copy(
        update={'public_test_argv': tuple(ARGV)})
    result = outcome() | {'cost_micros': 1, 'model_calls': 1}
    scored = runner._score_payload(task, profile, result)
    assert scored['status'] == 'FAIL'
    assert scored['model_phase_new_regression_failures'] == 4
    assert scored['new_regression_failures'] is None
    assert not scored['regression_evidence_known']


def test_unknown_regressions_prevent_quality_gate_and_preserve_raw_score():
    tasks = h12.load_frozen_tasks('dev')
    records = [{'task_id': t['task_id'], 'type': t['type'], 'split': 'dev',
                'status': 'PASS', 'raw_runtime_status': 'completed', 'judge_status': 'PASS',
                'final_tree': 'a' * 64, 'judge_tree': 'a' * 64, 'cost_micros': 0}
               for t in tasks]
    records[0].update(regression_evidence_known=False, new_regression_failures=None)
    original = copy.deepcopy(records)
    summary = h12.score_records(records, expected_tasks=tasks)
    assert not summary['threshold_met'] and summary['pass_count'] == 11
    assert summary['unverified_regression_tasks'] == [records[0]['task_id']]
    assert records == original


def test_no_regression_command_is_explicitly_not_applicable():
    assert regression_metrics({}, []) == {'new_regression_failures': 0,
        'regression_evidence_known': True, 'regression_evidence_status': 'not_applicable'}
