"""H11 local crash boundaries; Docker/PG acceptance is exercised separately."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness import workspace as workspace_module
from mgar_v3.harness.broker import CommandIdentity, IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.recovery_io import RecoveryIO, workspace_lock
from mgar_v3.harness.workspace import WorkspaceRegistry


class Journal:
    """Only the IO contract; authoritative PG journal is tested independently."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts
        self.epoch = 1
        self.status = "running"
        self.operations: dict[str, dict[str, Any]] = {}

    def guard(self, task_id: str, epoch: int) -> None:
        if self.epoch != epoch or self.status != "running":
            raise DomainError("FENCED", "Old owner", 409)

    def get(self, task_id: str) -> dict[str, Any]:
        return {"status": self.status, "epoch": self.epoch}

    def operation(self, task_id: str, operation_id: str) -> dict[str, Any] | None:
        return self.operations.get(operation_id)

    def start_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        kind: str,
        request_hash: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.guard(task_id, epoch)
        if operation_id in self.operations:
            row = self.operations[operation_id]
            if row["request_hash"] != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Changed request", 409)
            return row
        row = {
            "operation_id": operation_id,
            "kind": kind,
            "state": "prepared",
            "request_hash": request_hash,
            "intent": kwargs.get("intent", {}),
            "receipt": {},
        }
        self.operations[operation_id] = row
        return row

    def dispatch_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.guard(task_id, epoch)
        row = self.operations[operation_id]
        row["state"] = "dispatched"
        if kwargs.get("intent") is not None:
            row["intent"] = kwargs["intent"]
        return row

    def known_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.guard(task_id, epoch)
        self.artifacts.read(kwargs["result_hash"])
        row = self.operations[operation_id]
        row.update(state="known", **kwargs)
        return row

    def unknown_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.guard(task_id, epoch)
        row = self.operations[operation_id]
        row.update(state="unknown", **kwargs)
        return row

    def reconcile_operation(
        self,
        task_id: str,
        operation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert self.status == "paused"
        self.artifacts.read(kwargs["result_hash"])
        row = self.operations[operation_id]
        row.update(state="known", **kwargs)
        return row


def runtime(tmp_path: Path) -> tuple[Any, Any, Any, Journal, RecoveryIO]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("a = 1\n")
    (source / "b.py").write_text("b = 1\n")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "task-h11-io")
    store = Journal(ArtifactStore(tmp_path / "cas"))
    io = RecoveryIO(store, workspace.manifest.task_id, 1, workspace, run_id="run-h11-io")
    return registry, registration, workspace, store, io


def edits() -> list[WorkspaceEdit]:
    return [
        WorkspaceEdit(path="a.py", expected_sha256=sha256_text("a = 1\n"), content="a = 2\n"),
        WorkspaceEdit(path="b.py", expected_sha256=sha256_text("b = 1\n"), content="b = 2\n"),
    ]


@pytest.mark.parametrize("boundary", ["before", "mixed", "after"])
def test_edit_crash_reopens_and_finishes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    registry, registration, workspace, store, io = runtime(tmp_path)
    before_tree = workspace.snapshot().tree_hash
    replace = workspace_module.os.replace

    def interrupted(source: Any, target: Any, **kwargs: Any) -> None:
        if Path(target).name == "a.py" and boundary == "before":
            raise SystemExit("fault before first edit")
        replace(source, target, **kwargs)
        if (Path(target).name == "a.py" and boundary == "mixed") or (
            Path(target).name == "b.py" and boundary == "after"
        ):
            raise SystemExit("fault after file replacement")

    with monkeypatch.context() as patch:
        patch.setattr(workspace_module.os, "replace", interrupted)
        with pytest.raises(SystemExit):
            io.edit(edits(), expected_tree=before_tree, operation_id="edit-1")
    assert store.operations["edit-1"]["state"] == "dispatched"
    if boundary != "before":
        with pytest.raises(DomainError, match="manifest"):
            registry.open_task_workspace(registration, workspace.manifest.task_id)
    reopened = registry.open_task_workspace(
        registration,
        workspace.manifest.task_id,
        allow_unreconciled=True,
    )
    store.status = "paused"
    restored = RecoveryIO(store, workspace.manifest.task_id, 1, reopened)
    with workspace_lock(reopened.root):
        result = restored.reconcile_edit_locked("edit-1")
    assert (reopened.root / "a.py").read_text() == "a = 2\n"
    assert (reopened.root / "b.py").read_text() == "b = 2\n"
    assert (
        registry.open_task_workspace(registration, reopened.manifest.task_id).snapshot() == result
    )
    store.status = "running"
    assert restored.edit(edits(), expected_tree=before_tree, operation_id="edit-1") == result
    assert restored.workspace.snapshot().tree_hash == result.tree_hash


def test_edit_recovery_rejects_unrelated_mutation(tmp_path: Path) -> None:
    _, _, workspace, _, io = runtime(tmp_path)
    intent = workspace.prepare_recovery_edit(
        edits()[:1],
        expected_tree=workspace.snapshot().tree_hash,
        artifacts=io.artifacts,
    )
    (workspace.root / "b.py").write_text("foreign change\n")
    with pytest.raises(DomainError, match="Unrelated"):
        workspace.apply_recovery_edit(intent, artifacts=io.artifacts)
    assert (workspace.root / "a.py").read_text() == "a = 1\n"


def test_edit_recovery_checks_all_cas_before_first_write(tmp_path: Path) -> None:
    _, _, workspace, _, io = runtime(tmp_path)
    intent = workspace.prepare_recovery_edit(
        edits(),
        expected_tree=workspace.snapshot().tree_hash,
        artifacts=io.artifacts,
    )
    io.artifacts.path(intent["images"]["b.py"]["after"]).write_bytes(b"corrupt")
    with pytest.raises(DomainError, match="hash"):
        workspace.apply_recovery_edit(intent, artifacts=io.artifacts)
    assert (workspace.root / "a.py").read_text() == "a = 1\n"


def test_workspace_lock_reentrant_and_excludes_threads(tmp_path: Path) -> None:
    acquired = threading.Event()
    with workspace_lock(tmp_path):
        with workspace_lock(tmp_path):
            thread = threading.Thread(target=lambda: _lock_and_signal(tmp_path, acquired))
            thread.start()
            assert not acquired.wait(0.05)
    thread.join(timeout=2)
    assert acquired.is_set()


def _lock_and_signal(root: Path, acquired: threading.Event) -> None:
    with workspace_lock(root):
        acquired.set()


def test_workspace_lock_excludes_separate_process(tmp_path: Path) -> None:
    # Independent interpreter, not a lock inherited by a forked Python thread.
    program = (
        "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
        "print('waiting',flush=True); fcntl.flock(f,fcntl.LOCK_EX); print('acquired',flush=True)"
    )
    with workspace_lock(tmp_path):
        child = subprocess.Popen(
            ["/usr/bin/python3", "-c", program, str(tmp_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "waiting"
        assert child.poll() is None
    remaining, _ = child.communicate(timeout=3)
    assert remaining.strip() == "acquired"


def test_old_epoch_never_edits(tmp_path: Path) -> None:
    _, _, workspace, store, io = runtime(tmp_path)
    store.epoch = 2
    with pytest.raises(DomainError, match="Old owner"):
        io.edit(edits(), expected_tree=workspace.snapshot().tree_hash, operation_id="old-edit")
    assert not store.operations
    assert (workspace.root / "a.py").read_text() == "a = 1\n"


def test_known_invoke_preserves_receipt_unknown_is_not_resent(tmp_path: Path) -> None:
    _, _, _, store, io = runtime(tmp_path)
    calls: list[int] = []

    def invoke() -> dict[str, Any]:
        calls.append(1)
        return {"original_provenance": {"execution_epoch": 1}, "data": "kept"}

    result = io.invoke("read-1", "read", {}, invoke, encode=dict, decode=dict, read_only=True)
    assert (
        io.invoke("read-1", "read", {}, invoke, encode=dict, decode=dict, read_only=True) == result
    )
    assert calls == [1]
    store.start_operation(io.task_id, 1, "write-1", "command", digest({}))
    store.operations["write-1"]["state"] = "unknown"
    with pytest.raises(DomainError, match="reconciliation"):
        io.invoke("write-1", "command", {}, invoke, encode=dict, decode=dict)
    assert calls == [1]


def broker_runtime(tmp_path: Path) -> tuple[Any, Journal, RecoveryIO, Any, Any, Any]:
    _, registration, workspace, store, io = runtime(tmp_path)
    lease = issue_lease(
        mode="modify_test",
        actor="owner",
        workspace_id=registration.workspace_id,
        repository=registration.repository,
        execution_epoch=1,
        read_paths=("",),
        write_paths=("a.py",),
        allowed_commands=("python3",),
        approved_by="owner",
        approval_ref="h11-fixture",
        max_calls=100,
    )
    broker = IsolatedExecutionBroker(workspace.root, lease, image_id="sha256:" + "1" * 64)
    grant = CapabilityGrant(
        actor="owner",
        workspace_id=registration.workspace_id,
        repository=registration.repository,
        read_paths=("",),
        write_paths=("a.py",),
        execute=True,
        allowed_commands=("python3",),
    )
    request = ExecutionRequest(operation_id="command-1", argv=("python3", "-V"), execution_epoch=1)
    return broker, store, io, grant, request, CancellationToken()


def test_command_dispatch_intent_precedes_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, store, io, grant, request, token = broker_runtime(tmp_path)
    calls: list[list[str]] = []

    def docker(args: list[str]) -> Any:
        calls.append(args)
        if args[0] == "create":
            op = store.operations["command-1:process"]
            assert op["state"] == "dispatched"
            assert op["intent"]["launched"]["provenance"]["execution_epoch"] == 1
            assert "mgar.v4.h11.task=" + io.task_id in args
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(broker, "_docker", docker)
    launched = io.launch_command(broker, request, grant, token)
    assert launched.identity is not None
    assert launched.name == launched.identity.name
    assert [call[0] for call in calls] == ["create", "start"]


def test_command_wrong_labels_never_release_or_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _, io, _, request, _ = broker_runtime(tmp_path)
    identity = CommandIdentity(
        io.task_id, io.run_id, request.operation_id, 1, digest(str(broker.root))
    )
    calls: list[list[str]] = []

    def docker(args: list[str]) -> Any:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({}).encode(), b"")

    monkeypatch.setattr(broker, "_docker", docker)
    with pytest.raises(DomainError, match="labels"):
        broker.owned_command_logs(identity, max_bytes=100)
    with pytest.raises(DomainError, match="labels"):
        broker.remove_owned_command(identity)
    assert all(call[0] == "inspect" for call in calls)


@pytest.mark.parametrize("state", ["running", "exited"])
def test_command_reconciliation_archives_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    broker, store, io, grant, request, token = broker_runtime(tmp_path)
    monkeypatch.setattr(
        broker,
        "_docker",
        lambda args: subprocess.CompletedProcess(args, 0, b"", b""),
    )
    launched = io.launch_command(broker, request, grant, token)
    store.status = "paused"
    monkeypatch.setattr(broker, "owned_command_state", lambda identity: (state, 0))
    monkeypatch.setattr(broker, "halt_owned_command", lambda identity: None)
    monkeypatch.setattr(broker, "owned_command_logs", lambda identity, **kwargs: (b"raw\xff", 4))

    def remove(identity: Any) -> None:
        row = store.operations["command-1:process"]
        assert row["state"] == "known"
        assert io.artifacts.read(row["receipt"]["raw_log_hash"]) == b"raw\xff"
        assert row["receipt"]["launched"]["provenance"] == launched.provenance.model_dump(
            mode="json"
        )

    monkeypatch.setattr(broker, "remove_owned_command", remove)
    with workspace_lock(io.workspace.root):
        receipt = io.reconcile_command_locked(broker, "command-1:process")
    assert receipt["reconciled_interruption"] is (state == "running")
    historical = io.recovered_execution_receipt("command-1:process")
    assert historical.provenance == launched.provenance
    assert historical.status == ("failed" if state == "running" else "passed")
    assert historical.stdout == "raw\ufffd"


def test_missing_command_stays_unknown_and_is_not_relaunched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, store, io, grant, request, token = broker_runtime(tmp_path)
    monkeypatch.setattr(
        broker,
        "_docker",
        lambda args: subprocess.CompletedProcess(args, 0, b"", b""),
    )
    io.launch_command(broker, request, grant, token)
    store.status = "paused"
    store.operations["command-1:process"]["state"] = "unknown"
    monkeypatch.setattr(broker, "owned_command_state", lambda identity: ("missing", None))
    with workspace_lock(io.workspace.root), pytest.raises(DomainError, match="do not resend"):
        io.reconcile_command_locked(broker, "command-1:process")
    assert store.operations["command-1:process"]["state"] == "unknown"


def test_command_session_saves_receipt_before_rm_and_reuses_known(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, store, io, grant, request, token = broker_runtime(tmp_path)
    labels: dict[str, str] = {}
    present = False
    creates = 0

    def docker(args: list[str]) -> Any:
        nonlocal present, creates
        output = b""
        if args[0] == "create":
            creates += 1
            present = True
            for index, value in enumerate(args[:-1]):
                if value == "--label":
                    key, label_value = args[index + 1].split("=", 1)
                    labels[key] = label_value
        elif args[0] == "inspect":
            if not present:
                return subprocess.CompletedProcess(args, 1, b"", b"")
            output = json.dumps(labels).encode() if "Config.Labels" in args[2] else b"exited|0"
        elif args[0] == "rm":
            row = store.operations["command-1:process"]
            assert row["state"] == "known"
            assert io.artifacts.read(row["receipt"]["raw_log_hash"]) == b"stdout\n"
            present = False
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(broker, "_docker", docker)
    monkeypatch.setattr(broker, "command_logs", lambda name, **kwargs: (b"stdout\n", 7))
    broker.bind_recovery(io)
    session = CommandSession(broker)
    handle = session.run(request, grant, token)
    finished = session.wait(handle.handle_id)
    assert finished.state == "exited"
    assert not present
    assert session.read_log(handle.handle_id).data == "stdout\n"
    recovered_session = CommandSession(broker)
    assert recovered_session.run(request, grant, token) == finished
    assert recovered_session.read_log(handle.handle_id).data == "stdout\n"
    assert creates == 1
    session.close()
    recovered_session.close()


def test_invoke_cannot_publish_after_epoch_changes(tmp_path: Path) -> None:
    _, _, _, store, io = runtime(tmp_path)

    def finish_after_takeover() -> dict[str, Any]:
        store.epoch = 2
        return {"private_result": "old worker"}

    with pytest.raises(DomainError, match="Old owner"):
        io.invoke("late", "model", {}, finish_after_takeover, encode=dict, decode=dict)
    assert store.operations["late"]["state"] == "dispatched"
    assert store.operations["late"]["receipt"] == {}


def test_known_receipt_cas_corruption_prevents_reuse(tmp_path: Path) -> None:
    _, _, _, store, io = runtime(tmp_path)
    io.invoke("known", "read", {}, lambda: {"result": "original"}, encode=dict, decode=dict)
    result_hash = store.operations["known"]["result_hash"]
    io.artifacts.path(result_hash).write_bytes(b"corrupted receipt")
    with pytest.raises(DomainError, match="hash"):
        io.invoke("known", "read", {}, lambda: {"result": "new"}, encode=dict, decode=dict)


def test_fenced_session_stops_child_and_retains_unarchived_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, store, io, grant, request, token = broker_runtime(tmp_path)
    labels: dict[str, str] = {}
    state = "running"
    calls: list[str] = []

    def docker(args: list[str]) -> Any:
        nonlocal state
        calls.append(args[0])
        output = b""
        if args[0] == "create":
            for index, value in enumerate(args[:-1]):
                if value == "--label":
                    key, label_value = args[index + 1].split("=", 1)
                    labels[key] = label_value
        elif args[0] == "inspect":
            output = (
                json.dumps(labels).encode()
                if "Config.Labels" in args[2]
                else (state + "|137").encode()
            )
        elif args[0] == "kill":
            state = "exited"
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(broker, "_docker", docker)
    monkeypatch.setattr(broker, "command_logs", lambda name, **kwargs: (b"retained\n", 9))
    broker.bind_recovery(io)
    session = CommandSession(broker)
    handle = session.run(request, grant, token)
    store.epoch = 2
    session.cancel(handle.handle_id)
    session.close()
    assert "kill" in calls
    assert "rm" not in calls
    assert store.operations["command-1:process"]["state"] == "dispatched"
