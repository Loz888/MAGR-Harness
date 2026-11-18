"""Versioned, independent test observations; no model command or authority input."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.harness.diagnostics import CommandDiagnostics
from mgar_v3.harness.public_probe_types import PublicProbePlan

CheckStatus = Literal["PASS", "FAIL", "NOT_RUN", "BLOCKED"]
TestFramework = Literal[
    "pytest", "unittest", "go-json", "mocha-json", "tap", "public-probe", "unconfigured"
]
SHA = r"^[a-f0-9]{64}$"


class ExpectedFailureSource(Model):
    path: str
    sha256: str = Field(pattern=SHA)
    line: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=4096)


class RegressionCommand(Model):
    suite_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")
    public_argv: tuple[str, ...] = Field(min_length=1, max_length=128)
    argv: tuple[str, ...] = Field(min_length=1, max_length=144)
    cwd: str = "."
    framework: TestFramework
    public_probe: PublicProbePlan | None = None
    expected_exceptions: dict[str, Literal["skipped", "xfailed"]] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    expected_failure_sources: dict[str, ExpectedFailureSource] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    identity_policy: Literal["strict-v1", "mocha-file-title-occurrence-v1"] = Field(
        default="strict-v1", exclude_if=lambda value: value == "strict-v1"
    )

    @field_validator("argv", "public_argv")
    @classmethod
    def bounded_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v or len(v) > 4096 or any(c in v for c in "\x00\r\n") for v in value):
            raise ValueError("Fixed test argv needs bounded values without control characters")
        return value

    @field_validator("cwd")
    @classmethod
    def relative(cls, value: str) -> str:
        from mgar_v3.harness.contracts import normalize_relative_path

        return normalize_relative_path(value) or "."

    @model_validator(mode="after")
    def probe_shape(self) -> Self:
        if (self.framework == "public-probe") != (self.public_probe is not None):
            raise ValueError("Only public-probe execution may carry a frozen public probe")
        if self.public_probe and self.argv != self.public_probe.argv:
            raise ValueError("Public probe argv differs from its frozen asset")
        if (len(self.expected_exceptions) > 1024 or any(
            not name or len(name) > 4096 for name in self.expected_exceptions
        ) or self.expected_exceptions and self.public_probe is not None):
            raise ValueError("Only bounded exact existing suite exceptions may be frozen")
        if self.expected_failure_sources and (
            self.framework != "tap" or any(
                self.expected_exceptions.get(name) != "xfailed"
                for name in self.expected_failure_sources)
        ):
            raise ValueError("AVA expected failures need frozen TAP exception/source identities")
        if self.identity_policy != "strict-v1" and self.framework != "mocha-json":
            raise ValueError("Occurrence identities require the explicit Mocha JSON reporter")
        return self


class RegressionPlan(Model):
    version: Literal["harness-independent-regression-v1"] = "harness-independent-regression-v1"
    parser_version: Literal["harness-test-results-v1"] = "harness-test-results-v1"
    commands: tuple[RegressionCommand, ...] = Field(default=(), max_length=32)
    timeout_seconds: float = Field(default=300, gt=0, le=600)
    package_scripts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def distinct_suites(self) -> Self:
        if len(self.package_scripts) > 256 or any(
            not k or len(k) > 256 or not v or len(v) > 16384
            for k, v in self.package_scripts.items()
        ):
            raise ValueError("Frozen package scripts exceed their bound")
        if len({s.suite_id for s in self.commands}) != len(self.commands):
            raise ValueError("Independent suites need unique identifiers")
        if len({(s.argv, s.cwd) for s in self.commands}) != len(self.commands):
            raise ValueError("The same independent command may not be counted twice")
        return self

    @property
    def plan_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class TestCaseObservation(Model):
    test_id: str = Field(min_length=1, max_length=4096)
    outcome: Literal["passed", "failed", "skipped", "xfailed", "xpassed"]


class TestResult(Model):
    parser_version: Literal["harness-test-results-v1"] = "harness-test-results-v1"
    status: CheckStatus
    parsed: bool
    complete: bool
    tests_run: int | None = Field(default=None, ge=0)
    tests_failed: int | None = Field(default=None, ge=0)
    tests_skipped: int | None = Field(default=None, ge=0)
    tests_xfailed: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    tests_xpassed: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    cases: tuple[TestCaseObservation, ...] = ()
    failure_ids: tuple[str, ...] | None = None
    detail: str


class IndependentTestReceipt(Model):
    schema_version: Literal["harness-independent-test-v1"] = "harness-independent-test-v1"
    receipt_id: str = Field(pattern=SHA)
    producer: Literal["independent-runner"] = "independent-runner"
    task_id: str
    experiment_id: str
    execution_task_id: str | None = None
    run_id: str | None = None
    execution_epoch: int | None = Field(default=None, ge=1)
    phase: Literal["baseline", "final"]
    role: Literal["public_regression", "target_behavior", "structural_constraints"] = (
        "public_regression"
    )
    suite_id: str
    plan_hash: str = Field(pattern=SHA)
    tree_hash: str = Field(pattern=SHA)
    verifier_tree_before: str = Field(pattern=SHA)
    verifier_tree_after: str = Field(pattern=SHA)
    public_tests_hash: str = Field(pattern=SHA)
    public_argv: tuple[str, ...]
    argv: tuple[str, ...]
    cwd: str
    environment_hash: str = Field(pattern=SHA)
    environment_spec_hash: str = Field(pattern=SHA)
    image_id: str
    command_id: str
    handle_id: str
    state: str
    exit_code: int | None
    started_at: str
    finished_at: str
    log_hash: str = Field(pattern=SHA)
    log_bytes: int = Field(ge=0)
    log_complete: bool
    log_truncated: bool
    result: TestResult
    diagnostics: CommandDiagnostics | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class IndependentRegressionReport(Model):
    schema_version: Literal["harness-independent-report-v1"] = "harness-independent-report-v1"
    receipt_id: str = Field(pattern=SHA)
    producer: Literal["independent-runner"] = "independent-runner"
    task_id: str
    experiment_id: str
    execution_task_id: str | None = None
    run_id: str | None = None
    execution_epoch: int | None = Field(default=None, ge=1)
    phase: Literal["baseline", "final"]
    role: Literal["public_regression", "target_behavior", "structural_constraints"] = (
        "public_regression"
    )
    status: CheckStatus
    tree_hash: str = Field(pattern=SHA)
    observed_tree_before: str = Field(pattern=SHA)
    observed_tree_after: str = Field(pattern=SHA)
    public_tests_hash: str = Field(pattern=SHA)
    plan_hash: str = Field(pattern=SHA)
    environment_spec_hash: str = Field(pattern=SHA)
    image_id: str
    suite_ids: tuple[str, ...]
    receipt_refs: tuple[str, ...]
    detail: str


class IndependentComparison(Model):
    receipt_id: str = Field(pattern=SHA)
    status: CheckStatus
    comparable: bool
    new_regression_failures: int | None = Field(default=None, ge=0)
    baseline_failures: tuple[str, ...] | None = None
    final_failures: tuple[str, ...] | None = None
    introduced_failures: tuple[str, ...] | None = None
    final_regression_status: CheckStatus
    baseline_ref: str | None
    final_ref: str | None
    baseline_tree: str
    final_tree: str
    plan_hash: str
    detail: str


class JudgeAttestation(Model):
    receipt_id: str = Field(pattern=SHA)
    producer: Literal["independent-judge-adapter"] = "independent-judge-adapter"
    task_id: str
    experiment_id: str
    execution_task_id: str
    run_id: str
    execution_epoch: int = Field(ge=1)
    tree_hash: str = Field(pattern=SHA)
    judge_sha256: str = Field(pattern=SHA)
    status: CheckStatus
    raw_report_ref: str = Field(pattern=SHA)
    log_refs: tuple[str, ...]
    log_bytes: tuple[int, ...]
    commands: tuple[dict[str, Any], ...]
    detail: str
    traceability_ref: str | None = Field(
        default=None, pattern=SHA, exclude_if=lambda value: value is None
    )


class IndependentEvaluationEvidence(Model):
    receipt_id: str = Field(pattern=SHA)
    producer: Literal["independent-evaluator"] = "independent-evaluator"
    task_id: str
    experiment_id: str
    execution_task_id: str
    run_id: str
    execution_epoch: int = Field(ge=1)
    baseline_tree: str = Field(pattern=SHA)
    final_tree: str = Field(pattern=SHA)
    plan_hash: str = Field(pattern=SHA)
    baseline_ref: str | None
    final_ref: str | None
    target_ref: str | None
    judge_ref: str = Field(pattern=SHA)
    baseline_assessment_ref: str | None = Field(
        default=None, pattern=SHA, exclude_if=lambda value: value is None
    )
    structural_ref: str | None = Field(
        default=None, pattern=SHA, exclude_if=lambda value: value is None
    )


class DimensionReceipt(Model):
    receipt_id: str = Field(pattern=SHA)
    kind: Literal[
        "target_behavior",
        "public_regression",
        "independent_judge",
        "structural_constraints",
        "execution_integrity",
    ]
    status: CheckStatus
    required: bool
    tree_hash: str | None
    evidence_refs: tuple[str, ...]
    detail: str


def seal[T: Model](cls: type[T], values: dict[str, Any]) -> T:
    value = cls.model_validate({"receipt_id": "0" * 64, **values})
    return value.model_copy(
        update={
            "receipt_id": digest(
                value.model_dump(mode="json", exclude={"receipt_id"}),
            )
        }
    )


def valid_seal(value: Model) -> bool:
    return getattr(value, "receipt_id", None) == digest(
        value.model_dump(mode="json", exclude={"receipt_id"}),
    )
