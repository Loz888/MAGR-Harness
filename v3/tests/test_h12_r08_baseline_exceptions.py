"""Approved new-baseline exceptions preserve actual non-PASS identities."""

import hashlib
import json

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.regression_types import RegressionCommand, RegressionPlan
from mgar_v3.harness.test_results import parse_independent_test
from mgar_v3.quality_eval.h12_expected_failures import validate_expected_failures

PYTEST = """============================= test session starts ==============================
collected 4 items
tests/test_platform.py::test_local PASSED [ 25%]
tests/test_platform.py::test_windows SKIPPED (requires Windows) [ 50%]
tests/test_platform.py::test_legacy XFAIL [ 75%]
tests/test_platform.py::test_other PASSED [100%]
=================== 2 passed, 1 skipped, 1 xfailed in 0.12s =====================
"""
EXCEPTIONS = {
    "tests/test_platform.py::test_windows": "skipped",
    "tests/test_platform.py::test_legacy": "xfailed",
}


def command(exceptions=None):
    argv = ("python3", "-m", "pytest", "tests/", "-vv")
    return RegressionCommand(
        suite_id="full-public",
        public_argv=argv,
        argv=argv,
        framework="pytest",
        expected_exceptions=exceptions or {},
    )


def test_opt_in_terminal_inventory_keeps_skip_and_xfail_separate_from_passed():
    result = parse_independent_test(command(EXCEPTIONS), PYTEST, 0, complete=True)
    assert result.status == "PASS" and result.complete and result.parsed
    assert result.tests_run == 4 and result.tests_failed == 0
    assert result.tests_skipped == 1 and result.tests_xfailed == 1
    assert [c.outcome for c in result.cases] == ["passed", "skipped", "xfailed", "passed"]


def test_same_report_without_explicit_frozen_exceptions_still_blocks():
    result = parse_independent_test(command(), PYTEST, 0, complete=True)
    assert result.status == "BLOCKED" and not result.complete
    assert result.tests_run == 4 and result.tests_xfailed == 1


@pytest.mark.parametrize(
    "exceptions",
    [
        {"tests/test_platform.py::test_windows": "skipped"},
        {"tests/test_platform.py::test_legacy": "xfailed"},
        EXCEPTIONS | {"tests/test_platform.py::removed": "skipped"},
        EXCEPTIONS | {"tests/test_platform.py::test_windows": "xfailed"},
    ],
)
def test_new_changed_or_missing_exception_identity_blocks(exceptions):
    result = parse_independent_test(command(exceptions), PYTEST, 0, complete=True)
    assert result.status == "BLOCKED" and not result.complete


@pytest.mark.parametrize(
    "text",
    [
        PYTEST.replace("test_legacy XFAIL", "test_legacy XPASS").replace("1 xfailed", "1 xpassed"),
        PYTEST.replace("test_other PASSED", "test_other SKIPPED").replace(
            "2 passed, 1 skipped", "1 passed, 2 skipped"
        ),
        PYTEST.replace("test_other PASSED", "test_missing PASSED"),
        PYTEST.replace("collected 4 items", "collected 5 items"),
        PYTEST.replace("test_other PASSED [100%]\n", ""),
        PYTEST.replace("1 xfailed", "2 xfailed"),
    ],
)
def test_count_contradiction_xpass_added_skip_or_truncated_inventory_blocks(text):
    result = parse_independent_test(command(EXCEPTIONS), text, 0, complete=True)
    # Renaming an ordinary passing test cannot be detected by a reporter alone;
    # the original file hash is separately enforced by IndependentRegressionRunner.
    if "test_missing PASSED" in text:
        assert result.complete
    else:
        assert result.status == "BLOCKED" and not result.complete


def test_real_assertion_failure_remains_fail_with_valid_expected_exceptions():
    text = PYTEST.replace("test_other PASSED", "test_other FAILED").replace(
        "2 passed,", "1 failed, 1 passed,"
    )
    result = parse_independent_test(command(EXCEPTIONS), text, 1, complete=True)
    assert result.status == "FAIL" and result.complete and result.tests_failed == 1
    assert result.failure_ids == ("tests/test_platform.py::test_other",)


@pytest.mark.parametrize("complete,exit_code", [(False, 0), (True, 2), (True, None)])
def test_exceptions_do_not_pardon_unknown_execution_or_collection_errors(complete, exit_code):
    result = parse_independent_test(command(EXCEPTIONS), PYTEST, exit_code, complete=complete)
    assert result.status == "BLOCKED" and not result.complete


def test_legacy_command_serialization_does_not_add_exception_fields():
    assert "expected_exceptions" not in command().model_dump()
    assert command(EXCEPTIONS).model_dump()["expected_exceptions"] == EXCEPTIONS


def test_all_skipped_suite_cannot_be_a_successful_target():
    text = """collected 1 item
tests/test_a.py::test_one SKIPPED [100%]
==================== 1 skipped in 0.01s ====================
"""
    result = parse_independent_test(
        command({"tests/test_a.py::test_one": "skipped"}), text, 0, complete=True
    )
    assert result.status == "BLOCKED" and not result.complete


@pytest.mark.parametrize(
    "fragment",
    [
        "PASSED [100%]pytest-httpbin thread diagnostic",
        "pytest-httpbin server hit an exception serving request: SSL alert\nPASSED [100%]",
        "attempting to ignore so the rest of the tests can run\nPASSED [100%]",
    ],
)
def test_httpbin_thread_output_does_not_erase_a_real_terminal_identity(fragment):
    text = (
        "collected 1 item\ntests/test_ssl.py::test_certificate "
        + fragment
        + "\n==================== 1 passed in 0.01s ====================\n"
    )
    result = parse_independent_test(command(), text, 0, complete=True)
    assert result.complete and result.tests_run == 1
    assert result.cases[0].test_id == "tests/test_ssl.py::test_certificate"


def test_interrupted_httpbin_test_without_terminal_stays_blocked():
    text = (
        "collected 1 item\ntests/test_ssl.py::test_certificate "
        "pytest-httpbin server hit an exception serving request: SSL alert\n"
        "==================== 1 passed in 0.01s ====================\n"
    )
    assert not parse_independent_test(command(), text, 0, complete=True).complete


def test_pytest_wrapped_skip_reason_keeps_the_exact_known_test_identity():
    text = """collected 2 items
tests/test_platform.py::test_local PASSED [50%]
tests/test_platform.py::test_windows SKIPPED (Tests user-
facing behavior on a different platform.) [100%]
==================== 1 passed, 1 skipped in 0.01s ====================
"""
    result = parse_independent_test(
        command({"tests/test_platform.py::test_windows": "skipped"}), text, 0, complete=True
    )
    assert result.complete and result.tests_run == 2 and result.tests_skipped == 1


def test_ava_expected_failure_requires_original_source_and_does_not_mask_xpass(tmp_path):
    data = "test.failing('original limitation', t => { t.fail(); });\n"
    p = tmp_path / "test" / "arguments.ts"
    p.parent.mkdir()
    p.write_text(data)
    identity = "arguments › original limitation"
    argv = ("npm", "test", "--", "--tap")
    suite = RegressionCommand(
        suite_id="ava",
        public_argv=argv,
        argv=argv,
        framework="tap",
        expected_exceptions={identity: "xfailed"},
        expected_failure_sources={
            identity: {
                "path": "test/arguments.ts",
                "sha256": hashlib.sha256(data.encode()).hexdigest(),
                "line": 1,
                "title": "original limitation",
            }
        },
    )
    plan = RegressionPlan(commands=(suite,))
    validate_expected_failures(tmp_path, plan)
    text = "TAP version 13\nok 1 - arguments › original limitation\nok 2 - other\n1..2\n"
    result = parse_independent_test(suite, text, 0, complete=True)
    assert result.complete and result.tests_xfailed == 1
    assert result.cases[0].outcome == "xfailed" and result.cases[1].outcome == "passed"
    failure = parse_independent_test(suite, text.replace("ok 1", "not ok 1"), 1, complete=True)
    assert failure.status == "FAIL" and failure.tests_failed == 1
    p.write_text(data.replace("test.failing", "test"))
    with pytest.raises(DomainError, match="source differs"):
        validate_expected_failures(tmp_path, plan)


def test_explicit_mocha_duplicate_ordinals_keep_every_actual_observation():
    passed = {"file": "/workspace/test/a.js", "fullTitle": "same title", "duration": 1, "err": {}}
    failed = passed | {"duration": 2, "err": {"message": "actual assertion"}}
    raw = json.dumps(
        {
            "stats": {"tests": 2, "passes": 1, "failures": 1, "pending": 0},
            "tests": [passed, failed],
            "passes": [passed],
            "failures": [failed],
            "pending": [],
        }
    )
    argv = ("node", "/node_modules/mocha/bin/mocha.js", "--reporter", "json")
    strict = RegressionCommand(
        suite_id="mocha", public_argv=argv, argv=argv, framework="mocha-json"
    )
    explicit = strict.model_copy(update={"identity_policy": "mocha-file-title-occurrence-v1"})
    assert not parse_independent_test(strict, raw, 1, complete=True).complete
    result = parse_independent_test(explicit, raw, 1, complete=True)
    assert result.complete and result.tests_run == 2 and result.tests_failed == 1
    assert result.failure_ids == ("test/a.js::same title::occurrence-2",)
    altered = json.loads(raw)
    altered["tests"].pop()
    assert not parse_independent_test(explicit, json.dumps(altered), 1, complete=True).complete
