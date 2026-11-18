"""H12 trusted-command corrections and real reporter feedback regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_harness_v4_h10_tools import _call, _fixture

from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.project_commands import infer_test_runner, parse_tests
from mgar_v3.harness.service import (
    CodingToolRegistry,
    WorkspaceBoundBroker,
    _resolve_test_command,
    _test_feedback,
    _test_status,
)


@pytest.mark.parametrize(
    ("argv", "output", "exit_code", "expected"),
    [
        (("node", "/node_modules/mocha/bin/mocha.js"),
         "\x1b[32m  1260 passing (9s)\x1b[0m\n  4 failing\n", 4,
         ("failed", 1264, 4, 0)),
        (("node", "/node_modules/mocha/bin/mocha.js"),
         "  2 passing (12ms)\n  1 pending\n", 0, ("passed", 3, 0, 1)),
        (("node", "/node_modules/ava/entrypoints/cli.mjs"),
         "  42 tests passed\n  2 tests failed\n  1 test skipped\n", 1,
         ("failed", 45, 2, 1)),
        (("node", "/node_modules/ava/entrypoints/cli.mjs"),
         "  ✔ request › local server\n  ✔ timeout › abort\n  1 test failed\n", 1,
         ("failed", 3, 1, 0)),
        (("node", "/node_modules/ava/entrypoints/cli.mjs"),
         "  ✔ request › server\n  ✔ [expected fail] legacy\n"
         "  1 test failed\n  1 known failure\n  6 tests skipped\n", 1,
         ("failed", 9, 1, 7)),
        (("node", "--test"), "# tests 3\n# pass 2\n# fail 1\n# skip 0\n", 1,
         ("failed", 3, 1, 0)),
        (("go", "test", "-v", "./..."),
         "? example/no [no test files]\n--- PASS: TestA (0.00s)\nPASS\n", 0,
         ("passed", 1, 0, 0)),
        (("python3", "-m", "pytest"),
         "===== 1 failed, 2 passed, 1 skipped in 1.0s =====\n", 1,
         ("failed", 4, 1, 1)),
        (("python3", "-m", "unittest"),
         "Ran 3 tests in 0.01s\n\nFAILED (failures=1, skipped=1)\n", 1,
         ("failed", 3, 1, 1)),
    ],
)
def test_service_uses_actual_runner_instead_of_business_task_kind(
    argv, output, exit_code, expected
):
    assert _test_status("bugfix", output, exit_code, argv=argv)[:4] == expected
    assert infer_test_runner(argv) != "unknown"


@pytest.mark.parametrize(
    ("kind", "output", "exit_code", "status"),
    [
        ("node", "  3 pending\n", 0, "no_tests"),
        ("node", "  Killed\n", 137, "unverified"),
        ("node", "  2 passing (1ms)\n", 1, "failed"),
        ("node", "# tests 3\n# pass 0\n# fail 0\n# skip 0\n", 0, "unverified"),
        ("node", "# tests 3\n# pass 0\n# fail 0\n# skipped 3\n", 0, "no_tests"),
        ("node", "# tests 3\n# pass 1\n# fail 0\n# cancelled 1\n# todo 1\n", 1,
         "failed"),
        ("python", "Ran 1 test in 0.01s\n", 0, "unverified"),
        ("python", "2 skipped in 0.01s", 0, "no_tests"),
        ("go", "ok example 0.01s", 0, "unverified"),
        ("go", "--- SKIP: TestA (0.00s)\n", 0, "no_tests"),
    ],
)
def test_exit_zero_or_missing_or_all_skipped_output_does_not_create_pass(
    kind, output, exit_code, status
):
    assert parse_tests(kind, output, exit_code)[0] == status


def test_summary_remains_visible_and_full_log_can_be_paged():
    text = "first assertion\n" + "x" * 20_000 + "\nlast assertion"
    feedback = _test_feedback("Mocha: 4 failures", text)
    assert feedback.startswith("Mocha: 4 failures\nfirst assertion")
    assert feedback.endswith("last assertion")
    assert "artifact_page" in feedback
    assert len(feedback) < 7200


@pytest.mark.parametrize(
    "arguments",
    [{"command_id": "diagnostic"}, {"argv": ["python3", "-c", "print(1)"]},
     {"command_id": "tests", "argv": ["python3"]}, {"argv": "python3"}],
)
def test_only_exact_command_id_or_legacy_trusted_argv_resolves(arguments):
    trusted = ("python3", "-B", "-m", "unittest", "test_log")
    assert _resolve_test_command(arguments, trusted) is None
    assert _resolve_test_command({"command_id": "tests"}, trusted) == trusted
    assert _resolve_test_command({"argv": list(trusted)}, trusted) == trusted


@pytest.mark.parametrize("bad", [{"cursor": 1130}, {"cursor": "0"}, {"offset": 20000}])
def test_invalid_cursor_returns_a_usable_page_without_weakening_scope(tmp_path, bad):
    scoped, context, authority, _ = _fixture(tmp_path)
    broker = WorkspaceBoundBroker(
        scoped.workspace, authority,
        lock_path=Path(__file__).resolve().parents[1] / ".local/r03/sandbox-lock.json",
    )
    session = CommandSession(broker)
    registry = CodingToolRegistry(scoped.workspace, broker, session, (), kind="inspect")
    registry.context_tools = scoped
    ref = scoped.archive("汉字\n" * 500, context, path="code.py")
    arguments = {"path": "code.py", "artifact_hash": ref["artifact_hash"]}
    try:
        result = registry.invoke(_call(**arguments, **bad), context, CancellationToken())
        assert result.status == "error" and result.data["correctable"], result
        following = registry.invoke(
            _call(**result.data["next_arguments"]), context, CancellationToken(),
        )
        assert following.status == "ok" and following.data["offset"] > 0
        foreign = registry.invoke(
            _call(path="code.py", artifact_hash="a" * 64, cursor="0"),
            context, CancellationToken(),
        )
        assert foreign.status == "denied"
        authority.revoke()
        revoked = registry.invoke(_call(**arguments, **bad), context, CancellationToken())
        assert revoked.status == "denied" and revoked.code == "HARNESS_LEASE_REVOKED"
    finally:
        session.close()


def test_real_command_denial_is_correctable_and_fixed_id_executes_once(tmp_path: Path):
    scoped, context, _, _ = _fixture(tmp_path)
    (scoped.workspace.root / "test_log.py").write_text(
        "import unittest\nclass Test(unittest.TestCase):\n"
        " def test_value(self): self.assertEqual(2 + 2, 4)\n"
    )
    ancestor = scoped.workspace.root
    while ancestor != Path("/tmp"):
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    lease = issue_lease(
        mode="modify_test", actor=context.task.actor,
        workspace_id=context.task.workspace_id, repository=context.task.repository,
        execution_epoch=context.task.execution_epoch,
        write_paths=("",), allowed_commands=("python3",),
        approved_by="owner", approval_ref="h12-command-correction",
    )
    broker = WorkspaceBoundBroker(
        scoped.workspace, lease,
        lock_path=Path(__file__).resolve().parents[1] / ".local/r03/sandbox-lock.json",
    )
    context = context.model_copy(update={
        "grant": context.grant.model_copy(update={
            "execute": True, "write_paths": ("",), "allowed_commands": ("python3",),
        }),
        "workspace_tree": scoped.workspace.snapshot().tree_hash,
    })
    session = CommandSession(broker)
    trusted = ("python3", "-B", "-m", "unittest", "test_log")
    registry = CodingToolRegistry(scoped.workspace, broker, session, trusted, kind="bugfix")
    try:
        rejected = registry.invoke(
            _call("run_tests", argv=["python3", "-c", "print('no')"]),
            context, CancellationToken(),
        )
        assert (rejected.status, rejected.code) == ("error", "HARNESS_COMMAND_DENIED")
        assert rejected.data["correctable"] and not rejected.data["executed"]
        assert registry.active_commands() == ()
        result = registry.invoke(
            _call("run_tests", **rejected.data["retry_arguments"]),
            context.model_copy(update={"operation_id": "corrected-command"}),
            CancellationToken(),
        )
        assert result.status == "ok", result
        assert result.data["tests_run"] == 1
        assert result.data["test_runner"] == "unittest"
        assert registry.context_tools.artifacts.read(
            result.data["artifact"]["artifact_hash"]
        ).decode() == result.data["receipt"]["stdout"]
        broker.authority.revoke()
        revoked = registry.invoke(
            _call("run_tests", command_id="tests"), context, CancellationToken(),
        )
        assert revoked.status == "denied" and revoked.code == "HARNESS_LEASE_REVOKED"
    finally:
        session.close()
def test_success_summary_does_not_count_known_failures_as_passed() -> None:
    result = parse_tests("node", "746 tests passed\n1 known failure\n4 tests skipped\n", 0)
    assert result[:4] == ("passed", 751, 0, 5)
    assert result[4] == "AVA: 746 tests passed; 5 skipped/pending/known failures"
