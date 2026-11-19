"""Offline PostgreSQL and content-store snapshots for local Harness recovery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import psycopg
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from mgar_v3.domain import DomainError, digest
from mgar_v3.settings import Settings

_SCHEMA = "magr-harness-snapshot-v1"
_CAS = re.compile(r"artifacts/([a-f0-9]{2})/([a-f0-9]{64})")
_HASH = re.compile(r"[a-f0-9]{64}")
_DUMP = "database.dump"
_KEY = "web/credentials.key"
_PG_FIELDS = {
    "host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "sslrootcert",
    "sslcert", "sslkey", "connect_timeout", "options", "application_name",
}
_PG_OVERRIDES = {"dbname": "PGDATABASE"}
_RESTORE_LOCK = 31010018
_LOGGER = logging.getLogger(__name__)


def _pg_tool(tool: str, dsn: str, *args: str) -> None:
    params = {key: str(value) for key, value in conninfo_to_dict(dsn).items()
              if value is not None}
    if not set(params).issubset(_PG_FIELDS) or not params.get("dbname"):
        raise DomainError("SNAPSHOT_DSN_UNSUPPORTED", "Use an explicit PostgreSQL database DSN")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update({_PG_OVERRIDES.get(key, "PG" + key.upper()): value
                        for key, value in params.items()})
    try:
        result = subprocess.run(
            (tool, "--no-password", *args), env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900, check=False,
        )
    except FileNotFoundError as exc:
        raise DomainError("SNAPSHOT_POSTGRES_CLIENT_MISSING", f"Install {tool}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DomainError("SNAPSHOT_TOOL_TIMEOUT", f"{tool} exceeded its deadline") from exc
    if result.returncode < 0:
        raise DomainError("SNAPSHOT_TOOL_INTERRUPTED", f"{tool} was interrupted")
    if result.returncode:
        raise DomainError("SNAPSHOT_POSTGRES_TOOL_FAILED", f"{tool} failed")


def _hash_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise DomainError("SNAPSHOT_FILE_UNAVAILABLE", "Snapshot requires a regular file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy(source: Path, destination: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise DomainError("SNAPSHOT_FILE_UNAVAILABLE", "Snapshot requires a regular file")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        destination.chmod(0o600)
        shutil.copyfileobj(reader, writer)
    return _hash_file(destination)


def _validate_artifacts(conn: psycopg.Connection[Any], data: Path) -> None:
    for value, size in conn.execute("SELECT content_hash,byte_count FROM app.artifacts"):
        path = data / "artifacts" / value[:2] / value
        if not path.is_file() or path.stat().st_size != size or _hash_file(path) != value:
            raise DomainError(
                "SNAPSHOT_CAS_MISMATCH", "Database artifact bytes are missing or invalid",
            )


def _publish(stage: Path, output: Path) -> None:
    # Reserve a new destination without ever replacing an existing backup or runtime.
    output.mkdir(mode=0o700)
    try:
        stage.rename(output)
    except OSError:
        output.rmdir()
        raise


def backup(settings: Settings, output: Path) -> dict[str, Any]:
    root = settings.artifact_root.resolve()
    output = output.resolve()
    if output.is_relative_to(root) or output.is_relative_to(settings.harness_root.resolve()):
        raise DomainError("SNAPSHOT_OUTPUT_IN_RUNTIME", "Use a separate snapshot destination")
    if output.exists():
        raise DomainError("SNAPSHOT_DESTINATION_EXISTS", "Choose a new snapshot directory")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    dsn = settings.database_url.get_secret_value()
    with TemporaryDirectory(dir=output.parent, prefix=".backup-") as tmp:
        stage = Path(tmp) / "snapshot"
        stage.mkdir(mode=0o700)
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if conn.execute("SELECT 1 FROM app.runs WHERE status='running' LIMIT 1").fetchone():
                raise DomainError("SNAPSHOT_RUNS_ACTIVE", "Quiesce the controller before backup")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()
            assert snapshot is not None
            _pg_tool("pg_dump", dsn, "--format=custom", f"--snapshot={snapshot[0]}",
                     "--file", str(stage / _DUMP))
            files = {_DUMP: _hash_file(stage / _DUMP)}
            (stage / _DUMP).chmod(0o600)
            for shard in sorted(root.iterdir()) if root.exists() else ():
                if not re.fullmatch(r"[a-f0-9]{2}", shard.name):
                    continue
                if shard.is_symlink() or not shard.is_dir():
                    raise DomainError("SNAPSHOT_CAS_MISMATCH", "Invalid content store shard")
                for obj in sorted(shard.iterdir()):
                    if not _HASH.fullmatch(obj.name):
                        continue
                    name = f"artifacts/{shard.name}/{obj.name}"
                    checksum = _copy(obj, stage / name)
                    if obj.name[:2] != shard.name or checksum != obj.name:
                        raise DomainError("SNAPSHOT_CAS_MISMATCH", "Content store checksum differs")
                    files[name] = checksum
            key = root.parent / _KEY
            key_required = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM app.web_analysis_settings)"
            ).fetchone()
            tenant_keys = conn.execute(
                "SELECT to_regclass('app.harness_project_credentials')"
            ).fetchone()
            if tenant_keys and tenant_keys[0] and not (key_required and key_required[0]):
                key_required = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM app.harness_project_credentials)"
                ).fetchone()
            if key.exists() or (key_required and key_required[0]):
                files[_KEY] = _copy(key, stage / _KEY)
                try:
                    Fernet((stage / _KEY).read_bytes())
                except ValueError as exc:
                    raise DomainError(
                        "SNAPSHOT_KEY_INVALID", "Credential key is malformed",
                    ) from exc
            _validate_artifacts(conn, stage)
            migrations: dict[str, str] = dict(conn.execute(
                "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
            ).fetchall())
        (stage / "snapshot.json").write_text(json.dumps({
            "schema": _SCHEMA, "files": files, "migrations": migrations,
        }, indent=2, sort_keys=True) + "\n")
        (stage / "snapshot.json").chmod(0o600)
        _publish(stage, output)
    return {"schema": _SCHEMA, "files": len(files), "migrations": len(migrations)}


def _manifest(source: Path) -> dict[str, Any]:
    raw = source / "snapshot.json"
    _hash_file(raw)
    try:
        manifest = json.loads(raw.read_text())
        files = manifest["files"]
        if (manifest["schema"] != _SCHEMA or not isinstance(files, dict)
                or _DUMP not in files or not isinstance(manifest["migrations"], dict)):
            raise ValueError
        for name, checksum in files.items():
            if not isinstance(checksum, str) or not _HASH.fullmatch(checksum):
                raise ValueError
            if name in {_DUMP, _KEY}:
                continue
            match = _CAS.fullmatch(name)
            if not match or match[1] != match[2][:2] or checksum != match[2]:
                raise ValueError
    except (TypeError, ValueError, KeyError) as exc:
        raise DomainError("SNAPSHOT_MANIFEST_INVALID", "Invalid snapshot manifest") from exc
    return cast(dict[str, Any], manifest)


def _database_occupied(conn: psycopg.Connection[Any]) -> bool:
    return conn.execute(
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname NOT IN ('public','information_schema') AND nspname NOT LIKE 'pg_%' "
        "UNION ALL SELECT tablename FROM pg_tables WHERE schemaname='public' LIMIT 1"
    ).fetchone() is not None


def _restore_progress(receipt: Path, binding: str) -> str | None:
    if not receipt.exists() and not receipt.is_symlink():
        return None
    if not receipt.is_symlink() and receipt.is_file():
        content = receipt.read_text()
        if content == f"{binding}\n":
            return "prepared"
        if content == f"{binding}\nimported\n":
            return "imported"
    raise DomainError(
        "SNAPSHOT_RESTORE_SCOPE_MISMATCH",
        "The restore receipt does not match this snapshot and target",
    )


def _clear_restore_receipt(receipt: Path) -> None:
    try:
        receipt.unlink()
    except OSError:
        _LOGGER.warning("Could not remove the snapshot restore receipt")


def _load_into_empty_database(
    conn: psycopg.Connection[Any], dsn: str, receipt: Path, scratch: Path, binding: str,
    runtime: Path,
) -> None:
    target_db = make_conninfo("", dbname=conn.info.dbname)
    staged = scratch / "receipt"
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as journal:
        journal.write(binding + "\n")
        journal.flush()
        os.fsync(journal.fileno())
        try:
            os.link(staged, receipt)
        except FileExistsError as exc:
            raise DomainError(
                "SNAPSHOT_RESTORE_BUSY", "Another restore owns this destination",
            ) from exc
        if runtime.exists():
            _clear_restore_receipt(receipt)
            raise DomainError("SNAPSHOT_DESTINATION_EXISTS", "Restore into a new runtime directory")
        try:
            _pg_tool("pg_restore", dsn, "--dbname", target_db,
                     "--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
                     str(scratch / _DUMP))
        except DomainError as exc:
            if exc.code in {"SNAPSHOT_POSTGRES_CLIENT_MISSING", "SNAPSHOT_DSN_UNSUPPORTED"}:
                _clear_restore_receipt(receipt)
            elif exc.code == "SNAPSHOT_POSTGRES_TOOL_FAILED":
                # Preserve the receipt if a database error prevents proving rollback.
                try:
                    if not _database_occupied(conn):
                        _clear_restore_receipt(receipt)
                except psycopg.Error:
                    pass
            raise
        # Record the completed import before publishing. The open inode also
        # permits recording it if the parent directory has become unwritable.
        journal.write("imported\n")
        journal.flush()
        os.fsync(journal.fileno())


def restore(source: Path, dsn: str, runtime: Path) -> dict[str, Any]:
    source, runtime = source.resolve(), runtime.resolve()
    if runtime.exists() or runtime.is_relative_to(source):
        raise DomainError("SNAPSHOT_DESTINATION_EXISTS", "Restore into a new runtime directory")
    manifest = _manifest(source)
    runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = runtime.parent / (".mgar-restore-" + digest(runtime.name))
    # The session lock spans pg_restore and publication without holding a SQL
    # transaction. The filesystem receipt also protects a shared runtime path.
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (_RESTORE_LOCK,)).fetchone()
        if not locked or not locked[0]:
            raise DomainError("SNAPSHOT_RESTORE_BUSY", "Another restore owns this database")
        database = conn.execute(
            "SELECT oid FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        assert database is not None
        binding = digest({
            "manifest": manifest, "runtime": str(runtime), "database_oid": database[0],
            "host": conn.info.host, "port": conn.info.port, "database": conn.info.dbname,
        })
        progress = _restore_progress(receipt, binding)
        if progress == "prepared":
            raise DomainError(
                "SNAPSHOT_RESTORE_OUTCOME_UNKNOWN",
                "Reconcile the earlier database import before restoring again",
            )
        if progress is None and _database_occupied(conn):
            raise DomainError(
                "SNAPSHOT_TARGET_NOT_EMPTY", "Restore requires an empty target database",
            )
        with TemporaryDirectory(dir=runtime.parent, prefix=".restore-") as tmp:
            scratch = Path(tmp)
            stage = scratch / "runtime"
            (stage / "artifacts").mkdir(mode=0o700, parents=True)
            stage.chmod(0o700)
            for name, expected in manifest["files"].items():
                original = source / name
                if original.resolve() != original or original.is_symlink():
                    raise DomainError("SNAPSHOT_FILE_UNAVAILABLE", "Invalid snapshot file path")
                if name == _DUMP and progress == "imported":
                    continue
                destination = scratch / _DUMP if name == _DUMP else stage / name
                if _copy(original, destination) != expected:
                    raise DomainError("SNAPSHOT_CHECKSUM_MISMATCH", "Snapshot file hash differs")
            if progress is None:
                _load_into_empty_database(conn, dsn, receipt, scratch, binding, runtime)
            actual: dict[str, str] = dict(conn.execute(
                "SELECT name,sha256 FROM app.schema_migrations"
            ).fetchall())
            if actual != manifest["migrations"]:
                raise DomainError("SNAPSHOT_SCHEMA_MISMATCH", "Restored migration history differs")
            _validate_artifacts(conn, stage)
            try:
                _publish(stage, runtime)
            except OSError as exc:
                raise DomainError(
                    "SNAPSHOT_RUNTIME_PUBLISH_FAILED",
                    "Database imported and verified. Fix the filesystem and retry the same command",
                ) from exc
            _clear_restore_receipt(receipt)
    return {"schema": _SCHEMA, "files": len(manifest["files"]), "migrations": len(actual)}
