"""Bounded public probe assets and derived facts from real Broker receipts.

Public probes are engineering signals, never a substitute for independent
private scoring. Only a trusted, newly frozen controller can supply the plan.
This module neither grants execution permission nor owns another subprocess,
model call, budget or database connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import ExecutionReceipt, TaskBinding, sha256_text
from mgar_v3.harness.public_probe_types import (
    PUBLIC_PROBE_ASSET_LIMIT,
    PUBLIC_PROBE_PREFIX,
    PublicProbeCase,
    PublicProbeFrame,
    PublicProbePlan,
)


@contextmanager
def pinned_asset(plan: PublicProbePlan, root: Path) -> Iterator[tuple[Path, int]]:
    """Open the one canonical asset with no symlink at any path component."""
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise DomainError("PUBLIC_PROBE_ASSET_DENIED", "Public probe root must be canonical", 403)
    descriptors: list[int] = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open(root.anchor, directory_flags)
        descriptors.append(current)
        for part in root.parts[1:]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        directory = os.open(plan.asset_sha256, directory_flags, dir_fd=current)
        descriptors.append(directory)
        descriptor = os.open(
            "probe.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= PUBLIC_PROBE_ASSET_LIMIT:
            raise DomainError("PUBLIC_PROBE_ASSET_DENIED", "Public probe asset is not bounded", 403)
        data = os.read(descriptor, PUBLIC_PROBE_ASSET_LIMIT + 1)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(data) != before.st_size:
            raise DomainError("PUBLIC_PROBE_ASSET_CHANGED", "Public probe asset changed", 409)
        if hashlib.sha256(data).hexdigest() != plan.asset_sha256:
            raise DomainError("PUBLIC_PROBE_ASSET_HASH", "Public probe asset hash differs", 409)
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield root / plan.asset_sha256 / "probe.py", descriptor
    except OSError as exc:
        raise DomainError(
            "PUBLIC_PROBE_ASSET_DENIED", "Public probe asset is missing or traverses a symlink", 403
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_asset(plan: PublicProbePlan, root: Path) -> bytes:
    with pinned_asset(plan, root) as (_, descriptor):
        data = os.read(descriptor, PUBLIC_PROBE_ASSET_LIMIT + 1)
        if hashlib.sha256(data).hexdigest() != plan.asset_sha256:
            raise DomainError("PUBLIC_PROBE_ASSET_CHANGED", "Public probe asset changed", 409)
        return data


class PublicProbeResult(Model):
    schema_version: Literal["harness-public-probe-result-v1"] = "harness-public-probe-result-v1"
    plan_hash: str
    asset_sha256: str
    task_id: str
    execution_epoch: int
    tree_hash: str
    source_receipt_id: str
    outcome: Literal["red", "green", "invalid"]
    reason_code: str
    cases: tuple[PublicProbeCase, ...] = ()
    execution: ExecutionReceipt


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Duplicate JSON keys")
    return dict(pairs)


def normalize_probe_receipt(
    plan: PublicProbePlan,
    raw: ExecutionReceipt,
    *,
    task: TaskBinding,
    expected_tree: str,
    log_complete: bool,
    log_limit_bytes: int,
) -> PublicProbeResult:
    """Bind strict probe facts to the actual command, tree and generating epoch.

    Known H11 receipts may retain an earlier generating epoch. The caller is
    responsible for replay authorization using the original recovery ledger;
    an old provenance is never rewritten as a newly executed command.
    """
    reason = ""
    frame: PublicProbeFrame | None = None
    output = raw.stdout + raw.stderr
    provenance = raw.provenance
    if raw.argv != plan.argv or raw.purpose != "test":
        reason = "PUBLIC_PROBE_COMMAND_MISMATCH"
    elif raw.tree_hash != expected_tree:
        reason = "PUBLIC_PROBE_TREE_MISMATCH"
    elif provenance is None or (
        provenance.actor != task.actor
        or provenance.workspace_id != task.workspace_id
        or provenance.repository != task.repository
        or provenance.execution_epoch > task.execution_epoch
        or provenance.execution_epoch < 1
    ):
        reason = "PUBLIC_PROBE_PROVENANCE_MISMATCH"
    elif not log_complete or log_limit_bytes <= 0 or len(output.encode()) >= log_limit_bytes:
        reason = "PUBLIC_PROBE_LOG_INCOMPLETE"
    elif sha256_text(output) != raw.log_sha256:
        reason = "PUBLIC_PROBE_LOG_HASH_MISMATCH"
    elif raw.status not in {"passed", "failed"}:
        reason = "PUBLIC_PROBE_EXECUTION_ERROR"
    elif raw.stderr or not output.startswith(PUBLIC_PROBE_PREFIX) or not output.endswith("\n"):
        reason = "PUBLIC_PROBE_FRAME_INVALID"
    elif output.count("\n") != 1 or "\r" in output:
        reason = "PUBLIC_PROBE_FRAME_INVALID"
    else:
        try:
            value = json.loads(
                output[len(PUBLIC_PROBE_PREFIX) : -1], object_pairs_hook=_unique_object
            )
            frame = PublicProbeFrame.model_validate(value)
        except (ValueError, ValidationError, RecursionError):
            reason = "PUBLIC_PROBE_FRAME_INVALID"
        if frame is not None and not reason:
            if frame.plan_id != plan.plan_id or frame.asset_sha256 != plan.asset_sha256:
                reason = "PUBLIC_PROBE_PLAN_MISMATCH"
            elif {case.id for case in frame.cases} != set(plan.case_ids):
                reason = "PUBLIC_PROBE_CASES_MISMATCH"
    outcome: Literal["red", "green", "invalid"] = "invalid"
    if not reason and frame is not None:
        states = [case.status for case in frame.cases]
        if raw.status == "passed" and raw.exit_code == 0 and all(s == "passed" for s in states):
            outcome = "green"
        elif (
            raw.status == "failed"
            and raw.exit_code == 1
            and "assertion_failed" in states
            and all(s in {"passed", "assertion_failed"} for s in states)
        ):
            outcome = "red"
        else:
            reason = "PUBLIC_PROBE_NOT_ASSERTION_RESULT"
    epoch = provenance.execution_epoch if provenance is not None else task.execution_epoch
    metadata = {
        "plan": plan.model_dump(mode="json"),
        "plan_hash": plan.plan_hash,
        "asset_sha256": plan.asset_sha256,
        "task_id": task.task_id,
        "execution_epoch": epoch,
        "tree_hash": raw.tree_hash,
        "outcome": outcome,
        "source_receipt_id": raw.receipt_id,
        "reason_code": reason,
        "cases": [case.model_dump(mode="json") for case in frame.cases] if frame else [],
    }
    cases = frame.cases if frame is not None else ()
    status = "passed" if outcome == "green" else "failed" if outcome == "red" else "error"
    updates = {
        "receipt_id": digest(
            {
                "schema_version": "harness-public-probe-execution-v1",
                "raw": raw.model_dump(mode="json"),
                "public_probe": metadata,
            }
        ),
        "status": status,
        "tests_run": len(cases) if outcome != "invalid" else 0,
        "tests_failed": sum(case.status == "assertion_failed" for case in cases)
        if outcome == "red"
        else 0,
        "public_probe": metadata,
    }
    execution = raw.model_copy(update=updates)
    return PublicProbeResult(
        plan_hash=plan.plan_hash,
        asset_sha256=plan.asset_sha256,
        task_id=task.task_id,
        execution_epoch=epoch,
        tree_hash=raw.tree_hash,
        source_receipt_id=raw.receipt_id,
        outcome=outcome,
        reason_code=reason,
        cases=cases,
        execution=execution,
    )
