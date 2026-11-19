"""Project the existing Coding task policies into the GA01 task contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mgar_v3.domain import DomainError
from mgar_v3.general_agent.contracts import Deliverable, GeneralTaskSpec, SuccessCriterion
from mgar_v3.harness.contracts import TaskKind

if TYPE_CHECKING:
    from mgar_v3.harness.service import CodingTaskRequest

_POLICY_OUTPUTS: dict[TaskKind, tuple[tuple[Deliverable, str], ...]] = {
    "inspect": (
        (
            Deliverable(id="report", kind="report", description="Repository findings"),
            "Report findings with evidence from the pinned workspace.",
        ),
    ),
    "bugfix": (
        (
            Deliverable(id="patch", kind="patch", description="Bug fix"),
            "Verify that the patch addresses the reproduced defect.",
        ),
        (
            Deliverable(id="tests", kind="test_receipt", description="Regression results"),
            "Record failing reproduction and passing regression evidence.",
        ),
    ),
    "feature": (
        (
            Deliverable(id="patch", kind="patch", description="Feature implementation"),
            "Verify the requested feature on the resulting tree.",
        ),
        (
            Deliverable(id="tests", kind="test_receipt", description="Test results"),
            "Record passing feature and regression checks.",
        ),
    ),
    "refactor": (
        (
            Deliverable(id="patch", kind="patch", description="Refactored implementation"),
            "Verify that the changed implementation preserves the specified behavior.",
        ),
        (
            Deliverable(id="tests", kind="test_receipt", description="Behavior checks"),
            "Record evidence that the relevant behavior remains unchanged.",
        ),
    ),
    "run_app": (
        (
            Deliverable(id="service", kind="service_receipt", description="Application result"),
            "Verify the declared service or one-shot completion in the sandbox.",
        ),
    ),
}


def from_coding_request(request: CodingTaskRequest) -> GeneralTaskSpec:
    if request.task_kind not in _POLICY_OUTPUTS:
        raise DomainError("GENERAL_CODING_POLICY_INVALID", "编码任务类型无效。", 422)
    kind = request.task_kind
    outputs = _POLICY_OUTPUTS[kind]
    return GeneralTaskSpec(
        domain="coding",
        goal=request.objective,
        coding_policy=kind,
        deliverables=tuple(item for item, _ in outputs),
        success_criteria=tuple(
            SuccessCriterion(id=item.id, description=criterion, deliverable_ids=(item.id,))
            for item, criterion in outputs
        ),
    )


def to_coding_request(spec: GeneralTaskSpec, *, project_id: str) -> CodingTaskRequest:
    from mgar_v3.harness.service import CodingTaskRequest

    if (
        spec.domain != "coding"
        or spec.coding_policy is None
        or spec.goal is None
        or spec.clarifications()
    ):
        raise DomainError("GENERAL_CODING_TASK_INCOMPLETE", "编码任务需要明确的目标和验收。", 409)
    # The execution service applies its own project grants. User fields cannot
    # set approval, select live mode, inject argv, or delegate capability.
    return CodingTaskRequest(
        project_id=project_id,
        objective=spec.goal,
        task_kind=spec.coding_policy,
    )
