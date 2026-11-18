"""Developer-only H11 fault fixtures; never exposed as project tools.

Each PostgreSQL container and process belongs to this test invocation. There
is no fallback to the shared developer database and no image pull/install.
Connection strings stay in private fixture files and are omitted from reprs.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]
OWNER_LABEL = "mgar.h11.fixture-owner"
DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")


def export_safe_files(
    destination: Path,
    files: dict[str, Path],
    *,
    redactions: tuple[str, ...] = (),
) -> None:
    """Export an explicit whitelist; never copy a fixture directory wholesale."""
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = []
    for name, source in files.items():
        if not source.is_file():
            continue
        content = source.read_bytes()
        original = content
        for secret in redactions:
            if secret:
                content = content.replace(secret.encode(), b"[REDACTED]")
        target = destination / name
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        manifest.append(
            {
                "file": name,
                "sha256": sha256(content).hexdigest(),
                "bytes": len(content),
                "redacted": content != original,
            }
        )
    private_json(destination / "sha256-manifest.json", {"files": manifest})


def private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending-" + uuid4().hex[:8])
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def make_traversable(path: Path) -> None:
    """Expose only this test's unique /tmp hierarchy to the fixed sandbox UID."""
    path = path.resolve()
    if Path("/tmp") not in path.parents:
        raise AssertionError("H11 container fixture must be below /tmp")
    while path != Path("/tmp"):
        path.chmod(0o755)
        path = path.parent


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("H11 fixture expected a JSON object")
    return payload


def wait_json(
    path: Path, *, process: subprocess.Popen[bytes] | None = None, timeout: float = 45.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return read_json(path)
        if process is not None and process.poll() is not None:
            raise AssertionError(f"H11 worker exited {process.returncode} before its marker")
        time.sleep(0.02)
    raise AssertionError(f"H11 fixture timed out waiting for {path.name}")


def _docker(*arguments: str, timeout: float = 40.0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [*DOCKER, *arguments],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Do not include Docker inspect's environment or authentication data.
        raise AssertionError(
            f"H11 owned Docker fixture {arguments[0]} failed with exit {result.returncode}"
        )
    return result


@dataclass(repr=False)
class IsolatedPostgres:
    owner: str = field(default_factory=lambda: uuid4().hex)
    name: str = field(init=False)
    port: int = 0
    directory: Path = field(init=False, repr=False)
    password: str = field(default_factory=lambda: secrets.token_hex(24), repr=False)
    created: bool = False
    databases: set[str] = field(default_factory=set, repr=False)
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.name = "magr-h11-pg-" + self.owner[:16]
        self.directory = Path(tempfile.mkdtemp(prefix="magr-h11-pg-", dir="/tmp"))

    def __repr__(self) -> str:
        return f"IsolatedPostgres(name={self.name!r}, port={self.port})"

    @property
    def admin_dsn(self) -> str:
        return make_conninfo(
            host="127.0.0.1",
            port=self.port,
            user="h11_owner",
            dbname="postgres",
            password=self.password,
        )

    def start(self) -> IsolatedPostgres:
        compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
        match = re.search(
            r"^\s+image:\s+(pgvector/pgvector:[^\s]+@sha256:[a-f0-9]{64})$", compose, re.MULTILINE
        )
        if match is None:
            raise AssertionError("H11 needs the existing pinned PostgreSQL image")
        image = match.group(1)
        _docker("image", "inspect", image)
        # Docker reassigns an empty/random HostPort on a later `start`.  Select
        # a loopback port once and persist an explicit binding for this fixture.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = int(reservation.getsockname()[1])
        environment = self.directory / "postgres.env"
        descriptor = os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                "POSTGRES_USER=h11_owner\nPOSTGRES_DB=postgres\n"
                + "POSTGRES_PASSWORD="
                + self.password
                + "\n"
            )
        _docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            self.name,
            "--label",
            OWNER_LABEL + "=" + self.owner,
            "--label",
            "mgar.h11.role=postgres",
            "--publish",
            f"127.0.0.1:{self.port}:5432",
            "--env-file",
            str(environment),
            image,
        )
        self.created = True
        self.record_event("container_created")
        info = self._owned_inspect()
        bindings = info["NetworkSettings"]["Ports"]["5432/tcp"]
        if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise AssertionError("H11 PostgreSQL must listen only on host loopback")
        if int(bindings[0]["HostPort"]) != self.port:
            raise AssertionError("H11 PostgreSQL fixture port changed unexpectedly")
        self.wait_ready()
        self.record_event("postgres_ready")
        return self

    def record_event(self, kind: str) -> None:
        self.events.append(
            {
                "kind": kind,
                "at": datetime.now(UTC).isoformat(),
                "container": self.name,
                "host": "127.0.0.1",
                "port": self.port,
            }
        )

    def _owned_inspect(self) -> dict[str, Any]:
        info = json.loads(_docker("inspect", self.name).stdout)[0]
        if not isinstance(info, dict):
            raise AssertionError("H11 Docker inspection returned an invalid object")
        if (
            info["Name"] != "/" + self.name
            or info["Config"]["Labels"].get(OWNER_LABEL) != self.owner
            or info["Config"]["Labels"].get("mgar.h11.role") != "postgres"
        ):
            raise AssertionError("H11 refuses to operate on an unowned container")
        return info

    def wait_ready(self, timeout: float = 40.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn, connect_timeout=1, autocommit=True) as conn:
                    if conn.execute("SELECT 1").fetchone() == (1,):
                        return
            except psycopg.OperationalError:
                pass
            time.sleep(0.1)
        self.save_failure_logs()
        raise AssertionError(
            "Dedicated H11 PostgreSQL did not become ready; "
            f"see private {self.directory / 'postgres-failure.log'}"
        )

    def save_failure_logs(self) -> None:
        """Keep owned-container diagnostics without exposing its environment."""
        result = subprocess.run(
            [*DOCKER, "logs", "--tail", "200", self.name],
            cwd=ROOT,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        diagnostic = (result.stdout + result.stderr).replace(self.password, "[REDACTED]")
        path = self.directory / "postgres-failure.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(diagnostic)
        private_json(
            self.directory / "postgres-failure.json",
            {
                "container": self.name,
                "host": "127.0.0.1",
                "port": self.port,
                "docker_logs_exit_code": result.returncode,
            },
        )

    def create_database(self) -> str:
        name = "h11_" + uuid4().hex
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.databases.add(name)
        dsn = make_conninfo(self.admin_dsn, dbname=name)
        migrate(dsn)
        return dsn

    def stop(self, *, abrupt: bool = False) -> None:
        self._owned_inspect()
        self.record_event("abrupt_kill_requested" if abrupt else "stop_requested")
        if abrupt:
            _docker("kill", "--signal", "KILL", self.name)
        else:
            _docker("stop", "--time", "1", self.name)
        self.record_event("container_stopped")

    def restart(self) -> None:
        self._owned_inspect()
        self.record_event("restart_requested")
        _docker("start", self.name)
        self.wait_ready()
        self.record_event("restart_ready")

    def export_evidence(self) -> None:
        destination_root = os.environ.get("H11_EVIDENCE_DIR")
        if not destination_root:
            return
        private_json(self.directory / "postgres-events.json", {"events": self.events})
        files = {"postgres-events.json": self.directory / "postgres-events.json"}
        if self.created:
            result = subprocess.run(
                [*DOCKER, "logs", "--timestamps", "--tail", "400", self.name],
                cwd=ROOT,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                capture_output=True,
                timeout=15,
                check=False,
            )
            path = self.directory / "postgres.log"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(result.stdout + result.stderr)
            files["postgres.log"] = path
        elif (self.directory / "postgres.log").is_file():
            files["postgres.log"] = self.directory / "postgres.log"
        export_safe_files(
            Path(destination_root) / "fault-postgres" / self.name,
            files,
            redactions=(self.password, self.admin_dsn),
        )

    def close(self) -> None:
        if self.created:
            self._owned_inspect()
            self.record_event("owned_cleanup_requested")
            self.export_evidence()
            # -v removes only anonymous volumes belonging to this owned container.
            _docker("rm", "--force", "--volumes", self.name)
            self.created = False
            self.record_event("owned_cleanup_completed")
            self.export_evidence()
        else:
            self.export_evidence()


def fixture_settings(database_url: str, directory: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        local_token=secrets.token_hex(24),
        project_id="h11-owned-fault-fixture",
        repository="example/h11-test",
        local_actor="owner",
        allowed_actors=["owner"],
        artifact_root=directory / "artifacts",
        harness_root=directory / "harness",
        harness_image_lock=ROOT / ".local/r03/sandbox-lock.json",
        allow_paid=False,
        allow_external_write=False,
        deepseek_api_key=None,
        github_read_token=None,
        github_write_token=None,
        router_mode="disabled",
    )


@dataclass(repr=False)
class Worker:
    process: subprocess.Popen[bytes] = field(repr=False)
    directory: Path
    log_stream: Any = field(repr=False)
    redactions: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def launch(cls, directory: Path, payload: dict[str, Any]) -> Worker:
        directory.mkdir(parents=True, exist_ok=True)
        config = directory / "worker.json"
        private_json(config, payload)
        log_descriptor = os.open(
            directory / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        stream = os.fdopen(log_descriptor, "wb")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")]),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        }
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "scripts/h11_fault_probe.py"), str(config)],
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        database_url = str(payload.get("database_url", ""))
        password = conninfo_to_dict(database_url).get("password", "") if database_url else ""
        return cls(
            process=process,
            directory=directory,
            log_stream=stream,
            redactions=(database_url, password),
        )

    def wait_marker(self, name: str = "fault.json", timeout: float = 45.0) -> dict[str, Any]:
        return wait_json(self.directory / name, process=self.process, timeout=timeout)

    def kill(self) -> None:
        if self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGKILL)
        code = self.process.wait(timeout=15)
        self.log_stream.close()
        if code != -signal.SIGKILL:
            raise AssertionError(f"H11 worker expected actual SIGKILL, got {code}")

    def finish(self, timeout: float = 90.0) -> dict[str, Any]:
        code = self.process.wait(timeout=timeout)
        self.log_stream.close()
        if code != 0:
            raise AssertionError(f"H11 resumed worker exited {code}; see private worker.log")
        return read_json(self.directory / "result.json")

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.kill()
            elif not self.log_stream.closed:
                self.log_stream.close()
        finally:
            destination_root = os.environ.get("H11_EVIDENCE_DIR")
            if destination_root:
                private_json(
                    self.directory / "process.json",
                    {
                        "pid": self.process.pid,
                        "returncode": self.process.returncode,
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                )
                selected_files = {
                    "process.json": self.directory / "process.json",
                    "fault.json": self.directory / "fault.json",
                    "result.json": self.directory / "result.json",
                    "worker.log": self.directory / "worker.log",
                    "provider_calls.jsonl": self.directory.parent
                    / "shared"
                    / "provider_calls.jsonl",
                }
                # These are only controlled fixture CAS objects, never the
                # private worker configuration or PostgreSQL environment.
                for artifact in (self.directory.parent / "shared" / "artifacts").glob("*/*"):
                    if artifact.is_file() and re.fullmatch(r"[a-f0-9]{64}", artifact.name):
                        content = artifact.read_bytes()
                        if sha256(content).hexdigest() != artifact.name or any(
                            secret and secret.encode() in content for secret in self.redactions
                        ):
                            raise AssertionError("H11 fixture CAS failed safe evidence validation")
                        selected_files["cas-" + artifact.name] = artifact
                export_safe_files(
                    Path(destination_root)
                    / "fault-cases"
                    / self.directory.parent.name
                    / self.directory.name,
                    selected_files,
                    redactions=self.redactions,
                )
