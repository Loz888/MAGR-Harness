"""Validate final-tree lifecycle evidence without launching or repairing a process."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.lifecycle_types import EntryPointBatch, LifecyclePolicy

if TYPE_CHECKING:
    from mgar_v3.harness.coding_loop import ServicePlan, ServiceReceipt
    from mgar_v3.harness.contracts import EvaluationProfile, TaskBinding


def _sealed(value: Model) -> bool:
    return getattr(value, "receipt_id", None) == digest(
        value.model_dump(mode="json", exclude={"receipt_id"})
    )


def entry_batch_matches(
    batch: EntryPointBatch,
    task: TaskBinding,
    tree: str,
    acceptance: str,
    policy: LifecyclePolicy,
) -> bool:
    if (
        batch.status != "PASS"
        or batch.tree_hash != tree
        or batch.acceptance_hash != acceptance
        or len(batch.inventory_hash) != 64
        or len(batch.targets) != len(batch.receipts)
    ):
        return False
    for spec, receipt in zip(batch.targets, batch.receipts, strict=True):
        cleanup = receipt.cleanup
        if not (
            _sealed(receipt)
            and receipt.status == "PASS"
            and receipt.spec == spec
            and receipt.task_id == task.task_id
            and receipt.run_id == str(task.run_id)
            and 1 <= receipt.execution_epoch <= task.execution_epoch
            and receipt.tree_hash == tree
            and receipt.acceptance_hash == acceptance
            and receipt.loader_hash == policy.control_assets_hash
            and receipt.exit_code == 0
            and len(receipt.log_hash) == 64
            and cleanup is not None
            and _sealed(cleanup)
            and cleanup.command_id == receipt.operation_id
            and cleanup.tree_hash == tree
            and cleanup.processes_exited is True
            and cleanup.port_released is True
            and cleanup.container_removed
            and not cleanup.orphan_pids
            and not cleanup.forced
        ):
            return False
    return True


def service_receipt_matches(
    receipt: ServiceReceipt,
    task: TaskBinding,
    plan: ServicePlan,
    tree: str,
    acceptance: str,
    policy: LifecyclePolicy,
    *,
    cleanup: bool = False,
) -> bool:
    evidence = receipt.lifecycle
    if evidence is None:
        return False
    ready = evidence.ready
    snapshot = ready.snapshot
    if not (
        _sealed(ready)
        and receipt.tree_hash == tree
        and tuple(receipt.argv) == plan.argv
        and evidence.assets_hash == policy.control_assets_hash
        and evidence.mode == plan.mode
        and ready.task_id == task.task_id
        and ready.run_id == str(task.run_id)
        and 1 <= ready.execution_epoch <= task.execution_epoch
        and ready.tree_hash == tree
        and ready.acceptance_hash == acceptance
        and ready.handle_id == receipt.command_handle_id
        and ready.process_id == receipt.process_id
        and ready.process_id is not None
        and ready.group_id is not None
        and ready.port is not None
        and ready.duration_seconds is not None
        and ready.timeout_seconds == plan.timeout_seconds
        and 0 <= ready.duration_seconds <= plan.timeout_seconds
        and snapshot is not None
        and len(ready.environment_hash) == 64
        and ready.command_id.startswith(task.task_id[:36] + ":app:")
        and any(
            p.pid == ready.process_id
            and p.group_id == ready.group_id
            and p.parent_pid == snapshot.supervisor.pid
            for p in snapshot.processes
        )
    ):
        return False
    if cleanup:
        stopped = evidence.cleanup
        return bool(
            receipt.stopped
            and stopped is not None
            and _sealed(stopped)
            and stopped.command_id == ready.command_id
            and stopped.tree_hash == tree
            and stopped.graceful
            and not stopped.forced
            and not stopped.orphan_pids
            and stopped.processes_exited is True
            and stopped.port_released is True
            and stopped.container_removed
            and stopped.before is not None
            and stopped.after is not None
            and stopped.before.supervisor.pid == snapshot.supervisor.pid
            and stopped.before.supervisor.start_ticks == snapshot.supervisor.start_ticks
            and stopped.after.supervisor.pid == snapshot.supervisor.pid
            and stopped.after.supervisor.start_ticks == snapshot.supervisor.start_ticks
            and all(p.state == "Z" for p in stopped.after.processes)
            and not any(p.get("pids") for p in stopped.after.listeners)
            and stopped.after.bind_available is True
            and len(receipt.log_sha256) == 64
            and evidence.raw_log_hash == receipt.log_sha256
        )
    if not (
        receipt.ready
        and ready.ready
        and ready.observations
        and all(
            o.matched
            and o.port == ready.port
            and o.source == "independent-container-probe"
            and len(o.body_hash) == 64
            and o.body_artifact_hash == o.body_hash
            for o in ready.observations
        )
    ):
        return False
    for route in plan.routes:
        if not any(
            o.path == route.path
            and o.status_code == route.status_code
            and (
                route.content_type is None
                or o.content_type.split(";")[0].strip().lower() == route.content_type.lower()
            )
            and (
                route.body is None or o.body_hash == hashlib.sha256(route.body.encode()).hexdigest()
            )
            for o in ready.observations
        ):
            return False
    if plan.mode == "oneshot":
        return (
            evidence.root_exit_code == 0
            and (
                not policy.require_selfcheck_status
                or all(o.status_code in ready.printed_statuses for o in ready.observations)
            )
            and len(evidence.stdout_hash) == 64
        )
    return bool(
        (not policy.require_ready_line or ready.log_port == ready.port)
        and (plan.port == 0 or plan.port == ready.port)
        and any(p["port"] == ready.port and p.get("pids") for p in snapshot.listeners)
    )


def validate_lifecycle_result(result: Mapping[str, Any], profile: EvaluationProfile) -> None:
    """Scoring cannot turn missing/wrong-scope R04 observations into a PASS."""
    from mgar_v3.harness.coding_loop import ServicePlan, ServiceReceipt
    from mgar_v3.harness.contracts import TaskBinding

    policy = profile.lifecycle_policy
    if policy is None or profile.task_kind == "inspect":
        return
    try:
        task = TaskBinding.model_validate(result["acceptance_task"])
        tree = str(result["tree_after"])
        acceptance = str(result["acceptance_contract"]["acceptance_hash"])
        batches = [EntryPointBatch.model_validate(b) for b in result.get("entrypoint_batches", ())]
        valid = result.get("lifecycle_policy") == policy.model_dump(mode="json") and any(
            entry_batch_matches(b, task, tree, acceptance, policy) for b in batches
        )
        if profile.task_kind == "run_app":
            plan = ServicePlan(
                argv=profile.service_argv,
                mode=profile.service_mode,
                port=profile.service_port,
                path=profile.service_path,
                routes=profile.lifecycle_routes,
                timeout_seconds=profile.service_timeout_seconds,
            )
            receipts = [
                ServiceReceipt.model_validate(r) for r in result.get("service_receipts", ())
            ]
            valid = valid and any(
                service_receipt_matches(
                    r,
                    task,
                    plan,
                    tree,
                    acceptance,
                    policy,
                )
                and service_receipt_matches(
                    r,
                    task,
                    plan,
                    tree,
                    acceptance,
                    policy,
                    cleanup=True,
                )
                for r in receipts
            )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise DomainError(
            "H12_LIFECYCLE_EVIDENCE_INVALID", "Lifecycle evidence is malformed", 409
        ) from exc
    if not valid:
        raise DomainError(
            "H12_LIFECYCLE_EVIDENCE_INVALID",
            "Final-tree entry/Ready/cleanup evidence is missing or differs",
            409,
        )
