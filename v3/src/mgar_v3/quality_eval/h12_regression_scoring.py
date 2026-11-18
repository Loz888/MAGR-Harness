"""Recompute independent regression observations from immutable CAS and raw logs."""

from __future__ import annotations

from typing import Any, Literal

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.regression_types import (
    DimensionReceipt,
    IndependentComparison,
    IndependentRegressionReport,
    IndependentTestReceipt,
    RegressionPlan,
    seal,
    valid_seal,
)
from mgar_v3.harness.test_results import parse_independent_test


def load_report(
    artifacts: ArtifactStore,
    reference: str,
    plan: RegressionPlan,
    *,
    task_id: str,
    experiment_id: str,
    phase: Literal["baseline", "final"],
    tree: str,
    role: Literal[
        "public_regression", "target_behavior", "structural_constraints"
    ] = "public_regression",
    execution_task_id: str | None = None,
    run_id: str | None = None,
    execution_epoch: int | None = None,
    environment_spec_hash: str | None = None,
) -> tuple[IndependentRegressionReport, tuple[IndependentTestReceipt, ...]]:
    report = IndependentRegressionReport.model_validate_json(artifacts.read(reference))
    if not (
        valid_seal(report)
        and report.producer == "independent-runner"
        and report.task_id == task_id
        and report.experiment_id == experiment_id
        and report.phase == phase
        and report.role == role
        and report.tree_hash == tree
        and report.observed_tree_before == report.observed_tree_after == tree
        and report.plan_hash == plan.plan_hash
        and report.suite_ids == tuple(s.suite_id for s in plan.commands)
        and len(report.receipt_refs) == len(plan.commands)
        and bool(plan.commands)
        and len(set(report.receipt_refs)) == len(plan.commands)
        and (environment_spec_hash is None or report.environment_spec_hash == environment_spec_hash)
    ):
        raise ValueError("Independent manifest, final tree or complete command scope differs")
    if phase == "final":
        if not (
            run_id
            and execution_task_id
            and execution_epoch
            and report.run_id == run_id
            and report.execution_task_id == execution_task_id
            and report.execution_epoch is not None
            and 1 <= report.execution_epoch <= execution_epoch
        ):
            raise ValueError("Independent evidence belongs to another Run/epoch")
    elif (
        report.run_id is not None
        or report.execution_task_id is not None
        or report.execution_epoch is not None
    ):
        raise ValueError("Baseline was not an independent original-tree observation")
    receipts = []
    for suite, ref in zip(plan.commands, report.receipt_refs, strict=True):
        value = IndependentTestReceipt.model_validate_json(artifacts.read(ref))
        if not (
            valid_seal(value)
            and value.task_id == report.task_id
            and value.experiment_id == report.experiment_id
            and value.run_id == report.run_id
            and value.execution_task_id == report.execution_task_id
            and value.execution_epoch == report.execution_epoch
            and value.phase == report.phase
            and value.role == role
            and value.suite_id == suite.suite_id
            and value.plan_hash == plan.plan_hash
            and value.tree_hash == tree
            and value.verifier_tree_before == value.verifier_tree_after == tree
            and value.public_tests_hash == report.public_tests_hash
            and value.public_argv == suite.public_argv
            and value.argv == suite.argv
            and value.cwd == suite.cwd
            and value.environment_spec_hash == report.environment_spec_hash
            and value.image_id == report.image_id
            and value.command_id
            and value.handle_id
        ):
            raise ValueError("Independent command receipt scope or identity differs")
        raw = artifacts.read(value.log_hash)
        complete = (
            value.log_complete
            and not value.log_truncated
            and value.state in {"exited", "failed"}
            and len(raw) == value.log_bytes
        )
        parsed = parse_independent_test(
            suite, raw.decode("utf-8"), value.exit_code, complete=complete
        )
        if parsed != value.result:
            raise ValueError("Saved counters differ from raw independent test evidence")
        receipts.append(value)
    observed = (
        "BLOCKED"
        if any(not r.result.complete for r in receipts)
        else "FAIL"
        if any(r.result.status == "FAIL" for r in receipts)
        else "PASS"
    )
    if report.status != observed:
        raise ValueError("Independent report status differs from command outcomes")
    return report, tuple(receipts)


def compare_reports(
    artifacts: ArtifactStore,
    plan: RegressionPlan,
    *,
    baseline_ref: str | None,
    final_ref: str | None,
    baseline_tree: str,
    final_tree: str,
    task_id: str,
    experiment_id: str,
    execution_task_id: str | None,
    run_id: str | None,
    execution_epoch: int | None,
) -> IndependentComparison:
    values: dict[str, Any] = dict(
        status="NOT_RUN",
        comparable=False,
        new_regression_failures=None,
        final_regression_status="NOT_RUN",
        baseline_ref=baseline_ref,
        final_ref=final_ref,
        baseline_tree=baseline_tree,
        final_tree=final_tree,
        plan_hash=plan.plan_hash,
        detail="Independent baseline and final full-suite receipts are required",
    )
    if not baseline_ref or not final_ref:
        return seal(IndependentComparison, values)
    try:
        final, after = load_report(
            artifacts,
            final_ref,
            plan,
            task_id=task_id,
            experiment_id=experiment_id,
            phase="final",
            tree=final_tree,
            execution_task_id=execution_task_id,
            run_id=run_id,
            execution_epoch=execution_epoch,
        )
        values["final_regression_status"] = final.status
        if all(r.result.failure_ids is not None for r in after):
            values["final_failures"] = tuple(
                sorted(
                    r.suite_id + "::" + name for r in after for name in r.result.failure_ids or ()
                )
            )
        baseline, before = load_report(
            artifacts,
            baseline_ref,
            plan,
            task_id=task_id,
            experiment_id=experiment_id,
            phase="baseline",
            tree=baseline_tree,
        )
        if not (
            baseline.environment_spec_hash == final.environment_spec_hash
            and baseline.image_id == final.image_id
            and baseline.public_tests_hash == final.public_tests_hash
            and all(r.result.complete for r in (*before, *after))
        ):
            raise ValueError("Baseline/final environment, frozen tests or coverage is incomplete")
        before_ids: set[str] = set()
        after_ids: set[str] = set()
        for old, new in zip(before, after, strict=True):
            if old.environment_hash != new.environment_hash or {
                c.test_id for c in old.result.cases
            } != {c.test_id for c in new.result.cases}:
                raise ValueError("Baseline and final test identities or actual environment differ")
            before_ids.update(old.suite_id + "::" + name for name in old.result.failure_ids or ())
            after_ids.update(new.suite_id + "::" + name for name in new.result.failure_ids or ())
        introduced = tuple(sorted(after_ids - before_ids))
        values.update(
            status="FAIL" if after_ids else "PASS",
            comparable=True,
            new_regression_failures=len(introduced),
            baseline_failures=tuple(sorted(before_ids)),
            final_failures=tuple(sorted(after_ids)),
            introduced_failures=introduced,
            detail="Complete matching frozen scopes; failure identities compared independently",
        )
    except (DomainError, ValueError, KeyError, TypeError, AttributeError) as exc:
        values.update(
            status="BLOCKED",
            detail="Independent evidence not comparable: "
            + (getattr(exc, "code", None) or str(exc))[:500],
        )
    return seal(IndependentComparison, values)


def comparison_metrics(value: IndependentComparison) -> dict[str, Any]:
    data = value.model_dump(mode="json")
    return {
        "scoring_protocol": "harness-independent-regression-v1",
        "new_regression_failures": value.new_regression_failures,
        "regression_evidence_known": value.comparable,
        "regression_evidence_status": value.status,
        "regression_evidence_scope": "independent_full_public",
        "regression_comparison_receipt": value.receipt_id,
        "regression_baseline_verified": value.comparable,
        "regression_final_tree": value.final_tree,
        "regression_baseline_failures": data["baseline_failures"],
        "regression_final_failures": data["final_failures"],
        "regression_introduced_failures": data["introduced_failures"],
        "independent_public_regression_status": value.final_regression_status,
        "regression_detail": value.detail,
    }


def unverified_metrics(
    reason: str = "Independent authoritative receipts unavailable",
) -> dict[str, Any]:
    dimensions = {}
    for kind in (
        "target_behavior",
        "public_regression",
        "independent_judge",
        "structural_constraints",
        "execution_integrity",
    ):
        value = seal(
            DimensionReceipt,
            dict(
                kind=kind,
                status="NOT_RUN",
                required=kind not in {"target_behavior", "structural_constraints"},
                tree_hash=None,
                evidence_refs=(),
                detail=reason,
            ),
        )
        dimensions[kind] = value.model_dump(mode="json")
    return {
        "dimensions": dimensions,
        "scoring_protocol": "harness-independent-regression-v1",
        "new_regression_failures": None,
        "regression_evidence_known": False,
        "regression_evidence_status": "NOT_RUN",
        "regression_evidence_scope": "independent_full_public",
        "independent_public_regression_status": "NOT_RUN",
        "regression_detail": reason,
    }
