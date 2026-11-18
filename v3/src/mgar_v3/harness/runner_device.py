"""Pull one fixed local workspace job and execute it through the owned Broker."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import select
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.broker import (
    CommandIdentity,
    IsolatedExecutionBroker,
    _docker_environment,
    issue_lease,
)
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest
from mgar_v3.harness.project_commands import detect_project, project_files
from mgar_v3.harness.runner_contract import (
    OUTPUT_BYTES,
    RUNNER_API,
    TEST_SECONDS,
    Claim,
    Finish,
    Named,
    Pairing,
    Poll,
    PreflightFailure,
    Secret,
    Start,
    Ticket,
    WorkspaceSpec,
)
from mgar_v3.harness.runner_guard import RunnerBroker, owner, remove
from mgar_v3.harness.workspace import TaskWorkspace, WorkspaceRegistry, _write_json_atomic

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def server_origin(value: str) -> str:
    url = urlsplit(value)
    if (
        (url.scheme != "https" and not (url.scheme == "http" and url.hostname in _LOOPBACK))
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("Use an HTTPS server origin or a loopback HTTP origin.")
    return value.rstrip("/")


class DeviceConfig(Named):
    server: str
    device_id: UUID
    credential: Secret
    ca_file: Path | None = None
    paired: bool = False


class LocalProject(Model):
    spec: WorkspaceSpec
    root: Path
    registry_id: str
    registration_hash: str
    argv: tuple[str, ...]
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class PendingReceipt(Model):
    task_id: str = Field(pattern=r"^h15-[a-f0-9]{12}$")
    receipt: Finish


class DeviceClient:
    def __init__(self, config: DeviceConfig) -> None:
        verify = ssl.create_default_context(cafile=str(config.ca_file)) if config.ca_file else True
        self._http = httpx.Client(
            base_url=server_origin(config.server) + RUNNER_API + "/device",
            follow_redirects=False,
            trust_env=False,
            verify=verify,
            timeout=3,
            headers={"Authorization": "Bearer " + config.credential},
        )

    def __enter__(self) -> DeviceClient:
        return self

    def __exit__(self, *args: object) -> None:
        self._http.close()

    def post(self, route: str, body: Model) -> dict[str, Any]:
        document = body.model_dump(mode="json")
        try:
            response = self._http.post(route, json=document)
        except httpx.RequestError:
            # Each device mutation is replay-safe with the original payload.
            response = self._http.post(route, json=document)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_error or response.is_redirect:
            code = payload.get("code") if isinstance(payload, dict) else None
            if not isinstance(code, str) or not re.fullmatch("[A-Z0-9_]{1,80}", code):
                code = "RUNNER_REQUEST_DENIED"
            raise DomainError(code, "Runner server rejected the request", response.status_code)
        if not isinstance(payload, dict):
            raise DomainError("RUNNER_PROTOCOL_INVALID", "Expected a Runner response object", 502)
        return payload


class RunnerDirectory:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise DomainError(
                "RUNNER_STATE_PERMISSIONS", "Runner state must be a private directory", 403
            )
        self.root = self.root.resolve()
        self.projects = self.root / "projects"
        self.receipts = self.root / "receipts"
        self.guards = self.root / "guards"
        for directory in (self.projects, self.receipts, self.guards):
            directory.mkdir(exist_ok=True, mode=0o700)
        self.registries = self.root / "registry"

    def config(self) -> DeviceConfig:
        return DeviceConfig.model_validate_json((self.root / "device.json").read_bytes())

    @contextmanager
    def worker(self) -> Iterator[None]:
        with (self.root / "worker.lock").open("a+") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DomainError(
                    "RUNNER_ALREADY_ACTIVE", "This Runner is already polling", 409
                ) from exc
            yield

    def pair(
        self, server: str, name: str, code: str, *, ca_file: Path | None = None
    ) -> DeviceConfig:
        url = server_origin(server)
        if ca_file is not None:
            ca_file = ca_file.expanduser().resolve(strict=True)
        expected = Named(name=name).name
        with self.worker():
            if (self.root / "device.json").exists():
                config = self.config()
                if (
                    config.server != url
                    or config.name != expected
                    or config.ca_file != ca_file
                    or config.paired
                ):
                    raise DomainError(
                        "RUNNER_ALREADY_PAIRED", "Use a new device state directory", 409
                    )
            else:
                config = DeviceConfig(
                    server=url,
                    name=expected,
                    device_id=uuid4(),
                    credential=secrets.token_urlsafe(32),
                    ca_file=ca_file,
                )
                _write_json_atomic(self.root, "device.json", config.model_dump(mode="json"))
            with DeviceClient(config) as client:
                response = client.post(
                    "/pair",
                    Pairing(
                        name=config.name,
                        code=code,
                        device_id=config.device_id,
                        credential=config.credential,
                    ),
                )
            if response.get("device_id") != str(config.device_id):
                raise DomainError(
                    "RUNNER_PAIRING_MISMATCH", "The server returned another device", 409
                )
            config = config.model_copy(update={"paired": True})
            _write_json_atomic(self.root, "device.json", config.model_dump(mode="json"))
            return config

    def attach(self, path: Path, name: str, image_lock: Path) -> WorkspaceSpec:
        config = self.config()
        self._require_paired(config)
        root = path.expanduser().absolute()
        if root.is_symlink() or (
            root.resolve().is_relative_to(self.root) or self.root.is_relative_to(root.resolve())
        ):
            raise DomainError(
                "RUNNER_WORKSPACE_DENIED", "Project and Runner state must be separate", 403
            )
        workspace_id = uuid4()
        registry = WorkspaceRegistry(self.registries / str(workspace_id))
        registration = registry.register_local(root, actor="runner")
        # Adapter discovery reads an H03 copy, with the same exclusions as
        # execution. It never follows a manifest outside the chosen project.
        probe = registry.create_task_workspace(
            registration,
            "h15-enroll-" + uuid4().hex,
            source_root=root,
        )
        try:
            profile = detect_project(project_files(probe.root))
        finally:
            shutil.rmtree(probe.root)
        kind = profile.kind
        if kind not in {"python", "go", "node"} or not profile.test_argv:
            raise DomainError(
                "RUNNER_PROJECT_UNSUPPORTED",
                "No fixed Python, Node or Go test plan was detected",
                422,
            )
        image_id = IsolatedExecutionBroker._image_from_lock(image_lock)
        plan_hash = digest(
            {
                "registration_sha256": registration.registration_hash,
                "kind": kind,
                "argv": profile.test_argv,
                "image_id": image_id,
            }
        )
        spec = WorkspaceSpec(
            workspace_id=workspace_id, name=name, project_kind=kind, plan_hash=plan_hash
        )
        project = LocalProject(
            spec=spec,
            root=root,
            registry_id=registration.workspace_id,
            registration_hash=registration.registration_hash,
            argv=profile.test_argv,
            image_id=image_id,
        )
        # Write the private path before making the opaque handle visible.
        # Pending attachments are re-announced with the same id by the worker.
        _write_json_atomic(
            self.projects, str(spec.workspace_id) + ".json", project.model_dump(mode="json")
        )
        with DeviceClient(config) as client:
            client.post("/workspaces", spec)
        return spec

    @staticmethod
    def _require_paired(config: DeviceConfig) -> None:
        if not config.paired:
            raise DomainError("RUNNER_NOT_PAIRED", "Finish pairing before using the Runner", 409)

    def project(self, spec: WorkspaceSpec) -> LocalProject:
        project = LocalProject.model_validate_json(
            (self.projects / (str(spec.workspace_id) + ".json")).read_bytes(),
        )
        if spec != project.spec:
            raise DomainError(
                "RUNNER_PLAN_CHANGED", "The requested workspace no longer matches", 409
            )
        return project

    def snapshot(self, project: LocalProject, claim: Claim) -> TaskWorkspace:
        registry = WorkspaceRegistry(self.registries / str(project.spec.workspace_id))
        registration = registry.load(project.registry_id)
        if registration.registration_hash != project.registration_hash:
            raise DomainError("RUNNER_PLAN_CHANGED", "The local registration changed", 409)
        workspace = registry.create_task_workspace(
            registration,
            f"{claim.task_id}-{claim.execution_epoch}",
            source_root=project.root,
        )
        profile = detect_project(project_files(workspace.root))
        if profile.test_argv != project.argv or profile.kind != project.spec.project_kind:
            shutil.rmtree(workspace.root)
            raise DomainError("RUNNER_PLAN_CHANGED", "The fixed test plan changed", 409)
        return workspace

    def sync_projects(self, client: DeviceClient) -> None:
        for path in sorted(self.projects.glob("*.json")):
            project = LocalProject.model_validate_json(path.read_bytes())
            client.post("/workspaces", project.spec)

    def recover_owned_commands(self) -> None:
        for journal in sorted(self.guards.glob("lease-*")):
            manifest = journal / "owner.json"
            if not manifest.is_file():
                continue  # The journal was never passed to a process.
            supplied = json.loads(manifest.read_text())
            root = Path(supplied["root"]).resolve()
            if not root.is_relative_to(self.registries):
                raise DomainError("RUNNER_CLEANUP_UNKNOWN", "Recovery workspace differs", 409)
            if root.exists():
                broker, identity = owner(supplied)
                if not remove(journal, broker, identity):
                    raise DomainError("RUNNER_CLEANUP_UNKNOWN", "Sandbox cleanup is pending", 409)
            elif not (journal / "done").is_file():
                raise DomainError(
                    "RUNNER_CLEANUP_UNKNOWN", "Recovery workspace is unavailable", 409
                )
            shutil.rmtree(journal)
            if root.exists():
                shutil.rmtree(root)

    def flush_receipts(self, client: DeviceClient) -> None:
        for path in sorted(self.receipts.glob("*.json")):
            pending = PendingReceipt.model_validate_json(path.read_bytes())
            try:
                client.post(f"/jobs/{pending.task_id}/finish", pending.receipt)
            except DomainError as exc:
                if exc.code not in {"RUNNER_FENCED", "RUNNER_RECEIPT_CONFLICT"}:
                    raise
                # A stale result remains local evidence and cannot be used
                # to restart the command or overwrite the canonical Run.
                path.rename(path.with_suffix(".fenced"))
            else:
                path.unlink()

    def save_receipt(self, task_id: str, receipt: Finish) -> None:
        pending = PendingReceipt(task_id=task_id, receipt=receipt)
        _write_json_atomic(
            self.receipts,
            f"{task_id}-{receipt.execution_epoch}.json",
            pending.model_dump(mode="json"),
        )


class ClaimKeeper:
    """Renew one ticket and give the independent local process its cutoff."""

    def __init__(self, client: DeviceClient, claim: Claim, token: str, sent: float) -> None:
        self.client, self.claim = client, claim
        self.ticket = Ticket(ticket=token, execution_epoch=claim.execution_epoch)
        self.deadline = sent + claim.lease_ms / 1000
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._guard: Path | None = None
        self._thread = threading.Thread(target=self._keep_alive, daemon=True)

    def __enter__(self) -> ClaimKeeper:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=4)

    def _write_deadline(self) -> None:
        if self._guard is not None:
            temporary = self._guard / "renewing"
            temporary.write_text(str(self.deadline))
            temporary.replace(self._guard / "deadline")

    def set_guard(self, guard: Path | None) -> None:
        with self._lock:
            self._guard = guard
            self._write_deadline()

    def check(self) -> None:
        with self._lock:
            if time.monotonic() >= self.deadline:
                raise DomainError("RUNNER_FENCED", "The Runner no longer has a live ticket", 409)

    def _update(self, claim: Claim, sent: float) -> None:
        previous = self.claim
        if (
            claim.task_id,
            claim.run_id,
            claim.device_id,
            claim.workspace,
            claim.execution_epoch,
        ) != (
            previous.task_id,
            previous.run_id,
            previous.device_id,
            previous.workspace,
            previous.execution_epoch,
        ):
            raise DomainError("RUNNER_FENCED", "The server returned another execution scope", 409)
        with self._lock:
            if self.deadline <= time.monotonic():
                raise DomainError("RUNNER_FENCED", "The existing local ticket already expired", 409)
            self.deadline = max(self.deadline, sent + claim.lease_ms / 1000)
            self._write_deadline()

    def start(self, workspace: TaskWorkspace) -> None:
        request = Start(
            **self.ticket.model_dump(),
            tree_hash=workspace.tree_hash(),
            manifest_hash=workspace.manifest.manifest_hash,
        )
        self.check()
        sent = time.monotonic()
        response = Claim.model_validate(
            self.client.post(f"/jobs/{self.claim.task_id}/start", request)
        )
        if response.phase != "running":
            raise DomainError("RUNNER_FENCED", "The server did not acknowledge dispatch", 409)
        self._update(response, sent)

    def _keep_alive(self) -> None:
        while not self._stop.wait(1.5):
            try:
                self.check()
                sent = time.monotonic()
                response = Claim.model_validate(
                    self.client.post(f"/jobs/{self.claim.task_id}/heartbeat", self.ticket),
                )
                self._update(response, sent)
            except (httpx.RequestError, ValueError):
                # Lack of a verified response never extends the local deadline.
                continue
            except (DomainError, OSError):
                with self._lock:
                    self.deadline = 0
                    self._write_deadline()
                return


@contextmanager
def command_guard(
    state: RunnerDirectory,
    keeper: ClaimKeeper,
    broker: RunnerBroker,
    identity: CommandIdentity,
) -> Iterator[tuple[Path, subprocess.Popen[str]]]:
    directory = Path(tempfile.mkdtemp(prefix="lease-", dir=state.guards))
    cleaned = False
    try:
        keeper.set_guard(directory)
        supplied = {
            "journal": str(directory),
            "identity": asdict(identity),
            "root": str(broker.root),
            "image_id": broker.image_id,
        }
        _write_json_atomic(directory, "owner.json", supplied)
        process = subprocess.Popen(
            [sys.executable, "-m", "mgar_v3.harness.runner_guard"],
            env=_docker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps(supplied) + "\n")
            process.stdin.flush()
            if (
                not select.select([process.stdout], [], [], 5)[0]
                or process.stdout.readline() != "ready\n"
            ):
                raise DomainError(
                    "RUNNER_GUARD_FAILED", "The local process guard did not start", 409
                )
            yield directory, process
        finally:
            process.stdin.close()
            process.stdout.close()
            # A failed cleanup remains the watchdog's responsibility.
            try:
                cleaned = process.wait(timeout=20) == 0
            except subprocess.TimeoutExpired:
                cleaned = False
            if not cleaned:
                raise DomainError("RUNNER_CLEANUP_UNKNOWN", "Sandbox cleanup is pending", 409)
    finally:
        keeper.set_guard(None)
        if cleaned or not (directory / "creating").exists():
            shutil.rmtree(directory)


def _execute(
    state: RunnerDirectory,
    keeper: ClaimKeeper,
    project: LocalProject,
    workspace: TaskWorkspace,
) -> Finish:
    claim = keeper.claim
    binding = workspace.registration
    program = Path(project.argv[0]).name
    request = ExecutionRequest(
        operation_id=claim.task_id + ":tests",
        purpose="test",
        argv=project.argv,
        execution_epoch=claim.execution_epoch,
        timeout_seconds=TEST_SECONDS,
    )
    grant = CapabilityGrant(
        actor="runner",
        workspace_id=binding.workspace_id,
        repository=binding.repository,
        read_paths=("",),
        execute=True,
        allowed_commands=(program,),
        max_seconds=TEST_SECONDS,
    )
    lease = issue_lease(
        mode="investigate",
        actor=grant.actor,
        workspace_id=grant.workspace_id,
        repository=grant.repository,
        execution_epoch=claim.execution_epoch,
        allowed_commands=(program,),
        timeout_seconds=TEST_SECONDS,
        max_output_bytes=OUTPUT_BYTES,
        max_calls=1,
        ttl_seconds=TEST_SECONDS + 30,
    )
    broker = RunnerBroker(workspace.root, lease, image_id=project.image_id)
    identity = CommandIdentity(
        task_id=claim.task_id,
        run_id=str(claim.run_id),
        operation_id=request.operation_id,
        execution_epoch=claim.execution_epoch,
        workspace_hash=digest(str(broker.root)),
    )

    with command_guard(state, keeper, broker, identity) as (journal, watcher):

        def active() -> None:
            keeper.check()
            if watcher.poll() is not None:
                raise DomainError("RUNNER_FENCED", "The local process guard has exited", 409)

        def creating(_: object) -> None:
            active()
            (journal / "creating").touch()

        outcome: Literal["exited", "timeout", "unavailable"] = "unavailable"
        exit_code: int | None = None
        output = b""
        total = 0
        before = workspace.tree_hash()
        started = False
        try:
            launched = broker.launch(
                request,
                grant,
                CancellationToken(),
                identity=identity,
                before_create=creating,
                after_create=active,
            )
            started = True
            deadline = time.monotonic() + launched.timeout_seconds
            while True:
                active()
                process_state, code = broker.owned_command_state(identity)
                if process_state in {"exited", "dead"}:
                    if code is not None and 0 <= code <= 255:
                        outcome, exit_code = "exited", code
                        output, total = broker.owned_command_logs(identity, max_bytes=OUTPUT_BYTES)
                    break
                if process_state == "missing":
                    break
                if time.monotonic() >= deadline:
                    outcome = "timeout"
                    broker.halt_owned_command(identity)
                    output, total = broker.owned_command_logs(identity, max_bytes=OUTPUT_BYTES)
                    break
                time.sleep(0.12)
        except DomainError as exc:
            if exc.code not in {"HARNESS_SANDBOX_ERROR", "HARNESS_WORKSPACE_DENIED"}:
                raise
        finally:
            broker.remove_owned_command(identity)
            if started and broker.owned_command_state(identity)[0] == "missing":
                (journal / "done").touch()
        keeper.check()
        if workspace.tree_hash() != before:
            outcome, exit_code, output, total = "unavailable", None, b"", 0
        log = output.decode("utf-8", errors="replace")
        wire = log.encode("utf-8")[:OUTPUT_BYTES].decode("utf-8", errors="ignore")
        return Finish(
            **keeper.ticket.model_dump(),
            tree_hash=before,
            state=outcome,
            exit_code=exit_code,
            output=wire,
            truncated=total > len(output) or wire != log,
        )


def run_one(state: RunnerDirectory, client: DeviceClient, config: DeviceConfig) -> bool:
    token = secrets.token_urlsafe(32)
    sent = time.monotonic()
    payload = client.post("/claim", Poll(ticket=token))
    if payload["claim"] is None:
        return False
    claim = Claim.model_validate(payload["claim"])
    if claim.device_id != config.device_id or claim.phase != "claimed":
        raise DomainError("RUNNER_FENCED", "A fresh claim must belong to this device", 409)
    with ClaimKeeper(client, claim, token, sent) as keeper:
        try:
            project = state.project(claim.workspace)
            workspace = state.snapshot(project, claim)
        except (OSError, ValueError, DomainError) as exc:
            failure: PreflightFailure = (
                "RUNNER_PLAN_CHANGED"
                if isinstance(exc, DomainError) and exc.code == "RUNNER_PLAN_CHANGED"
                else "RUNNER_WORKSPACE_UNAVAILABLE"
            )
            receipt = Finish(**keeper.ticket.model_dump(), state="rejected", failure=failure)
        else:
            keep_for_cleanup = False
            try:
                keeper.start(workspace)
                receipt = _execute(state, keeper, project, workspace)
            except DomainError as exc:
                keep_for_cleanup = exc.code == "RUNNER_CLEANUP_UNKNOWN"
                raise
            finally:
                if not keep_for_cleanup:
                    shutil.rmtree(workspace.root)
        state.save_receipt(claim.task_id, receipt)
    state.flush_receipts(client)
    return True
