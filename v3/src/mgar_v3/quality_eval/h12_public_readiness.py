"""Zero-model original-tree readiness for explicitly frozen public dev plans.

This is an engineering controller, not a model tool or a quality judge. Every
process uses the existing read-only Broker and CommandSession. No Engine,
wallet, model, hidden assertion, or other split is consulted here.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import CapabilityLease, IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    EvaluationProfile,
    ExecutionReceipt,
    ExecutionRequest,
    TaskBinding,
    VerificationRequirement,
    canonical_hash,
    sha256_text,
)
from mgar_v3.harness.project_commands import ProjectKind, parse_tests
from mgar_v3.harness.public_probe import normalize_probe_receipt, read_asset
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval.h12_public_plan import (
    FrozenPublicPlan,
    _manifest_bytes,
    validate_public_original,
)
from mgar_v3.quality_eval.h12_public_regression import declared_assertion_failure
from mgar_v3.quality_eval.harness_v4 import CatalogError

SCHEMA = "h12-public-original-readiness-v2"
LEGACY_SCHEMA = "h12-public-original-readiness-v1"


def _profile_hash(profile: EvaluationProfile, schema: str) -> str:
    # A zero-model environment proof precedes experiment registration. Its
    # authority and resources are fixed, but the later experiment name is not.
    # Keep the original hash semantics for historical v1 evidence.
    excluded = {"experiment_id"} if schema == SCHEMA else set()
    return canonical_hash(profile.model_dump(mode="json", exclude=excluded))


def write_evidence_json(path: Path, value: Any) -> str:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def read_evidence(path: Path, expected: str, *, limit: int = 8_388_608) -> bytes:
    """Recheck explicit bounded evidence; never follow an evidence symlink."""
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise CatalogError("Public readiness evidence must not traverse symlinks")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CatalogError("Public readiness evidence must be a regular file")
            raw = stream.read(limit + 1)
    except OSError:
        raise CatalogError("Public readiness evidence is unavailable") from None
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != expected:
        raise CatalogError("Public readiness evidence changed from its explicit hash")
    return raw


def read_evidence_json(path: Path, expected: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in items}) != len(items):
            raise CatalogError("Duplicate public readiness evidence field")
        return dict(items)

    try:
        value = json.loads(read_evidence(path, expected), object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise CatalogError("Public readiness evidence is not valid JSON") from None
    if not isinstance(value, dict):
        raise CatalogError("Public readiness evidence must be an object")
    return value


def _mount(frozen: FrozenPublicPlan) -> dict[str, Any]:
    return {
        "workspace_read_only": True,
        "probe_read_only": True,
        "probe_asset_sha256": frozen.plan.asset_sha256,
        "probe_container_path": frozen.plan.container_path,
        "probe_single_file": True,
    }


def _observe(
    session: CommandSession,
    grant: CapabilityGrant,
    argv: tuple[str, ...],
    *,
    role: str,
    deadline: float,
    evidence: Path,
) -> dict[str, Any]:
    remaining = min(600.0, deadline - time.monotonic())
    if remaining <= 0:
        raise CatalogError("Public original readiness exhausted its frozen deadline")
    operation_id = "public-readiness-" + role + "-" + uuid4().hex[:16]
    started = time.monotonic()
    request = ExecutionRequest(
        operation_id=operation_id,
        purpose="test",
        argv=argv,
        cwd=".",
        execution_epoch=1,
        timeout_seconds=remaining,
    )
    handle = session.run(request, grant, CancellationToken(), purpose="test")
    final = session.wait(handle.handle_id, timeout=remaining + 5)
    provenance = session.assert_authority(handle.handle_id)
    page = session.read_log(handle.handle_id, offset=0)
    chunks = [page.data]
    complete = not (page.truncated or page.dropped_bytes)
    while not page.eof:
        following = session.read_log(handle.handle_id, offset=page.next_offset)
        if following.next_offset <= page.next_offset:
            raise CatalogError("Public readiness log pagination did not advance")
        page = following
        complete = complete and not (page.truncated or page.dropped_bytes)
        chunks.append(page.data)
    session.assert_authority(handle.handle_id)
    output = "".join(chunks)
    complete = complete and page.total_bytes == len(output.encode())
    status = (
        "passed"
        if final.state == "exited" and final.exit_code == 0
        else "failed"
        if final.state in {"exited", "failed"}
        else "timeout"
        if final.state == "timed_out"
        else "error"
    )
    raw = ExecutionReceipt(
        receipt_id=canonical_hash(final.model_dump(mode="json")),
        operation_id=operation_id,
        purpose="test",
        status=cast(Any, status),
        argv=argv,
        cwd=".",
        exit_code=final.exit_code if final.exit_code is not None else -1,
        stdout=output,
        stderr="",
        duration_seconds=time.monotonic() - started,
        tree_hash=final.tree_hash,
        log_sha256=sha256_text(output),
        environment_hash=final.environment_hash,
        provenance=provenance,
    )
    (evidence / f"{role}.log").write_text(output)
    receipt_sha = write_evidence_json(
        evidence / f"{role}-receipt.json", raw.model_dump(mode="json")
    )
    return {
        "receipt_sha256": receipt_sha,
        "log_sha256": raw.log_sha256,
        "log_complete": complete,
        "total_bytes": page.total_bytes,
        "truncated": page.truncated,
        "dropped_bytes": page.dropped_bytes,
        "terminal_state": final.state,
    }


def _facts(
    result: Mapping[str, Any],
    evidence: Path,
    *,
    frozen: FrozenPublicPlan,
    profile: EvaluationProfile,
    image_lock: Path,
) -> dict[str, Any]:
    try:
        _manifest_bytes(frozen.manifest_path, frozen.manifest_sha256)
        read_asset(frozen.plan, frozen.asset_root)
        lock_raw = image_lock.read_bytes()
        lock = json.loads(lock_raw)
        task = TaskBinding.model_validate(result["binding"])
        lease = CapabilityLease.model_validate(result["lease"])
        grant = CapabilityGrant.model_validate(result["grant"])
        expected_commands = tuple(sorted({frozen.plan.argv[0], frozen.regression_argv[0]}))
        if (
            result["schema"] not in {SCHEMA, LEGACY_SCHEMA}
            or result["zero_model"] is not True
            or result["readiness_only"] is not True
            or result["paid_model_calls"] != 0
            or result["cost_micros"] != 0
            or result["quality_status"] != "NOT_RUN"
            or result["behavior"] != "AWAITING_IMPLEMENTATION"
            or result["task_id"] != frozen.task_id
            or result["frozen_plan"] != frozen.freeze_metadata()
            or result["profile_sha256"] != _profile_hash(profile, result["schema"])
            or result["image_lock_sha256"] != hashlib.sha256(lock_raw).hexdigest()
            or result["image_id"] != lock["image_id"]
            or result["mount"] != _mount(frozen)
            or result["original_tree_before"] != frozen.original_tree_sha256
            or result["original_tree_after"] != frozen.original_tree_sha256
            or task.task_id != frozen.task_id
            or task.repository != frozen.repository.lower()
            or task.base_commit != frozen.base_commit
            or task.execution_epoch != 1
            or task.task_kind != profile.task_kind
            or task.objective != profile.objective
            or lease.lease_hash != lease.computed_hash()
            or lease.mode != "investigate"
            or lease.write_paths
            or lease.network_hosts
            or lease.credentials
            or lease.external_write
            or lease.read_paths != ("",)
            or lease.allowed_commands != expected_commands
            or lease.max_calls != profile.resources.max_tool_calls
            or lease.max_output_bytes != profile.sandbox_log_bytes
            or lease.offline_network != profile.sandbox_offline_network
            or lease.memory_mb != profile.sandbox_memory_mb
            or lease.nofile_limit != profile.sandbox_nofile
            or lease.cpus != 1.0 or lease.pids != 64
            or lease.scratch_mb != profile.sandbox_scratch_mb
            or lease.protected_paths != (".git", ".mgar", ".env")
            or lease.timeout_seconds != min(600, profile.resources.deadline_seconds)
            or lease.execution_epoch != task.execution_epoch
            or lease.actor != task.actor
            or lease.workspace_id != task.workspace_id
            or lease.repository != task.repository
            or lease.approved_by
            or lease.approval_ref
            or grant.actor != task.actor
            or grant.workspace_id != task.workspace_id
            or grant.repository != task.repository
            or grant.write_paths
            or grant.network
            or not grant.execute
            or grant.read_paths != ("",)
            or grant.allowed_commands != expected_commands
            or grant.max_seconds != lease.timeout_seconds
            or result["tool_calls"] != 2
            or set(result["commands"]) != {"probe", "regression"}
        ):
            raise CatalogError("Public readiness source, plan, resources or authority differs")
        receipts: dict[str, ExecutionReceipt] = {}
        for role, expected_argv in (
            ("probe", frozen.plan.argv),
            ("regression", frozen.regression_argv),
        ):
            command = result["commands"][role]
            raw = ExecutionReceipt.model_validate(
                read_evidence_json(
                    evidence / f"{role}-receipt.json",
                    command["receipt_sha256"],
                )
            )
            log = read_evidence(
                evidence / f"{role}.log", command["log_sha256"], limit=profile.sandbox_log_bytes
            )
            provenance = raw.provenance
            if (
                raw.argv != expected_argv
                or raw.purpose != "test"
                or raw.cwd != "."
                or raw.public_probe is not None
                or raw.tree_hash != frozen.original_tree_sha256
                or raw.log_sha256 != command["log_sha256"]
                or log.decode() != raw.stdout + raw.stderr
                or command["log_complete"] is not True
                or command["truncated"]
                or command["dropped_bytes"] != 0
                or command["total_bytes"] != len(log)
                or len(log) >= profile.sandbox_log_bytes
                or command["terminal_state"] not in {"exited", "failed"}
                or provenance is None
                or provenance.operation_id != raw.operation_id
                or provenance.actor != task.actor
                or provenance.workspace_id != task.workspace_id
                or provenance.repository != task.repository
                or provenance.execution_epoch != 1
                or provenance.lease_id != lease.lease_id
                or provenance.lease_hash != lease.lease_hash
                or provenance.grant_hash != canonical_hash(grant.model_dump(mode="json"))
                or provenance.read_paths != ("",)
                or provenance.write_paths
                or provenance.protected_paths != lease.protected_paths
            ):
                raise CatalogError("Public readiness command, log or generating authority differs")
            receipts[role] = raw
        if receipts["probe"].operation_id == receipts["regression"].operation_id:
            raise CatalogError("Public reproduction and regression require distinct executions")
        probe = normalize_probe_receipt(
            frozen.plan,
            receipts["probe"],
            task=task,
            expected_tree=frozen.original_tree_sha256,
            log_complete=True,
            log_limit_bytes=profile.sandbox_log_bytes,
        )
        regression = receipts["regression"]
        language: ProjectKind = (
            "go" if frozen.regression_argv[0].rsplit("/", 1)[-1] == "go" else "python"
        )
        status, ran, failed, skipped, detail = parse_tests(
            language, regression.stdout, regression.exit_code
        )
        blockers = []
        if probe.outcome == "invalid":
            blockers.append(probe.reason_code)
        elif profile.task_kind == "bugfix" and probe.outcome != "red":
            blockers.append("BUGFIX_PUBLIC_REPRODUCTION_NOT_RED")
        elif profile.task_kind not in {"bugfix", "feature"} and probe.outcome != "green":
            blockers.append("ORIGINAL_PUBLIC_BEHAVIOR_NOT_GREEN")
        green_regression = (
            regression.status == "passed" and status == "passed" and ran > 0 and failed == 0
        )
        declared_red_regression = (
            profile.task_kind == "bugfix" and probe.outcome == "red"
            and regression.status == "failed" and regression.exit_code == 1
            and status == "failed" and ran > 0
            and declared_assertion_failure(
                regression.stdout, failed,
                [path for path, _ in frozen.public_basis if path.startswith("tests/")],
            )
        )
        if not green_regression and not declared_red_regression:
            blockers.append("ORIGINAL_PUBLIC_REGRESSION_NOT_GREEN")
        return {
            "status": "READY" if not blockers else "NOT_RUN",
            "probe_outcome": probe.outcome,
            "probe_reason": probe.reason_code,
            "regression_status": status,
            "regression_tests": ran,
            "regression_failed": failed,
            "regression_skipped": skipped,
            "regression_detail": detail,
            "blockers": blockers,
        }
    except (DomainError, KeyError, TypeError, ValueError, OSError) as exc:
        raise CatalogError("Public readiness evidence is incomplete or malformed") from exc


def validate_public_original_readiness(
    result: Mapping[str, Any],
    evidence: Path,
    *,
    frozen: FrozenPublicPlan,
    profile: EvaluationProfile,
    image_lock: Path,
) -> None:
    """Recompute readiness from raw complete logs, not a cached green label."""
    facts = _facts(result, evidence, frozen=frozen, profile=profile, image_lock=image_lock)
    if facts["status"] != "READY" or any(result.get(key) != value for key, value in facts.items()):
        raise CatalogError("Public original reproduction and regression are not ready")


def execute_public_original(
    frozen: FrozenPublicPlan,
    workspace: Path,
    profile: EvaluationProfile,
    image_lock: Path,
    evidence: Path,
) -> dict[str, Any]:
    validate_public_original(frozen, workspace)
    if (
        profile.public_probe != frozen.plan
        or profile.verification_protocol != "public-probe-v1"
        or profile.split != "dev"
        or profile.task_id != frozen.task_id
        or profile.public_test_argv != frozen.regression_argv
        or profile.resources.max_tool_calls < 2
        or not frozen.regression_argv
        or evidence.resolve().is_relative_to(workspace.resolve())
    ):
        raise CatalogError(
            "Public original readiness needs its exact dev profile and external evidence"
        )
    evidence.mkdir(parents=True, exist_ok=False)
    identity = uuid4().hex[:24]
    task = TaskBinding(
        task_id=frozen.task_id,
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="h12_readiness",
        repository=frozen.repository,
        base_commit=frozen.base_commit,
        workspace_id="h12-readiness-" + identity,
        acceptance_revision="public-readiness-v1",
        objective=profile.objective,
        task_kind=profile.task_kind,
        requirements=(VerificationRequirement(requirement_id="public-original"),),
    )
    timeout = min(600, profile.resources.deadline_seconds)
    commands = tuple(sorted({frozen.plan.argv[0], frozen.regression_argv[0]}))
    lease = issue_lease(
        mode="investigate",
        actor=task.actor,
        workspace_id=task.workspace_id,
        repository=task.repository,
        execution_epoch=1,
        allowed_commands=commands,
        timeout_seconds=timeout,
        max_output_bytes=profile.sandbox_log_bytes,
        memory_mb=profile.sandbox_memory_mb,
        scratch_mb=profile.sandbox_scratch_mb,
        nofile_limit=profile.sandbox_nofile,
        offline_network=profile.sandbox_offline_network,
        max_calls=profile.resources.max_tool_calls,
        ttl_seconds=profile.resources.deadline_seconds + 60,
    )
    grant = CapabilityGrant(
        actor=task.actor,
        workspace_id=task.workspace_id,
        repository=task.repository,
        execute=True,
        allowed_commands=commands,
        max_seconds=timeout,
    )
    broker = IsolatedExecutionBroker(
        workspace,
        lease,
        lock_path=image_lock,
        public_probe=frozen.plan,
        public_probe_root=frozen.asset_root,
    )
    session = CommandSession(broker)
    deadline = time.monotonic() + profile.resources.deadline_seconds
    lock_bytes = image_lock.read_bytes()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "task_id": frozen.task_id,
        "zero_model": True,
        "readiness_only": True,
        "paid_model_calls": 0,
        "cost_micros": 0,
        "quality_status": "NOT_RUN",
        "behavior": "AWAITING_IMPLEMENTATION",
        "frozen_plan": frozen.freeze_metadata(),
        "profile_sha256": _profile_hash(profile, SCHEMA),
        "image_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "image_id": broker.image_id,
        "original_tree_before": frozen.original_tree_sha256,
        "mount": _mount(frozen),
        "binding": task.model_dump(mode="json"),
        "lease": lease.model_dump(mode="json"),
        "grant": grant.model_dump(mode="json"),
        "tool_calls": 2,
        "commands": {},
    }
    # An interrupted controller leaves this non-ready attempt rather than a green report.
    write_evidence_json(evidence / "attempt.json", {**result, "status": "RUNNING"})
    try:
        for role, argv in (("probe", frozen.plan.argv), ("regression", frozen.regression_argv)):
            result["commands"][role] = _observe(
                session, grant, argv, role=role, deadline=deadline, evidence=evidence
            )
    finally:
        session.close()
    result["original_tree_after"] = _tree_hash(_scan(workspace)[0])
    try:
        result.update(
            _facts(result, evidence, frozen=frozen, profile=profile, image_lock=image_lock)
        )
    except CatalogError:
        result.update(status="NOT_RUN", blockers=["PUBLIC_ORIGINAL_EVIDENCE_INVALID"])
    write_evidence_json(evidence / "result.json", result)
    return result
