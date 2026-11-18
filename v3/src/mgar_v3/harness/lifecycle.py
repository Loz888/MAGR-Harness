"""R04 lifecycle observer over existing CommandSession/Broker handles."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.commands import CommandHandle, CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    TaskBinding,
)
from mgar_v3.harness.lifecycle_assets import assets_hash
from mgar_v3.harness.lifecycle_types import (
    EntryPointReceipt,
    EntryPointSpec,
    HTTPExpectation,
    HTTPObservation,
    LifecyclePolicy,
    ProcessObservation,
    ProcessPortReceipt,
    ReadyReceipt,
    RuntimeControl,
    RuntimeSnapshot,
    ServiceEvidence,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


T = TypeVar("T", bound=Model)


def sealed[T: Model](cls: type[T], values: dict[str, Any]) -> T:
    value = cls.model_validate({"receipt_id": "pending", **values})
    return value.model_copy(
        update={
            "receipt_id": digest(value.model_dump(mode="json", exclude={"receipt_id"})),
        }
    )


class LifecycleRunner:
    def __init__(
        self,
        session: CommandSession,
        task: TaskBinding,
        policy: LifecyclePolicy,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.session, self.task, self.policy = session, task, policy
        self.artifacts = artifacts or (
            session.recovery_io.artifacts if session.recovery_io else None
        )

    def wire(self, handle: str) -> tuple[str, list[dict[str, Any]], bool]:
        raw = bytearray()
        offset = 0
        complete = True
        while True:
            page = self.session.read_log(handle, offset=offset)
            complete = complete and not page.truncated and page.offset == offset
            raw.extend(page.data.encode())
            if page.next_offset <= offset or page.next_offset >= page.total_bytes:
                break
            offset = page.next_offset
        output = bytearray()
        frames = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
                if value["kind"] == "output":
                    data = base64.b64decode(value["bytes"], validate=True)
                    output.extend(data)
                    frames.append({"kind": value["stream"], "bytes": value["bytes"]})
                elif value["kind"] == "observation":
                    frames.append(value["value"])
                else:
                    frames.append(value)
            except (KeyError, TypeError, ValueError):
                complete = False
        return output.decode("utf-8", "replace"), frames, complete

    @staticmethod
    def root(snapshot: RuntimeSnapshot, frames: list[dict[str, Any]]) -> ProcessObservation | None:
        launched = [f for f in frames if f.get("kind") == "launch"]
        if len(launched) != 1:
            return None
        pid = launched[0].get("pid")
        return next(
            (
                p
                for p in snapshot.processes
                if p.pid == pid and p.group_id == pid and p.parent_pid == snapshot.supervisor.pid
            ),
            None,
        )

    @staticmethod
    def ready_port(text: str) -> tuple[int | None, str]:
        lines = [
            line
            for line in text.splitlines()
            if re.search(r"\bready\b", line, re.I) and not re.search(r"\bnot\s+ready\b", line, re.I)
        ]
        if len(lines) != 1:
            return None, "Exactly one Ready line is required"
        line = lines[0]
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        if isinstance(value, dict):
            port = value.get("port")
            if value.get("ready") is True and type(port) is int and 1 <= port <= 65535:
                return port, ""
        found = re.search(
            r"(?:\bport\s*[:=]?\s*|\bready\s*[:=]?\s*|(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\]):)"
            r"([0-9]{1,5})(?![0-9])",
            line,
            re.I,
        )
        if found and 1 <= int(found[1]) <= 65535:
            return int(found[1]), ""
        return None, "Ready line does not include the actual bound port"

    def fetch(self, handle: str, port: int, expected: HTTPExpectation) -> HTTPObservation:
        status, content_type, body = self.session.fetch_http(
            handle, port=port, path=expected.path, max_bytes=65536
        )
        detail = ""
        if status != expected.status_code:
            detail = f"{expected.path}: observed HTTP {status}, expected {expected.status_code}"
        if (
            expected.content_type is not None
            and content_type.split(";")[0].strip().lower() != expected.content_type.lower()
        ):
            detail = "Observed Content-Type differs from the public contract"
        if expected.body is not None and body.decode("utf-8", "replace") != expected.body:
            detail = "Observed response body differs from the public contract"
        if expected.json_body is not None:
            try:
                valid = json.loads(body) == expected.json_body
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                detail = "Observed JSON body differs from the public contract"
        return HTTPObservation(
            path=expected.path,
            port=port,
            status_code=status,
            content_type=content_type,
            body_hash=hashlib.sha256(body).hexdigest(),
            observed_at=now(),
            source="independent-container-probe",
            matched=not detail,
            detail=detail,
            body_bytes=len(body),
            body_artifact_hash=self.artifacts.put(body) if self.artifacts else None,
        )

    def launch(
        self,
        argv: tuple[str, ...],
        grant: CapabilityGrant,
        operation_id: str,
        mode: Literal["http", "oneshot", "entry"],
        *,
        cwd: str,
        timeout: float,
        entry: EntryPointSpec | None = None,
    ) -> CommandHandle:
        control = RuntimeControl(
            mode=mode, entry=entry, assets_hash=self.policy.control_assets_hash
        )
        request = ExecutionRequest(
            operation_id=operation_id,
            purpose="command",
            argv=argv,
            cwd=cwd,
            timeout_seconds=min(
                1800, timeout + self.policy.shutdown_seconds + self.policy.cleanup_seconds + 10
            ),
            execution_epoch=self.task.execution_epoch,
            runtime_control=control,
        )
        # The task token is checked by the observer. A separate handle token
        # lets it inspect cleanup before the owned supervisor is removed.
        return self.session.run(request, grant, CancellationToken(), purpose="service")

    def cleanup(
        self,
        handle: str,
        command_id: str,
        tree: str,
        *,
        port: int = 0,
        expect_running: bool = False,
    ) -> ProcessPortReceipt:
        before = None
        after = None
        orphans: tuple[int, ...] = ()
        graceful = False
        forced = False
        exited: bool | None = None
        released: bool | None = None
        detail = "Process/port observation unavailable"
        started = time.monotonic()
        signal_at = None
        try:
            before = self.session.runtime_snapshot(handle, port=port)
            text, frames, _ = self.wire(handle)
            del text
            root = self.root(before, frames)
            if root is None:
                raise DomainError("HARNESS_PROCESS_OBSERVATION_INVALID", "Project PID missing", 409)
            ports = {p["port"] for p in before.listeners if p.get("pids")}
            if port:
                ports.add(port)
            was_running = root.state != "Z"
            signal_started = time.monotonic()
            if was_running:
                signal_at = now()
                self.session.signal_service(handle)
            deadline = signal_started + self.policy.shutdown_seconds
            while True:
                after = self.session.runtime_snapshot(handle, port=port)
                if (
                    after.supervisor.pid != before.supervisor.pid
                    or after.supervisor.start_ticks != before.supervisor.start_ticks
                ):
                    raise DomainError(
                        "HARNESS_PROCESS_OBSERVATION_INVALID", "Supervisor changed", 409
                    )
                live = [p for p in after.processes if p.state != "Z"]
                current = next((p for p in after.processes if p.pid == root.pid), None)
                root_exited = current is not None and current.state == "Z"
                orphans = tuple(p.pid for p in live if p.pid != root.pid) if root_exited else ()
                if root_exited and not live:
                    graceful = (not expect_running or was_running) and (
                        not was_running
                        or time.monotonic() <= deadline
                        and current is not None
                        and current.exit_code is not None
                        and current.exit_code in self.policy.shutdown_exit_codes
                    )
                    break
                if root_exited and orphans or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            if not graceful:
                forced = after is not None and any(p.state != "Z" for p in after.processes)
                if forced:
                    self.session.runtime_snapshot(handle, port=port, action="kill")
            deadline = time.monotonic() + self.policy.cleanup_seconds
            while True:
                after = self.session.runtime_snapshot(handle, port=port)
                exited = not any(p.state != "Z" for p in after.processes)
                released = not any(p.get("pids") for p in after.listeners)
                for number in sorted(ports):
                    check = self.session.runtime_snapshot(handle, port=number)
                    released = released and check.bind_available is True
                if exited and released or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            detail = "Observed project PIDs exited and ports reusable before supervisor removal"
        except DomainError as exc:
            detail = exc.code
        finally:
            self.session.cancel(handle)
        removed = self.session.controlled_container_removed(handle)
        values = dict(
            observed_at=now(),
            command_id=command_id,
            tree_hash=tree,
            signal="SIGTERM",
            graceful=graceful and released is True,
            forced=forced,
            processes_exited=exited,
            port_released=released,
            container_removed=removed,
            orphan_pids=orphans,
            before=before,
            after=after,
            detail=detail,
        )
        values.update(
            signal_sent_at=signal_at,
            grace_seconds=self.policy.shutdown_seconds,
            duration_seconds=round(time.monotonic() - started, 6),
        )
        return sealed(ProcessPortReceipt, values)

    def application(
        self,
        argv: tuple[str, ...],
        grant: CapabilityGrant,
        operation_id: str,
        tree: str,
        token: CancellationToken,
        *,
        mode: Literal["http", "oneshot"],
        port: int,
        routes: tuple[HTTPExpectation, ...],
        cwd: str,
        timeout: float,
    ) -> tuple[CommandHandle, ServiceEvidence]:
        token.ensure_active()
        if mode == "http" and not routes:
            routes = (HTTPExpectation(),)
        handle = self.launch(argv, grant, operation_id, mode, cwd=cwd, timeout=timeout)
        observation_started_at = now()
        started = time.monotonic()
        deadline = started + timeout
        snapshot = None
        root = None
        actual_port = None
        log_port = None
        observations: list[HTTPObservation] = []
        detail = "No readiness evidence"
        ok = False
        exit_code = None
        released_closing: set[int] = set()
        client_pids: set[int] = set()
        printed_statuses: tuple[int, ...] = ()
        stdout = ""
        stdout_bytes = b""
        while time.monotonic() < deadline and not token.cancelled:
            try:
                output, frames, complete = self.wire(handle.handle_id)
                stdout_bytes = b"".join(
                    base64.b64decode(f["bytes"], validate=True)
                    for f in frames
                    if f.get("kind") == "stdout"
                )
                stdout = stdout_bytes.decode("utf-8", "replace")
                snapshot = self.session.runtime_snapshot(handle.handle_id)
                root = self.root(snapshot, frames)
                if root is None:
                    detail = "Project PID has not been observed"
                    continue
                if not complete:
                    detail = "Lifecycle log is incomplete"
                    break
                if mode == "http":
                    if root.state == "Z":
                        detail = "HTTP entry exited before readiness"
                        break
                    log_port, detail = self.ready_port(stdout)
                    actual_port = log_port if self.policy.require_ready_line else port
                    if actual_port is None or port and actual_port != port:
                        detail = detail or "Ready port differs from the assigned port"
                        continue
                    if not any(
                        p["port"] == actual_port and p.get("pids") for p in snapshot.listeners
                    ):
                        detail = "Ready port is not bound by the project process tree"
                        continue
                    observations = [self.fetch(handle.handle_id, actual_port, r) for r in routes]
                    ok = all(o.matched for o in observations)
                    detail = (
                        "Ready line, owned listener and HTTP contracts verified"
                        if ok
                        else next(o.detail for o in observations if not o.matched)
                    )
                    if ok:
                        break
                    continue
                closing = [f for f in frames if f.get("kind") == "closing"]
                clients = [f for f in frames if f.get("kind") == "http"]
                for close in closing:
                    pid = close.get("pid")
                    number = close.get("port")
                    if pid in released_closing:
                        continue
                    if (
                        type(pid) is not int
                        or type(number) is not int
                        or not 1 <= number <= 65535
                        or not any(
                            p["port"] == number and pid in p.get("pids", [])
                            for p in snapshot.listeners
                        )
                    ):
                        raise DomainError(
                            "HARNESS_SELFCHECK_OWNER", "Closing server not owned", 409
                        )
                    matches = [
                        f
                        for f in clients
                        if f.get("port") == number
                        and f.get("host") in {"localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"}
                    ]
                    actual_port = number
                    if matches:
                        match = matches[-1]
                        expected = next((r for r in routes if r.path == match["path"]), None)
                        expected = expected or HTTPExpectation(
                            path=match["path"], status_code=match["status"]
                        )
                        observed = self.fetch(handle.handle_id, number, expected)
                        if observed.body_hash != match["body_hash"]:
                            raise DomainError(
                                "HARNESS_SELFCHECK_BODY", "Observed response differs", 422
                            )
                        observations.append(observed)
                        client_pids.add(match["pid"])
                    # Release only the exact observed server owner, even if a
                    # failed selfcheck needs to finish its own finally block.
                    self.session.runtime_snapshot(handle.handle_id, action=f"release:{pid}")
                    released_closing.add(pid)
                if root.state == "Z":
                    exit_code = root.exit_code
                    printed = "".join(
                        str(f.get("text", ""))
                        for f in frames
                        if f.get("kind") == "print" and f.get("pid") in client_pids
                    )
                    if not printed and argv[0].rsplit("/", 1)[-1] not in {"npm", "npx"}:
                        printed = stdout
                    printed_statuses = tuple(
                        sorted(
                            {
                                o.status_code
                                for o in observations
                                if re.search(
                                    r"(?<![0-9])" + str(o.status_code) + r"(?![0-9])", printed
                                )
                            }
                        )
                    )
                    status_printed = all(o.status_code in printed_statuses for o in observations)
                    ok = (
                        exit_code == 0
                        and bool(observations)
                        and all(o.matched for o in observations)
                        and (status_printed or not self.policy.require_selfcheck_status)
                    )
                    detail = (
                        "Selfcheck response independently observed, status printed, exit zero"
                        if ok
                        else "Selfcheck needs actual HTTP/status output and exit zero"
                    )
                    break
            except DomainError as exc:
                detail = exc.code
            time.sleep(0.02)
        if self.session.broker.workspace_tree_hash() != tree:
            ok, detail = False, "Workspace tree changed during lifecycle verification"
        if assets_hash() != self.policy.control_assets_hash:
            ok, detail = False, "Frozen lifecycle assets changed during verification"
        if token.cancelled:
            ok, detail = False, "Task cancelled during lifecycle observation"
        elapsed = time.monotonic() - started
        if elapsed > timeout:
            ok, detail = False, "Lifecycle observation deadline exceeded: " + detail
        ready_values = dict(
            ready=ok,
            observed_at=now(),
            task_id=self.task.task_id,
            run_id=str(self.task.run_id),
            command_id=operation_id,
            execution_epoch=self.task.execution_epoch,
            tree_hash=tree,
            environment_hash=handle.environment_hash,
            handle_id=handle.handle_id,
            port=actual_port,
            process_id=root.pid if root else None,
            group_id=root.group_id if root else None,
            log_port=log_port,
            observations=tuple(observations),
            snapshot=snapshot,
            detail=detail,
            printed_statuses=printed_statuses,
            observation_started_at=observation_started_at,
            duration_seconds=elapsed,
            timeout_seconds=timeout,
        )
        ready = sealed(ReadyReceipt, ready_values)
        cleanup = None
        if mode == "oneshot" or not ok:
            cleanup = self.cleanup(handle.handle_id, operation_id, tree, port=actual_port or 0)
        return handle, ServiceEvidence(
            mode=mode,
            assets_hash=self.policy.control_assets_hash,
            ready=ready,
            cleanup=cleanup,
            root_exit_code=exit_code,
            stdout_hash=hashlib.sha256(stdout_bytes).hexdigest(),
        )

    def entry(
        self,
        spec: EntryPointSpec,
        permission_argv: tuple[str, ...],
        grant: CapabilityGrant,
        operation_id: str,
        tree: str,
        token: CancellationToken,
    ) -> EntryPointReceipt:
        token.ensure_active()
        handle = self.launch(
            permission_argv,
            grant,
            operation_id,
            "entry",
            cwd=spec.cwd,
            timeout=self.policy.entry_timeout_seconds,
            entry=spec,
        )
        deadline = time.monotonic() + self.policy.entry_timeout_seconds
        ok = False
        code = None
        detail = "Entry load timed out"
        try:
            while time.monotonic() < deadline and not token.cancelled:
                output, frames, complete = self.wire(handle.handle_id)
                snapshot = self.session.runtime_snapshot(handle.handle_id)
                root = self.root(snapshot, frames)
                loaded = [
                    f
                    for f in frames
                    if f.get("kind") == "entry"
                    and f.get("path") == spec.path
                    and root
                    and f.get("pid") == root.pid
                ]
                if root and root.state == "Z":
                    code = root.exit_code
                    exited = [
                        f for f in frames
                        if f.get("kind") == "exit" and f.get("pid") == root.pid
                    ]
                    matching_exit = len(exited) == 1 and exited[0].get("exit_code") == code
                    if not exited or (complete and matching_exit and code == 0 and not loaded):
                        # The audit pipe and waitid() are independent. Either observation can
                        # arrive after this log prefix, even while the OS already reports exit.
                        detail = (f"Entry exited with code {code}. Waiting for matching "
                                  f"observations (exit frames={len(exited)}, "
                                  f"load frames={len(loaded)}).")
                        time.sleep(0.02)
                        continue
                    ok = (
                        code == 0
                        and time.monotonic() <= deadline
                        and complete
                        and matching_exit
                        and len(loaded) == 1
                        and loaded[0].get("loaded") is True
                        and (not spec.no_listen_on_import or not loaded[0].get("ports"))
                    )
                    detail = (
                        "Independent module load completed" if ok else output[-1500:] or (
                            f"Entry evidence invalid: exit={code}; log_complete={complete}; "
                            f"exit_frames={len(exited)}; load_frames={len(loaded)}; "
                            f"entry_loaded={loaded[0].get('loaded') if len(loaded) == 1 else None}"
                        )
                    )
                    break
                time.sleep(0.02)
        finally:
            cleanup = self.cleanup(handle.handle_id, operation_id, tree)
        final = self.session.poll(handle.handle_id)
        ok = (
            ok
            and not token.cancelled
            and cleanup.processes_exited is True
            and cleanup.port_released is True
            and cleanup.container_removed
            and not cleanup.orphan_pids
            and self.session.broker.workspace_tree_hash() == tree
            and assets_hash() == self.policy.control_assets_hash
        )
        values = dict(
            operation_id=operation_id,
            task_id=self.task.task_id,
            run_id=str(self.task.run_id),
            execution_epoch=self.task.execution_epoch,
            tree_hash=tree,
            spec=spec,
            status="PASS" if ok else "FAIL",
            loader_hash=self.policy.control_assets_hash,
            log_hash=final.log_sha256,
            exit_code=code,
            cleanup=cleanup,
            detail=detail,
        )
        return sealed(EntryPointReceipt, values)
