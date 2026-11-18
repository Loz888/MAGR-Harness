"""R07 public receipts: exact scope, bounded differences and stable signatures."""

import json
from types import SimpleNamespace

import pytest
from test_harness_v4_h10_tools import _fixture

from mgar_v3.harness.coding_loop import CodingLoop
from mgar_v3.harness.contracts import ExecutionReceipt, sha256_text
from mgar_v3.harness.public_feedback import (
    PREFIX,
    PublicFeedbackPolicy,
    failure_observation,
    observe_public_command,
    render_public_feedback,
)
from mgar_v3.harness.regression_types import RegressionCommand
from mgar_v3.harness.service import _test_summary
from mgar_v3.harness.test_results import parse_independent_test


def observe(
    tmp_path,
    output,
    *,
    argv=("python3", "-m", "pytest"),
    status="failed",
    exit_code=1,
    complete=True,
    purpose="test",
):
    _, context, _, _ = _fixture(tmp_path)
    receipt = ExecutionReceipt(
        receipt_id="a" * 64,
        operation_id=context.operation_id,
        purpose=purpose,
        status=status,
        argv=argv,
        cwd=".",
        exit_code=exit_code,
        stdout=output,
        stderr="",
        duration_seconds=0.1,
        tree_hash=context.workspace_tree,
        log_sha256=sha256_text(output),
        environment_hash="e" * 64,
    )
    feedback = observe_public_command(
        receipt, context, command_id="tests", artifact_hash=sha256_text(output), complete=complete
    )
    return receipt, feedback, context


@pytest.mark.parametrize(
    "output,expected",
    [
        (
            "E assert 6 == 5\nFAILED tests/test_x.py::test_value - AssertionError\n"
            "===== 1 failed in 0.1s =====\n",
            "tests/test_x.py::test_value",
        ),
        (
            "FAIL: test_value (test_x.Values.test_value)\nAssertionError: 6 != 5\n"
            "Ran 1 test in 0.1s\nFAILED (failures=1)\n",
            "test_value (test_x.Values.test_value)",
        ),
        (
            '{"Action":"output","Package":"example/value","Test":"TestValue",'
            '"Output":"value_test.go:8: expected 5 actual 6\\n"}\n'
            '{"Action":"fail","Package":"example/value","Test":"TestValue"}\n',
            "example/value::TestValue",
        ),
        (
            json.dumps(
                {
                    "failures": [
                        {
                            "file": "/workspace/test/value.js",
                            "fullTitle": "value matches",
                            "err": {"message": "assertion mismatch", "expected": 5, "actual": 6},
                        }
                    ],
                    "duration": 123,
                }
            ),
            "test/value.js::value matches",
        ),
        ("not ok 7 - value matches\n  expected: 5\n  actual: 6\n", "value matches"),
    ],
    ids=["pytest", "unittest", "go-json", "mocha-json", "node-tap"],
)
def test_public_reporters_expose_real_failure_identity_and_difference(output, expected):
    ids, excerpt = failure_observation(output)
    assert expected in ids and any(word in excerpt for word in ["assert", "Assertion", "expected"])


def test_feedback_is_bound_to_public_receipt_tree_log_and_contract(tmp_path):
    receipt, feedback, context = observe(
        tmp_path,
        "E assert 6 == 5\n"
        "FAILED tests/test_x.py::test_value - AssertionError\n=== 1 failed in 0.1s ===",
    )
    assert feedback.task_id == context.task.task_id and feedback.run_id == str(context.task.run_id)
    assert feedback.workspace_id == context.task.workspace_id
    assert feedback.tree_hash == receipt.tree_hash and feedback.log_sha256 == receipt.log_sha256
    assert feedback.receipt_id == receipt.receipt_id and feedback.tests_failed == 1
    assert feedback.category == "PUBLIC_ASSERTION" and feedback.outcome == "failed"
    assert render_public_feedback(feedback).startswith(PREFIX)


@pytest.mark.parametrize("change", ["truncated", "tree", "hash", "unknown", "no-tests"])
def test_incomplete_stale_unknown_or_empty_public_execution_cannot_claim_green(tmp_path, change):
    receipt, _, context = observe(
        tmp_path, "=== 1 passed in 0.1s ===", status="passed", exit_code=0
    )
    updates = {}
    if change == "tree":
        updates["tree_hash"] = "f" * 64
    if change == "hash":
        updates["log_sha256"] = "f" * 64
    if change == "unknown":
        updates["status"] = "timeout"
    if change == "no-tests":
        updates.update(stdout="no useful result", log_sha256=sha256_text("no useful result"))
    feedback = observe_public_command(
        receipt.model_copy(update=updates),
        context,
        command_id="tests",
        artifact_hash="a" * 64,
        complete=change != "truncated",
    )
    assert feedback.outcome == "unverified" and feedback.tests_run is None


def test_stable_failure_signature_ignores_receipt_uuid_address_and_runner_timing(tmp_path):
    text = (
        "E AssertionError: object at 0x123fff after 0.8 seconds\n"
        "FAILED tests/test_x.py::test_value - AssertionError\n=== 1 failed in 0.1s ==="
    )
    raw, first, context = observe(tmp_path, text)
    other = (
        text.replace("0x123fff", "0xabcdef")
        .replace("0.8 seconds", "22.3 seconds")
        .replace("0.1s", "2.9s")
    )
    changed = raw.model_copy(
        update={
            "receipt_id": "b" * 64,
            "duration_seconds": 99,
            "stdout": other,
            "log_sha256": sha256_text(other),
        }
    )
    second = observe_public_command(
        changed, context, command_id="tests", artifact_hash="c" * 64, complete=True
    )
    assert first.failure_signature == second.failure_signature
    assert CodingLoop._receipt_key(raw.model_copy(update={"public_feedback": first})) == (
        CodingLoop._receipt_key(changed.model_copy(update={"public_feedback": second}))
    )
    new = other.replace("test_value", "test_another").replace(
        "AssertionError: object", "AssertionError: changed"
    )
    third = observe_public_command(
        changed.model_copy(update={"stdout": new, "log_sha256": sha256_text(new)}),
        context,
        command_id="tests",
        artifact_hash="d" * 64,
        complete=True,
    )
    assert third.failure_signature != first.failure_signature


def test_failure_context_is_bounded_and_raw_log_identity_retained(tmp_path):
    text = "\n".join("E AssertionError: " + str(i) + "x" * 2000 for i in range(90))
    _, feedback, _ = observe(tmp_path, text + "\n=== 1 failed in 0.1s ===")
    assert len(feedback.assertion_excerpt) <= 4000
    assert feedback.artifact_hash == sha256_text(text + "\n=== 1 failed in 0.1s ===")


@pytest.mark.parametrize("value", [1, 7, 0, -1])
def test_stagnation_threshold_is_explicit_frozen_and_bounded(value):
    with pytest.raises(ValueError):
        PublicFeedbackPolicy(stagnant_batches=value)


def test_skips_keep_their_real_count_and_cannot_be_presented_as_complete_green(tmp_path):
    _, feedback, _ = observe(
        tmp_path, "=== 1 passed, 1 skipped in 0.1s ===", status="passed", exit_code=0
    )
    assert feedback.tests_run == 2 and feedback.tests_skipped == 1
    assert feedback.outcome == "unverified" and feedback.category == "UNVERIFIED"


def test_final_summary_cannot_display_old_tree_green_as_new_patch_evidence(tmp_path):
    raw, feedback, _ = observe(tmp_path, "=== 1 passed in 0.1s ===", status="passed", exit_code=0)
    receipt = raw.model_copy(update={"public_feedback": feedback})
    summary = _test_summary(SimpleNamespace(receipts=[receipt], tree_after="f" * 64))
    assert summary["status"] == "unverified" and summary["run"] is None


def test_very_long_or_many_failure_names_are_bounded_with_explicit_truncation(tmp_path):
    text = (
        "\n".join(
            "FAILED tests/test_x.py::test_" + str(i) + "x" * 1000 + " - AssertionError"
            for i in range(80)
        )
        + "\n=== 80 failed in 0.1s ==="
    )
    _, feedback, _ = observe(tmp_path, text)
    assert len(feedback.failure_ids) == 16 and all(len(i) <= 256 for i in feedback.failure_ids)
    assert feedback.failure_ids_truncated and len(feedback.assertion_excerpt) <= 4000


@pytest.mark.parametrize("variant", ["interleaved", "orphan", "unfinished", "wrong-count"])
def test_unittest_interleaved_output_keeps_identity_but_incomplete_outcomes_stay_blocked(variant):
    text = "test_x (test_public.Values.test_x) ... usage: parser\n"
    text += "unrecognized argument: --foreign\nERROR\nRan 1 test in 0.1s\nFAILED (errors=1)\n"
    if variant == "orphan":
        text = text.replace("test_x (test_public.Values.test_x) ... ", "")
    if variant == "unfinished":
        text = text.replace("\nERROR\n", "\n")
    if variant == "wrong-count":
        text = text.replace("Ran 1 test", "Ran 2 tests")
    command = RegressionCommand(
        suite_id="unit",
        public_argv=("python3", "-m", "unittest", "-v"),
        argv=("python3", "-m", "unittest", "-v"),
        framework="unittest",
    )
    observed = parse_independent_test(command, text, 1, complete=True)
    assert observed.status == ("FAIL" if variant == "interleaved" else "BLOCKED")
    if variant == "interleaved":
        assert observed.tests_run == observed.tests_failed == 1
        assert observed.failure_ids == ("test_x (test_public.Values.test_x)",)
