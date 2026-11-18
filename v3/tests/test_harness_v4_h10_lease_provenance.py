"""H10 F02: real command output keeps its generating authorization at release."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import CapabilityAuthority, CapabilityLease, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    HarnessToolCall,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
)
from mgar_v3.harness.service import CodingToolRegistry, WorkspaceBoundBroker
from mgar_v3.harness.workspace import WorkspaceRegistry

ROOT = Path(__file__).resolve().parents[1]
MARKER = "H10_PREVIOUS_SCOPE_ONLY"


@dataclass
class Runtime:
    tools: CodingToolRegistry
    session: CommandSession
    authority: CapabilityAuthority
    lease: CapabilityLease
    context: ToolContext
    argv: tuple[str, ...]


def _runtime(tmp_path: Path) -> Runtime:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "private.txt").write_text((MARKER + "\n") * 120, encoding="utf-8")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "h10-lease-" + uuid4().hex[:12])
    ancestor = workspace.root
    assert Path("/tmp") in ancestor.parents
    while ancestor != Path("/tmp"):
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    task = TaskBinding(
        task_id=workspace.manifest.task_id,
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository=registration.repository,
        base_commit=registration.base_commit,
        workspace_id=registration.workspace_id,
        acceptance_revision="h10-f02-v1",
        objective="Verify the temporary module",
        task_kind="bugfix",
        requirements=(VerificationRequirement(requirement_id="suite"),),
    )
    lease = issue_lease(
        mode="modify_test",
        actor=task.actor,
        workspace_id=task.workspace_id,
        repository=task.repository,
        execution_epoch=task.execution_epoch,
        read_paths=("",),
        write_paths=("module.py",),
        allowed_commands=("python3",),
        approved_by="owner",
        approval_ref="h10-f02-test",
        max_calls=1000,
    )
    authority = CapabilityAuthority(lease, budget_check=lambda: True)
    broker = WorkspaceBoundBroker(
        workspace, authority, lock_path=ROOT / ".local/r03/sandbox-lock.json"
    )
    session = CommandSession(broker, page_bytes=128)
    program = (
        "import sys, unittest\n"
        "sys.stderr = sys.stdout\n"
        "print(open('private.txt').read(), flush=True)\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self): self.assertTrue(True)\n"
        "unittest.main(argv=['h10-lease-provenance'])\n"
    )
    argv = ("python3", "-B", "-c", program)
    grant = CapabilityGrant(
        actor=task.actor,
        workspace_id=task.workspace_id,
        repository=task.repository,
        read_paths=("",),
        write_paths=("module.py",),
        execute=True,
        allowed_commands=("python3",),
    )
    context = ToolContext(
        task=task,
        grant=grant,
        operation_id="h10-f02-command",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    tools = CodingToolRegistry(workspace, broker, session, argv, kind="bugfix")
    return Runtime(tools, session, authority, lease, context, argv)


def _replacement(runtime: Runtime, *, retain_id: bool = False) -> CapabilityLease:
    replacement = issue_lease(
        mode="modify_test",
        actor=runtime.lease.actor,
        workspace_id=runtime.lease.workspace_id,
        repository=runtime.lease.repository,
        execution_epoch=runtime.lease.execution_epoch,
        read_paths=("",),
        write_paths=("module.py",),
        allowed_commands=("python3",),
        protected_paths=(*runtime.lease.protected_paths, "private.txt"),
        approved_by="owner",
        approval_ref="h10-f02-replacement",
        max_calls=1000,
    )
    if retain_id:
        replacement = replacement.model_copy(update={"lease_id": runtime.lease.lease_id})
        replacement = replacement.model_copy(update={"lease_hash": replacement.computed_hash()})
        assert replacement.lease_id == runtime.lease.lease_id
    assert replacement.lease_hash != runtime.lease.lease_hash
    return replacement


def _run_tests(runtime: Runtime):
    return runtime.tools.invoke(
        HarnessToolCall(
            call_id="real-command", name="run_tests", arguments={"argv": list(runtime.argv)}
        ),
        runtime.context,
        CancellationToken(),
    )


@pytest.mark.parametrize("boundary", ["wait", "poll", "read_log", "archive", "response"])
def test_replacement_at_log_release_boundaries_returns_no_old_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Every injected boundary follows the real fixed H04/H06 container run."""
    runtime = _runtime(tmp_path)
    replacement = _replacement(runtime)
    rotated = []

    def rotate() -> None:
        if not rotated:
            runtime.authority.replace(replacement)
            rotated.append(True)

    if boundary in {"wait", "poll", "read_log"}:
        original = getattr(runtime.session, boundary)

        def intercept(*args: Any, **kwargs: Any):
            value = original(*args, **kwargs)
            if boundary != "poll" or value.state == "exited":
                rotate()
            return value

        monkeypatch.setattr(runtime.session, boundary, intercept)
    elif boundary == "archive":
        original_archive = runtime.tools.context_tools.archive

        def archive_then_replace(*args: Any, **kwargs: Any):
            value = original_archive(*args, **kwargs)
            rotate()
            return value

        monkeypatch.setattr(runtime.tools.context_tools, "archive", archive_then_replace)
    else:
        from mgar_v3.harness import service

        original_result = service.HarnessToolResult

        def result_then_replace(*args: Any, **kwargs: Any):
            value = original_result(*args, **kwargs)
            if MARKER in value.content:
                rotate()
            return value

        monkeypatch.setattr(service, "HarnessToolResult", result_then_replace)
    try:
        result = _run_tests(runtime)
        assert rotated, "The real release boundary was not reached"
        assert result.status in {"denied", "error"}
        assert MARKER not in result.content
        assert MARKER not in str(result.data)
        assert "artifact" not in result.data
        for reference in runtime.tools.context_tools.references():
            assert reference["lease_hash"] == runtime.lease.lease_hash
            with pytest.raises(DomainError):
                runtime.tools.context_tools.page(
                    HarnessToolCall(
                        call_id="reread-old",
                        name="artifact_page",
                        arguments={
                            "path": reference["path"],
                            "artifact_hash": reference["artifact_hash"],
                        },
                    ),
                    runtime.context,
                )
    finally:
        runtime.session.close()


def test_finished_command_log_cannot_be_read_after_same_id_scope_replacement(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    try:
        handle = runtime.session.run(
            ExecutionRequest(
                operation_id="finished-log",
                argv=runtime.argv,
                execution_epoch=runtime.context.task.execution_epoch,
            ),
            runtime.context.grant,
            CancellationToken(),
        )
        assert runtime.session.wait(handle.handle_id, timeout=20).state == "exited"
        runtime.authority.replace(_replacement(runtime, retain_id=True))
        runtime.session.poll(handle.handle_id)
        with pytest.raises(DomainError):
            runtime.session.read_log(handle.handle_id)
    finally:
        runtime.session.close()


def test_revocation_after_log_collection_releases_no_old_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    original = runtime.session.read_log
    revoked = []

    def collect_then_revoke(*args: Any, **kwargs: Any):
        page = original(*args, **kwargs)
        runtime.authority.revoke()
        revoked.append(True)
        return page

    monkeypatch.setattr(runtime.session, "read_log", collect_then_revoke)
    try:
        result = _run_tests(runtime)
        assert revoked
        assert result.status == "denied"
        assert MARKER not in result.content and MARKER not in str(result.data)
        assert runtime.tools.context_tools.references() == ()
    finally:
        runtime.session.close()


def test_unmodified_generating_authority_is_preserved_through_receipt_and_cas(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    try:
        result = _run_tests(runtime)
        assert result.status == "ok", result
        assert MARKER in result.data["receipt"]["stdout"]
        provenance = result.data["receipt"]["provenance"]
        assert provenance["lease_hash"] == runtime.lease.lease_hash
        assert provenance["lease_id"] == runtime.lease.lease_id
        assert provenance["read_paths"] == [""]
        assert provenance["write_paths"] == ["module.py"]
        reference = result.data["artifact"]
        assert reference["provenance"] == provenance
        assert reference["lease_hash"] == runtime.lease.lease_hash
        page = runtime.tools.context_tools.page(
            HarnessToolCall(
                call_id="read-authorized",
                name="artifact_page",
                arguments={
                    "path": "",
                    "artifact_hash": reference["artifact_hash"],
                },
            ),
            runtime.context,
        )
        assert MARKER in page.content
    finally:
        runtime.session.close()


@pytest.mark.parametrize("change", ["replacement", "revocation"])
def test_baseline_adapter_cannot_publish_old_output_after_authority_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from mgar_v3.harness.adapters import InMemoryBudgetLedger, InMemoryEventSink, LocalVerifier
    from mgar_v3.harness.coding_loop import CodingLoop
    from mgar_v3.harness.long_context import LongContextManager
    from mgar_v3.harness.service import CodingWorkspace

    runtime = _runtime(tmp_path)
    original_execute = runtime.tools.broker.execute
    injected = []

    def execute_then_change(*args: Any, **kwargs: Any):
        receipt = original_execute(*args, **kwargs)
        # This adapter receives the real completed H04 command, with its own
        # immutable provenance. Only its return scheduling is controlled.
        assert MARKER in receipt.stdout
        assert receipt.provenance.lease_hash == runtime.lease.lease_hash
        if change == "replacement":
            runtime.authority.replace(_replacement(runtime))
        else:
            runtime.authority.revoke()
        injected.append(True)
        return receipt

    monkeypatch.setattr(runtime.tools.broker, "execute", execute_then_change)

    class NoModelCall:
        calls = 0

        def complete(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise AssertionError("A denied baseline must stop before model execution")

    model = NoModelCall()
    events = InMemoryEventSink()
    loop = CodingLoop(
        model=model,
        tools=runtime.tools,
        workspace=CodingWorkspace(runtime.tools.workspace),
        broker=runtime.tools.broker,
        context=LongContextManager(
            runtime.tools.context_tools.artifacts,
            max_tokens=8192,
            state_path=tmp_path / "context.json",
        ),
        verifier=LocalVerifier(),
        events=events,
        budgets=InMemoryBudgetLedger(),
        reservation_micros=0,
    )
    try:
        result = loop.run(
            runtime.context.task,
            runtime.context.grant,
            loop.plan(runtime.context.task, baseline_command=runtime.argv),
        )
        assert injected
        assert result.status != "completed"
        assert model.calls == 0 and result.model_calls == 0
        assert result.receipts == ()
        assert MARKER not in result.model_dump_json()
        assert MARKER not in str(
            [e.model_dump(mode="json") for e in events.events(runtime.context.task.run_id)]
        )
        assert runtime.tools.context_tools.references() == ()
    finally:
        runtime.session.close()
