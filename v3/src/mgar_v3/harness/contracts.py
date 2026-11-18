"""Versioned contracts for the Harness V4 composition layer.

The harness owns orchestration ports only.  Threads, runs, execution epochs,
budgets, findings, approvals and final business status stay with the existing
Engine and PostgreSQL transaction boundaries.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.acceptance import AcceptanceBinding, AcceptanceContract, AcceptanceScope
from mgar_v3.harness.lifecycle_types import HTTPExpectation, LifecyclePolicy, RuntimeControl
from mgar_v3.harness.output_protocol import (
    ModelOutputReceipt,
    OutputBudget,
    OutputContinuation,
    OutputPolicy,
)
from mgar_v3.harness.public_feedback import PublicFeedbackPolicy, PublicTestFeedback
from mgar_v3.harness.public_probe_types import (
    PUBLIC_PROBE_PREFIX,
    PublicProbeFrame,
    PublicProbePlan,
)
from mgar_v3.harness.regression_types import RegressionPlan
from mgar_v3.harness.semantic_types import TaskBaselinePolicy
from mgar_v3.harness.tool_errors import ToolErrorPolicy

CONTRACT_VERSION = "harness-contract-v1.1"
TASK_KINDS = ("inspect", "bugfix", "feature", "refactor", "run_app")

TaskKind = Literal["inspect", "bugfix", "feature", "refactor", "run_app"]
OperationKind = Literal["model", "tool"]
ReservationStatus = Literal["reserved", "settled", "released", "unknown"]
ToolStatus = Literal["ok", "denied", "error", "cancelled"]
ExecutionStatus = Literal["passed", "failed", "timeout", "denied", "error"]
VerificationStatus = Literal["verified", "failed", "incomplete"]
HarnessStatus = Literal["verified", "incomplete", "cancelled", "denied", "unknown"]
FinishReason = Literal["stop", "tool_calls", "length", "cancelled", "error"]

STATE_OWNERSHIP: dict[str, str] = {
    "session": "app.threads",
    "turn": "app.runs",
    "step": "app.run_events.safe_payload.operation_id",
    "execution_epoch": "app.runs.execution_epoch",
    "budget_reservation": "Engine model/tool ledger",
    "budget_settlement": "Engine model/tool ledger",
    "finding": "existing finding store",
    "approval": "existing approval record",
    "task_status": "Engine task transaction",
    "final_completion": "Engine transaction plus verifier receipt",
    "event_cursor": "app.run_events.event_id",
    "workspace_version": "WorkspaceSnapshot.tree_hash",
    "review_bundle": "existing Review Bundle service",
}

HARNESS_DOES_NOT_OWN = (
    "task status",
    "finding lifecycle",
    "approval decision",
    "budget settlement",
    "publication",
    "final business completion",
)


def canonical_hash(value: Any) -> str:
    return digest(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_relative_path(value: str) -> str:
    """Normalize a repository-relative path and reject traversal."""
    if value in {"", "."}:
        return ""
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise DomainError("HARNESS_PATH_DENIED", "Path must be repository-relative", 403)
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise DomainError("HARNESS_PATH_DENIED", "Path cannot traverse outside the workspace", 403)
    return "/".join(parts)


# Single source of truth for paths that never participate in a workspace tree
# hash.  Both the workspace scanner and the execution broker must use this rule
# so a test run inside the container cannot make their hashes disagree.
WORKSPACE_ARTIFACT_PARTS = frozenset(
    {
        ".git",
        ".mgar",
        ".harness-tmp",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "node_modules",
        ".tox",
    }
)
WORKSPACE_ARTIFACT_SUFFIXES = (".pyc", ".pyo")
WORKSPACE_ARTIFACT_NAMES = frozenset({".coverage"})
_COVERAGE_PARALLEL_DATA = re.compile(r"\.coverage\..+\.[0-9]+\.[^.]+")


def is_workspace_artifact(relative: str) -> bool:
    """Classify reserved metadata, dependencies and runtime cache paths.

    Coverage data is limited to .coverage and parallel files named
    .coverage.<host>.<numeric-pid>.<nonce>. Configuration files, XML reports
    and HTML report directories remain versioned project inputs. Put
    disposable reports under .harness-tmp to exclude them explicitly.
    """

    parts = tuple(part for part in relative.split("/") if part)
    if not parts:
        return False
    if any(part in WORKSPACE_ARTIFACT_PARTS for part in parts):
        return True
    name = parts[-1]
    if name.endswith(WORKSPACE_ARTIFACT_SUFFIXES):
        return True
    if name in WORKSPACE_ARTIFACT_NAMES:
        return True
    return _COVERAGE_PARALLEL_DATA.fullmatch(name) is not None


def _within(path: str, roots: Sequence[str]) -> bool:
    normalized = normalize_relative_path(path)
    for root in roots:
        prefix = normalize_relative_path(root)
        if prefix == "" or normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


class CancellationToken:
    """Cooperative cancellation checked at every model and tool boundary."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def ensure_active(self) -> None:
        if self._cancelled:
            raise DomainError("HARNESS_CANCELLED", "Harness execution was cancelled", 409)


class VerificationRequirement(Model):
    requirement_id: str = Field(min_length=1, max_length=100)
    kind: Literal["test", "build"] = "test"
    command_contains: tuple[str, ...] = ()
    min_tests: int = Field(default=1, ge=1, le=100000)
    acceptance_role: Literal["legacy", "regression", "behavior"] = Field(
        default="legacy", exclude_if=lambda value: value == "legacy"
    )
    command_argv: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    public_probe_plan_hash: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )


class TaskBinding(Model):
    """Bind one harness task to an existing Engine run.

    ``actor`` is a trusted internal Engine principal produced by the SA02
    identity mapping.  It is case-sensitive and is never derived from message
    text, model output, or a raw platform opaque ID; the raw SourceActorRef is
    stored separately by the owning identity service.
    """

    task_id: str = Field(min_length=1, max_length=100)
    thread_id: UUID
    run_id: UUID
    execution_epoch: int = Field(ge=1)
    actor: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    workspace_id: str = Field(min_length=1, max_length=100)
    acceptance_revision: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=16000)
    task_kind: TaskKind = "bugfix"
    requirements: tuple[VerificationRequirement, ...] = Field(min_length=0, max_length=32)
    public_requirements: tuple[str, ...] = Field(
        default=(), max_length=64, exclude_if=lambda value: not value
    )
    acceptance_baseline_commit: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{40}$", exclude_if=lambda value: value is None
    )
    acceptance_scope: AcceptanceScope | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def verification_requirements(self) -> Self:
        if len(set(self.public_requirements)) != len(self.public_requirements) or any(
            not value.strip() or len(value) > 1024 for value in self.public_requirements
        ):
            raise ValueError("Public acceptance clauses must be unique and bounded")
        if not self.requirements and self.task_kind != "run_app":
            raise ValueError("This task needs at least one verification requirement")
        roles = {requirement.acceptance_role for requirement in self.requirements}
        if roles - {"legacy"}:
            if self.task_kind == "inspect":
                raise ValueError("Inspection cannot require public command execution")
            if roles != {"regression", "behavior"}:
                raise ValueError("Public verification needs explicit regression and behavior roles")
            if not any(
                req.kind == "test" and req.acceptance_role == "regression"
                for req in self.requirements
            ):
                raise ValueError("Public verification needs a real regression test requirement")
            for requirement in self.requirements:
                if not requirement.command_argv:
                    raise ValueError("Public verification needs an exact fixed command")
                if requirement.acceptance_role == "behavior":
                    if (
                        requirement.kind != "test"
                        or not requirement.public_probe_plan_hash
                        or len(requirement.command_argv) != 3
                        or requirement.command_argv[:2] != ("python3", "-B")
                        or not re.fullmatch(
                            r"/harness-public-probe/[a-f0-9]{64}\.py",
                            requirement.command_argv[2],
                        )
                    ):
                        raise ValueError("Public behavior needs a hash-bound fixed probe")
                elif requirement.public_probe_plan_hash is not None:
                    raise ValueError("Regression cannot impersonate the public behavior probe")
        return self

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return value.lower()


class CapabilityGrant(Model):
    actor: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    read_paths: tuple[str, ...] = ("",)
    write_paths: tuple[str, ...] = ()
    execute: bool = False
    network: bool = False
    allowed_commands: tuple[str, ...] = ()
    max_seconds: int = Field(default=120, ge=1, le=3600)

    def can_read(self, path: str) -> bool:
        return _within(path, self.read_paths)

    def can_write(self, path: str) -> bool:
        return _within(path, self.write_paths)

    def can_execute(self, argv: Sequence[str]) -> bool:
        if not self.execute or not argv:
            return False
        name = argv[0].rsplit("/", 1)[-1]
        return name in self.allowed_commands


class BudgetLimits(Model):
    max_model_calls: int = Field(default=8, ge=1, le=64)
    max_tool_calls: int = Field(default=32, ge=1, le=256)
    max_cost_micros: int = Field(default=500000, ge=0, le=100000000)
    max_seconds: int = Field(default=900, ge=1, le=3600)


class EvaluationResources(Model):
    """Frozen H12 resources supplied by the trusted evaluation controller."""

    max_model_calls: int = Field(ge=1, le=32)
    max_tool_calls: int = Field(ge=1, le=200)
    max_cost_micros: int = Field(ge=0, le=12000000)
    deadline_seconds: int = Field(ge=1, le=3600)
    max_context_tokens: int = Field(ge=2048, le=400000)
    max_repair_rounds: int = Field(ge=1, le=12)
    max_output_tokens: int = Field(default=8192, ge=512, le=131072)

    def budget_limits(self) -> BudgetLimits:
        return BudgetLimits(
            max_model_calls=self.max_model_calls,
            max_tool_calls=self.max_tool_calls,
            max_cost_micros=self.max_cost_micros,
            max_seconds=self.deadline_seconds,
        )


class EvaluationProfile(Model):
    """Internal port; public Web requests cannot select this authority.

    Engine binds this exact profile to a pre-authorized frozen experiment.
    ``model`` contains the complete ModelCapabilities document.
    """

    experiment_id: str = Field(min_length=1, max_length=100)
    task_id: str = Field(min_length=1, max_length=150)
    split: Literal["dev", "holdout"]
    arm: str = Field(min_length=1, max_length=100)
    task_kind: TaskKind
    objective: str = Field(min_length=1)
    output_policy: OutputPolicy | None = Field(default=None, exclude_if=lambda value: value is None)
    tool_error_policy: ToolErrorPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    lifecycle_policy: LifecyclePolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    lifecycle_routes: tuple[HTTPExpectation, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    independent_regression_plan: RegressionPlan | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    baseline_policy: TaskBaselinePolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    structural_probe: PublicProbePlan | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    judge_traceability_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )
    judge_registry_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )
    public_feedback_policy: PublicFeedbackPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    resources: EvaluationResources
    model: dict[str, Any]
    read_paths: tuple[str, ...] = ("",)
    write_paths: tuple[str, ...] = ()
    public_test_argv: tuple[str, ...] = ()
    public_build_argv: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    service_argv: tuple[str, ...] = ()
    service_mode: Literal["http", "oneshot"] = "http"
    service_port: int = Field(default=8000, ge=0, le=65535)
    service_path: str = "/"
    service_timeout_seconds: float = Field(default=10, gt=0, le=300)
    sandbox_nofile: int = Field(default=128, ge=128, le=4096, exclude_if=lambda value: value == 128)
    sandbox_log_bytes: int = Field(
        default=65536, ge=1024, le=1048576, exclude_if=lambda value: value == 65536
    )
    sandbox_scratch_mb: int = Field(
        default=64, ge=16, le=2048, exclude_if=lambda value: value == 64
    )
    sandbox_memory_mb: int = Field(
        default=256, ge=256, le=4096, exclude_if=lambda value: value == 256
    )
    acceptance_revision: str = Field(default="harness-v4-scoring-v1", min_length=1)
    acceptance_protocol: Literal["legacy", "contract-v1"] = Field(
        default="legacy", exclude_if=lambda value: value == "legacy"
    )
    public_requirements: tuple[str, ...] = Field(
        default=(), max_length=64, exclude_if=lambda value: not value
    )
    acceptance_baseline_commit: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{40}$", exclude_if=lambda value: value is None
    )
    verification_protocol: Literal["legacy", "public-probe-v1"] = Field(
        default="legacy", exclude_if=lambda value: value == "legacy"
    )
    public_probe: PublicProbePlan | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    sandbox_offline_network: Literal["none", "connect-timeout-v1", "got-network-v1"] = Field(
        default="none", exclude_if=lambda value: value == "none"
    )

    @field_validator("read_paths", "write_paths")
    @classmethod
    def scope_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_relative_path(path) for path in paths)

    @field_validator("public_test_argv", "public_build_argv", "service_argv")
    @classmethod
    def fixed_commands(cls, argv: tuple[str, ...]) -> tuple[str, ...]:
        if len(argv) > 128 or any(
            not value or len(value) > 4096 or any(char in value for char in "\x00\r\n")
            for value in argv
        ):
            raise ValueError("Frozen commands need bounded nonempty argv without control lines")
        return argv

    @model_validator(mode="after")
    def dynamic_port_needs_observation(self) -> Self:
        if self.service_port == 0 and self.lifecycle_policy is None:
            raise ValueError("A dynamic service port requires the frozen lifecycle protocol")
        return self

    @model_validator(mode="after")
    def semantic_revision_binding(self) -> Self:
        policy = self.baseline_policy
        if policy is None:
            if (self.structural_probe is not None or self.judge_traceability_sha256 is not None
                    or self.judge_registry_sha256 is not None):
                raise ValueError("R06 checks require an explicit public task-type policy")
            return self
        if (policy.task_kind != self.task_kind or self.acceptance_protocol != "contract-v1"
                or self.acceptance_revision != policy.benchmark_revision
                or self.independent_regression_plan != policy.behavior_plan
                or not self.judge_traceability_sha256
                or not self.judge_registry_sha256
                or {r.description for r in policy.requirements}
                != set(self.public_requirements)):
            raise ValueError("Semantic policy differs from the frozen task/public acceptance")
        if policy.shared_helper is not None:
            from mgar_v3.quality_eval.h12_structural_probe import structural_program

            _, expected = structural_program(policy.shared_helper)
            if self.structural_probe != expected:
                raise ValueError("Structural probe is not the fixed declared public checker")
        elif self.structural_probe != policy.public_structural_probe:
            raise ValueError("Structural probe needs an explicit matching public specification")
        if policy.task_kind == "run_app" and self.lifecycle_policy is None:
            raise ValueError("R06 run_app final requires the R04 lifecycle policy")
        return self

    @model_validator(mode="after")
    def public_feedback_binding(self) -> Self:
        if self.public_feedback_policy is None:
            return self
        plan = self.independent_regression_plan
        if (self.acceptance_protocol != "contract-v1" or plan is None or not plan.commands
                or self.verification_protocol != "public-probe-v1" or self.public_probe is None):
            raise ValueError("R07 requires a public contract and frozen complete regression plan")
        programs = {argv[0] for argv in (self.public_test_argv, self.public_build_argv,
                    self.public_probe.argv if self.public_probe else ()) if argv}
        if len(plan.commands) > 8 or any(
            c.cwd != "." or c.framework == "unconfigured" or c.argv[0] not in programs
            for c in plan.commands
        ):
            raise ValueError(
                "R07 checks need root-scoped configured commands within existing executables")
        return self


class BudgetSnapshot(Model):
    task_id: str
    model_calls: int
    tool_calls: int
    cost_micros: int


class BudgetReservation(Model):
    reservation_id: str
    task_id: str
    operation_id: str
    kind: OperationKind
    reserved_micros: int = Field(ge=0)
    reserved_units: int = Field(ge=1)
    status: ReservationStatus = "reserved"
    actual_micros: int = Field(default=0, ge=0)
    actual_units: int = Field(default=0, ge=0)
    reason: str = ""


class HarnessToolCall(Model):
    call_id: str = Field(min_length=1, max_length=100)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProviderContextRef(Model):
    """Private provider continuation bytes, never report or tool evidence."""

    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    wire_bytes: int = Field(ge=2, le=262144)


class ModelMessage(Model):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=200000)
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[HarnessToolCall, ...] = ()
    provider_context: ProviderContextRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ToolSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    capability: Literal["read", "write", "execute"] = "read"


class ModelUsage(Model):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micros: int = Field(default=0, ge=0)
    known: bool = True
    reasoning_tokens: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )


class ModelRequest(Model):
    operation_id: str = Field(min_length=1, max_length=100)
    task: TaskBinding
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    model_id: str = Field(min_length=1, max_length=100)
    price_version: str = Field(min_length=1, max_length=100)
    max_output_tokens: int = Field(ge=1, le=131072)
    reservation_micros: int = Field(ge=0)
    context_revision: str = Field(min_length=1, max_length=100)
    workspace_tree: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_budget: OutputBudget | None = Field(default=None, exclude_if=lambda value: value is None)
    output_continuation: OutputContinuation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    acceptance_binding: AcceptanceBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ModelResponse(Model):
    text: str = Field(default="", max_length=200000)
    tool_calls: tuple[HarnessToolCall, ...] = ()
    finish_reason: FinishReason = "stop"
    usage: ModelUsage = Field(default_factory=ModelUsage)
    provider: str = ""
    model_id: str = ""
    price_version: str = ""
    error_code: str = ""
    cached: bool = False
    output_receipt: ModelOutputReceipt | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    provider_context: ProviderContextRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ModelStreamEvent(Model):
    type: Literal[
        "text_delta",
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
        "usage",
        "finish",
        "metadata",
        "reasoning_delta",
    ]
    text: str = ""
    tool_index: int = Field(default=0, ge=0)
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""
    initial_arguments: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    usage: ModelUsage | None = None
    finish_reason: FinishReason | None = None
    provider_finish_reason: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    error_code: str = ""
    provider_request_id: str | None = Field(default=None, max_length=200)
    provider_context: ProviderContextRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class WorkspaceSnapshot(Model):
    workspace_id: str
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: dict[str, str] = Field(default_factory=dict)


class TextReplacement(Model):
    """One hash-pinned snippet replacement located by unique context.

    This mirrors the H05 replacement protocol: ``old_text`` must match exactly,
    ``old_sha256`` pins that fragment when the caller can provide it (a real
    language model cannot compute hashes, so it may omit the field and the
    harness pins ``sha256(old_text)`` itself), ``occurrence`` disambiguates
    repeated fragments, and a missing or non-unique context is rejected
    instead of guessed.
    """

    old_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    old_text: str = Field(min_length=1, max_length=2000000)
    new_text: str = Field(max_length=2000000)
    occurrence: int | None = Field(default=None, ge=1, le=1000)


class WorkspaceEdit(Model):
    """One file edit carrying either full ``content`` or ``replacements``.

    The full-file form is unchanged for compatibility.  The incremental form
    keeps the same pre-image ``expected_sha256`` check but only transmits the
    changed fragments, so a large file can be edited without re-emitting it.
    """

    path: str = Field(min_length=1, max_length=300)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    create: bool = False
    content: str | None = Field(default=None, max_length=2000000)
    replacements: list[TextReplacement] | None = Field(default=None, max_length=512)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def edit_shape(self) -> Self:
        if (self.content is None) == (self.replacements is None):
            raise ValueError("Workspace edits require exactly one of content or replacements")
        if self.create:
            if self.expected_sha256 is not None or self.replacements is not None:
                raise ValueError("A new file requires an absent pre-image and full content")
        elif self.expected_sha256 is None:
            raise ValueError("An existing file edit requires expected_sha256")
        return self


class WorkspaceChange(Model):
    path: str
    change: Literal["added", "modified"]
    before_sha256: str | None
    after_sha256: str


class WorkspaceDiff(Model):
    workspace_id: str
    tree_before: str
    tree_after: str
    changes: tuple[WorkspaceChange, ...]
    patch: str = ""


class ToolContext(Model):
    task: TaskBinding
    grant: CapabilityGrant
    operation_id: str
    workspace_tree: str = Field(pattern=r"^[a-f0-9]{64}$")


class HarnessToolResult(Model):
    call_id: str
    status: ToolStatus
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    code: str = ""


class ExecutionRequest(Model):
    operation_id: str = Field(min_length=1, max_length=100)
    purpose: Literal["test", "build", "command"] = "test"
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    cwd: str = "."
    timeout_seconds: float = Field(default=60, gt=0, le=1800)
    execution_epoch: int = Field(default=0, ge=0)
    runtime_control: RuntimeControl | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ExecutionProvenance(Model):
    """Immutable provenance captured by the Broker's actual authorization.

    This identifies the grant that generated output; it cannot restore that
    grant or authorize reads after the current authority changes.
    """

    schema_version: Literal["harness-execution-provenance-v1"] = "harness-execution-provenance-v1"
    operation_id: str
    lease_id: str
    lease_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str
    workspace_id: str
    repository: str
    execution_epoch: int = Field(ge=1)
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    grant_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExecutionReceipt(Model):
    receipt_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_id: str
    purpose: Literal["test", "build", "command"]
    status: ExecutionStatus
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    log_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tests_run: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)
    provenance: ExecutionProvenance | None = None
    public_probe: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    public_feedback: PublicTestFeedback | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class VerificationCheck(Model):
    requirement_id: str
    status: Literal["passed", "failed", "missing"]
    detail: str = ""
    receipt_id: str | None = None


def execution_matches(
    task: TaskBinding,
    requirement: VerificationRequirement,
    receipt: ExecutionReceipt,
) -> bool:
    """Keep legacy matching; new proof plans additionally require exact identity."""
    if requirement.command_argv and receipt.purpose != (
        "build" if requirement.kind == "build" else "test"
    ):
        return False
    if requirement.command_argv and receipt.argv != requirement.command_argv:
        return False
    if any(token not in " ".join(receipt.argv) for token in requirement.command_contains):
        return False
    if requirement.public_probe_plan_hash is None:
        return True
    proof = receipt.public_probe or {}
    origin = receipt.provenance
    asset = proof.get("asset_sha256")
    # These fields are derived facts, not acceptance labels supplied by a model.
    # Keep their meaning consistent even if an in-process caller changes a DTO.
    output = receipt.stdout + receipt.stderr
    if (
        receipt.stderr
        or not output.startswith(PUBLIC_PROBE_PREFIX)
        or not output.endswith("\n")
        or output.count("\n") != 1
        or "\r" in output
        or sha256_text(output) != receipt.log_sha256
    ):
        return False

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("Duplicate public frame keys")
        return dict(pairs)

    try:
        plan = PublicProbePlan.model_validate(proof.get("plan"))
        frame = PublicProbeFrame.model_validate(
            json.loads(
                output[len(PUBLIC_PROBE_PREFIX) : -1],
                object_pairs_hook=unique_object,
            )
        )
    except (ValueError, RecursionError):
        return False
    if (
        plan.plan_hash != requirement.public_probe_plan_hash
        or plan.argv != receipt.argv
        or frame.plan_id != plan.plan_id
        or frame.asset_sha256 != plan.asset_sha256
        or {case.id for case in frame.cases} != set(plan.case_ids)
    ):
        return False
    cases = [case.model_dump(mode="json") for case in frame.cases]
    failures = sum(case.status == "assertion_failed" for case in frame.cases)
    green = all(case.status == "passed" for case in frame.cases)
    red = failures > 0 and all(
        case.status in {"passed", "assertion_failed"} for case in frame.cases
    )
    outcome = proof.get("outcome")
    consistent = (
        outcome == "green"
        and green
        and receipt.status == "passed"
        and receipt.exit_code == 0
        or outcome == "red"
        and red
        and receipt.status == "failed"
        and receipt.exit_code == 1
    )
    return bool(
        origin is not None
        and proof.get("plan_hash") == requirement.public_probe_plan_hash
        and proof.get("task_id") == task.task_id
        and proof.get("tree_hash") == receipt.tree_hash
        and proof.get("execution_epoch") == origin.execution_epoch
        and 1 <= origin.execution_epoch <= task.execution_epoch
        and origin.actor == task.actor
        and origin.workspace_id == task.workspace_id
        and origin.repository == task.repository
        and isinstance(asset, str)
        and re.fullmatch(r"[a-f0-9]{64}", asset)
        and f"/harness-public-probe/{asset}.py" in receipt.argv
        and receipt.purpose == "test"
        and proof.get("reason_code") == ""
        and isinstance(proof.get("source_receipt_id"), str)
        and re.fullmatch(r"[a-f0-9]{64}", proof["source_receipt_id"])
        and frame.asset_sha256 == asset
        and proof.get("cases") == cases
        and receipt.tests_run == len(cases)
        and receipt.tests_run >= requirement.min_tests
        and receipt.tests_failed == failures
        and consistent
    )


class VerificationRequest(Model):
    task: TaskBinding
    snapshot: WorkspaceSnapshot
    receipts: tuple[ExecutionReceipt, ...] = ()
    acceptance_contract: AcceptanceContract | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class VerificationReceipt(Model):
    verification_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str
    status: VerificationStatus
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    checks: tuple[VerificationCheck, ...]
    receipt_ids: tuple[str, ...] = ()
    acceptance_binding: AcceptanceBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ContextView(Model):
    revision: str
    system_prompt: str
    messages: tuple[ModelMessage, ...]
    estimated_tokens: int = Field(default=0, ge=0)


class EventDraft(Model):
    run_id: UUID
    operation_id: str = Field(min_length=1, max_length=100)
    execution_epoch: int = Field(ge=1)
    revision: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(Model):
    """A versioned harness event projected onto ``app.run_events``.

    ``cursor`` is the persisted ``event_id bigserial`` value.  ``envelope_id``
    and ``operation_id`` are not table columns; they live inside the versioned
    ``safe_payload`` JSON object built by :meth:`to_run_event_safe_payload`.
    """

    envelope_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    operation_id: str
    execution_epoch: int
    revision: int
    cursor: int
    kind: str
    payload: dict[str, Any]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_run_event_safe_payload(self) -> dict[str, Any]:
        """Return the exact object stored in ``app.run_events.safe_payload``."""
        return {
            "schema": "harness-event-v1",
            "envelope_id": str(self.envelope_id),
            "operation_id": self.operation_id,
            "execution_epoch": self.execution_epoch,
            "revision": self.revision,
            "payload": self.payload,
        }


class HarnessResult(Model):
    task_id: str
    status: HarnessStatus
    summary: str = ""
    verification: VerificationReceipt | None = None
    events_cursor: int = 0
    unresolved: tuple[str, ...] = ()
    model_calls: int = 0
    tool_calls: int = 0


@runtime_checkable
class ModelAdapter(Protocol):
    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse: ...


@runtime_checkable
class StreamingModelAdapter(Protocol):
    def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> Iterator[ModelStreamEvent]: ...

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse: ...


@runtime_checkable
class ToolRegistry(Protocol):
    def specs(self) -> Sequence[ToolSpec]: ...

    def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult: ...


@runtime_checkable
class WorkspaceProvider(Protocol):
    def snapshot(self) -> WorkspaceSnapshot: ...

    def read(self, path: str) -> str: ...

    def list_dir(self, path: str) -> tuple[str, ...]: ...

    def apply(self, edits: Sequence[WorkspaceEdit], *, expected_tree: str) -> WorkspaceSnapshot: ...

    def diff(self, tree_before: str) -> WorkspaceDiff: ...


@runtime_checkable
class ExecutionBroker(Protocol):
    def execute(
        self, request: ExecutionRequest, grant: CapabilityGrant, cancellation: CancellationToken
    ) -> ExecutionReceipt: ...


@runtime_checkable
class ContextManager(Protocol):
    def build(self, task: TaskBinding, events: Sequence[EventEnvelope]) -> ContextView: ...


@runtime_checkable
class Verifier(Protocol):
    def verify(self, request: VerificationRequest) -> VerificationReceipt: ...


@runtime_checkable
class EventSink(Protocol):
    def emit(self, draft: EventDraft) -> EventEnvelope: ...

    def events(self, run_id: UUID, after_cursor: int = 0) -> Sequence[EventEnvelope]: ...


@runtime_checkable
class BudgetLedger(Protocol):
    def reserve(
        self,
        task_id: str,
        operation_id: str,
        kind: OperationKind,
        *,
        reserved_micros: int = 0,
        reserved_units: int = 1,
    ) -> BudgetReservation: ...

    def settle(
        self,
        reservation_id: str,
        *,
        actual_micros: int,
        actual_units: int = 1,
    ) -> BudgetReservation: ...

    def mark_unknown(self, reservation_id: str, reason: str) -> BudgetReservation: ...

    def release(self, reservation_id: str) -> BudgetReservation: ...

    def snapshot(self, task_id: str) -> BudgetSnapshot: ...
