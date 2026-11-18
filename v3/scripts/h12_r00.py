"""R00 evidence snapshots and offline diagnostics; never execute a model or repair a Run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def exported_task_metadata(value: Any) -> dict[str, Any] | None:
    """A task export can supply catalog metadata; a local workspace registration cannot."""
    if not isinstance(value, dict):
        return None
    task = value.get("task")
    if (
        not isinstance(task, dict)
        or not isinstance(task.get("task_id"), str)
        or not isinstance(task.get("repository"), str)
        or not isinstance(task.get("base_commit"), str)
        or re.fullmatch("[0-9a-f]{40}", task["base_commit"]) is None
    ):
        return None
    return task


def evidence_status(*, attempted: bool, blocked: bool = False, verified: bool | None = None) -> str:
    """Classify an evidence check, not a historical model score."""
    if blocked:
        return "BLOCKED"
    if not attempted:
        if verified is not None:
            raise ValueError("An unattempted check cannot have a verification result")
        return "NOT_RUN"
    if verified is None:
        return "BLOCKED"
    return "PASS" if verified else "FAIL"


class ReadonlyArchive:
    """A read-only evidence backup. It is never a business state or recovery store."""

    def __init__(self, attempt: Path):
        self.attempt = attempt.resolve()
        self.objects = self.attempt / "private" / "blobs"
        self.objects.mkdir(parents=True, mode=0o700)
        self.objects.parent.chmod(0o700)
        self.entries: dict[str, dict[str, Any]] = {}

    def add(self, raw: bytes, *, origin: str, category: str) -> dict[str, Any]:
        checksum = digest_bytes(raw)
        if origin in self.entries:
            entry = self.entries[origin]
            if entry["sha256"] != checksum:
                raise ValueError("Evidence changed while taking the snapshot")
            if category not in entry["categories"]:
                entry["categories"].append(category)
            return entry
        target = self.objects / checksum
        if target.exists() or target.is_symlink():
            if target.is_symlink() or digest_bytes(target.read_bytes()) != checksum:
                raise ValueError("Archive object collision or symlink")
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        entry = {
            "origin": origin,
            "sha256": checksum,
            "bytes": len(raw),
            "archive_path": str(target.relative_to(self.attempt)),
            "categories": [category],
            "visibility": "controller_private",
        }
        self.entries[origin] = entry
        return entry

    def add_file(self, source: Path, *, category: str) -> dict[str, Any]:
        if source.is_symlink():
            raise ValueError("Evidence symlinks are not followed")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Evidence must be a regular file")
            raw = stream.read()
            after = os.fstat(stream.fileno())
        current = source.stat(follow_symlinks=False)
        signature = lambda value: (  # noqa: E731
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
        )
        if signature(before) != signature(after) or signature(before) != signature(current):
            raise ValueError("Evidence changed during the read")
        entry = self.add(raw, origin=str(source.absolute()), category=category)
        entry["source_mode"] = stat.S_IMODE(before.st_mode)
        return entry

    def seal(self) -> Path:
        target = self.attempt / "evidence-manifest.json"
        document = {
            "schema": "h12-r00-evidence-manifest-v1",
            "status": evidence_status(attempted=bool(self.entries), verified=True)
            if self.entries
            else "NOT_RUN",
            "entries": list(self.entries.values()),
            "purpose": "Read-only provenance backup; PostgreSQL remains authoritative",
        }
        with target.open("x") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        target.chmod(0o400)
        self.objects.chmod(0o500)
        return target


def verify_archive(attempt: Path) -> dict[str, Any]:
    manifest = attempt / "evidence-manifest.json"
    if manifest.is_symlink():
        return {"status": "FAIL", "reason": "MANIFEST_SYMLINK"}
    if not manifest.is_file():
        return {"status": "BLOCKED", "reason": "MANIFEST_MISSING"}
    try:
        document = json.loads(manifest.read_bytes())
        if document.get("schema") != "h12-r00-evidence-manifest-v1":
            raise ValueError("Unsupported manifest schema")
        entries = document["entries"]
        if not entries:
            return {"status": "NOT_RUN", "reason": "NO_ARCHIVED_EVIDENCE"}
        origins: set[str] = set()
        for entry in entries:
            checksum = entry["sha256"]
            if not isinstance(checksum, str) or re.fullmatch("[0-9a-f]{64}", checksum) is None:
                raise ValueError("Invalid hash")
            relative = entry["archive_path"]
            if relative != f"private/blobs/{checksum}":
                raise ValueError("Invalid archive path")
            target = attempt / relative
            if (
                target.is_symlink()
                or not target.resolve().is_relative_to(attempt.resolve())
                or not target.is_file()
                or target.stat().st_mode & 0o222
            ):
                raise ValueError("Evidence is missing, writable, or outside the archive")
            raw = target.read_bytes()
            if digest_bytes(raw) != checksum or len(raw) != entry["bytes"]:
                raise ValueError("Evidence hash/size mismatch")
            if entry["origin"] in origins or entry["visibility"] != "controller_private":
                raise ValueError("Duplicate origin or wrong visibility")
            origins.add(entry["origin"])
        if manifest.stat().st_mode & 0o222:
            raise ValueError("Manifest is writable")
        return {
            "status": "PASS",
            "verified_entries": len(entries),
            "unique_objects": len({e["sha256"] for e in entries}),
        }
    except (KeyError, TypeError, ValueError, OSError):
        return {"status": "FAIL", "reason": "EVIDENCE_INTEGRITY_FAILED"}


def replay(fixture: dict[str, Any]) -> dict[str, Any]:
    """Observe a synthetic recorded protocol failure, without adding remediation policy."""
    kind, data = fixture["kind"], fixture["observation"]
    if fixture["visibility"] != "public_synthetic":
        raise ValueError("Private evidence must not become a public diagnostic fixture")
    if kind == "contract_loss":
        missing = sorted(set(data["required_ids"]) - set(data["projected_ids"]))
        return {"issue_observed": bool(missing), "missing_required_ids": missing}
    if kind == "output_truncated":
        return {"issue_observed": data["finish_reason"] == "length", "completion_verified": False}
    if kind == "invalid_tool_call":
        try:
            parsed = json.loads(data["arguments"])
        except json.JSONDecodeError:
            parsed = None
        return {"issue_observed": not isinstance(parsed, dict), "dispatch_allowed": False}
    if kind == "path_denied":
        from mgar_v3.domain import DomainError
        from mgar_v3.harness.contracts import normalize_relative_path

        try:
            normalize_relative_path(data["path"])
        except DomainError as exc:
            return {"issue_observed": True, "denial_code": exc.code}
        return {"issue_observed": False}
    if kind == "ready_missing":
        ready = re.search(data["required_ready_pattern"], data["stdout"]) is not None
        return {"issue_observed": not ready, "http_success_alone_is_ready": False}
    if kind == "regression_not_run":
        unverified_zero = data["independent_receipt"] is None and data["claimed_failures"] == 0
        return {"issue_observed": unverified_zero, "verified_failure_count": None}
    if kind == "feature_red_baseline":
        from mgar_v3.quality_eval.h12_public_regression import missing_public_go_target

        expected = missing_public_go_target(data["compiler_output"], tuple(data["public_files"]))
        return {
            "issue_observed": data["legacy_environment_status"] == "BLOCKED" and expected,
            "declared_missing_target": expected,
        }
    if kind == "refactor_structure":
        return {
            "issue_observed": data["original_behavior_passed"]
            and not data["original_has_new_structure"]
            and data["legacy_baseline_required_new_structure"],
            "original_behavior_passed": data["original_behavior_passed"],
        }
    raise ValueError("Unknown diagnostic fixture type")
