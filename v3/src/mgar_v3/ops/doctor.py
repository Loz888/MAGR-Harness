"""Redacted diagnostic results for the local Harness controller."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg
from redis import Redis
from redis.exceptions import RedisError

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker, _docker_environment
from mgar_v3.harness.product_runtime import product_readiness
from mgar_v3.settings import ROOT, Settings


def migration_status(conn: psycopg.Connection[Any], directory: Path) -> dict[str, Any]:
    required = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(directory.glob("*.sql"))}
    if not required:
        return {"code": "MIGRATIONS_MISSING"}
    table = conn.execute("SELECT to_regclass('app.schema_migrations')").fetchone()
    if not table or not table[0]:
        return {"code": "SCHEMA_NOT_INITIALIZED"}
    applied = dict(conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall())
    unknown = sorted(applied.keys() - required.keys())
    changed = sorted(name for name in applied.keys() & required.keys()
                     if applied[name] != required[name])
    pending = sorted(required.keys() - applied.keys())
    if changed:
        return {"code": "SCHEMA_CHECKSUM_MISMATCH", "files": changed}
    if unknown:
        return {"code": "SCHEMA_NEWER_THAN_RELEASE", "files": unknown}
    if pending:
        return {"code": "SCHEMA_UPGRADE_REQUIRED", "files": pending}
    return {"code": "OK", "applied": len(applied), "latest": max(applied)}


def _postgres(settings: Settings) -> dict[str, Any]:
    try:
        with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=3) as conn:
            return migration_status(conn, ROOT / "migrations")
    except psycopg.Error as exc:
        details = str(exc).lower()
        if exc.sqlstate in {"28P01", "28000"} or "password authentication failed" in details:
            return {"code": "PG_AUTHENTICATION_FAILED"}
        if exc.sqlstate == "3D000" or "does not exist" in details and "database " in details:
            return {"code": "PG_DATABASE_MISSING"}
        if exc.sqlstate == "42501":
            return {"code": "PG_PERMISSION_DENIED"}
        if isinstance(exc, psycopg.ProgrammingError):
            return {"code": "PG_CONFIGURATION_INVALID"}
        return {"code": "PG_UNREACHABLE"}


def _cache(settings: Settings) -> dict[str, Any]:
    try:
        with Redis.from_url(
            settings.redis_url.get_secret_value(), socket_connect_timeout=3, socket_timeout=3,
        ) as cache:
            return {"code": "OK" if cache.ping() else "REDIS_UNREACHABLE"}
    except (OSError, RedisError, ValueError):
        return {"code": "REDIS_UNREACHABLE"}


def _sandbox(lock_path: Path) -> dict[str, Any]:
    def check(*args: str) -> bool:
        return subprocess.run(
            ("docker", *args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_docker_environment(), timeout=10, check=False,
        ).returncode == 0

    try:
        if not check("info", "--format", "{{.ServerVersion}}"):
            return {"code": "DOCKER_UNREACHABLE"}
        if not lock_path.is_file():
            return {"code": "SANDBOX_LOCK_MISSING"}
        image = IsolatedExecutionBroker._image_from_lock(lock_path)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            return {"code": "SANDBOX_LOCK_INVALID"}
        if not check("image", "inspect", image):
            return {"code": "SANDBOX_IMAGE_MISSING"}
        usable = check(
            "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=64m",
            "--entrypoint=/bin/true", image,
        )
        return {"code": "OK" if usable else "SANDBOX_START_FAILED"}
    except FileNotFoundError:
        return {"code": "DOCKER_MISSING"}
    except subprocess.TimeoutExpired:
        return {"code": "DOCKER_TIMEOUT"}
    except DomainError:
        return {"code": "SANDBOX_LOCK_INVALID"}
    except OSError:
        return {"code": "DOCKER_UNAVAILABLE"}


def _network(url: str) -> dict[str, Any]:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            return {"code": "NETWORK_URL_INVALID"}
        response = httpx.head(url, timeout=4, follow_redirects=False)
    except (ValueError, httpx.InvalidURL):
        return {"code": "NETWORK_URL_INVALID"}
    except httpx.TimeoutException:
        return {"code": "NETWORK_TIMEOUT"}
    except (httpx.ProxyError, httpx.ConnectError):
        return {"code": "NETWORK_CONNECTION_FAILED"}
    except httpx.HTTPError:
        return {"code": "NETWORK_UNREACHABLE"}
    if response.status_code in {401, 403, 407, 451}:
        return {"code": "NETWORK_ACCESS_DENIED", "http_status": response.status_code}
    return {"code": "OK", "http_status": response.status_code}


def diagnose(
    settings: Settings, *, project: Path | None = None, network_url: str | None = None,
) -> tuple[dict[str, Any], int]:
    checks = {
        "postgres": _postgres(settings), "redis": _cache(settings),
        "sandbox": _sandbox(settings.harness_image_lock),
    }
    if project is not None:
        if not project.is_dir():
            checks["project"] = {"code": "PROJECT_DIRECTORY_UNAVAILABLE"}
        else:
            try:
                prepared = product_readiness(project, settings.harness_image_lock)
                checks["project"] = {
                    "code": "OK" if prepared["status"] == "ready" else "PROJECT_NOT_READY",
                    "readiness": prepared["status"],
                }
            except (OSError, DomainError):
                checks["project"] = {"code": "PROJECT_PREPARATION_FAILED"}
    if network_url is not None:
        checks["network"] = _network(network_url)
    result = {name: "PASS" if check["code"] == "OK" else "FAIL"
              for name, check in checks.items()}
    return {**result, "live": "NOT_RUN", "diagnostics": checks}, (
        0 if all(value == "PASS" for value in result.values()) else 2
    )
