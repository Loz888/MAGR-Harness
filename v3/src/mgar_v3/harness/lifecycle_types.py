"""Frozen R04 control plans and observations, never new execution authority."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import Model


def _control_assets_hash() -> str:
    from mgar_v3.harness.lifecycle_assets import assets_hash

    return assets_hash()


class LifecyclePolicy(Model):
    version: Literal["harness-lifecycle-v1"] = "harness-lifecycle-v1"
    entry_timeout_seconds: float = Field(default=10, gt=0, le=60)
    shutdown_seconds: float = Field(default=5, gt=0, le=30)
    cleanup_seconds: float = Field(default=5, gt=0, le=30)
    require_ready_line: bool = True
    require_selfcheck_status: bool = True
    control_assets_hash: str = Field(
        default_factory=_control_assets_hash, pattern=r"^[a-f0-9]{64}$"
    )
    shutdown_exit_codes: tuple[int, ...] = (0, -15)


class EntryPointSpec(Model):
    path: str = Field(min_length=1, max_length=1024)
    runtime: Literal["python", "node"]
    cwd: str = ""
    import_roots: tuple[str, ...] = ("",)
    no_listen_on_import: bool = False

    @field_validator("path", "cwd")
    @classmethod
    def relative(cls, value: str) -> str:
        from mgar_v3.harness.contracts import normalize_relative_path

        return normalize_relative_path(value)

    @field_validator("import_roots")
    @classmethod
    def roots(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(cls.relative(value) for value in values)

    @model_validator(mode="after")
    def executable_file(self) -> Self:
        suffixes = (".py",) if self.runtime == "python" else (".js", ".cjs", ".mjs")
        if not self.path or not self.path.endswith(suffixes):
            raise ValueError("Entry must be an explicit file matching its loader runtime")
        return self


class HTTPExpectation(Model):
    path: str = Field(default="/", min_length=1, max_length=1024)
    status_code: int = Field(default=200, ge=100, le=599)
    content_type: str | None = Field(default=None, max_length=128)
    body: str | None = Field(default=None, max_length=8192)
    json_body: dict[str, Any] | None = None

    @field_validator("path")
    @classmethod
    def route(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or any(ord(c) < 32 for c in value):
            raise ValueError("Route must be a local HTTP path")
        return value


class RuntimeControl(Model):
    version: Literal["harness-lifecycle-v1"] = "harness-lifecycle-v1"
    mode: Literal["http", "oneshot", "entry"]
    entry: EntryPointSpec | None = None
    assets_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_entry(self) -> Self:
        if (self.mode == "entry") != (self.entry is not None):
            raise ValueError("Only entry control requires an entry specification")
        return self


class ProcessObservation(Model):
    pid: int
    parent_pid: int
    group_id: int
    session_id: int
    start_ticks: int
    state: str
    exit_code: int | None = None


class RuntimeSnapshot(Model):
    observed_at: str
    monotonic: float
    supervisor: ProcessObservation
    processes: tuple[ProcessObservation, ...]
    listeners: tuple[dict[str, Any], ...]
    bind_available: bool | None = None


class HTTPObservation(Model):
    path: str
    host: str = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    status_code: int
    content_type: str
    body_hash: str
    observed_at: str
    source: Literal["independent-container-probe", "instrumented-client"]
    matched: bool = True
    detail: str = ""
    body_bytes: int = 0
    body_artifact_hash: str | None = None


class ReadyReceipt(Model):
    receipt_id: str
    ready: bool
    observed_at: str
    task_id: str
    run_id: str
    command_id: str
    execution_epoch: int
    tree_hash: str
    environment_hash: str
    handle_id: str
    host: str = "127.0.0.1"
    port: int | None = Field(default=None, ge=1, le=65535)
    process_id: int | None = None
    group_id: int | None = None
    log_port: int | None = None
    observations: tuple[HTTPObservation, ...] = ()
    printed_statuses: tuple[int, ...] = ()
    observation_started_at: str = ""
    duration_seconds: float | None = None
    timeout_seconds: float | None = None
    snapshot: RuntimeSnapshot | None = None
    detail: str = ""
    acceptance_hash: str | None = None


class ProcessPortReceipt(Model):
    receipt_id: str
    observed_at: str
    command_id: str
    tree_hash: str
    signal: str
    graceful: bool
    forced: bool
    processes_exited: bool | None
    port_released: bool | None
    container_removed: bool
    orphan_pids: tuple[int, ...] = ()
    before: RuntimeSnapshot | None = None
    after: RuntimeSnapshot | None = None
    detail: str = ""
    signal_sent_at: str | None = None
    grace_seconds: float | None = None
    duration_seconds: float | None = None


class ServiceEvidence(Model):
    mode: Literal["http", "oneshot"]
    assets_hash: str
    ready: ReadyReceipt
    cleanup: ProcessPortReceipt | None = None
    root_exit_code: int | None = None
    raw_log_hash: str = ""
    stdout_hash: str = ""


class EntryPointReceipt(Model):
    receipt_id: str
    operation_id: str
    task_id: str
    run_id: str
    execution_epoch: int
    tree_hash: str
    spec: EntryPointSpec
    status: Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
    loader_hash: str
    log_hash: str
    exit_code: int | None
    cleanup: ProcessPortReceipt | None = None
    detail: str
    acceptance_hash: str | None = None


class EntryPointBatch(Model):
    operation_id: str
    tree_hash: str
    receipts: tuple[EntryPointReceipt, ...] = ()
    status: Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
    detail: str = ""
    targets: tuple[EntryPointSpec, ...] = ()
    inventory_hash: str = ""
    acceptance_hash: str | None = None
