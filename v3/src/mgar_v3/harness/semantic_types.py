"""Public task-type policies and private, content-bound Judge review metadata."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.regression_types import CheckStatus, RegressionPlan

SHA = r"^[a-f0-9]{64}$"
IDENTIFIER = r"^[A-Za-z_][A-Za-z_0-9]*$"


def public_id(description: str) -> str:
    # The same stable public requirement identity as R01 AcceptanceContract.
    return "public." + digest(description)[:16]


class SemanticRequirement(Model):
    acceptance_requirement_id: str = Field(pattern=r"^public\.[a-f0-9]{16}$")
    description: str = Field(min_length=1, max_length=8192)
    kind: Literal["behavior", "target", "structure", "lifecycle"]

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.acceptance_requirement_id != public_id(self.description):
            raise ValueError("Requirement ID differs from the public R01 description")
        return self


class MissingTargetSymbol(Model):
    acceptance_requirement_id: str
    name: str = Field(pattern=IDENTIFIER)
    receiver: str | None = Field(default=None, pattern=IDENTIFIER)
    reference_paths: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def paths(self) -> Self:
        from mgar_v3.harness.contracts import normalize_relative_path

        if any(not p or normalize_relative_path(p) != p for p in self.reference_paths):
            raise ValueError("Target reference paths must be canonical workspace paths")
        return self


class SharedHelperSpec(Model):
    acceptance_requirement_id: str
    path: str
    language: Literal["python", "go"] = "python"
    owner_class: str | None = Field(default=None, pattern=IDENTIFIER)
    callers: tuple[str, ...] = Field(min_length=2, max_length=16)
    required_name: str | None = Field(default=None, pattern=IDENTIFIER)

    @model_validator(mode="after")
    def shape(self) -> Self:
        from mgar_v3.harness.contracts import normalize_relative_path

        suffix = ".py" if self.language == "python" else ".go"
        if not self.path.endswith(suffix) or normalize_relative_path(self.path) != self.path:
            raise ValueError("The helper check needs a canonical public source path")
        if self.language == "python" and self.owner_class is None:
            raise ValueError("The Python mapping-key check needs a declared class")
        if len(set(self.callers)) != len(self.callers) or any(
            re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", name) is None for name in self.callers
        ):
            raise ValueError("Shared-helper callers need distinct declared identifiers")
        return self


class TaskBaselinePolicy(Model):
    version: Literal["harness-task-baseline-v1"] = "harness-task-baseline-v1"
    benchmark_revision: str = Field(min_length=1, max_length=100)
    task_kind: Literal["bugfix", "feature", "refactor", "run_app"]
    requirements: tuple[SemanticRequirement, ...] = Field(min_length=1, max_length=64)
    behavior_plan: RegressionPlan
    target_plan: RegressionPlan | None = None
    missing_targets: tuple[MissingTargetSymbol, ...] = Field(default=(), max_length=32)
    shared_helper: SharedHelperSpec | None = None
    public_structural_probe: PublicProbePlan | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @property
    def public_acceptance_hash(self) -> str:
        return digest([r.model_dump(mode="json") for r in self.requirements])

    @property
    def policy_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def stages(self) -> Self:
        requirements = {r.acceptance_requirement_id: r for r in self.requirements}
        if len(requirements) != len(self.requirements):
            raise ValueError("Public acceptance IDs must be unique")
        if not self.behavior_plan.commands:
            raise ValueError("Every policy needs a frozen existing behavior matrix")
        if self.task_kind in {"bugfix", "feature"} and (
            self.target_plan is None or not self.target_plan.commands
        ):
            raise ValueError("Bugfix/feature need a separate frozen target plan")
        if self.missing_targets and (self.task_kind != "feature" or self.target_plan is None):
            raise ValueError("Only a declared feature target may have an expected compiler red")
        for symbol in self.missing_targets:
            req = requirements.get(symbol.acceptance_requirement_id)
            names = [symbol.name, *([symbol.receiver] if symbol.receiver else [])]
            if (
                req is None
                or req.kind != "target"
                or any(
                    re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", req.description) is None
                    for name in names
                )
            ):
                raise ValueError(
                    "Expected missing names must occur in the public target requirement"
                )
        if self.shared_helper is not None:
            req = requirements.get(self.shared_helper.acceptance_requirement_id)
            names = [
                *([self.shared_helper.owner_class] if self.shared_helper.owner_class else []),
                *self.shared_helper.callers,
            ]
            if self.shared_helper.required_name:
                names.append(self.shared_helper.required_name)
            if (
                req is None
                or req.kind != "structure"
                or any(
                    re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", req.description) is None
                    for name in names
                )
            ):
                raise ValueError("Structural API/name constraints must be explicitly public")
            if self.task_kind != "refactor":
                raise ValueError("A shared-helper refactor check belongs to a refactor task")
        if self.public_structural_probe is not None and (
            self.task_kind != "refactor"
            or self.shared_helper is not None
            or not any(r.kind == "structure" for r in self.requirements)
        ):
            raise ValueError(
                "A custom public structure plan needs an explicit refactor requirement"
            )
        return self


class BaselineAssessment(Model):
    receipt_id: str = Field(pattern=SHA)
    producer: Literal["independent-baseline-policy"] = "independent-baseline-policy"
    policy_hash: str = Field(pattern=SHA)
    tree_hash: str = Field(pattern=SHA)
    status: CheckStatus
    behavior_ref: str
    target_ref: str | None
    target_observation: Literal[
        "NOT_RUN", "ASSERTION_RED", "GREEN", "DECLARED_TARGET_MISSING", "UNVERIFIED"
    ]
    detail: str


class JudgeAssertion(Model):
    assertion_id: str = Field(pattern=SHA)
    acceptance_requirement_id: str
    file: str
    line: int = Field(ge=1)
    column: int = Field(ge=0)
    expression_hash: str = Field(pattern=SHA)


class JudgeTraceabilityManifest(Model):
    version: Literal["harness-judge-traceability-v1", "harness-judge-traceability-v2"] = (
        "harness-judge-traceability-v1"
    )
    task_id: str
    benchmark_revision: str
    public_acceptance_hash: str = Field(pattern=SHA)
    files: dict[str, str] = Field(min_length=1, max_length=64)
    assertions: tuple[JudgeAssertion, ...] = Field(min_length=1, max_length=4096)
    command_hash: str | None = Field(default=None, pattern=SHA)
    observation_commands: tuple[tuple[str, ...], ...] = Field(
        default=(), max_length=128, exclude_if=lambda value: not value
    )
    observer_sources: dict[str, str] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def observation_version(self) -> Self:
        if self.version.endswith("-v1") and (self.observation_commands or self.observer_sources):
            raise ValueError("Observation adapters require the explicit v2 review protocol")
        if self.version.endswith("-v2") and not self.observation_commands:
            raise ValueError("Observation review needs a nonempty fixed command inventory")
        if len(set(self.observation_commands)) != len(self.observation_commands):
            raise ValueError("Observation command identities must be unique")
        return self


class SemanticRevision(Model):
    policy: TaskBaselinePolicy
    traceability_path: Path
    traceability_sha256: str = Field(pattern=SHA)
    registry_path: Path
    registry_sha256: str = Field(pattern=SHA)
    structural_probe: PublicProbePlan | None = None
