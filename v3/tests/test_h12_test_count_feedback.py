"""Test feedback must distinguish successful assertions from skipped outcomes."""

import pytest

from mgar_v3.harness.project_commands import parse_tests


@pytest.mark.parametrize(
    "kind,output,exit_code,expected,detail_parts",
    [
        ("python", "=== 3 passed, 2 skipped, 1 xfailed in 0.2s ===", 0,
         ("passed", 6, 0, 3), ("3 tests passed", "2 skipped", "1 xfailed")),
        ("python", "=== 2 xfailed in 0.2s ===", 0,
         ("no_tests", 2, 0, 2), ("expected",)),
        ("python", "=== 2 skipped, 1 xfailed in 0.2s ===", 0,
         ("no_tests", 3, 0, 3), ("expected",)),
        ("python", "=== 2 failed, 3 passed, 1 skipped, 1 xfailed in 0.2s ===", 1,
         ("failed", 7, 2, 2), ("3 tests passed", "2 failures/errors", "1 xfailed")),
        ("python", "=== 1 xpassed in 0.2s ===", 0,
         ("passed", 1, 0, 0), ("0 tests passed", "1 xpassed")),
        ("python", "Ran 3 tests in 0.01s\n\nOK (skipped=1)\n", 0,
         ("passed", 3, 0, 1), ("2 tests passed", "1 skipped")),
        ("go", "--- PASS: TestA (0.0s)\n--- SKIP: TestB (0.0s)\nPASS\n", 0,
         ("passed", 2, 0, 1), ("1 tests passed", "1 skipped")),
        ("python", "=== 2 passed in 0.1s ===\n=== 3 passed in 0.2s ===", 0,
         ("passed", 3, 0, 0), ("3 tests passed",)),
    ],
    ids=["mixed-outcomes", "xfail-only", "no-successful-assertions", "failed-mixed",
         "unexpected-pass", "unittest-skip", "go-skip", "last-summary-only"],
)
def test_feedback_reports_real_pass_count(kind, output, exit_code, expected, detail_parts):
    result = parse_tests(kind, output, exit_code)
    assert result[:4] == expected
    assert all(part in result[4] for part in detail_parts), result[4]
