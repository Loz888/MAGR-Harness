"""Versioned task intent, plan, state and verified outcome contracts."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from mgar_v3.domain import Model
from mgar_v3.harness.contracts import TaskKind

TaskDomain = Literal["coding", "research", "data", "office"]
TaskState = Literal[
    "draft",
    "ready",
    "running",
    "waiting",
    "blocked",
    "verifying",
    "completed",
    "failed",
    "cancelled",
]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048, pattern=r"^[^\x00]+$"),
]
Goal = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=16000, pattern=r"^[^\x00]+$"),
]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Limit = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


def unique(values: tuple[str, ...], field: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must be unique")


class TaskBudget(Model):
    max_cost_micros: Limit | None = None
    max_model_calls: Limit | None = None
    max_tool_calls: Limit | None = None
    max_total_tokens: Limit | None = None
    max_seconds: Limit | None = None


class SourceRef(Model):
    kind: Literal["repository", "web", "dataset", "document", "artifact"]
    reference: Text
    version: Text | None = None


class Deliverable(Model):
    id: Identifier
    kind: Literal[
        "report", "patch", "test_receipt", "service_receipt", "table", "chart", "file", "draft"
    ]
    description: Text


class SuccessCriterion(Model):
    id: Identifier
    description: Text
    deliverable_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def check_deliverables(self) -> Self:
        unique(self.deliverable_ids, "deliverable_ids")
        return self


class GeneralTaskSpec(Model):
    schema_version: Literal["general-task-v1"] = "general-task-v1"
    domain: TaskDomain | None = None
    goal: Goal | None = None
    context: Annotated[tuple[Text, ...], Field(max_length=12)] = ()
    constraints: Annotated[tuple[Text, ...], Field(max_length=16)] = ()
    deliverables: Annotated[tuple[Deliverable, ...], Field(max_length=12)] = ()
    success_criteria: Annotated[tuple[SuccessCriterion, ...], Field(max_length=24)] = ()
    risk_level: Literal["low", "medium", "high"] | None = None
    allowed_capabilities: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    budget: TaskBudget = Field(default_factory=TaskBudget)
    deadline: AwareDatetime | None = None
    approval_policy: Literal["manual", "deny_external_writes"] | None = None
    source_refs: Annotated[tuple[SourceRef, ...], Field(max_length=16)] = ()
    coding_policy: TaskKind | None = None
    open_questions: Annotated[tuple[Text, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        if self.coding_policy is not None and self.domain != "coding":
            raise ValueError("coding_policy requires the coding domain")
        ids = tuple(item.id for item in self.deliverables)
        unique(ids, "deliverables")
        unique(tuple(item.id for item in self.success_criteria), "success_criteria")
        unique(self.allowed_capabilities, "allowed_capabilities")
        if any(set(item.deliverable_ids) - set(ids) for item in self.success_criteria):
            raise ValueError("A success criterion references an undeclared deliverable")
        return self

    def clarifications(self) -> tuple[str, ...]:
        covered = {value for item in self.success_criteria for value in item.deliverable_ids}
        return tuple(
            field
            for field, missing in (
                ("goal", self.goal is None),
                ("open_questions", bool(self.open_questions)),
                ("domain", self.domain is None),
                ("deliverables", not self.deliverables),
                (
                    "success_criteria",
                    not self.success_criteria
                    or any(item.id not in covered for item in self.deliverables),
                ),
                ("coding_policy", self.domain == "coding" and self.coding_policy is None),
            )
            if missing
        )


class TaskPlan(Model):
    schema_version: Literal["general-plan-v1"] = "general-plan-v1"
    task_revision: Revision
    steps: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]


class OutcomeArtifact(Model):
    deliverable_id: Identifier
    sha256: Sha256


class CriterionEvidence(Model):
    criterion_id: Identifier
    receipt_sha256: Sha256


class OutcomeVerificationReceipt(Model):
    """CAS record published by a trusted verifier after evaluating one criterion."""

    schema_version: Literal["general-verification-receipt-v1"] = "general-verification-receipt-v1"
    status: Literal["verified"]
    task_id: UUID
    task_revision: Revision
    plan_version: Revision
    verification_version: Revision
    criterion_id: Identifier
    criterion_sha256: Sha256
    deliverables: Annotated[tuple[OutcomeArtifact, ...], Field(min_length=1, max_length=12)]
    evidence_sha256: Annotated[tuple[Sha256, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def check_bindings(self) -> Self:
        unique(tuple(item.deliverable_id for item in self.deliverables), "deliverables")
        unique(self.evidence_sha256, "evidence_sha256")
        return self


class OutcomeContract(Model):
    """A success claim. The Engine verifies the referenced receipt and artifact bytes."""

    schema_version: Literal["general-outcome-v1"] = "general-outcome-v1"
    task_revision: Revision
    plan_version: Revision
    deliverables: Annotated[tuple[OutcomeArtifact, ...], Field(min_length=1, max_length=12)]
    evidence: Annotated[tuple[CriterionEvidence, ...], Field(min_length=1, max_length=24)]
    summary: Text

    @model_validator(mode="after")
    def check_bindings(self) -> Self:
        unique(tuple(item.deliverable_id for item in self.deliverables), "deliverables")
        unique(tuple(item.criterion_id for item in self.evidence), "evidence")
        unique(tuple(item.receipt_sha256 for item in self.evidence), "receipt_sha256")
        return self


class TaskSnapshot(Model):
    task_id: UUID
    project_id: Identifier
    membership_id: UUID | None = Field(exclude=True)
    revision: Revision
    version: Revision
    state: TaskState
    reason: str | None
    clarifications: tuple[str, ...]
    spec: GeneralTaskSpec
    plan: TaskPlan | None
    plan_version: Revision | None
    outcome: OutcomeContract | None
