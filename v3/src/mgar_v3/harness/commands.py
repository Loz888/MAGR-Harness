"""H06 command handles: run, poll, page logs, cancel and owned cleanup.

Every container is created by ``IsolatedExecutionBroker``.  This module never
accepts a raw docker command from a model: it only turns an already authorized
``ExecutionRequest`` into a handle, and it refuses to poll or stop a handle it
does not own.
"""

from __future__ import annotations

import base64
import codecs
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.harness.broker import IsolatedExecutionBroker, LaunchedCommand
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionProvenance,
    ExecutionRequest,
    sha256_text,
)
from mgar_v3.harness.diagnostics import CommandDiagnostics, merge_snapshot
from mgar_v3.harness.lifecycle_types import RuntimeSnapshot

CommandState = Literal["running", "exited", "timed_out", "cancelled", "failed", "revoked"]
CommandPurpose = Literal["test", "build", "command", "service"]

if TYPE_CHECKING:
    from mgar_v3.harness.recovery_io import RecoveryIO

_TOKEN = r"^[A-Za-z0-9_-]{1,100}$"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ReadinessProbe(Model):
    """A fixed, broker-executed probe inside the container network namespace."""

    kind: Literal["tcp", "http", "command"] = "tcp"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    path: str = "/"
    command: tuple[str, ...] = ()
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 10.0
    interval_seconds: Annotated[float, Field(gt=0, le=10)] = 0.25


class ReadinessResult(Model):
    ready: bool
    kind: str
    address: str
    attempts: Annotated[int, Field(ge=0)]
    detail: str = ""


class CommandHandle(Model):
    handle_id: str
    operation_id: str
    purpose: CommandPurpose
    argv: tuple[str, ...]
    cwd: str
    state: CommandState
    exit_code: int | None = None
    started_at: str
    deadline_seconds: float
    tree_hash: str
    environment_hash: str
    log_bytes: Annotated[int, Field(ge=0)] = 0
    log_truncated: bool = False
    log_sha256: str = ""
    provenance: ExecutionProvenance | None = None
    diagnostics: CommandDiagnostics | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class LogPage(Model):
    handle_id: str
    offset: Annotated[int, Field(ge=0)]
    next_offset: Annotated[int, Field(ge=0)]
    data: str
    eof: bool
    total_bytes: Annotated[int, Field(ge=0)]
    dropped_bytes: Annotated[int, Field(ge=0)] = 0
    truncated: bool = False
    provenance: ExecutionProvenance | None = None


class ServiceLaunch(Model):
    handle: CommandHandle
    address: str
    container_port: Annotated[int, Field(ge=1, le=65535)]
    # H04 containers use --network=none and publish no ports.  The address is
    # therefore reachable only inside the container network namespace.
    host_reachable: bool = False


@dataclass
class _Record:
    handle: CommandHandle
    launched: LaunchedCommand
    deadline: float
    token: CancellationToken
    buffer: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    # Full byte count reported by the broker, including bytes the lease cap
    # clipped.  Never derive this from the retained buffer.
    total_bytes: int = 0
    truncated: bool = False
    finished: bool = False
    logs_final: bool = False
    monitor: threading.Thread | None = None
    cleanup_deferred: bool = False
    diagnostics: CommandDiagnostics | None = None
    diagnostic_next_sample: float = 0.0


class CommandSession:
    """Owns command handles for one lease and cleans every container it starts."""

    def __init__(
        self,
        broker: IsolatedExecutionBroker,
        *,
        page_bytes: int = 16_384,
        max_buffer_bytes: int | None = None,
        recovery_io: RecoveryIO | None = None,
    ) -> None:
        self.broker = broker
        self.page_bytes = max(1, page_bytes)
        # Reading the limit must not spend a lease call: construction is not work.
        self.max_buffer_bytes = max_buffer_bytes or broker.authority.peek().max_output_bytes
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._closed = False
        self.recovery_io = recovery_io or broker.recovery_io

    # -- lifecycle -----------------------------------------------------

    def run(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
        *,
        purpose: CommandPurpose = "command",
    ) -> CommandHandle:
        self._ensure_open()
        cancellation.ensure_active()
        cached = (
            self.recovery_io.cached_command(request, grant)
            if self.recovery_io is not None
            else None
        )
        if cached is not None:
            assert self.recovery_io is not None
            launched = self.recovery_io.restore_launch(cached["launched"])
            handle = CommandHandle.model_validate(cached["handle"])
            raw = self.recovery_io.artifacts.read(cached["raw_log_hash"])
            record = _Record(
                handle=handle,
                launched=launched,
                deadline=time.monotonic(),
                token=cancellation,
                buffer=bytearray(raw),
                dropped=cached.get("dropped_bytes", 0),
                total_bytes=cached["total_bytes"],
                truncated=handle.log_truncated,
                finished=True,
                logs_final=True,
            )
            with self._lock:
                self._records[handle.handle_id] = record
            return handle
        launched = (
            self.recovery_io.launch_command(self.broker, request, grant, cancellation)
            if self.recovery_io is not None
            else self.broker.launch(request, grant, cancellation)
        )
        try:
            self.broker.require_current_provenance(launched.provenance)
        except DomainError:
            self.broker.stop_command(launched.name)
            raise
        handle = CommandHandle(
            handle_id=uuid.uuid4().hex,
            operation_id=request.operation_id,
            purpose=purpose,
            argv=tuple(request.argv),
            cwd=request.cwd,
            state="running",
            started_at=_utc_now(),
            deadline_seconds=launched.timeout_seconds,
            tree_hash=launched.tree_hash,
            environment_hash=launched.environment_hash,
            provenance=launched.provenance,
        )
        record = _Record(
            handle=handle,
            launched=launched,
            deadline=time.monotonic() + launched.timeout_seconds,
            token=cancellation,
        )
        self._sample_resources(record)
        thread = threading.Thread(target=self._monitor, args=(record,), daemon=True)
        record.monitor = thread
        with self._lock:
            self._records[handle.handle_id] = record
        thread.start()
        return handle

    def poll(self, handle_id: str) -> CommandHandle:
        with self._lock:
            record = self._record(handle_id)
            self._refresh(record)
            return record.handle

    def read_log(self, handle_id: str, *, offset: int = 0, limit: int | None = None) -> LogPage:
        with self._lock:
            record = self._record(handle_id)
            self._refresh(record)
            provenance = self.assert_authority(handle_id)
            size = max(1, min(limit or self.page_bytes, self.page_bytes))
            data = bytes(record.buffer)
            dropped = record.dropped
            retained = dropped + len(data)
            total = max(record.total_bytes, retained)
            start = max(0, offset)
            if start < dropped:
                # The requested prefix was discarded to keep memory bounded;
                # resume at the oldest retained byte instead of blocking.
                start = dropped
            relative = start - dropped
            chunk = data[relative : relative + size]
            # Preserve an incomplete trailing UTF-8 character for the next
            # page rather than replacing each half independently.  Byte
            # offsets and the page bound still refer to retained raw bytes.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            text = decoder.decode(
                chunk, final=record.finished and relative + len(chunk) >= len(data)
            )
            pending, _ = decoder.getstate()
            if pending:
                chunk = chunk[: -len(pending)]
                if not chunk:
                    raise DomainError(
                        "HARNESS_LOG_PAGE_TOO_SMALL", "Page cannot fit one UTF-8 character", 422
                    )
            next_offset = start + len(chunk)
            page = LogPage(
                handle_id=handle_id,
                offset=start,
                next_offset=next_offset,
                data=text,
                # eof covers every retained byte; a truncated tail is reported
                # through ``truncated``/``total_bytes`` instead of withholding
                # eof forever.
                eof=record.finished and next_offset >= retained,
                total_bytes=total,
                dropped_bytes=dropped,
                truncated=record.truncated,
                provenance=provenance,
            )
            self.assert_authority(handle_id)
            return page

    def assert_authority(self, handle_id: str) -> ExecutionProvenance:
        """A finished buffer retains the authorization of the real launch."""
        with self._lock:
            record = self._record(handle_id)
            provenance = record.launched.provenance
            self.broker.require_current_provenance(provenance)
            assert provenance is not None
            return provenance

    def wait(self, handle_id: str, *, timeout: float | None = None) -> CommandHandle:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            handle = self.poll(handle_id)
            if handle.state != "running":
                return handle
            if deadline is not None and time.monotonic() >= deadline:
                return handle
            time.sleep(0.02)

    def cancel(self, handle_id: str) -> CommandHandle:
        with self._lock:
            record = self._record(handle_id)
            record.token.cancel()
            self._finish(record, "cancelled", 137)
            return record.handle

    def close(self) -> int:
        """Stop every container this session started; return the count."""

        with self._lock:
            self._closed = True
            stopped = 0
            for record in self._records.values():
                if not record.finished:
                    self._finish(record, "cancelled", 137)
                if record.cleanup_deferred:
                    stopped += 1
                    continue
                if record.launched.identity is not None:
                    self.broker.remove_owned_command(record.launched.identity)
                else:
                    self.broker.stop_command(record.launched.name)
                stopped += 1
            return stopped

    # -- services ------------------------------------------------------

    def runtime_snapshot(
        self, handle_id: str, *, port: int = 0, action: str = "inspect",
    ) -> RuntimeSnapshot:
        """Fixed OS observer for an exact owned supervised process namespace."""
        from mgar_v3.harness.lifecycle_assets import PROCESS_PROBE, asset_sources

        if not 0 <= port <= 65535 or not (
            action in {"inspect", "kill"}
            or action.startswith("release:") and action[8:].isdigit()
        ):
            raise DomainError("HARNESS_LIFECYCLE_ARGUMENT", "Invalid controller probe", 422)
        with self._lock:
            record = self._record(handle_id)
            if record.launched.runtime_control is None:
                raise DomainError(
                    "HARNESS_CONTROL_SESSION_REQUIRED", "Not a supervised handle", 409
                )
            self.assert_authority(handle_id)
            if record.launched.identity is not None:
                self.broker.owned_command_state(record.launched.identity)
            name = record.launched.name
        code, raw = self.broker.exec_probe(name, (
            "python3", "-I", "-c", PROCESS_PROBE,
            sha256_text(asset_sources()["supervisor"]), action, str(port),
        ), timeout=3)
        self.assert_authority(handle_id)
        if code:
            raise DomainError("HARNESS_PROCESS_OBSERVATION_UNAVAILABLE", "OS probe failed", 409)
        try:
            return RuntimeSnapshot.model_validate_json(raw)
        except ValueError as exc:
            raise DomainError(
                "HARNESS_PROCESS_OBSERVATION_INVALID", "OS receipt invalid", 409
            ) from exc

    def signal_service(self, handle_id: str) -> None:
        with self._lock:
            record = self._record(handle_id)
            self.assert_authority(handle_id)
            if record.launched.identity is not None:
                self.broker.owned_command_state(record.launched.identity)
            if record.launched.runtime_control is None:
                raise DomainError(
                    "HARNESS_CONTROL_SESSION_REQUIRED", "Not a supervised handle", 409
                )
            self.broker._docker(["kill", "--signal=SIGTERM", record.launched.name])

    def controlled_container_removed(self, handle_id: str) -> bool:
        with self._lock:
            record = self._record(handle_id)
            if record.launched.identity is not None:
                state, _ = self.broker.owned_command_state(record.launched.identity)
            else:
                state, _ = self.broker.inspect_command(record.launched.name)
            return state == "missing"

    def start_service(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
        probe: ReadinessProbe,
    ) -> ServiceLaunch:
        handle = self.run(request, grant, cancellation, purpose="service")
        readiness = self.wait_ready(handle.handle_id, probe)
        if not readiness.ready:
            self.cancel(handle.handle_id)
            raise DomainError(
                "HARNESS_SERVICE_NOT_READY",
                f"Service did not become ready: {readiness.detail}",
                504,
            )
        return ServiceLaunch(
            handle=self.poll(handle.handle_id),
            address=f"127.0.0.1:{probe.port}",
            container_port=probe.port,
            host_reachable=False,
        )

    def wait_ready(self, handle_id: str, probe: ReadinessProbe) -> ReadinessResult:
        deadline = time.monotonic() + probe.timeout_seconds
        attempts = 0
        detail = "probe did not run"
        address = f"127.0.0.1:{probe.port}"
        while time.monotonic() < deadline:
            handle = self.poll(handle_id)
            if handle.state not in {"running", "exited"}:
                return ReadinessResult(
                    ready=False,
                    kind=probe.kind,
                    address=address,
                    attempts=attempts,
                    detail=f"process ended as {handle.state}",
                )
            attempts += 1
            ok, detail = self._probe(handle_id, probe)
            if ok:
                return ReadinessResult(
                    ready=True, kind=probe.kind, address=address, attempts=attempts
                )
            if handle.state != "running":
                # A short-lived process may have already exited successfully;
                # the probe result above is the only readiness evidence.
                break
            time.sleep(probe.interval_seconds)
        return ReadinessResult(
            ready=False, kind=probe.kind, address=address, attempts=attempts, detail=detail
        )

    def fetch_http(
        self, handle_id: str, *, port: int, path: str = "/", max_bytes: int = 1_048_576
    ) -> tuple[int, str, bytes]:
        """Fetch one bounded response from this session's own service container.

        No host port is published and no browser headers or credentials enter
        the container. Redirects are deliberately not followed: a project
        cannot turn preview into an arbitrary network client.
        """
        if (
            not 1 <= port <= 65535 or not 1 <= max_bytes <= 2_097_152
            or not path.startswith("/") or path.startswith("//")
            or len(path) > 4096 or any(ord(character) < 32 for character in path)
        ):
            raise DomainError("HARNESS_PREVIEW_PATH", "Invalid preview request", 422)
        with self._lock:
            record = self._record(handle_id)
            if record.handle.purpose != "service":
                raise DomainError("HARNESS_PREVIEW_DENIED", "Handle is not a service", 403)
            self.assert_authority(handle_id)
            if self.poll(handle_id).state != "running":
                raise DomainError("HARNESS_PREVIEW_STOPPED", "Preview process has stopped", 409)
            name = record.launched.name
        script = (
            "import base64,json,sys,urllib.request,urllib.error\n"
            "class NoRedirect(urllib.request.HTTPRedirectHandler):\n"
            " def redirect_request(self,*args,**kwargs): return None\n"
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())\n"
            "try:\n"
            " response=opener.open('http://127.0.0.1:'+sys.argv[1]+sys.argv[2],timeout=3)\n"
            "except urllib.error.HTTPError as error: response=error\n"
            "with response:\n"
            " body=response.read(int(sys.argv[3])+1)\n"
            " if len(body)>int(sys.argv[3]): raise ValueError('preview response exceeds limit')\n"
            " print(json.dumps({'status':response.status,'type':response.headers.get("
            "'Content-Type','application/octet-stream'),'body':base64.b64encode(body).decode()}))\n"
        )
        code, output = self.broker.exec_probe(
            name, ("python3", "-c", script, str(port), path, str(max_bytes)), timeout=5
        )
        self.assert_authority(handle_id)
        if code:
            raise DomainError("HARNESS_PREVIEW_FETCH", "Preview response unavailable", 502)
        try:
            payload = json.loads(output)
            body = base64.b64decode(payload["body"], validate=True)
            status = int(payload["status"])
            content_type = str(payload["type"])
            if len(body) > max_bytes or not 100 <= status <= 599 or len(content_type) > 256:
                raise ValueError("invalid preview response")
            if any(ord(character) < 32 for character in content_type):
                raise ValueError("invalid content type")
        except (KeyError, ValueError, TypeError) as exc:
            raise DomainError("HARNESS_PREVIEW_FETCH", "Invalid preview response", 502) from exc
        return status, content_type, body

    # -- internals -----------------------------------------------------

    def _monitor(self, record: _Record) -> None:
        while True:
            with self._lock:
                if record.finished:
                    return
                remaining = record.deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._finish(record, "timed_out", 124)
                return
            if self._lease_lost(record):
                with self._lock:
                    self._finish(record, "revoked", 137)
                return
            time.sleep(min(0.05, max(0.005, remaining)))

    def _refresh(self, record: _Record) -> None:
        if record.finished and record.logs_final:
            if self._lease_lost(record):
                record.handle = record.handle.model_copy(
                    update={"state": "revoked", "exit_code": 137}
                )
            return
        if record.token.cancelled and not record.finished:
            self._finish(record, "cancelled", 137)
            return
        if not record.finished and self._lease_lost(record):
            self._finish(record, "revoked", 137)
            return
        status, exit_code = self.broker.inspect_command(record.launched.name)
        if status == "missing":
            if not record.finished:
                self._finish(record, "failed", None)
            return
        if status in {"exited", "dead"}:
            if not record.finished:
                state: CommandState = "exited" if exit_code == 0 else "failed"
                self._finish(record, state, exit_code)
            return
        if time.monotonic() >= record.diagnostic_next_sample:
            self._sample_resources(record)
        self._pull_logs(record)

    def _lease_lost(self, record: _Record) -> bool:
        return self.broker.command_revoked(
            record.launched.lease_id,
            record.launched.execution_epoch,
            lease_hash=(
                record.launched.provenance.lease_hash
                if record.launched.provenance is not None
                else None
            ),
        )

    def _finish(self, record: _Record, state: CommandState, exit_code: int | None) -> None:
        if record.finished:
            return
        self._sample_resources(record, state=state)
        if self.recovery_io is not None and record.launched.identity is not None:
            # Stop first but retain the container until its final output and
            # receipt are durable; a SIGKILL leaves it available for recovery.
            self.broker.halt_owned_command(record.launched.identity)
        self._pull_logs(record, final=True)
        record.handle = record.handle.model_copy(
            update={
                "state": state,
                "exit_code": exit_code,
                "log_bytes": record.total_bytes,
                "log_truncated": record.truncated or record.dropped > 0,
                "log_sha256": sha256_text(bytes(record.buffer).decode("utf-8", "replace")),
                "diagnostics": record.diagnostics,
            }
        )
        if self.recovery_io is not None:
            try:
                self.recovery_io.finish_command(
                    self.broker,
                    record.launched,
                    handle=record.handle.model_dump(mode="json"),
                    raw_logs=bytes(record.buffer),
                    total_bytes=record.total_bytes,
                    dropped_bytes=record.dropped,
                )
            except DomainError:
                try:
                    self.recovery_io.store.guard(
                        self.recovery_io.task_id,
                        self.recovery_io.execution_epoch,
                    )
                except DomainError:
                    # A fenced owner may stop its exact owned child, but must
                    # neither publish a result nor delete unarchived output.
                    # The paused-task reconciler owns the retained container.
                    record.cleanup_deferred = True
                    record.finished = True
                    return
                raise
        else:
            self.broker.stop_command(record.launched.name)
        record.finished = True

    def _sample_resources(self, record: _Record, *, state: CommandState = "running") -> None:
        # Runtime sampling is fixed and bounded, and adds no model tool or
        # caller-provided path/command. Keep only first/last/peaks, never a log.
        method = getattr(self.broker, "command_resource_snapshot", None)
        if method is None:  # Older adapters retain their exact receipt shape.
            return
        snapshot = method(record.launched)
        reason = (
            state if state in {"timed_out", "cancelled", "revoked"}
            else "missing" if state == "failed" and snapshot.container_state == "unavailable"
            else "running" if state == "running" else "natural_exit"
        )
        record.diagnostics = merge_snapshot(record.diagnostics, snapshot, stop_reason=reason)
        record.diagnostic_next_sample = time.monotonic() + 0.25

    def _pull_logs(self, record: _Record, *, final: bool = False) -> None:
        if record.logs_final:
            return
        data, total = self.broker.command_logs(
            record.launched.name, max_bytes=record.launched.max_output_bytes
        )
        # Keep the broker's real total: it is the only value that can tell a
        # lease-clipped log apart from one that was always that short.
        record.total_bytes = max(record.total_bytes, total)
        if total > len(data):
            # The lease cap clipped the tail; never report a shortened log as
            # complete just because the in-memory buffer did not overflow.
            record.truncated = True
        if len(data) > len(record.buffer) + record.dropped:
            fresh = data[len(record.buffer) + record.dropped :]
            record.buffer.extend(fresh)
        overflow = len(record.buffer) - self.max_buffer_bytes
        if overflow > 0:
            del record.buffer[:overflow]
            record.dropped += overflow
        if final:
            record.logs_final = True

    def _probe(self, handle_id: str, probe: ReadinessProbe) -> tuple[bool, str]:
        record = self._record(handle_id)
        if probe.kind == "command":
            if not probe.command:
                return False, "command probe needs an argv"
            argv = list(probe.command)
        elif probe.kind == "tcp":
            argv = [
                "python3",
                "-c",
                (
                    "import socket,sys\n"
                    "s=socket.socket();s.settimeout(1)\n"
                    "s.connect(('127.0.0.1',int(sys.argv[1])))\n"
                    "print('ready')\n"
                ),
                str(probe.port),
            ]
        else:
            argv = [
                "python3",
                "-c",
                (
                    "import sys,urllib.request\n"
                    "url='http://127.0.0.1:' + sys.argv[1] + sys.argv[2]\n"
                    "urllib.request.urlopen(url, timeout=1)\n"
                    "print('ready')\n"
                ),
                str(probe.port),
                probe.path,
            ]
        try:
            code, output = self.broker.exec_probe(
                record.launched.name, argv, timeout=min(2.0, probe.interval_seconds + 1.5)
            )
        except DomainError as exc:
            return False, str(exc)
        return code == 0, output.decode("utf-8", "replace")[-500:]

    def _record(self, handle_id: str) -> _Record:
        if self._closed:
            raise DomainError("HARNESS_SESSION_CLOSED", "Command session is closed", 409)
        record = self._records.get(handle_id)
        if record is None:
            raise DomainError(
                "HARNESS_COMMAND_NOT_FOUND", "Command handle is not owned by this session", 404
            )
        return record

    def _ensure_open(self) -> None:
        if self._closed:
            raise DomainError("HARNESS_SESSION_CLOSED", "Command session is closed", 409)
