"""Fenced runtime IO over the Engine-owned H11 operation journal.

The filesystem lock is always acquired before claiming an Engine epoch.  It is
also held across an authorized mutation, so takeover cannot invalidate a check
and then race an old worker's write.  This module does not write business SQL.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import CommandIdentity, IsolatedExecutionBroker, LaunchedCommand
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionProvenance,
    ExecutionReceipt,
    ExecutionRequest,
    WorkspaceEdit,
    WorkspaceSnapshot,
    sha256_text,
)
from mgar_v3.harness.result_artifacts import dump_result, load_result
from mgar_v3.harness.workspace import TaskWorkspace, _source_lock

T = TypeVar("T")


class RecoveryStore(Protocol):
    def get(self, task_id: str) -> dict[str, Any]: ...

    def guard(self, task_id: str, epoch: int) -> Any: ...

    def operation(self, task_id: str, operation_id: str) -> dict[str, Any] | None: ...

    def operations(self, task_id: str) -> list[dict[str, Any]]: ...

    def start_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        kind: str,
        request_hash: str,
        *,
        read_only: bool = False,
        intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def dispatch_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        *,
        intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def known_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        *,
        result_hash: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def unknown_operation(
        self,
        task_id: str,
        epoch: int,
        operation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]: ...

    def reconcile_operation(
        self,
        task_id: str,
        operation_id: str,
        *,
        result_hash: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@contextmanager
def workspace_lock(root: Path) -> Iterator[None]:
    """Shared lock order: workspace inode first, Engine claim/guard second."""
    if root.is_symlink() or not root.is_dir():
        raise DomainError("RECOVERY_WORKSPACE_DENIED", "Workspace root is unavailable", 409)
    with _source_lock(root):
        yield


class RecoveryIO:
    def __init__(
        self,
        store: RecoveryStore,
        task_id: str,
        execution_epoch: int,
        workspace: TaskWorkspace,
        *,
        artifacts: ArtifactStore | None = None,
        run_id: str = "",
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if task_id != workspace.manifest.task_id:
            raise DomainError("RECOVERY_WORKSPACE_DENIED", "Task workspace owner mismatch", 409)
        self.store = store
        self.task_id = task_id
        self.execution_epoch = execution_epoch
        self.workspace = workspace
        self.run_id = run_id or task_id
        self.fault = fault
        self.artifacts = (
            artifacts
            or getattr(store, "artifacts", None)
            or getattr(getattr(store, "engine", None), "artifacts", None)
            or ArtifactStore(workspace._metadata_root() / "recovery-cas")
        )

    def _receipt_hash(self, receipt: dict[str, Any]) -> str:
        return self.artifacts.put(
            dump_result(self.artifacts, receipt, self.task_id, self.run_id)
        )

    def _verified_receipt(self, operation: dict[str, Any]) -> dict[str, Any]:
        content_hash = operation.get("result_hash")
        if not content_hash:
            raise DomainError("RECOVERY_BLOCKED", "Known result has no artifact receipt", 409)
        try:
            payload = load_result(
                self.artifacts, self.artifacts.read(content_hash), self.task_id, self.run_id
            )
        except (ValueError, TypeError) as exc:
            raise DomainError("RECOVERY_BLOCKED", "Known result receipt is invalid", 409) from exc
        if not isinstance(payload, dict) or payload != operation["receipt"]:
            raise DomainError("RECOVERY_BLOCKED", "Known result receipt does not match", 409)
        return payload

    def _fault(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)

    def _require_paused(self) -> None:
        row = self.store.get(self.task_id)
        if row["status"] != "paused" or int(row.get("epoch", 0)) != self.execution_epoch:
            raise DomainError("FENCED", "Recovery does not own the paused epoch", 409)

    @contextmanager
    def locked(self) -> Iterator[None]:
        with workspace_lock(self.workspace.root):
            self.store.guard(self.task_id, self.execution_epoch)
            yield

    def edit(
        self,
        edits: Sequence[WorkspaceEdit],
        *,
        expected_tree: str,
        operation_id: str,
        request_hash: str | None = None,
    ) -> WorkspaceSnapshot:
        fingerprint = request_hash or digest(
            {
                "tree": expected_tree,
                "edits": [item.model_dump(mode="json") for item in edits],
            }
        )
        with self.locked():
            existing = self.store.operation(self.task_id, operation_id)
            if existing is not None:
                if existing.get("request_hash") != fingerprint or existing.get("kind") != "edit":
                    raise DomainError(
                        "RECOVERY_OPERATION_CONFLICT", "Operation identity changed", 409
                    )
                if existing["state"] == "known":
                    return WorkspaceSnapshot.model_validate(
                        self._verified_receipt(existing)["snapshot"]
                    )
                # Unknown effects must pass the explicit paused-task reconciler.
                if existing["state"] in {"unknown", "dispatched"}:
                    raise DomainError("RECOVERY_BLOCKED", "Edit requires reconciliation", 409)
                intent = existing["intent"]
            else:
                intent = self.workspace.prepare_recovery_edit(
                    edits,
                    expected_tree=expected_tree,
                    artifacts=self.artifacts,
                )
            self.store.start_operation(
                self.task_id,
                self.execution_epoch,
                operation_id,
                "edit",
                fingerprint,
                intent=intent,
            )
            self.store.dispatch_operation(
                self.task_id,
                self.execution_epoch,
                operation_id,
                intent=intent,
            )
            self.store.guard(self.task_id, self.execution_epoch)
            self._fault("edit.before_apply")
            snapshot = self.workspace.apply_recovery_edit(
                intent,
                artifacts=self.artifacts,
                fault_hook=self._fault,
            )
            self._fault("edit.after_apply")
            self.store.guard(self.task_id, self.execution_epoch)
            receipt = {
                "snapshot": snapshot.model_dump(mode="json"),
                "tree_hash": snapshot.tree_hash,
            }
            self.store.known_operation(
                self.task_id,
                self.execution_epoch,
                operation_id,
                result_hash=self._receipt_hash(receipt),
                receipt=receipt,
            )
            return snapshot

    def reconcile_edit_locked(self, operation_id: str) -> WorkspaceSnapshot:
        """Called while paused, under workspace_lock, before Engine claim.

        A dispatched/unknown edit is completed from its journalled images, not
        replayed from a model request.  Engine validates paused ownership when
        recording the reconciliation.  Callers keep the lock through claim.
        """
        self._require_paused()
        operation = self.store.operation(self.task_id, operation_id)
        if operation is None or operation["kind"] != "edit":
            raise DomainError("RECOVERY_OPERATION_CONFLICT", "Edit journal is missing", 409)
        snapshot = self.workspace.apply_recovery_edit(operation["intent"], artifacts=self.artifacts)
        receipt = {"snapshot": snapshot.model_dump(mode="json"), "tree_hash": snapshot.tree_hash}
        self.store.reconcile_operation(
            self.task_id,
            operation_id,
            result_hash=self._receipt_hash(receipt),
            receipt=receipt,
        )
        return snapshot

    @staticmethod
    def command_operation_id(request: ExecutionRequest) -> str:
        return request.operation_id + ":process"

    @staticmethod
    def _launch_payload(launched: LaunchedCommand) -> dict[str, Any]:
        identity = launched.identity
        assert identity is not None
        payload = {
            "name": launched.name,
            "lease_id": launched.lease_id,
            "execution_epoch": launched.execution_epoch,
            "timeout_seconds": launched.timeout_seconds,
            "max_output_bytes": launched.max_output_bytes,
            "tree_hash": launched.tree_hash,
            "environment_hash": launched.environment_hash,
            "provenance": (
                launched.provenance.model_dump(mode="json")
                if launched.provenance is not None
                else None
            ),
            "identity": {
                "task_id": identity.task_id,
                "run_id": identity.run_id,
                "operation_id": identity.operation_id,
                "execution_epoch": identity.execution_epoch,
                "workspace_hash": identity.workspace_hash,
            },
        }
        if launched.runtime_control is not None:
            payload["runtime_control"] = launched.runtime_control.model_dump(mode="json")
        return payload

    @staticmethod
    def restore_launch(payload: dict[str, Any]) -> LaunchedCommand:
        values = dict(payload)
        values["identity"] = CommandIdentity(**values["identity"])
        values["provenance"] = ExecutionProvenance.model_validate(values["provenance"])
        if values.get("runtime_control") is not None:
            from mgar_v3.harness.lifecycle_types import RuntimeControl

            values["runtime_control"] = RuntimeControl.model_validate(values["runtime_control"])
        return LaunchedCommand(**values)

    def cached_command(
        self, request: ExecutionRequest, grant: CapabilityGrant
    ) -> dict[str, Any] | None:
        with self.locked():
            operation = self.store.operation(self.task_id, self.command_operation_id(request))
            if operation is None:
                return None
            fingerprint = self._command_fingerprint(request, grant)
            if operation["request_hash"] != fingerprint or operation["kind"] != "command":
                raise DomainError("RECOVERY_OPERATION_CONFLICT", "Command request changed", 409)
            return self._verified_receipt(operation) if operation["state"] == "known" else None

    def launch_command(
        self,
        broker: IsolatedExecutionBroker,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> LaunchedCommand:
        operation_id = self.command_operation_id(request)
        fingerprint = self._command_fingerprint(request, grant)
        with self.locked():
            operation = self.store.start_operation(
                self.task_id,
                self.execution_epoch,
                operation_id,
                "command",
                fingerprint,
                intent={"request": request.model_dump(mode="json")},
            )
            if operation["state"] != "prepared":
                raise DomainError("RECOVERY_BLOCKED", "Command requires reconciliation", 409)
            identity = CommandIdentity(
                self.task_id,
                self.run_id,
                request.operation_id,
                self.execution_epoch,
                digest(str(broker.root)),
            )

            def before_create(launched: LaunchedCommand) -> None:
                self.store.guard(self.task_id, self.execution_epoch)
                self.store.dispatch_operation(
                    self.task_id,
                    self.execution_epoch,
                    operation_id,
                    intent={
                        "request": request.model_dump(mode="json"),
                        "started_at": datetime.now(UTC).isoformat(),
                        "launched": self._launch_payload(launched),
                    },
                )
                self._fault("command.before_create")

            return broker.launch(
                request,
                grant,
                cancellation,
                identity=identity,
                before_create=before_create,
                after_create=lambda: self._fault("command.after_create"),
                after_start=lambda: self._fault("command.after_start"),
            )

    @staticmethod
    def _command_fingerprint(request: ExecutionRequest, grant: CapabilityGrant) -> str:
        document = request.model_dump(mode="json")
        document.pop("execution_epoch", None)
        return digest({"request": document, "grant": grant.model_dump(mode="json")})

    def finish_command(
        self,
        broker: IsolatedExecutionBroker,
        launched: LaunchedCommand,
        *,
        handle: dict[str, Any],
        raw_logs: bytes,
        total_bytes: int,
        dropped_bytes: int = 0,
    ) -> None:
        """Persist original raw output/provenance before removing its container."""
        assert launched.identity is not None
        with self.locked():
            self._fault("command.before_log_cas")
            log_hash = self.artifacts.put(raw_logs)
            self._fault("command.after_log_cas")
            receipt = {
                "handle": handle,
                "launched": self._launch_payload(launched),
                "raw_log_hash": log_hash,
                "total_bytes": total_bytes,
                "dropped_bytes": dropped_bytes,
                "tree_hash": self.workspace.snapshot().tree_hash,
            }
            self.store.known_operation(
                self.task_id,
                self.execution_epoch,
                launched.identity.operation_id + ":process",
                result_hash=self._receipt_hash(receipt),
                receipt=receipt,
            )
            self._fault("command.after_receipt")
            broker.remove_owned_command(launched.identity)

    def reconcile_command_locked(
        self, broker: IsolatedExecutionBroker, operation_id: str
    ) -> dict[str, Any]:
        """Paused-task reconciliation; retain unknown when no owned receipt exists."""
        self._require_paused()
        operation = self.store.operation(self.task_id, operation_id)
        if operation is None or operation["kind"] != "command":
            raise DomainError("RECOVERY_OPERATION_CONFLICT", "Command intent is missing", 409)
        if operation["state"] == "known":
            # The journalled launch identity is enough to stop our surviving
            # child even when a result artifact has since become unreadable.
            # Retain the stopped container until receipt verification succeeds.
            launch_payload = operation["intent"].get("launched")
            if launch_payload is not None:
                original = self.restore_launch(launch_payload)
                assert original.identity is not None
                broker.halt_owned_command(original.identity)
            receipt = self._verified_receipt(operation)
            launched = self.restore_launch(receipt["launched"])
            assert launched.identity is not None
            broker.remove_owned_command(launched.identity)
            return receipt
        intent = operation["intent"]
        if "launched" not in intent:
            raise DomainError("RECOVERY_BLOCKED", "Command dispatch was not recorded", 409)
        launched = self.restore_launch(intent["launched"])
        assert launched.identity is not None
        state, code = broker.owned_command_state(launched.identity)
        if state == "missing":
            raise DomainError("RECOVERY_BLOCKED", "Command receipt is missing; do not resend", 409)
        interrupted = state not in {"exited", "dead"}
        broker.halt_owned_command(launched.identity)
        _, code = broker.owned_command_state(launched.identity)
        raw, total = broker.owned_command_logs(
            launched.identity,
            max_bytes=launched.max_output_bytes,
        )
        request = ExecutionRequest.model_validate(intent["request"])
        handle = {
            "handle_id": launched.identity.identity_hash,
            "operation_id": request.operation_id,
            "purpose": "command",
            "argv": list(request.argv),
            "cwd": request.cwd,
            "state": "cancelled" if interrupted else ("exited" if code == 0 else "failed"),
            "exit_code": code,
            "started_at": intent["started_at"],
            "deadline_seconds": launched.timeout_seconds,
            "tree_hash": launched.tree_hash,
            "environment_hash": launched.environment_hash,
            "log_bytes": total,
            "log_truncated": total > len(raw),
            "log_sha256": sha256_text(raw.decode("utf-8", "replace")),
            "provenance": intent["launched"]["provenance"],
        }
        receipt = {
            "handle": handle,
            "launched": intent["launched"],
            "raw_log_hash": self.artifacts.put(raw),
            "total_bytes": total,
            "dropped_bytes": 0,
            "reconciled_interruption": interrupted,
            "tree_hash": self.workspace.snapshot().tree_hash,
        }
        self.store.reconcile_operation(
            self.task_id,
            operation_id,
            result_hash=self._receipt_hash(receipt),
            receipt=receipt,
        )
        broker.remove_owned_command(launched.identity)
        return receipt

    def recovered_execution_receipt(
        self, process_operation_id: str, *, pre_command_tree: bool = False,
    ) -> ExecutionReceipt:
        """Rebuild a known command result without issuing a replacement lease.

        This is historical evidence.  The service must validate its exact
        request, immutable grant and tree binding before reconciling an outer
        tool/baseline operation; it does not authorize new log paging.
        """
        operation = self.store.operation(self.task_id, process_operation_id)
        if operation is None or operation["kind"] != "command" or operation["state"] != "known":
            raise DomainError("RECOVERY_BLOCKED", "Known command receipt is unavailable", 409)
        request = ExecutionRequest.model_validate(operation["intent"]["request"])
        receipt = self._verified_receipt(operation)
        raw = self.artifacts.read(receipt["raw_log_hash"])
        launched = self.restore_launch(receipt["launched"])
        handle = receipt["handle"]
        status = (
            "passed"
            if handle["state"] == "exited" and handle["exit_code"] == 0
            else "timeout"
            if handle["state"] == "timed_out"
            else "failed"
        )
        text = raw.decode("utf-8", "replace")
        return ExecutionReceipt(
            receipt_id=self._receipt_hash(receipt),
            operation_id=request.operation_id,
            purpose=request.purpose,
            status=status,
            argv=request.argv,
            cwd=request.cwd,
            exit_code=handle["exit_code"] if handle["exit_code"] is not None else 1,
            stdout=text,
            stderr="",
            duration_seconds=0.0,
            tree_hash=launched.tree_hash if pre_command_tree else receipt.get(
                "tree_hash", launched.tree_hash
            ),
            log_sha256=sha256_text(text),
            environment_hash=launched.environment_hash,
            provenance=launched.provenance,
        )

    def execute_command(
        self,
        broker: IsolatedExecutionBroker,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ExecutionReceipt:
        """Synchronous baseline execution through the durable command lifecycle."""
        from mgar_v3.harness.commands import CommandSession

        started = time.monotonic()
        session = CommandSession(broker, recovery_io=self)
        handle = session.run(request, grant, cancellation, purpose=request.purpose)
        try:
            finished = session.wait(handle.handle_id)
            session.assert_authority(handle.handle_id)
            operation = self.store.operation(self.task_id, self.command_operation_id(request))
            if operation is None or operation["state"] != "known":
                raise DomainError("RECOVERY_BLOCKED", "Command result is not durable", 409)
            receipt = self._verified_receipt(operation)
            raw = self.artifacts.read(receipt["raw_log_hash"])
            text = raw.decode("utf-8", "replace")
            status = (
                "passed"
                if finished.state == "exited" and finished.exit_code == 0
                else "timeout"
                if finished.state == "timed_out"
                else "failed"
            )
            return ExecutionReceipt(
                receipt_id=self._receipt_hash(receipt),
                operation_id=request.operation_id,
                purpose=request.purpose,
                status=status,
                argv=request.argv,
                cwd=request.cwd,
                exit_code=finished.exit_code if finished.exit_code is not None else 1,
                stdout=text,
                stderr="",
                duration_seconds=time.monotonic() - started,
                tree_hash=broker.workspace_tree_hash(),
                log_sha256=sha256_text(text),
                environment_hash=finished.environment_hash,
                provenance=finished.provenance,
            )
        finally:
            session.close()

    def invoke(
        self,
        operation_id: str,
        kind: str,
        request: dict[str, Any],
        call: Callable[[], T],
        *,
        encode: Callable[[T], dict[str, Any]],
        decode: Callable[[dict[str, Any]], T],
        read_only: bool = False,
        validate_known: Callable[[T], None] | None = None,
    ) -> T:
        """Reuse known results; never resend a dispatched/unknown side effect."""
        fingerprint = digest(request)
        with self.locked():
            operation = self.store.start_operation(
                self.task_id,
                self.execution_epoch,
                operation_id,
                kind,
                fingerprint,
                read_only=read_only,
                intent=request,
            )
            if operation["state"] == "known":
                value = decode(self._verified_receipt(operation))
                if validate_known is not None:
                    validate_known(value)
                return value
            if operation["state"] in {"unknown", "dispatched"} and not read_only:
                raise DomainError("RECOVERY_BLOCKED", "Operation requires reconciliation", 409)
            self.store.dispatch_operation(self.task_id, self.execution_epoch, operation_id)
        try:
            # Actual mutation adapters acquire locked() themselves.  Releasing
            # here lets command monitors make progress while remote work runs;
            # final publication is fenced again below.
            value = call()
            receipt = encode(value)
            with self.locked():
                self.store.known_operation(
                    self.task_id,
                    self.execution_epoch,
                    operation_id,
                    result_hash=self._receipt_hash(receipt),
                    receipt=receipt,
                )
                return value
        except BaseException:
            try:
                self.store.unknown_operation(
                    self.task_id,
                    self.execution_epoch,
                    operation_id,
                    reason="runtime interrupted after dispatch; reconciliation required",
                )
            except DomainError:
                # A takeover/cancel already fenced this owner; do not replace
                # its error or write into the new owner's task.
                pass
            raise
