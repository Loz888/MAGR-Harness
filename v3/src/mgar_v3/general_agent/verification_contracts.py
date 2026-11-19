"""Versioned minimal mechanical checks for General Agent outcomes."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from mgar_v3.domain import Model
from mgar_v3.general_agent.contracts import (
    Deliverable,
    Identifier,
    OutcomeArtifact,
    OutcomeContract,
    Revision,
    Sha256,
    SourceRef,
    Text,
    unique,
)


class Step(Model):
    id: Identifier
    output_id: Identifier
    max_age_seconds: Annotated[int, Field(strict=True, ge=1, le=31_536_000)] = 3600


class SourceStep(Step):
    source: SourceRef
    source_sha256: Sha256

    @model_validator(mode="after")
    def pinned_source(self) -> Self:
        if self.source.version is None:
            raise ValueError("A verification source must pin its version.")
        return self


class CodingRun(Step):
    kind: Literal["coding_test", "coding_build"]
    tree_sha256: Sha256
    environment_sha256: Sha256
    argv: Annotated[tuple[Text, ...], Field(min_length=1, max_length=64)]


class CodingDiff(Step):
    kind: Literal["coding_diff"]
    before_sha256: Sha256
    tree_sha256: Sha256


class CodingProof(Step):
    kind: Literal["coding_inspect", "coding_service"]
    harness_task_id: Identifier
    harness_run_id: UUID
    harness_result_sha256: Sha256
    tree_sha256: Sha256


class Citation(SourceStep):
    kind: Literal["citation", "document"]
    excerpt: Text


class Numeric(SourceStep):
    kind: Literal["numeric"]
    field: Identifier
    formula: Literal["sum", "mean", "count"]

    @model_validator(mode="after")
    def dataset_source(self) -> Self:
        if self.source.kind not in {"dataset", "artifact"}:
            raise ValueError("Numeric checks require a pinned dataset.")
        return self


class ExternalAction(Step):
    kind: Literal["email", "calendar"]
    action_id: UUID
    operation: Identifier


class BrowserState(SourceStep):
    kind: Literal["browser"]
    expected_text: Text

    @model_validator(mode="after")
    def web_source(self) -> Self:
        if self.source.kind != "web":
            raise ValueError("Browser checks require a pinned web source.")
        return self


VerificationStep = Annotated[
    CodingRun | CodingDiff | CodingProof | Citation | Numeric | ExternalAction | BrowserState,
    Field(discriminator="kind"),
]


class VerificationPlan(Model):
    schema_version: Literal["general-verification-plan-v1"] = "general-verification-plan-v1"
    task_id: UUID
    task_revision: Revision
    plan_version: Revision
    expected_outputs: Annotated[tuple[Deliverable, ...], Field(min_length=1, max_length=12)]
    verification_steps: Annotated[tuple[VerificationStep, ...], Field(min_length=1, max_length=40)]
    independent_check: Literal[True] = True
    require_confirmation: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def check_steps(self) -> Self:
        outputs = tuple(item.id for item in self.expected_outputs)
        unique(outputs, "expected outputs")
        unique(tuple(step.id for step in self.verification_steps), "verification steps")
        if {step.output_id for step in self.verification_steps} != set(outputs):
            raise ValueError("All expected outputs need verification steps.")
        return self


class EvidenceObservation(Model):
    schema_version: Literal["general-evidence-v1"] = "general-evidence-v1"
    collector: Identifier
    observed_at: AwareDatetime
    content_sha256: Sha256
    source: SourceRef | None = None


class ResultDraft(Model):
    summary: Text
    outputs: Annotated[tuple[OutcomeArtifact, ...], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def unique_deliveries(self) -> Self:
        unique(tuple(item.deliverable_id for item in self.outputs), "output IDs")
        return self


class CheckResult(Model):
    step_id: Identifier
    status: Literal["verified", "unverified"]
    reason: Identifier | None = None
    evidence_version: Revision | None = None
    evidence_sha256: Sha256 | None = None


class VerificationReport(Model):
    schema_version: Literal["general-verification-report-v1"] = "general-verification-report-v1"
    report_id: UUID
    verification_id: UUID
    verifier: Identifier
    checked_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    plan_sha256: Sha256
    result_sha256: Sha256
    status: Literal["verified", "unverified"]
    check_results: tuple[CheckResult, ...]
    outcome: OutcomeContract | None = None
    confirmed_by: Identifier | None = None
