"""Preserve old diagnostics without upgrading summary-only logs to R05 proof."""

import copy
import hashlib
import json

import pytest

from mgar_v3.quality_eval.h12_independent_regression import independent_regression_metrics


def fixture(tmp_path, before="2 passed in 0.01s\n", after=None):
    if after is None:
        after = (
            "FAILED tests/test_imports.py::test_light_imports - AssertionError\n"
            "1 failed, 1 passed in 0.01s\n"
        )
    argv = ["python3", "-B", "-m", "pytest", "tests/"]
    task = {"task_id": "fixture", "judge_sha256": "b" * 64, "language": "python"}
    for phase, relative, output, tree in (
        ("environment", "environment-private", before, "a" * 64),
        ("final", "judge-private/public-regressions", after, "c" * 64),
    ):
        root = tmp_path / relative
        root.mkdir(parents=True)
        log = root / "command-0.log"
        log.write_text(output)
        failed = "FAILED " in output or "build failed" in output
        receipt = {
            "operation_id": phase,
            "tree_hash": tree,
            "argv": argv,
            "cwd": ".",
            "complete": True,
            "log_truncated": False,
            "private_log": str(log),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "log_bytes": len(log.read_bytes()),
            "exit_code": 1 if failed else 0,
            "state": "failed" if failed else "exited",
        }
        report = {
            "task_id": "fixture",
            "judge_sha256": "b" * 64,
            "tree_hash": tree,
            "phase": phase,
            "status": "FAIL" if failed else "PASS",
            "receipts": [receipt],
        }
        path = (
            tmp_path / "environment-private/judge-result.json"
            if phase == "environment"
            else tmp_path / "judge-private/judge-result.json"
        )
        path.write_text(json.dumps(report))
    result = {
        "tree_before": "a" * 64,
        "tree_after": "c" * 64,
        "independent_gate": {"status": "FAIL"},
    }
    return task, result


def test_nested_public_failure_overrides_green_target_command(tmp_path):
    task, result = fixture(tmp_path)
    value = independent_regression_metrics(tmp_path, task, result)
    assert value["new_regression_failures"] is None
    assert not value["regression_evidence_known"]
    assert value["legacy_observed_final_failures"] == ["tests/test_imports.py::test_light_imports"]
    assert value["legacy_diagnostic_only"]
    assert value["regression_evidence_scope"] == "independent_full_public"


def test_same_failure_count_does_not_hide_a_new_failure(tmp_path):
    task, result = fixture(
        tmp_path,
        before=(
            "FAILED tests/test_old.py::test_old - AssertionError\n1 failed, 1 passed in 0.01s\n"
        ),
    )
    value = independent_regression_metrics(tmp_path, task, result)
    assert value["new_regression_failures"] is None
    assert not value["regression_evidence_known"]
    assert value["legacy_observed_final_failures"] == ["tests/test_imports.py::test_light_imports"]


@pytest.mark.parametrize(
    "variant", ["final-tree", "log-hash", "truncated", "coverage", "foreign-log"]
)
def test_mismatched_or_incomplete_evidence_cannot_claim_zero(tmp_path, variant):
    before = (
        "FAILED tests/test_old.py::test_old - AssertionError\n1 failed, 1 passed in 0.01s\n"
        if variant == "coverage"
        else "2 passed in 0.01s\n"
    )
    task, result = fixture(tmp_path, before=before)
    path = tmp_path / "judge-private/judge-result.json"
    report = json.loads(path.read_bytes())
    if variant == "final-tree":
        report["tree_hash"] = "d" * 64
    elif variant == "log-hash":
        report["receipts"][0]["log_sha256"] = "0" * 64
    elif variant == "truncated":
        report["receipts"][0]["log_truncated"] = True
    elif variant == "foreign-log":
        report.update(status="PASS")
        report["receipts"][0]["private_log"] = str(tmp_path.parent / "foreign.log")
    else:
        log = tmp_path / "judge-private/public-regressions/command-0.log"
        log.write_text(
            "FAILED tests/test_imports.py::test_light_imports\n1 failed, 2 passed in 0.01s\n"
        )
        receipt = report["receipts"][0]
        receipt.update(
            log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(), log_bytes=len(log.read_bytes())
        )
    path.write_text(json.dumps(report))
    value = independent_regression_metrics(tmp_path, task, result)
    assert value["new_regression_failures"] is None
    assert value["regression_evidence_known"] is False


def test_legacy_green_cannot_prove_zero_with_unparsed_baseline(tmp_path):
    task, result = fixture(
        tmp_path, before="FAIL fixture [build failed]\n", after="2 passed in 0.01s\n"
    )
    original = copy.deepcopy(result)
    value = independent_regression_metrics(tmp_path, task, result)
    assert value["new_regression_failures"] is None and not value["regression_evidence_known"]
    assert value["legacy_diagnostic_only"]
    assert result == original


@pytest.mark.parametrize("status", ["PASS", "FAIL", "NOT_RUN"])
def test_private_check_without_full_public_suite_cannot_reuse_target_zero(tmp_path, status):
    task, result = fixture(tmp_path)
    path = tmp_path / "judge-private/judge-result.json"
    report = json.loads(path.read_bytes())
    report.update(status=status, receipts=[])
    path.write_text(json.dumps(report))
    value = independent_regression_metrics(tmp_path, task, result)
    assert value["new_regression_failures"] is None
    assert value["regression_evidence_known"] is False
    assert value["regression_evidence_scope"] == "independent_full_public"
    assert value["independent_public_regression_status"] == "NOT_RUN"


@pytest.mark.parametrize("status", ["PASS", "FAIL", "NOT_RUN"])
def test_missing_report_for_recorded_independent_gate_cannot_claim_zero(tmp_path, status):
    task, result = fixture(tmp_path)
    (tmp_path / "judge-private/judge-result.json").unlink()
    result["independent_gate"] = {"status": status}
    value = independent_regression_metrics(tmp_path, task, result)
    assert value["new_regression_failures"] is None
    assert value["regression_evidence_known"] is False


def test_score_merge_clears_target_zero_when_private_check_stops_full_regression(tmp_path):
    from test_h12_regression_metrics import ARGV, receipt
    from test_harness_v4_h12_runner import _runner

    from mgar_v3.quality_eval import h12
    from mgar_v3.quality_eval.h12_regression_metrics import regression_metrics
    from mgar_v3.quality_eval.h12_runner import profile_for

    runner = _runner(tmp_path)
    runner.store.get_experiment = lambda _: {"usage": {"unknown_calls": 0}, "reserved_micros": 0}
    task = h12.load_frozen_tasks("dev")[0]
    profile = profile_for(task, "synthetic", "candidate").model_copy(
        update={"public_test_argv": tuple(ARGV)}
    )
    case = runner.evidence / profile.arm / task["split"] / task["task_id"]
    _, result = fixture(case)
    path = case / "judge-private/judge-result.json"
    report = json.loads(path.read_bytes())
    report.update(task_id=task["task_id"], judge_sha256=task["judge_sha256"], receipts=[])
    path.write_text(json.dumps(report))
    result.update(
        status="blocked", receipts=[receipt("a" * 64, 0), receipt("c" * 64, 0)],
        model_calls=1, cost_micros=1,
    )
    assert regression_metrics(result, ARGV)["new_regression_failures"] == 0
    scored = runner._score_payload(task, profile, result)
    assert scored["status"] == "FAIL"
    assert scored["new_regression_failures"] is None
    assert scored["regression_evidence_known"] is False
    assert scored["regression_evidence_scope"] == "independent_full_public"
