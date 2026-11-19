"""Translate task graph claims and immutable H11 coding results at the GA09 boundary."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.general_agent.capabilities import CapabilityAccess
from mgar_v3.general_agent.coding import to_coding_request
from mgar_v3.general_agent.coding_executor_contract import CODING_CAPABILITIES, CodingDispatch
from mgar_v3.general_agent.contracts import OutcomeArtifact, TaskSnapshot
from mgar_v3.general_agent.graph_store import NodeClaim, NodeReceipt
from mgar_v3.general_agent.verification_contracts import (
    CodingDiff,
    CodingProof,
    CodingRun,
    EvidenceObservation,
    ResultDraft,
    VerificationPlan,
    VerificationReport,
    VerificationStep,
)
from mgar_v3.harness.coding_loop import ServicePlan, ServiceReceipt
from mgar_v3.harness.contracts import (
    BudgetLimits,
    ExecutionReceipt,
    VerificationReceipt,
    WorkspaceDiff,
    normalize_relative_path,
)
from mgar_v3.harness.result_artifacts import load_result
from mgar_v3.harness.tenancy import require_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row
    from mgar_v3.general_agent.verification_store import GeneralVerificationStore
    from mgar_v3.harness.service import HarnessCodingService


@dataclass(frozen=True)
class CodingOptions:
    """Server-selected fixture input, kept outside the public task request schema."""

    target_path: str
    approved: bool = False
    old_text: str = ""
    new_text: str = ""
    test_argv: tuple[str, ...] = ()
    service_plan: ServicePlan | None = None


def _deny() -> DomainError:
    return DomainError("GENERAL_CODING_SCOPE", "Invalid Coding executor binding.", 403)


def _invalid() -> DomainError:
    return DomainError("GENERAL_CODING_EVIDENCE", "Coding evidence is incomplete or stale.", 409)


def _bound(row: Row) -> CodingDispatch:
    try:
        return CodingDispatch.model_validate(row["request"]["general_dispatch"])
    except (KeyError, ValidationError) as exc:
        raise _deny() from exc


def _verified_result(engine: Engine, row: Row) -> tuple[dict[str, Any], VerificationReceipt]:
    if row["status"] != "succeeded" or not row["result_hash"]:
        raise _invalid()
    result = load_result(
        engine.artifacts,
        engine.artifacts.read(row["result_hash"]),
        row["task_id"],
        str(row["run_id"]),
    )
    verification = VerificationReceipt.model_validate(result.get("verification"))
    if (
        result.get("status") != "completed"
        or result.get("usage_known") is not True
        or verification.status != "verified"
        or verification.task_id != row["task_id"]
        or verification.tree_hash != row["tree_hash"]
    ):
        raise _invalid()
    return result, verification


def _special_output(
    kind: Literal["coding_inspect", "coding_service"],
    row: Row,
    result: dict[str, Any],
    verification: VerificationReceipt,
) -> bytes:
    if kind == "coding_inspect":
        if (
            result.get("task_kind") != "inspect"
            or result.get("changes")
            or result.get("tree_before") != verification.tree_hash
            or not any(
                check.requirement_id == "evidence" and check.status == "passed"
                for check in verification.checks
            )
        ):
            raise _invalid()
        return json.dumps(
            {
                "summary": result["summary"],
                "verification": verification.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    service_plan = _bound(row).service_plan
    if service_plan is None or result.get("task_kind") != "run_app":
        raise _invalid()
    for document in reversed(result.get("service_receipts") or []):
        receipt = ServiceReceipt.model_validate(document)
        if (
            receipt.argv == service_plan.argv
            and receipt.tree_hash == verification.tree_hash
            and receipt.started
            and receipt.ready
            and receipt.stopped
        ):
            return receipt.model_dump_json().encode()
    raise _invalid()


def check_coding_proof(
    engine: Engine, task: TaskSnapshot, proof: CodingProof, output: bytes, verifier: str
) -> tuple[datetime, str] | None:
    """Reload the independently checked H11 result and join it to the settled graph."""
    row = engine.harness.get(proof.harness_task_id)
    dispatch = _bound(row)
    claim = dispatch.claim
    graph = engine.general_graphs.get(task.task_id)
    progress = graph.progress.get(claim.node_id)
    if (
        row["workspace_id"] != task.project_id
        or row["actor"] == verifier
        or row["result_hash"] != proof.harness_result_sha256
        or row["run_id"] != proof.harness_run_id
        or (claim.task_id, claim.task_revision) != (task.task_id, task.revision)
        or progress is None
        or progress.claim != claim
        or progress.state != "succeeded"
        or progress.outputs.get(proof.output_id) != hashlib.sha256(output).hexdigest()
    ):
        return None
    result, verification = _verified_result(engine, row)
    if (
        verification.tree_hash != proof.tree_sha256
        or _special_output(proof.kind, row, result, verification) != output
    ):
        return None
    return row["updated_at"], row["result_hash"]


class CodingExecutor:
    """A thin adapter over the registered HarnessCodingService and its durable ledgers."""

    def __init__(self, engine: Engine, harness: HarnessCodingService) -> None:
        if harness.engine is not engine or harness.store is not engine.harness:
            raise _deny()
        self.engine, self.harness = engine, harness

    def start(self, claim: NodeClaim, access: CapabilityAccess, options: CodingOptions) -> str:
        if claim.capability_id not in CODING_CAPABILITIES:
            raise _deny()
        path = normalize_relative_path(options.target_path)
        if not path:
            raise _deny()
        task = self.engine.general_tasks.get(claim.task_id)
        request = to_coding_request(task.spec, project_id=task.project_id)
        if (
            (request.task_kind != "inspect" and not options.approved)
            or (request.task_kind in {"bugfix", "feature", "refactor"} and not options.old_text)
            or (
                request.task_kind not in {"bugfix", "feature", "refactor"}
                and (options.old_text or options.new_text or options.test_argv)
            )
            or (request.task_kind == "run_app") != bool(options.service_plan)
        ):
            raise _deny()
        if task.membership_id is not None:
            with self.engine.transaction() as conn:
                require_membership(conn, task.project_id, str(task.membership_id), write=True)
        self._outputs(claim, task)
        descriptor = self.engine.general_graphs.registry.definition(claim.capability_id)
        seconds = int((claim.expires_at - datetime.now(UTC)).total_seconds())
        if descriptor is None or seconds < 1:
            raise _deny()
        resources = descriptor.upper_bound
        request = replace(
            request,
            target_path=path,
            approved=options.approved,
            old_text=options.old_text,
            new_text=options.new_text,
            test_command=shlex.join(options.test_argv),
        )
        dispatch = CodingDispatch(
            claim=claim,
            access=access,
            request_sha256=digest(asdict(request)),
            limits=BudgetLimits(
                max_model_calls=resources.model_calls,
                max_tool_calls=resources.tool_calls,
                max_cost_micros=0,
                max_seconds=min(seconds, 3600),
            ),
            service_plan=options.service_plan,
        )
        self.harness.submit_general_task(request, dispatch)
        return dispatch.harness_task_id

    def _bound_row(self, claim: NodeClaim, access: CapabilityAccess) -> tuple[Row, CodingDispatch]:
        row = self.engine.harness.get(f"h08-{claim.operation_id.hex[:12]}")
        dispatch = _bound(row)
        if dispatch.claim != claim or dispatch.access != access:
            raise _deny()
        return row, dispatch

    def pause(self, claim: NodeClaim, access: CapabilityAccess) -> dict[str, Any]:
        row, dispatch = self._bound_row(claim, access)
        dispatch.require_current(self.engine)
        return self.harness.pause_task(row["task_id"])

    def resume(self, claim: NodeClaim, access: CapabilityAccess) -> dict[str, Any]:
        row, dispatch = self._bound_row(claim, access)
        dispatch.require_current(self.engine)
        return self.harness.resume_task(row["task_id"])

    def poll(self, claim: NodeClaim, access: CapabilityAccess) -> dict[str, Any]:
        row, _ = self._bound_row(claim, access)
        task = self.engine.general_tasks.get(claim.task_id)
        graph = self.engine.general_graphs.get(claim.task_id)
        progress = graph.progress.get(claim.node_id)
        if (
            task.revision != claim.task_revision
            or task.project_id != access.project_id
            or progress is None
            or progress.claim != claim
        ):
            raise _deny()
        if task.membership_id is not None:
            with self.engine.transaction() as conn:
                require_membership(conn, task.project_id, str(task.membership_id), write=True)
        if progress.state in {"succeeded", "failed"}:
            return self._projection(row, progress.receipt_sha256)
        if row["status"] not in {"succeeded", "failed", "cancelled"}:
            return self._projection(row, None)
        outputs: dict[str, str] = {}
        status: Literal["succeeded", "failed", "unknown"] = "failed"
        if not row["result_hash"]:
            status = "unknown"
        else:
            result = load_result(
                self.engine.artifacts,
                self.engine.artifacts.read(row["result_hash"]),
                row["task_id"],
                str(row["run_id"]),
            )
            if result.get("usage_known") is False or any(
                operation["state"] == "unknown"
                for operation in self.engine.harness.operations(row["task_id"])
            ):
                status = "unknown"
            elif row["status"] == "succeeded":
                try:
                    _, outputs, _ = self._materials(task, row)
                    status = "succeeded"
                except (DomainError, ValidationError):
                    pass
        receipt = NodeReceipt(
            operation_id=claim.operation_id,
            task_id=claim.task_id,
            task_revision=claim.task_revision,
            node_id=claim.node_id,
            node_sha256=claim.node_sha256,
            descriptor_sha256=claim.descriptor_sha256,
            status=status,
            outputs=outputs,
        )
        content = self.engine.artifacts.put(receipt.model_dump_json().encode())
        self.engine.general_graphs.settle(content)
        return self._projection(row, content)

    @staticmethod
    def _projection(row: Row, receipt: str | None) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "run_id": str(row["run_id"]),
            "execution_epoch": row["execution_epoch"],
            "status": row["public_status"],
            "result_sha256": row["result_hash"],
            "node_receipt_sha256": receipt,
        }

    def _outputs(self, claim: NodeClaim, task: TaskSnapshot) -> dict[str, str]:
        graph = self.engine.general_graphs.get(task.task_id)
        node = next((item for item in graph.graph.nodes if item.id == claim.node_id), None)
        declared = {item.id: item.kind for item in task.spec.deliverables}
        allowed = (
            {"report"}
            if task.spec.coding_policy == "inspect"
            else {"service_receipt"}
            if task.spec.coding_policy == "run_app"
            else {"patch", "test_receipt"}
        )
        if (
            node is None
            or not node.outputs
            or any(declared.get(key) not in allowed for key in node.outputs)
        ):
            raise _invalid()
        return {key: declared[key] for key in node.outputs}

    def _materials(
        self,
        task: TaskSnapshot,
        row: Row,
    ) -> tuple[tuple[VerificationStep, ...], dict[str, str], dict[str, EvidenceObservation]]:
        result, verification = _verified_result(self.engine, row)
        binding = _bound(row)
        collector = row["actor"]
        observed_at = row["updated_at"]
        proof_steps: list[VerificationStep] = []
        artifacts: dict[str, str] = {}
        observations: dict[str, EvidenceObservation] = {}

        def add_evidence(step: VerificationStep, raw: bytes) -> str:
            content: str = self.engine.artifacts.put(raw)
            proof_steps.append(step)
            observations[step.id] = EvidenceObservation(
                collector=collector,
                observed_at=observed_at,
                content_sha256=content,
            )
            return content

        for index, (key, kind) in enumerate(self._outputs(binding.claim, task).items()):
            if kind in {"report", "service_receipt"}:
                proof_kind: Literal["coding_inspect", "coding_service"] = (
                    "coding_inspect" if kind == "report" else "coding_service"
                )
                output = _special_output(proof_kind, row, result, verification)
                artifacts[key] = self.engine.artifacts.put(output)
                proof_steps.append(
                    CodingProof(
                        id=f"coding-proof-{index}",
                        kind=proof_kind,
                        output_id=key,
                        harness_task_id=row["task_id"],
                        harness_run_id=row["run_id"],
                        harness_result_sha256=row["result_hash"],
                        tree_sha256=verification.tree_hash,
                    )
                )
                continue
            raw_receipts = [
                ExecutionReceipt.model_validate(item) for item in result.get("receipts") or []
            ]
            test = next(
                (
                    item
                    for item in reversed(raw_receipts)
                    if self._passing_test(item, verification, task)
                ),
                None,
            )
            if test is None:
                raise _invalid()
            test_sha = add_evidence(
                CodingRun(
                    id=f"coding-test-{index}",
                    kind="coding_test",
                    output_id=key,
                    tree_sha256=test.tree_hash,
                    environment_sha256=test.environment_hash,
                    argv=test.argv,
                ),
                test.model_dump_json().encode(),
            )
            if kind == "test_receipt":
                artifacts[key] = test_sha
                continue
            diff = WorkspaceDiff(
                workspace_id=task.project_id,
                tree_before=result["tree_before"],
                tree_after=result["tree_after"],
                patch=result["diff"],
                changes=result["changes"],
            )
            if not diff.changes or not diff.patch or diff.tree_after != verification.tree_hash:
                raise _invalid()
            artifacts[key] = self.engine.artifacts.put(diff.patch.encode())
            add_evidence(
                CodingDiff(
                    id=f"coding-diff-{index}",
                    kind="coding_diff",
                    output_id=key,
                    before_sha256=diff.tree_before,
                    tree_sha256=diff.tree_after,
                ),
                diff.model_dump_json().encode(),
            )
        return tuple(proof_steps), artifacts, observations

    @staticmethod
    def _passing_test(
        item: ExecutionReceipt, verification: VerificationReceipt, task: TaskSnapshot
    ) -> bool:
        return (
            item.receipt_id in verification.receipt_ids
            and item.purpose == "test"
            and item.status == "passed"
            and item.exit_code == 0
            and item.tests_run > 0
            and item.tests_failed == 0
            and item.tree_hash == verification.tree_hash
            and item.provenance is not None
            and item.provenance.workspace_id == task.project_id
        )

    def verification(
        self, claim: NodeClaim, access: CapabilityAccess, *, verifier: str
    ) -> VerificationReport:
        """Create an attestation with the existing GA05 verifier; publication stays separate."""
        row, _ = self._bound_row(claim, access)
        task = self.engine.general_tasks.get(claim.task_id)
        if row["actor"] == verifier or task.plan_version is None:
            raise _deny()
        steps, artifacts, observations = self._materials(task, row)
        plan = VerificationPlan(
            task_id=task.task_id,
            task_revision=task.revision,
            plan_version=task.plan_version,
            expected_outputs=task.spec.deliverables,
            verification_steps=steps,
        )
        store: GeneralVerificationStore = self.engine.general_verification
        key = store.install(plan, task.version)
        for step_id, entry in observations.items():
            store.capture(key, step_id, entry)
        draft = ResultDraft(
            summary=f"Verified Coding run {row['run_id']}.",
            outputs=tuple(
                OutcomeArtifact(deliverable_id=name, sha256=content)
                for name, content in artifacts.items()
            ),
        )
        return store.verify(key, draft, verifier=verifier)
