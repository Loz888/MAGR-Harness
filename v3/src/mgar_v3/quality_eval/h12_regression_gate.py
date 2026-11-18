"""Separate independent dimensions and anchor their immutable refs in the original gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import EvaluationProfile
from mgar_v3.harness.regression_types import (
    CheckStatus,
    DimensionReceipt,
    IndependentEvaluationEvidence,
    JudgeAttestation,
    RegressionCommand,
    RegressionPlan,
    seal,
    valid_seal,
)
from mgar_v3.harness.semantic_types import BaselineAssessment, JudgeTraceabilityManifest
from mgar_v3.quality_eval.h12_baseline_policy import assess_baseline
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_scoring import (
    compare_reports,
    comparison_metrics,
    load_report,
)


def target_plan(profile: EvaluationProfile) -> RegressionPlan | None:
    if profile.baseline_policy is not None:
        return profile.baseline_policy.target_plan
    probe = profile.public_probe
    if probe is None:
        return None
    return RegressionPlan(
        commands=(
            RegressionCommand(
                suite_id="target-" + probe.plan_hash[:20],
                public_argv=probe.argv,
                argv=probe.argv,
                framework="public-probe",
                public_probe=probe,
            ),
        )
    )


def structural_plan(profile: EvaluationProfile) -> RegressionPlan | None:
    probe = profile.structural_probe
    if probe is None:
        return None
    return RegressionPlan(
        commands=(
            RegressionCommand(
                suite_id="structure-" + probe.plan_hash[:20],
                public_argv=probe.argv,
                argv=probe.argv,
                framework="public-probe",
                public_probe=probe,
            ),
        )
    )


def attest_judge(
    artifacts: ArtifactStore,
    raw: Mapping[str, Any],
    *,
    root: Path,
    task_id: str,
    experiment_id: str,
    execution_task_id: str,
    run_id: str,
    execution_epoch: int,
    final_tree: str,
    judge_sha256: str,
    image_id: str,
) -> str:
    status: CheckStatus = "BLOCKED"
    detail = "Independent Judge lacks verified same-tree raw receipts"
    refs, sizes, commands = [], [], []
    traceability_ref = None
    report_ref = artifacts.put(json.dumps(dict(raw), sort_keys=True).encode())
    try:
        if not (
            raw.get("task_id") == task_id
            and raw.get("phase") == "final"
            and raw.get("tree_hash") == final_tree
            and raw.get("judge_sha256") == judge_sha256
        ):
            raise ValueError("Judge identity or final source tree differs")
        if (
            raw.get("receipt_protocol") != "h12-independent-judge-v1"
            or raw.get("image_id") != image_id
        ):
            raise ValueError("Judge execution protocol or actual pinned image differs")
        receipts = raw.get("receipts", ())
        if not isinstance(receipts, list) or not receipts:
            raise ValueError("Judge has no independently executed command receipts")
        for row in receipts:
            path = Path(row["private_log"]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Judge log is outside its private evidence root")
            data = path.read_bytes()
            if (
                row.get("receipt_id") != digest({k: v for k, v in row.items() if k != "receipt_id"})
                or row.get("source_tree_hash") != final_tree
                or row.get("tree_hash") != row.get("verifier_tree_hash")
                or row.get("verifier_tree_hash") not in {raw.get("verifier_tree_hash"), final_tree}
                or row.get("image_id") != image_id
                or row.get("phase") not in {"final", "regression"}
            ):
                raise ValueError("Judge command source/verifier tree, phase or image differs")
            if (
                hashlib.sha256(data).hexdigest() != row["log_sha256"]
                or len(data) != row["log_bytes"]
                or row.get("complete") is not True
                or row.get("log_truncated") is not False
                or row.get("state") not in {"exited", "failed"}
                or type(row.get("exit_code")) is not int
            ):
                raise ValueError("Judge command/log incomplete or tampered")
            refs.append(artifacts.put(data))
            sizes.append(len(data))
            commands.append(
                {
                    k: row[k]
                    for k in (
                        "receipt_id",
                        "operation_id",
                        "argv",
                        "cwd",
                        "tree_hash",
                        "source_tree_hash",
                        "verifier_tree_hash",
                        "image_id",
                        "environment_hash",
                        "state",
                        "exit_code",
                        "log_sha256",
                        "log_bytes",
                    )
                }
            )
        if raw.get("status") not in {"PASS", "FAIL", "NOT_RUN", "BLOCKED"}:
            raise ValueError("Judge status unknown")
        status = cast(CheckStatus, raw["status"])
        if raw.get("judge_traceability_sha256"):
            source = root / "traceability-manifest.json"
            if (
                source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest()
                != raw["judge_traceability_sha256"]
            ):
                raise ValueError("Judge private traceability manifest changed")
            traceability_ref = artifacts.put(source.read_bytes())
        if status == "PASS" and any(
            c["state"] != "exited" or c["exit_code"] != 0 for c in commands
        ):
            raise ValueError("Judge PASS conflicts with actual commands")
        detail = "Independent Judge report and full raw command logs preserved"
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        status, detail = "BLOCKED", "Judge evidence unverified: " + str(exc)[:500]
    value = seal(
        JudgeAttestation,
        dict(
            task_id=task_id,
            experiment_id=experiment_id,
            execution_task_id=execution_task_id,
            run_id=run_id,
            execution_epoch=execution_epoch,
            tree_hash=final_tree,
            judge_sha256=judge_sha256,
            status=status,
            raw_report_ref=report_ref,
            log_refs=tuple(refs),
            log_bytes=tuple(sizes),
            commands=tuple(commands),
            detail=detail,
            traceability_ref=traceability_ref,
        ),
    )
    return artifacts.put(value.model_dump_json().encode())


def _load_judge(
    artifacts: ArtifactStore,
    reference: str,
    evidence: IndependentEvaluationEvidence,
    judge_sha256: str,
    profile: EvaluationProfile,
) -> JudgeAttestation:
    value = JudgeAttestation.model_validate_json(artifacts.read(reference))
    if not (
        valid_seal(value)
        and value.task_id == evidence.task_id
        and value.experiment_id == evidence.experiment_id
        and value.execution_task_id == evidence.execution_task_id
        and value.run_id == evidence.run_id
        and value.execution_epoch == evidence.execution_epoch
        and value.tree_hash == evidence.final_tree
        and value.judge_sha256 == judge_sha256
    ):
        raise ValueError("Judge attestation scope differs")
    raw = json.loads(artifacts.read(value.raw_report_ref))
    if (
        raw.get("tree_hash") != evidence.final_tree
        or raw.get("task_id") != evidence.task_id
        or raw.get("judge_sha256") != judge_sha256
        or raw.get("phase") != "final"
    ):
        raise ValueError("Raw Judge source tree or specification differs")
    if profile.baseline_policy is not None:
        if (
            not value.traceability_ref
            or value.traceability_ref != profile.judge_traceability_sha256
            or raw.get("judge_traceability_sha256") != profile.judge_traceability_sha256
            or raw.get("baseline_policy_hash") != profile.baseline_policy.policy_hash
            or raw.get("judge_review", {}).get("status") != "PASS"
            or raw.get("judge_review", {}).get("execution_plan_verified") is not True
            or raw.get("judge_registry_sha256") != profile.judge_registry_sha256
        ):
            raise ValueError("Judge does not belong to the frozen R06 contract/review")
        review = JudgeTraceabilityManifest.model_validate_json(
            artifacts.read(value.traceability_ref)
        )
        if (
            review.task_id != evidence.task_id
            or review.public_acceptance_hash != profile.baseline_policy.public_acceptance_hash
            or review.benchmark_revision != profile.baseline_policy.benchmark_revision
            or review.files != raw.get("hashes")
        ):
            raise ValueError("Judge review source hashes or public requirements differ")
        actual_private = [r["argv"] for r in raw.get("receipts", []) if r.get("phase") == "final"]
        if (review.command_hash != digest(actual_private)
                or raw["judge_review"].get("assertion_count") != len(review.assertions)
                or raw["judge_review"].get("source_hashes") != review.files):
            raise ValueError("Actual private Judge commands differ from the frozen review")
    if value.status != "BLOCKED":
        rows = raw.get("receipts")
        if (
            raw.get("status") != value.status
            or raw.get("receipt_protocol") != "h12-independent-judge-v1"
            or not isinstance(rows, list)
            or len(value.commands) != len(rows)
            or len(value.commands) != len(value.log_refs)
            or len(value.commands) != len(value.log_bytes)
            or not value.commands
        ):
            raise ValueError("Judge status or receipt set differs")
        for row, command, ref, size in zip(
            rows, value.commands, value.log_refs, value.log_bytes, strict=True
        ):
            if (
                not isinstance(row, dict)
                or row.get("receipt_id")
                != digest({k: v for k, v in row.items() if k != "receipt_id"})
                or any(row.get(k) != v for k, v in command.items())
                or row.get("source_tree_hash") != evidence.final_tree
                or row.get("tree_hash") != row.get("verifier_tree_hash")
                or row.get("verifier_tree_hash")
                not in {raw.get("verifier_tree_hash"), evidence.final_tree}
                or row.get("image_id") != raw.get("image_id")
                or row.get("phase") not in {"final", "regression"}
                or row.get("complete") is not True
                or row.get("log_truncated") is not False
                or row.get("state") not in {"exited", "failed"}
                or type(row.get("exit_code")) is not int
                or value.status == "PASS"
                and (row["exit_code"] != 0 or row["state"] != "exited")
            ):
                raise ValueError("Judge command differs from original native report")
            data = artifacts.read(ref)
            if len(data) != size or ref != command["log_sha256"] or size != command["log_bytes"]:
                raise ValueError("Judge raw log differs")
    return value


def assess_evaluation(
    artifacts: ArtifactStore,
    reference: str,
    profile: EvaluationProfile,
    *,
    task_id: str,
    judge_sha256: str,
    result: Mapping[str, Any],
) -> tuple[CheckStatus, dict[str, Any]]:
    plan = profile.independent_regression_plan
    if plan is None:
        raise ValueError("No frozen independent regression plan")
    evidence = IndependentEvaluationEvidence.model_validate_json(artifacts.read(reference))
    if not (
        valid_seal(evidence)
        and evidence.task_id == task_id
        and evidence.experiment_id == profile.experiment_id
        and evidence.plan_hash == plan.plan_hash
        and evidence.execution_task_id == result.get("task_id")
        and evidence.run_id == result.get("run_id")
        and type(result.get("execution_epoch")) is int
        and 1 <= evidence.execution_epoch <= result["execution_epoch"]
        and evidence.baseline_tree == result.get("tree_before")
        and evidence.final_tree == result.get("tree_after")
    ):
        raise ValueError("Independent evaluation belongs to another sealed candidate")
    comparison = compare_reports(
        artifacts,
        plan,
        baseline_ref=evidence.baseline_ref,
        final_ref=evidence.final_ref,
        baseline_tree=evidence.baseline_tree,
        final_tree=evidence.final_tree,
        task_id=task_id,
        experiment_id=profile.experiment_id,
        execution_task_id=evidence.execution_task_id,
        run_id=evidence.run_id,
        execution_epoch=evidence.execution_epoch,
    )
    comparison_ref = artifacts.put(comparison.model_dump_json().encode())
    dimensions = {}
    baseline_policy_ok = profile.baseline_policy is None
    if profile.baseline_policy is not None:
        if not evidence.baseline_assessment_ref:
            raise ValueError("Original independent task-type baseline proof missing")
        assessment = BaselineAssessment.model_validate_json(
            artifacts.read(evidence.baseline_assessment_ref)
        )
        recomputed = assess_baseline(
            artifacts,
            profile.baseline_policy,
            task_id=task_id,
            experiment_id=profile.experiment_id,
            tree=evidence.baseline_tree,
            behavior_ref=assessment.behavior_ref,
            target_ref=assessment.target_ref,
        )
        if (
            not valid_seal(assessment)
            or assessment != recomputed
            or assessment.behavior_ref != evidence.baseline_ref
        ):
            raise ValueError("Task-type baseline differs from original independent evidence")
        baseline_policy_ok = assessment.status == "PASS"

    def dimension(
        kind: Any, status: CheckStatus, required: bool, refs: tuple[str, ...], detail: str
    ) -> None:
        value = seal(
            DimensionReceipt,
            dict(
                kind=kind,
                status=status,
                required=required,
                tree_hash=evidence.final_tree,
                evidence_refs=refs,
                detail=detail,
            ),
        )
        dimensions[kind] = value.model_dump(mode="json")

    dimension("public_regression", comparison.status, True, (comparison_ref,), comparison.detail)
    target = target_plan(profile)
    if target is not None:
        try:
            if not evidence.target_ref:
                raise ValueError("Independent public target probe not executed")
            report, _ = load_report(
                artifacts,
                evidence.target_ref,
                target,
                task_id=task_id,
                experiment_id=profile.experiment_id,
                phase="final",
                tree=evidence.final_tree,
                role="target_behavior",
                execution_task_id=evidence.execution_task_id,
                run_id=evidence.run_id,
                execution_epoch=evidence.execution_epoch,
            )
            dimension("target_behavior", report.status, True, (evidence.target_ref,), report.detail)
        except (ValueError, DomainError) as exc:
            dimension("target_behavior", "BLOCKED", True, (), getattr(exc, "code", str(exc)))
    else:
        dimension(
            "target_behavior",
            "NOT_RUN",
            False,
            (),
            "No separately frozen public target probe; "
            "combined oracle remains in independent_judge",
        )
    structure = structural_plan(profile)
    if structure is None:
        required_structure = profile.baseline_policy is not None and any(
            r.kind == "structure" for r in profile.baseline_policy.requirements
        )
        dimension(
            "structural_constraints",
            "NOT_RUN",
            required_structure,
            (),
            "No separate frozen structural receipt; no structural PASS inferred",
        )
    else:
        try:
            if not evidence.structural_ref:
                raise ValueError("Declared independent structural probe not executed")
            structural, _ = load_report(
                artifacts,
                evidence.structural_ref,
                structure,
                task_id=task_id,
                experiment_id=profile.experiment_id,
                phase="final",
                tree=evidence.final_tree,
                role="structural_constraints",
                execution_task_id=evidence.execution_task_id,
                run_id=evidence.run_id,
                execution_epoch=evidence.execution_epoch,
            )
            dimension(
                "structural_constraints",
                structural.status,
                True,
                (evidence.structural_ref,),
                structural.detail,
            )
        except (ValueError, DomainError) as exc:
            dimension("structural_constraints", "BLOCKED", True, (), str(exc))
    judge_status: CheckStatus
    try:
        judge = _load_judge(artifacts, evidence.judge_ref, evidence, judge_sha256, profile)
        judge_status, judge_tree, judge_detail = judge.status, judge.tree_hash, judge.detail
    except (ValueError, DomainError, KeyError, TypeError) as exc:
        judge_status, judge_tree, judge_detail = "BLOCKED", None, getattr(exc, "code", str(exc))
    dimension("independent_judge", judge_status, True, (evidence.judge_ref,), judge_detail)
    integrity: CheckStatus = (
        "PASS"
        if comparison.comparable
        and baseline_policy_ok
        and judge_status not in {"BLOCKED", "NOT_RUN"}
        and all(d["status"] in {"PASS", "FAIL"} for d in dimensions.values() if d["required"])
        else "BLOCKED"
    )
    dimension(
        "execution_integrity",
        integrity,
        True,
        (reference,),
        "Same sealed source and complete immutable independent receipts"
        if integrity == "PASS"
        else "Independent execution facts incomplete or not comparable",
    )
    required = [d["status"] for d in dimensions.values() if d["required"]]
    overall: CheckStatus = (
        "FAIL"
        if "FAIL" in required
        else "BLOCKED"
        if any(s != "PASS" for s in required)
        else "PASS"
    )
    metrics = comparison_metrics(comparison)
    if integrity != "PASS":
        metrics.update(
            new_regression_failures=None,
            regression_evidence_known=False,
            regression_evidence_status="BLOCKED",
        )
    return overall, {
        **metrics,
        "dimensions": dimensions,
        "independent_evidence_ref": reference,
        "independent_judge_tree": judge_tree,
        "independent_judge_status": judge_status,
        **(
            {"baseline_policy_status": "PASS" if baseline_policy_ok else "BLOCKED"}
            if profile.baseline_policy is not None
            else {}
        ),
    }


def evaluate_final(
    runner: IndependentRegressionRunner,
    profile: EvaluationProfile,
    source: Path,
    evidence_root: Path,
    *,
    baseline_ref: str | None,
    baseline_tree: str,
    final_tree: str,
    frozen_tests: Mapping[str, str],
    execution_task_id: str,
    run_id: str,
    execution_epoch: int,
    sealed: bool,
    judge_sha256: str,
    judge_root: Path,
    judge: Callable[[], Mapping[str, Any]],
    baseline_assessment_ref: str | None = None,
) -> dict[str, Any]:
    plan = profile.independent_regression_plan
    if plan is None:
        raise ValueError("Independent evaluation needs an explicit frozen plan")
    if not sealed:
        return {
            "status": "NOT_RUN",
            "tree_hash": final_tree,
            "execution_status": "BLOCKED",
            "reason": "Original Engine seal required",
            "new_regression_failures": None,
            "regression_evidence_known": False,
        }
    scope = dict(
        execution_task_id=execution_task_id, run_id=run_id, execution_epoch=execution_epoch
    )
    final_ref, _ = runner.execute(
        source,
        plan,
        evidence_root / "final",
        phase="final",
        expected_tree=final_tree,
        frozen_tests=frozen_tests,
        sealed=sealed,
        execution_task_id=execution_task_id,
        run_id=run_id,
        execution_epoch=execution_epoch,
    )
    target_ref = None
    target = target_plan(profile)
    if target is not None:
        target_ref, _ = runner.execute(
            source,
            target,
            evidence_root / "target",
            phase="final",
            expected_tree=final_tree,
            frozen_tests=frozen_tests,
            sealed=sealed,
            role="target_behavior",
            execution_task_id=execution_task_id,
            run_id=run_id,
            execution_epoch=execution_epoch,
        )
    structural_ref = None
    structure = structural_plan(profile)
    if structure is not None:
        structural_ref, _ = runner.execute(
            source,
            structure,
            evidence_root / "structure",
            phase="final",
            expected_tree=final_tree,
            frozen_tests=frozen_tests,
            sealed=sealed,
            role="structural_constraints",
            execution_task_id=execution_task_id,
            run_id=run_id,
            execution_epoch=execution_epoch,
        )
    try:
        raw = judge()
    except Exception as exc:
        raw = {
            "status": "BLOCKED",
            "task_id": runner.task_id,
            "tree_hash": final_tree,
            "judge_sha256": judge_sha256,
            "phase": "final",
            "reason": type(exc).__name__,
        }
    judge_ref = attest_judge(
        runner.artifacts,
        raw,
        root=judge_root,
        task_id=runner.task_id,
        experiment_id=runner.experiment_id,
        final_tree=final_tree,
        judge_sha256=judge_sha256,
        image_id=runner.image_id,
        execution_task_id=execution_task_id,
        run_id=run_id,
        execution_epoch=execution_epoch,
    )
    value = seal(
        IndependentEvaluationEvidence,
        dict(
            task_id=runner.task_id,
            experiment_id=runner.experiment_id,
            baseline_tree=baseline_tree,
            final_tree=final_tree,
            plan_hash=plan.plan_hash,
            baseline_ref=baseline_ref,
            final_ref=final_ref,
            target_ref=target_ref,
            judge_ref=judge_ref,
            baseline_assessment_ref=baseline_assessment_ref,
            structural_ref=structural_ref,
            **scope,
        ),
    )
    ref = runner.artifacts.put(value.model_dump_json().encode())
    status, metrics = assess_evaluation(
        runner.artifacts,
        ref,
        profile,
        task_id=runner.task_id,
        judge_sha256=judge_sha256,
        result={
            "task_id": execution_task_id,
            "run_id": run_id,
            "execution_epoch": execution_epoch,
            "tree_before": baseline_tree,
            "tree_after": final_tree,
        },
    )
    return {
        "status": "NOT_RUN" if status == "BLOCKED" else status,
        "tree_hash": final_tree,
        "execution_status": status,
        "reason": "Independent required dimensions: " + status,
        **metrics,
    }
