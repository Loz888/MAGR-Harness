"""H04 capability leases and the single real-container execution broker.

Every built-in or future tool that needs a process goes through
``IsolatedExecutionBroker``.  The lease is the authority: actor, workspace,
read/write scope, command allowlist, network intent, timeout, resource limits,
credentials and external-write permission.  The project process receives none
of the host environment or credentials and runs in a real container with a
read-only root, no network, a non-root uid and cgroup limits.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionProvenance,
    ExecutionReceipt,
    ExecutionRequest,
    canonical_hash,
    normalize_relative_path,
    sha256_text,
)
from mgar_v3.harness.diagnostics import ResourceSnapshot
from mgar_v3.harness.lifecycle_types import RuntimeControl
from mgar_v3.harness.public_probe import pinned_asset
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.workspace import _open_read_target, _scan, _tree_hash

LEASE_VERSION = "harness-capability-lease-v1"
SANDBOX_UID = "65532:65532"
DEFAULT_IMAGE_LOCK = Path(".local/r03/sandbox-lock.json")


_MASK_LOCK = threading.Lock()
_MASK_FILE: str | None = None

if TYPE_CHECKING:
    from mgar_v3.harness.recovery_io import RecoveryIO


def _now() -> datetime:
    return datetime.now(UTC)


class CapabilityLease(Model):
    """One explicit, scoped capability grant for a task workspace."""

    schema_version: str = LEASE_VERSION
    lease_id: str
    mode: Literal["investigate", "modify_test", "publish"]
    actor: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str
    execution_epoch: int = Field(ge=1)
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...]
    network_hosts: tuple[str, ...] = ()
    offline_network: Literal["none", "connect-timeout-v1", "got-network-v1"] = Field(
        default="none", exclude_if=lambda value: value == "none"
    )
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_output_bytes: int = Field(default=65536, ge=1024, le=1_048_576)
    memory_mb: int = Field(default=256, ge=32, le=4096)
    cpus: float = Field(default=1.0, gt=0, le=8)
    pids: int = Field(default=64, ge=8, le=1024)
    nofile_limit: int = Field(default=128, ge=128, le=4096, exclude_if=lambda value: value == 128)
    scratch_mb: int = Field(default=64, ge=16, le=2048)
    credentials: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = (".git", ".mgar", ".env")
    max_calls: int = Field(default=100, ge=1, le=100_000)
    external_write: bool = False
    approved_by: str = ""
    approval_ref: str = ""
    issued_at: str
    expires_at: str
    lease_hash: str

    def computed_hash(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"lease_hash"}))

    def expired(self, moment: datetime | None = None) -> bool:
        return datetime.fromisoformat(self.expires_at) <= (moment or _now())


def issue_lease(
    *,
    mode: Literal["investigate", "modify_test", "publish"],
    actor: str,
    workspace_id: str,
    repository: str,
    execution_epoch: int,
    read_paths: Sequence[str] = ("",),
    write_paths: Sequence[str] = (),
    allowed_commands: Sequence[str] = ("cat", "ls"),
    network_hosts: Sequence[str] = (),
    offline_network: Literal["none", "connect-timeout-v1", "got-network-v1"] = "none",
    timeout_seconds: int = 60,
    max_output_bytes: int = 65536,
    memory_mb: int = 256,
    cpus: float = 1.0,
    pids: int = 64,
    nofile_limit: int = 128,
    scratch_mb: int = 64,
    credentials: Sequence[str] = (),
    protected_paths: Sequence[str] = (".git", ".mgar", ".env"),
    max_calls: int = 100,
    approved_by: str = "",
    approval_ref: str = "",
    ttl_seconds: int = 3600,
) -> CapabilityLease:
    """Issue one preset lease and fail closed for unsafe combinations."""

    if network_hosts:
        raise DomainError(
            "HARNESS_LEASE_DENIED",
            "H04 does not authorize network egress",
            403,
        )
    if mode == "investigate":
        if write_paths:
            raise DomainError("HARNESS_LEASE_DENIED", "Investigate leases cannot write", 403)
        if approved_by or approval_ref:
            raise DomainError(
                "HARNESS_LEASE_DENIED", "Investigate leases do not accept write approval", 422
            )
        external_write = False
    elif mode == "modify_test":
        if not write_paths:
            raise DomainError(
                "HARNESS_LEASE_DENIED", "Writable mode requires explicit write paths", 403
            )
        if not approved_by or not approval_ref:
            raise DomainError(
                "HARNESS_LEASE_DENIED", "First writable scope needs explicit approval", 403
            )
        external_write = False
    else:
        if not approved_by or not approval_ref:
            raise DomainError("HARNESS_LEASE_DENIED", "Publish mode needs explicit approval", 403)
        external_write = True
    issued = _now()
    lease = CapabilityLease(
        lease_id="",
        mode=mode,
        actor=actor,
        workspace_id=workspace_id,
        repository=repository.lower(),
        execution_epoch=execution_epoch,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
        allowed_commands=tuple(allowed_commands),
        network_hosts=tuple(network_hosts),
        offline_network=offline_network,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        memory_mb=memory_mb,
        cpus=cpus,
        pids=pids,
        nofile_limit=nofile_limit,
        scratch_mb=scratch_mb,
        credentials=tuple(credentials),
        protected_paths=tuple(protected_paths),
        max_calls=max_calls,
        external_write=external_write,
        approved_by=approved_by,
        approval_ref=approval_ref,
        issued_at=issued.isoformat(),
        expires_at=(issued + timedelta(seconds=ttl_seconds)).isoformat(),
        lease_hash="",
    )
    lease = lease.model_copy(update={"lease_id": lease.computed_hash()[:32]})
    return lease.model_copy(update={"lease_hash": lease.computed_hash()})


def _docker_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
    }


def _masked_file() -> str:
    """A host file used to mask an existing protected file.

    ``/dev/null`` accepted writes even through a read-only bind, so a project
    command could "write" its way past a protected file.  An unreadable empty
    regular file gives both reads and writes ``EACCES`` while the real file
    stays untouched.
    """

    global _MASK_FILE
    with _MASK_LOCK:
        if _MASK_FILE is None:
            directory = Path(tempfile.mkdtemp(prefix="mgar-harness-mask-"))
            path = directory / "masked"
            path.write_bytes(b"")
            path.chmod(0o000)
            _MASK_FILE = str(path)
        return _MASK_FILE


def _within(path: str, roots: Sequence[str]) -> bool:
    normalized = normalize_relative_path(path)
    for root in roots:
        prefix = normalize_relative_path(root)
        if prefix == "" or normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _intersect(paths: Sequence[str], other: Sequence[str]) -> tuple[str, ...]:
    """Return the most specific root shared by both authorization scopes."""

    result: set[str] = set()
    for path in paths:
        left = normalize_relative_path(path)
        for candidate in other:
            right = normalize_relative_path(candidate)
            if _within(left, (right,)):
                result.add(left)
            elif _within(right, (left,)):
                result.add(right)
    return tuple(sorted(result))


def _is_protected(path: str, protected_paths: Sequence[str]) -> bool:
    normalized = normalize_relative_path(path)
    for protected in protected_paths:
        prefix = normalize_relative_path(protected)
        if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
            return True
    return False


class CapabilityAuthority:
    """Current trusted lease plus revocation and budget checks.

    The Broker and every built-in tool read the lease through this object on
    each call, so an expired, revoked or replaced lease cannot be bypassed by
    holding an old context or a previously constructed Broker.  A production
    integration can pass ``lease_source`` so the current lease is re-read from
    the trusted authority on every call; ``replace`` supports local wiring.
    """

    def __init__(
        self,
        lease: CapabilityLease | None = None,
        *,
        lease_source: Callable[[], CapabilityLease] | None = None,
        budget_check: Callable[[], bool],
        revocation_check: Callable[[], bool] | None = None,
    ) -> None:
        if (lease is None) == (lease_source is None):
            raise DomainError(
                "HARNESS_AUTHORITY_REQUIRED",
                "Provide exactly one of lease or lease_source",
                409,
            )
        self._lease = lease
        self._lease_source = lease_source
        self._budget_check = budget_check
        self._revocation_check = revocation_check
        self._revoked = False

    def revoke(self) -> None:
        self._revoked = True

    def replace(self, lease: CapabilityLease) -> None:
        if self._lease_source is not None:
            raise DomainError(
                "HARNESS_AUTHORITY_DENIED",
                "A dynamic lease source cannot be replaced locally",
                409,
            )
        self._lease = lease

    def current(self) -> CapabilityLease:
        lease = self.peek()
        if not self._budget_check():
            raise DomainError("HARNESS_BUDGET_EXHAUSTED", "Tool budget is exhausted", 429)
        return lease

    def peek(self) -> CapabilityLease:
        """Return the validated lease without charging the call budget.

        Runtime liveness checks (a command handle noticing that its lease was
        revoked, expired or replaced) must not spend the model/actor budget:
        they observe state, they do not perform work.
        """

        lease = self._lease_source() if self._lease_source is not None else self._lease
        if lease is None:
            raise DomainError("HARNESS_AUTHORITY_REQUIRED", "No current lease", 409)
        if lease.lease_hash != lease.computed_hash():
            raise DomainError("HARNESS_LEASE_DENIED", "Lease hash changed", 409)
        if self._revoked or (self._revocation_check and self._revocation_check()):
            raise DomainError("HARNESS_LEASE_REVOKED", "Capability lease was revoked", 403)
        if lease.expired():
            raise DomainError("HARNESS_LEASE_EXPIRED", "Capability lease has expired", 403)
        return lease

    def check_scope(
        self,
        *,
        actor: str,
        workspace_id: str,
        repository: str,
        execution_epoch: int,
        paths: Sequence[str],
        write: bool,
    ) -> CapabilityLease:
        lease = self.current()
        if actor != lease.actor:
            raise DomainError("HARNESS_ACTOR_MISMATCH", "Actor does not match lease", 403)
        if workspace_id != lease.workspace_id:
            raise DomainError("HARNESS_WORKSPACE_MISMATCH", "Workspace does not match lease", 403)
        if repository.lower() != lease.repository:
            raise DomainError("HARNESS_REPOSITORY_MISMATCH", "Repository does not match lease", 403)
        if execution_epoch != lease.execution_epoch:
            raise DomainError("HARNESS_EPOCH_MISMATCH", "Execution epoch is stale", 409)
        if write and lease.mode == "investigate":
            raise DomainError("HARNESS_WRITE_DENIED", "Investigate lease cannot write", 403)
        roots = lease.write_paths if write else lease.read_paths
        for path in paths:
            if _is_protected(path, lease.protected_paths):
                raise DomainError(
                    "HARNESS_PROTECTED_PATH_DENIED",
                    "Path is protected workspace metadata",
                    403,
                )
            if not _within(path, roots):
                raise DomainError(
                    "HARNESS_WRITE_DENIED" if write else "HARNESS_READ_DENIED",
                    "Path is outside the leased scope",
                    403,
                )
        return lease


@dataclass(frozen=True)
class CommandIdentity:
    """Trusted Engine operation identity, never supplied by a project command."""

    task_id: str
    run_id: str
    operation_id: str
    execution_epoch: int
    workspace_hash: str

    @property
    def identity_hash(self) -> str:
        return digest(self.labels_payload())

    def labels_payload(self) -> dict[str, str]:
        return {
            "task": self.task_id,
            "run": self.run_id,
            "operation": self.operation_id,
            "epoch": str(self.execution_epoch),
            "workspace": self.workspace_hash,
        }

    @property
    def name(self) -> str:
        return "mgar-h11-" + self.identity_hash[:32]

    def labels(self) -> dict[str, str]:
        return {
            **{"mgar.v4.h11." + key: value for key, value in self.labels_payload().items()},
            "mgar.v4.h11.identity": self.identity_hash,
        }


@dataclass(frozen=True)
class LaunchedCommand:
    """One container that was created and started by the broker.

    The container name is an internal capability: callers poll, page logs and
    stop only through the broker, never with their own docker command.
    """

    name: str
    lease_id: str
    execution_epoch: int
    timeout_seconds: float
    max_output_bytes: int
    tree_hash: str
    environment_hash: str
    provenance: ExecutionProvenance | None = None
    identity: CommandIdentity | None = None
    runtime_control: RuntimeControl | None = None


class IsolatedExecutionBroker:
    """The only process execution entry for H04 leases."""

    def __init__(
        self,
        workspace_root: Path,
        lease: CapabilityLease | CapabilityAuthority,
        *,
        image_id: str | None = None,
        docker: str = "docker",
        credentials: Mapping[str, str] | None = None,
        budget_check: Callable[[], bool] | None = None,
        lock_path: Path = DEFAULT_IMAGE_LOCK,
        allow_internal_write_symlinks: bool = False,
        allow_scoped_root_cwd: bool = False,
        public_probe: PublicProbePlan | None = None,
        public_probe_root: Path | None = None,
    ) -> None:
        self.root = Path(workspace_root).resolve()
        self.allow_scoped_root_cwd = allow_scoped_root_cwd
        if self.root.is_symlink() or not self.root.is_dir():
            raise DomainError("HARNESS_WORKSPACE_DENIED", "Workspace root is unavailable", 409)
        if isinstance(lease, CapabilityAuthority):
            self.authority = lease
        else:
            if budget_check is None:
                # A missing external ledger does not mean unlimited spending:
                # the lease's own bounded call counter still fails closed.
                remaining = {"calls": 0}

                def lease_budget() -> bool:
                    remaining["calls"] += 1
                    return remaining["calls"] <= lease.max_calls

                budget_check = lease_budget
            self.authority = CapabilityAuthority(lease, budget_check=budget_check)
        self.docker = docker
        self.image_id = image_id or self._image_from_lock(lock_path)
        # A trusted product composition option, never model/API input. It does
        # not extend the lease: alias and resolved target must both already be
        # writable. Frozen/default integrations retain their strict behavior.
        self.allow_internal_write_symlinks = allow_internal_write_symlinks
        if (public_probe is None) != (public_probe_root is None):
            raise DomainError(
                "PUBLIC_PROBE_CONFIGURATION", "A public probe needs its frozen plan and root", 403
            )
        if public_probe_root is not None and public_probe_root.resolve().is_relative_to(self.root):
            raise DomainError(
                "PUBLIC_PROBE_ASSET_DENIED", "Controller assets must be outside the workspace", 403
            )
        self.public_probe, self.public_probe_root = public_probe, public_probe_root
        # Credentials stay in the Broker; only names appear in the lease and
        # no value is ever added to the project container environment.
        self._credentials = dict(credentials or {})
        # Mount targets that the broker had to create inside the workspace so a
        # mask can cover a missing protected path.  They are owned by the
        # workspace user, empty and removed as soon as the container is
        # removed, so docker never leaves root-owned placeholders behind.
        self._placeholders: dict[str, list[Path]] = {}
        self._placeholder_lock = threading.Lock()
        self._mount_fds: dict[str, list[int]] = {}
        self._offline_names: set[str] = set()
        self.recovery_io: RecoveryIO | None = None

    def bind_recovery(self, recovery_io: RecoveryIO) -> None:
        if recovery_io.workspace.root.resolve() != self.root:
            raise DomainError("RECOVERY_COMMAND_OWNER", "Recovery workspace differs", 409)
        self.recovery_io = recovery_io

    @staticmethod
    def _image_from_lock(lock_path: Path) -> str:
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            image_id = str(lock["image_id"])
        except (OSError, ValueError, KeyError) as exc:
            raise DomainError(
                "SANDBOX_NOT_PREPARED", "A pinned sandbox image is required", 409
            ) from exc
        if not image_id.startswith("sha256:"):
            raise DomainError("SANDBOX_NOT_PREPARED", "Sandbox image id is not pinned", 409)
        return image_id

    def execute(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ExecutionReceipt:
        if self.recovery_io is not None:
            return self.recovery_io.execute_command(self, request, grant, cancellation)
        cancellation.ensure_active()
        if request.runtime_control is not None:
            raise DomainError(
                "HARNESS_CONTROL_SESSION_REQUIRED", "Use the owned command session", 409
            )
        lease, read_paths, write_paths = self._authorize(request, grant)
        provenance = self._provenance(request, grant, lease, read_paths, write_paths)
        name = "mgar-h04-" + uuid.uuid4().hex[:16]
        create_args, placeholders = self._create_args(name, request, lease, read_paths, write_paths)
        self._register_placeholders(name, placeholders)
        try:
            created = self._docker(create_args)
        except BaseException:
            self._remove_placeholders(name)
            raise
        if created.returncode != 0:
            self._remove_placeholders(name)
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Restricted container could not be created", 502
            )
        started = time.monotonic()
        status: str = "error"
        stdout = b""
        exit_code = 1
        try:
            try:
                completed = subprocess.run(
                    [self.docker, "start", "-a", name],
                    capture_output=True,
                    env=_docker_environment(),
                    timeout=min(
                        request.timeout_seconds,
                        lease.timeout_seconds,
                        grant.max_seconds,
                    ),
                    check=False,
                )
                stdout = completed.stdout + completed.stderr
                exit_code = completed.returncode
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                stdout = (exc.stdout or b"")[: lease.max_output_bytes]
                status = "timeout"
                exit_code = 124
        finally:
            subprocess.run(
                [self.docker, "rm", "-f", name],
                capture_output=True,
                env=_docker_environment(),
                check=False,
            )
            self._remove_placeholders(name)
        duration = time.monotonic() - started
        text = stdout.decode("utf-8", "replace")[: lease.max_output_bytes]
        receipt = ExecutionReceipt(
            receipt_id=digest(
                {
                    "lease_id": lease.lease_id,
                    "operation_id": request.operation_id,
                    "argv": list(request.argv),
                    "status": status,
                    "exit_code": exit_code,
                    "output": sha256_text(text),
                    "provenance": provenance.model_dump(mode="json"),
                }
            ),
            operation_id=request.operation_id,
            purpose=request.purpose,
            status=cast(Any, status),
            argv=request.argv,
            cwd=request.cwd,
            exit_code=exit_code,
            stdout=text,
            stderr="",
            duration_seconds=duration,
            tree_hash=digest({"workspace": str(self.root), "files": sorted(os.listdir(self.root))}),
            log_sha256=sha256_text(text),
            environment_hash=digest({"env": ["PATH", "HOME", "LANG"], "credentials": []}),
            provenance=provenance,
        )
        self.require_current_provenance(provenance)
        return receipt

    def launch(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
        *,
        identity: CommandIdentity | None = None,
        before_create: Callable[[LaunchedCommand], None] | None = None,
        after_create: Callable[[], None] | None = None,
        after_start: Callable[[], None] | None = None,
    ) -> LaunchedCommand:
        """Create and start a container without waiting for it to finish.

        H06 uses this primitive for run/poll/cancel handles and controlled
        background services.  Authorization is identical to ``execute``: the
        lease, grant intersection, cwd, command allowlist and epoch are checked
        before any container exists.
        """

        cancellation.ensure_active()
        lease, read_paths, write_paths = self._authorize(request, grant)
        if request.runtime_control is not None and request.runtime_control.mode == "entry":
            write_paths = ()  # Entry loading gets less filesystem authority, never more.
        provenance = self._provenance(request, grant, lease, read_paths, write_paths)
        if identity is not None and (
            identity.operation_id != request.operation_id
            or identity.execution_epoch != lease.execution_epoch
            or identity.workspace_hash != digest(str(self.root))
        ):
            raise DomainError(
                "RECOVERY_COMMAND_OWNER", "Command identity does not match lease", 409
            )
        name = identity.name if identity is not None else "mgar-h06-" + uuid.uuid4().hex[:16]
        launched = LaunchedCommand(
            name=name,
            lease_id=lease.lease_id,
            execution_epoch=lease.execution_epoch,
            timeout_seconds=min(request.timeout_seconds, lease.timeout_seconds, grant.max_seconds),
            max_output_bytes=lease.max_output_bytes,
            tree_hash=self.workspace_tree_hash(),
            environment_hash=(digest({
                "container": self.environment_hash(lease),
                "runtime_control": request.runtime_control.model_dump(mode="json"),
            }) if request.runtime_control is not None else self.environment_hash(lease)),
            provenance=provenance,
            identity=identity,
            runtime_control=request.runtime_control,
        )
        if before_create is not None:
            before_create(launched)
        create_args, placeholders = self._create_args(name, request, lease, read_paths, write_paths)
        if identity is not None:
            for key, value in identity.labels().items():
                create_args[1:1] = ["--label", f"{key}={value}"]
        self._register_placeholders(name, placeholders)
        try:
            created = self._docker(create_args)
        except BaseException:
            self._remove_placeholders(name)
            raise
        if created.returncode != 0:
            self._remove_placeholders(name)
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Restricted container could not be created", 502
            )
        if after_create is not None:
            after_create()
        try:
            started = self._docker(["start", name])
        except BaseException:
            if self.allow_internal_write_symlinks:
                # Product previews journal ownership before create. A lost
                # start response must retain that unknown receipt while
                # releasing all host inode pins and attempting exact owned
                # cleanup. Cleanup errors never replace the original outcome.
                try:
                    if identity is not None:
                        self.remove_owned_command(identity)
                    else:
                        self.stop_command(name)
                except BaseException:
                    pass
                try:
                    self._remove_placeholders(name)
                except BaseException:
                    pass
            raise
        if started.returncode != 0:
            self._docker(["rm", "-f", name])
            self._remove_placeholders(name)
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Restricted container could not be started", 502
            )
        if after_start is not None:
            after_start()
        try:
            self.require_current_provenance(provenance)
        except DomainError:
            self.stop_command(name)
            raise
        return launched

    def owned_command_state(self, identity: CommandIdentity) -> tuple[str, int | None]:
        """Inspect only an exact journalled name with all original owner labels."""
        if identity.workspace_hash != digest(str(self.root)):
            raise DomainError("RECOVERY_COMMAND_OWNER", "Command workspace changed", 409)
        probe = self._docker(["inspect", "--format", "{{json .Config.Labels}}", identity.name])
        if probe.returncode:
            return "missing", None
        try:
            labels = json.loads(probe.stdout)
        except (ValueError, TypeError) as exc:
            raise DomainError(
                "RECOVERY_COMMAND_OWNER", "Container labels are invalid", 409
            ) from exc
        if (
            not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in identity.labels().items())
            or labels.get("mgar.v4.h04.broker") != "1"
        ):
            raise DomainError("RECOVERY_COMMAND_OWNER", "Container owner labels changed", 409)
        return self.inspect_command(identity.name)

    def owned_command_logs(self, identity: CommandIdentity, *, max_bytes: int) -> tuple[bytes, int]:
        if self.owned_command_state(identity)[0] == "missing":
            raise DomainError("RECOVERY_COMMAND_MISSING", "Owned command is missing", 409)
        return self.command_logs(identity.name, max_bytes=max_bytes)

    def halt_owned_command(self, identity: CommandIdentity) -> None:
        state, _ = self.owned_command_state(identity)
        if state not in {"missing", "exited", "dead", "created"}:
            self._docker(["kill", identity.name])

    def remove_owned_command(self, identity: CommandIdentity) -> None:
        if self.owned_command_state(identity)[0] != "missing":
            self.stop_command(identity.name)
        from mgar_v3.harness.offline_network import remove_network

        # A restarted broker has no in-memory namespace registry. The original
        # Engine identity/labels remain sufficient to clean its exact helper.
        remove_network(self, identity.name)

    def list_owned_commands(self, *, task_id: str, run_id: str) -> tuple[str, ...]:
        """Discovery only; each result still needs its Engine intent for access."""
        result = self._docker(
            [
                "ps",
                "-a",
                "--filter",
                "label=mgar.v4.h04.broker=1",
                "--filter",
                "label=mgar.v4.h11.task=" + task_id,
                "--filter",
                "label=mgar.v4.h11.run=" + run_id,
                "--filter",
                "label=mgar.v4.h11.workspace=" + digest(str(self.root)),
                "--format",
                "{{.Names}}",
            ]
        )
        if result.returncode:
            raise DomainError("HARNESS_SANDBOX_ERROR", "Owned container discovery failed", 502)
        return tuple(line for line in result.stdout.decode().splitlines() if line)

    @staticmethod
    def _provenance(
        request: ExecutionRequest,
        grant: CapabilityGrant,
        lease: CapabilityLease,
        read_paths: tuple[str, ...],
        write_paths: tuple[str, ...],
    ) -> ExecutionProvenance:
        return ExecutionProvenance(
            operation_id=request.operation_id,
            lease_id=lease.lease_id,
            lease_hash=lease.lease_hash,
            actor=lease.actor,
            workspace_id=lease.workspace_id,
            repository=lease.repository,
            execution_epoch=lease.execution_epoch,
            read_paths=read_paths,
            write_paths=write_paths,
            protected_paths=lease.protected_paths,
            grant_hash=canonical_hash(grant.model_dump(mode="json")),
        )

    def require_current_provenance(self, provenance: ExecutionProvenance | None) -> CapabilityLease:
        """Check release authority without spending another execution budget."""
        if provenance is None:
            raise DomainError(
                "HARNESS_COMMAND_PROVENANCE_REQUIRED", "Command provenance is missing", 403
            )
        lease = self.authority.peek()
        if (
            lease.lease_hash != provenance.lease_hash
            or lease.lease_id != provenance.lease_id
            or lease.execution_epoch != provenance.execution_epoch
            or lease.actor != provenance.actor
            or lease.workspace_id != provenance.workspace_id
            or lease.repository != provenance.repository
        ):
            raise DomainError("HARNESS_COMMAND_AUTHORITY_CHANGED", "Command authority changed", 403)
        return lease

    def inspect_command(self, name: str) -> tuple[str, int | None]:
        """Return the container state and exit code, or ``missing``."""

        probe = self._docker(["inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", name])
        if probe.returncode:
            return "missing", None
        status, _, code = probe.stdout.decode("utf-8", "replace").strip().partition("|")
        parsed = int(code) if code.lstrip("-").isdigit() else None
        return status or "missing", parsed

    def command_resource_snapshot(self, launched: LaunchedCommand) -> ResourceSnapshot:
        """Read fixed kernel fields for an owned launch, never project paths.

        A Docker daemon in another PID namespace may have no readable local
        cgroup. That is explicitly unavailable, not a zero-valued observation.
        """
        sampled_at = _now().isoformat()
        if launched.identity is not None:
            expected = launched.identity.labels()
            if launched.identity.workspace_hash != digest(str(self.root)):
                raise DomainError("RECOVERY_COMMAND_OWNER", "Command workspace changed", 409)
        else:
            with self._placeholder_lock:
                if launched.name not in self._mount_fds:
                    raise DomainError("HARNESS_COMMAND_NOT_FOUND", "Launch is not owned", 404)
            expected = {}
        template = (
            '{"id":{{json .Id}},"state":{{json .State}},'
            '"labels":{{json .Config.Labels}},"ulimits":{{json .HostConfig.Ulimits}}}'
        )
        probe = self._docker(["inspect", "--format", template, launched.name])
        if probe.returncode or len(probe.stdout) > 16384:
            return ResourceSnapshot(sampled_at=sampled_at, unavailable=("docker_inspect",))
        snapshot = ResourceSnapshot(sampled_at=sampled_at)
        try:
            payload = json.loads(probe.stdout)
            labels = payload["labels"]
            state = payload["state"]
            container_id = payload["id"]
            if not isinstance(labels, dict) or not isinstance(state, dict):
                raise ValueError("invalid inspect types")
            if labels.get("mgar.v4.h04.broker") != "1" or any(
                labels.get(key) != value for key, value in expected.items()
            ):
                raise DomainError("RECOVERY_COMMAND_OWNER", "Container owner labels changed", 409)
            if not isinstance(container_id, str) or len(container_id) != 64 or any(
                character not in "0123456789abcdef" for character in container_id
            ):
                raise ValueError("invalid container ID")
            snapshot = ResourceSnapshot(
                sampled_at=sampled_at,
                container_id=container_id,
                container_state=str(state.get("Status", "unavailable")),
                exit_code=state.get("ExitCode"),
                oom_killed=state.get("OOMKilled"),
            )
            for limit in payload.get("ulimits") or ():
                if limit.get("Name") == "nofile":
                    snapshot = snapshot.model_copy(update={
                        "nofile_soft": limit.get("Soft"), "nofile_hard": limit.get("Hard")
                    })
            pid = state.get("Pid")
            if not isinstance(pid, int) or pid <= 0:
                return snapshot.model_copy(update={"unavailable": ("cgroup_process_exited",)})
            membership = Path(f"/proc/{pid}/cgroup").read_text()[:4096]
            unified = next(line[3:] for line in membership.splitlines() if line.startswith("0::"))
            # Avoid a daemon PID interpreted as an unrelated local process.
            if container_id not in unified or ".." in Path(unified).parts:
                raise ValueError("container cgroup identity unavailable")
            cgroup_root = Path("/sys/fs/cgroup").resolve()
            directory = (cgroup_root / unified.lstrip("/")).resolve()
            if not directory.is_relative_to(cgroup_root):
                raise ValueError("cgroup path outside kernel mount")
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return snapshot.model_copy(update={"unavailable": ("cgroup_identity",)})
        missing = []
        for field in ("memory.events", "pids.events", "cpu.stat", "pids.current", "pids.max",
                      "memory.current", "memory.peak", "memory.max"):
            try:
                descriptor = os.open(directory / field, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    raw = os.read(descriptor, 4097)
                finally:
                    os.close(descriptor)
                if len(raw) > 4096:
                    raise ValueError("kernel counter exceeded bound")
                value = raw.decode("ascii")
                if field.endswith(".events") or field == "cpu.stat":
                    counters = {key: int(number) for key, number in (
                        line.split() for line in value.splitlines()
                    )}
                    if any(number < 0 for number in counters.values()):
                        raise ValueError("negative kernel counter")
                    snapshot = snapshot.model_copy(update={field.replace(".", "_"): counters})
                elif value.strip().isdigit():
                    snapshot.values[field] = int(value)
                else:
                    missing.append(field)
            except (OSError, ValueError, UnicodeError):
                missing.append(field)
        return snapshot.model_copy(update={"unavailable": tuple(missing)})

    def command_logs(self, name: str, *, max_bytes: int) -> tuple[bytes, int]:
        """Read the container's combined output, bounded by the lease.

        Returns the retained prefix and the exact number of bytes the container
        wrote, so a caller can report that the tail was clipped instead of
        silently presenting a shortened log as complete.  Reading is streamed
        in fixed-size chunks: a hostile process cannot make the harness buffer
        an unbounded amount of output.
        """

        limit = max(0, max_bytes)
        try:
            process = subprocess.Popen(
                [self.docker, "logs", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_docker_environment(),
            )
        except OSError as exc:
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Docker could not read command logs", 502
            ) from exc
        kept = bytearray()
        total = 0
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                room = limit - len(kept)
                if room > 0:
                    kept.extend(chunk[:room])
            process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            process.kill()
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Docker could not read command logs", 502
            ) from exc
        finally:
            if process.stdout is not None:
                process.stdout.close()
        return bytes(kept), total

    def stop_command(self, name: str) -> None:
        """Stop and remove one owned container.  Safe to call repeatedly."""

        self._docker(["rm", "-f", name])
        self._remove_placeholders(name)

    def command_revoked(
        self, lease_id: str, execution_epoch: int, *, lease_hash: str | None = None
    ) -> bool:
        """True when a running command's lease is no longer the current one.

        Revocation, expiry and epoch replacement all invalidate the authority
        for a command that is already running; the command session polls this
        without charging the call budget.
        """

        try:
            lease = self.authority.peek()
        except DomainError:
            return True
        return (
            lease.lease_id != lease_id
            or lease.execution_epoch != execution_epoch
            or (lease_hash is not None and lease.lease_hash != lease_hash)
        )

    def _register_placeholders(self, name: str, paths: Sequence[Path]) -> None:
        if not paths:
            return
        with self._placeholder_lock:
            self._placeholders[name] = list(paths)

    def _remove_placeholders(self, name: str) -> None:
        from mgar_v3.harness.offline_network import remove_network

        if name in self._offline_names:
            remove_network(self, name)
            self._offline_names.discard(name)
        with self._placeholder_lock:
            paths = self._placeholders.pop(name, [])
            descriptors = self._mount_fds.pop(name, [])
        self._remove_paths(paths)
        for fd in descriptors:
            os.close(fd)

    def exec_probe(
        self, name: str, argv: Sequence[str], *, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        """Run a fixed readiness probe inside an owned container.

        The argv is supplied by trusted adapter code, never by model output.
        ``docker exec`` does not create a new authorization surface: it only
        reaches a container that the broker already authorized.
        """

        result = self._docker(["exec", name, *argv], timeout=timeout)
        return result.returncode, result.stdout + result.stderr

    def workspace_tree_hash(self) -> str:
        """Hash the visible worktree so a command is bound to one version.

        Uses the shared ``is_workspace_artifact`` rule so interpreter and test
        caches written by a container run (``__pycache__``, ``*.pyc``, pytest
        and coverage output) never change the hash.  Real source edits are not
        covered by that rule and still change the hash.
        """

        return _tree_hash(_scan(self.root)[0])

    def environment_hash(self, lease: CapabilityLease) -> str:
        """Summarize the fixed container environment; never its values."""
        return expected_environment_hash(lease)

    def _authorize(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
    ) -> tuple[CapabilityLease, tuple[str, ...], tuple[str, ...]]:
        lease = self.authority.check_scope(
            actor=grant.actor,
            workspace_id=grant.workspace_id,
            repository=grant.repository,
            execution_epoch=request.execution_epoch,
            paths=(),
            write=False,
        )
        if lease.external_write or lease.mode == "publish":
            raise DomainError(
                "HARNESS_EXTERNAL_WRITE_DENIED",
                "External writes must use Action/Approval/Outbox",
                403,
            )
        if lease.network_hosts or grant.network:
            raise DomainError(
                "HARNESS_NETWORK_DENIED",
                "H04 project processes have no network authorization",
                403,
            )
        if not grant.execute:
            raise DomainError("HARNESS_EXECUTE_DENIED", "Execution is not granted", 403)
        cwd = normalize_relative_path(request.cwd)
        read_paths = _intersect(lease.read_paths, grant.read_paths)
        write_paths = _intersect(lease.write_paths, grant.write_paths)
        if lease.mode == "modify_test" and not write_paths:
            raise DomainError(
                "HARNESS_WRITE_DENIED",
                "Writable lease and grant scopes do not intersect",
                403,
            )
        allowed_paths = tuple(sorted(set(read_paths) | set(write_paths)))
        if _is_protected(cwd, lease.protected_paths):
            raise DomainError(
                "HARNESS_PROTECTED_PATH_DENIED",
                "Working directory is protected workspace metadata",
                403,
            )
        # An operator's read-only adapter can start at /workspace while
        # _mount_args still binds only its named source files underneath it.
        scoped_root = (
            self.allow_scoped_root_cwd and not cwd and lease.mode == "investigate"
            and bool(read_paths) and not write_paths
        )
        if not _within(cwd, allowed_paths) and not scoped_root:
            raise DomainError("HARNESS_READ_DENIED", "Working directory is outside the scopes", 403)
        program = Path(request.argv[0]).name
        if program not in lease.allowed_commands:
            raise DomainError(
                "HARNESS_COMMAND_DENIED", f"Command {program!r} is not in the lease", 403
            )
        if not grant.allowed_commands or program not in grant.allowed_commands:
            raise DomainError(
                "HARNESS_COMMAND_DENIED", f"Command {program!r} is not in the grant", 403
            )
        return lease, read_paths, write_paths

    def prepare_dependencies(
        self,
        plan: Mapping[str, Any],
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> None:
        """Dependency preparation is a separate phase from task execution.

        H04 does not authorize any package source or network egress; the port
        exists so callers cannot smuggle installation into the run phase.
        """

        cancellation.ensure_active()
        del plan, grant
        raise DomainError(
            "HARNESS_DEPENDENCY_SOURCE_DENIED",
            "Dependency preparation needs a separately approved source phase",
            403,
        )

    def _scope_source(self, relative: str) -> Path:
        """Resolve one approved scope without following host symlinks."""

        relative = normalize_relative_path(relative)
        target = self.root if relative == "" else self.root.joinpath(*relative.split("/"))
        current = self.root
        if relative:
            for part in relative.split("/"):
                current = current / part
                if current.is_symlink():
                    raise DomainError(
                        "HARNESS_PATH_DENIED",
                        "Approved scope traverses a host symlink",
                        403,
                    )
        resolved = target.resolve()
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise DomainError("HARNESS_PATH_DENIED", "Approved scope escapes the workspace", 403)
        return target

    def _mount_args(
        self,
        read_paths: Sequence[str],
        write_paths: Sequence[str],
        protected_paths: Sequence[str],
        *,
        descriptors: list[int] | None = None,
        identities: list[tuple[str, str]] | None = None,
    ) -> tuple[list[str], list[Path]]:
        """Mount exactly ``read ∪ write`` and hide protected paths.

        Every bind target must already exist on the host.  Mounting a scope and
        then masking a child used to duplicate the target, so the earlier
        implementation expanded a read-only scope into per-child mounts.  That
        still conflicted with file masks, and a missing protected path forced
        the runtime to create a root-owned placeholder inside the workspace.

        Instead the declared scopes are mounted once, exactly as leased, and a
        protected path is masked only when it would otherwise be visible:

        * existing file -> unreadable empty regular-file bind;
        * existing directory -> empty read-only tmpfs with mode ``0555``;
        * missing path under a read-only mount -> no mask, because the
          read-only parent already makes it impossible to create or read;
        * missing path under a writable mount -> the broker creates the mount
          target itself (owned by the workspace user) and removes it together
          with the container, so docker never writes into the workspace.
        """

        read_set = {normalize_relative_path(path) for path in read_paths}
        write_set = {normalize_relative_path(path) for path in write_paths}
        allowed = tuple(read_set | write_set)
        hashes, unsafe_links = _scan(self.root)
        for relative, value in hashes.items():
            if not value.startswith("symlink:") or not _within(relative, allowed):
                continue
            if relative in unsafe_links:
                raise DomainError(
                    "HARNESS_PATH_DENIED", "Symlink is unsafe", 403
                )
            writable_alias = _within(relative, tuple(write_set))
            with _open_read_target(self.root, relative) as (descriptor, canonical):
                if (
                    _is_protected(canonical, protected_paths)
                    or _is_protected(relative, protected_paths)
                ):
                    raise DomainError(
                        "HARNESS_PATH_DENIED", "Symlink touches a protected path", 403
                    )
                if writable_alias:
                    if not self.allow_internal_write_symlinks or not _within(
                        canonical, tuple(write_set)
                    ):
                        raise DomainError(
                            "HARNESS_PATH_DENIED",
                            "Symlink and target need an explicitly supported writable scope", 403,
                        )
                    inode = os.fstat(descriptor)
                    if stat.S_ISDIR(inode.st_mode) and (
                        canonical == "" or relative.startswith(canonical + "/")
                    ):
                        raise DomainError(
                            "HARNESS_PATH_DENIED", "Recursive directory symlink is denied", 403
                        )
                    if descriptors is not None and identities is not None:
                        # Keep the no-follow resolved inode alive and verify
                        # its alias again inside the container before argv.
                        # A host link/parent replacement after this check must
                        # not redirect an authorized command onto another inode.
                        descriptors.append(os.dup(descriptor))
                        identities.append((
                            f"{inode.st_dev}:{inode.st_ino}", f"/workspace/{relative}"
                        ))
                elif not _within(canonical, allowed) or _within(canonical, tuple(write_set)):
                    raise DomainError(
                        "HARNESS_PATH_DENIED", "Symlink target is outside read-only scope", 403
                    )
        if "" in write_set:
            read_only: set[str] = set()
        else:
            read_only = {
                path
                for path in read_set - write_set
                if not any(path.startswith(writable + "/") for writable in write_set)
            }
        plan: list[tuple[str, bool]] = [(path, True) for path in sorted(read_only)]
        plan.extend((path, False) for path in sorted(write_set))
        mounts: list[str] = []
        mounted: list[str] = []
        for path, readonly in plan:
            source = self._scope_source(path)
            file_mount = self.allow_scoped_root_cwd and readonly and source.is_file()
            if not source.is_dir() and not file_mount:
                continue
            if descriptors is not None:
                # Retain the approved inode for the trusted container-entry
                # check. WSL/runc cannot bind a client /proc/<pid>/fd path.
                with _open_read_target(self.root, path) as (fd, canonical):
                    if canonical != path:
                        raise DomainError("HARNESS_PATH_DENIED", "Scope root changed", 403)
                    pinned = os.dup(fd)
                descriptors.append(pinned)
            target = "/workspace" + (f"/{path}" if path else "")
            if descriptors is not None and identities is not None:
                inode = os.fstat(pinned)
                identities.append((f"{inode.st_dev}:{inode.st_ino}", target))
            suffix = ",readonly" if readonly else ""
            mounts.extend(["--mount", f"type=bind,source={source},target={target}{suffix}"])
            mounted.append(path)
        masks, placeholders = self._mask_args(mounted, write_set, protected_paths)
        return mounts + masks, placeholders

    def _mask_args(
        self,
        mounted: Sequence[str],
        write_set: set[str],
        protected_paths: Sequence[str],
    ) -> tuple[list[str], list[Path]]:
        """Hide protected paths without letting docker create host mountpoints."""

        args: list[str] = []
        placeholders: list[Path] = []
        try:
            for protected in protected_paths:
                relative = normalize_relative_path(protected)
                if relative == "" or not _within(relative, tuple(mounted)):
                    continue
                target = f"/workspace/{relative}"
                source = self.root.joinpath(*relative.split("/"))
                if source.is_symlink():
                    raise DomainError(
                        "HARNESS_PROTECTED_PATH_DENIED",
                        "Protected path is a host symlink",
                        403,
                    )
                if source.exists():
                    if source.is_dir():
                        args.extend(["--tmpfs", f"{target}:ro,size=1m,mode=0555"])
                    else:
                        args.extend(
                            [
                                "--mount",
                                f"type=bind,source={_masked_file()},target={target},readonly",
                            ]
                        )
                    continue
                writable = "" in write_set or any(
                    relative.startswith(path + "/") for path in write_set
                )
                if not writable:
                    # The parent mount is read-only; the path stays absent and
                    # the project process can neither read nor create it.
                    continue
                placeholders.extend(self._create_placeholder(source))
                args.extend(["--tmpfs", f"{target}:ro,size=1m,mode=0555"])
        except BaseException:
            self._remove_paths(placeholders)
            raise
        return args, placeholders

    def _create_placeholder(self, target: Path) -> list[Path]:
        """Create the empty, user-owned mount target a mask needs."""

        missing: list[Path] = []
        current = target
        while not current.exists():
            missing.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir():
            raise DomainError(
                "HARNESS_PATH_DENIED",
                "A protected path has a non-directory host parent",
                403,
            )
        created: list[Path] = []
        for path in reversed(missing):
            try:
                path.mkdir(mode=0o700)
            except OSError as exc:
                self._remove_paths(created)
                raise DomainError(
                    "HARNESS_SANDBOX_ERROR",
                    "A protected path mount target could not be created",
                    502,
                ) from exc
            created.append(path)
        return created

    def _remove_paths(self, paths: Sequence[Path]) -> None:
        for path in reversed(list(paths)):
            try:
                path.rmdir()
            except OSError:
                continue

    def _create_args(
        self,
        name: str,
        request: ExecutionRequest,
        lease: CapabilityLease,
        read_paths: Sequence[str],
        write_paths: Sequence[str],
    ) -> tuple[list[str], list[Path]]:
        memory = lease.memory_mb * 1024 * 1024
        cwd = normalize_relative_path(request.cwd)
        # Validate frozen control bytes before opening/pinning any mounts.
        command = request.argv
        if request.runtime_control is not None:
            from mgar_v3.harness.lifecycle_assets import control_command

            command = control_command(request.runtime_control, request.argv)
        descriptors: list[int] = []
        identities: list[tuple[str, str]] = []
        placeholders: list[Path] = []
        probe_arguments: list[str] = []
        try:
            mounts, placeholders = self._mount_args(
                read_paths,
                write_paths,
                lease.protected_paths,
                descriptors=descriptors,
                identities=identities,
            )
            if self.public_probe is not None and request.argv == self.public_probe.argv:
                assert self.public_probe_root is not None
                with pinned_asset(self.public_probe, self.public_probe_root) as (source, fd):
                    if any(value in str(source) for value in (",", "\n", "\r")):
                        raise DomainError(
                            "PUBLIC_PROBE_ASSET_DENIED", "Controller mount path is invalid", 403
                        )
                    descriptors.append(os.dup(fd))
                    inode = os.fstat(fd)
                    target = self.public_probe.container_path
                    identities.append((f"{inode.st_dev}:{inode.st_ino}", target))
                    mounts.extend(
                        [
                            "--mount",
                            f"type=bind,source={source},target={target},readonly",
                        ]
                    )
                    probe_arguments = [self.public_probe.asset_sha256, target]
        except BaseException:
            for fd in descriptors:
                os.close(fd)
            self._remove_paths(placeholders)
            raise
        with self._placeholder_lock:
            self._mount_fds[name] = descriptors
        network = "none"
        if lease.offline_network != "none":
            from mgar_v3.harness.offline_network import create_network

            self._offline_names.add(name)
            try:
                network = "container:" + create_network(self, name, mode=lease.offline_network)
            except BaseException:
                self._remove_placeholders(name)
                self._remove_paths(placeholders)
                raise
        entry_check = (
            'count=$1; shift; while [ "$count" -gt 0 ]; do '
            "expected=$1; target=$2; shift 2; "
            'actual=$(/usr/bin/stat -Lc "%d:%i" -- "$target") || exit 126; '
            '[ "$actual" = "$expected" ] || '
            '{ printf "%s\\n" "HARNESS_MOUNT_IDENTITY_CHANGED" >&2; exit 126; }; '
            "count=$((count - 1)); done; "
        )
        if probe_arguments:
            entry_check += (
                "expected=$1; target=$2; shift 2; "
                'actual=$(/usr/bin/sha256sum -- "$target") || exit 126; '
                'actual=${actual%% *}; [ "$actual" = "$expected" ] || '
                '{ printf "%s\\n" "PUBLIC_PROBE_ASSET_CHANGED" >&2; exit 126; }; '
            )
        if lease.mode in {"investigate", "modify_test"}:
            # Preserve the image's plugin options; keep pytest's cache off the readonly tree.
            entry_check += (
                'export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }'
                '-o cache_dir=/scratch/pytest-cache"; '
            )
        entry_check += 'exec "$@"'
        args = [
            "create",
            "--name",
            name,
            "--label",
            "mgar.v4.h04.broker=1",
            "--network=" + network,
            "--read-only",
            "--user=" + SANDBOX_UID,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=" + str(lease.pids),
            "--memory=" + str(memory),
            "--memory-swap=" + str(memory),
            "--cpus=" + f"{lease.cpus:g}",
            "--ipc=none",
            "--init",
            *mounts,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--tmpfs",
            (f"/scratch:rw,exec,nosuid,nodev,size={lease.scratch_mb}m,mode=1777"),
            "--ulimit",
            f"nofile={lease.nofile_limit}:{lease.nofile_limit}",
            "--ulimit",
            "core=0:0",
            "--env",
            "PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "TMPDIR=/scratch",
            "--env",
            "GOCACHE=/scratch/go-cache",
            "--env",
            "GOTMPDIR=/scratch",
            "--workdir",
            "/workspace" + (f"/{cwd}" if cwd else ""),
            "--entrypoint",
            "/bin/sh",
            self.image_id,
            "-c",
            # All substitutions operate on trusted metadata. Model argv stays
            # positional data and is executed only after every mounted inode
            # matches its no-follow host descriptor. A source swap cannot run
            # the project process against another directory.
            entry_check,
            "harness-mount-check",
            str(len(identities)),
            *(part for identity in identities for part in identity),
            *probe_arguments,
            *command,
        ]
        return args, placeholders

    def _docker(
        self, args: Sequence[str], *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.docker, *args],
                capture_output=True,
                env=_docker_environment(),
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Docker could not run the sandbox", 502
            ) from exc


def expected_environment_hash(lease: CapabilityLease) -> str:
    """Summarize the fixed container environment; never its values."""

    offline = {}
    if lease.offline_network != "none":
        from mgar_v3.harness.offline_network import network_policy_hash

        offline = {"offline_network": lease.offline_network,
                   "offline_network_policy_sha256": network_policy_hash(lease.offline_network)}
    return digest(
        {
            "PATH": (
                "/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "TMPDIR": "/scratch",
            "GOCACHE": "/scratch/go-cache",
            "GOTMPDIR": "/scratch",
            **({"readonly_pytest_cache": "/scratch/pytest-cache"}
               if lease.mode in {"investigate", "modify_test"} else {}),
            "credentials": sorted(lease.credentials),
            **offline,
        }
    )
