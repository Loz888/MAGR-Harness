"""Task-type baseline evidence; compiler red never becomes a green test receipt."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.regression_types import IndependentTestReceipt, seal
from mgar_v3.harness.semantic_types import BaselineAssessment, TaskBaselinePolicy
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_scoring import load_report

_INFRA = re.compile(
    r"SyntaxError|NameError|ModuleNotFoundError|No module named|cannot find (?:module|package)|"
    r"no required module provides|go: downloading|(?:command|executable) not found|"
    r"permission denied|network is unreachable|resource temporarily unavailable|"
    r"out of memory|too many open files|unexpected (?:name|token|EOF)|syntax error|"
    r"no Go files|build constraints exclude all|\[setup failed\]",
    re.I,
)


def _text(artifacts: ArtifactStore, receipts: tuple[IndependentTestReceipt, ...]) -> str:
    chunks = []
    for receipt in receipts:
        data = artifacts.read(receipt.log_hash).decode()
        for line in data.splitlines():
            try:
                item = json.loads(line)
                chunks.append(item.get("Output", "") if isinstance(item, dict) else line + "\n")
            except ValueError:
                chunks.append(line + "\n")
    # Test identities are not compiler/runtime diagnostics. Real Go projects
    # contain names such as TestInvalidSyntaxError and TestNotExistError.
    return "\n".join(line for line in "".join(chunks).splitlines() if not re.match(
        r"^(?:=== (?:RUN|PAUSE|CONT)\s+|\s*--- (?:PASS|FAIL|SKIP):\s+|"
        r"[^\s]+\.py::.*\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS)(?:\s|$))", line
    ))


def declared_missing_target(output: str, policy: TaskBaselinePolicy) -> bool:
    """Only exact publicly declared symbols in frozen target reference files qualify.

    Go's receiver is checked as well: an unrelated name or dependency at the
    same public file is still a blocker. Other languages use assertion probes
    until an exact diagnostic adapter is registered; no guessed import pardon.
    """
    if policy.task_kind != "feature" or not policy.missing_targets or _INFRA.search(output):
        return False
    errors = re.findall(r"(?m)^(?:\./)?([^\s:]+\.go):\d+:\d+: ([^\n]+)$", output)
    if not errors:
        return False
    for path, message in errors:
        plain = re.fullmatch(r"undefined: (\w+)", message)
        method = re.fullmatch(
            r"\w+\.(\w+) undefined \(type \*?(\w+) has no field or method (\w+)\)", message
        )
        if not any(
            path in target.reference_paths
            and (
                plain is not None
                and target.receiver is None
                and plain[1] == target.name
                or method is not None
                and method[1] == method[3] == target.name
                and method[2] == target.receiver
            )
            for target in policy.missing_targets
        ):
            return False
    # Reject other compiler diagnostics rather than accepting one matching line
    # beside an unrelated failure. Go build trailers are observations, not tests.
    diagnostic = re.findall(r"(?m)^([^\s:]+(?:/[^\s:]+)*):\d+(?::\d+)?: ([^\n]+)$", output)
    return len(diagnostic) == len(errors)


def assess_baseline(
    artifacts: ArtifactStore,
    policy: TaskBaselinePolicy,
    *,
    task_id: str,
    experiment_id: str,
    tree: str,
    behavior_ref: str,
    target_ref: str | None,
) -> BaselineAssessment:
    status: Literal["PASS", "BLOCKED"] = "BLOCKED"
    observation: Literal[
        "NOT_RUN", "ASSERTION_RED", "GREEN", "DECLARED_TARGET_MISSING", "UNVERIFIED"
    ] = "NOT_RUN"
    detail = "The task-type baseline has not been independently verified"
    try:
        behavior, old = load_report(
            artifacts,
            behavior_ref,
            policy.behavior_plan,
            task_id=task_id,
            experiment_id=experiment_id,
            phase="baseline",
            tree=tree,
        )
        if not all(r.result.complete for r in old) or _INFRA.search(_text(artifacts, old)):
            raise ValueError("Existing behavior did not execute completely")
        if behavior.status != "PASS" and policy.task_kind != "bugfix":
            raise ValueError("The original existing behavior matrix must be green")
        if policy.target_plan is not None:
            if not target_ref:
                raise ValueError("Separate public target baseline not executed")
            target, rows = load_report(
                artifacts,
                target_ref,
                policy.target_plan,
                task_id=task_id,
                experiment_id=experiment_id,
                phase="baseline",
                tree=tree,
                role="target_behavior",
            )
            text = _text(artifacts, rows)
            if not all(
                r.log_complete
                and not r.log_truncated
                and r.exit_code in {0, 1}
                and r.state in {"exited", "failed"}
                for r in rows
            ):
                raise ValueError("Target command has incomplete/unknown execution or logs")
            if target.status == "PASS" and all(r.result.complete for r in rows):
                observation = "GREEN"
            elif (
                target.status == "FAIL"
                and all(r.result.complete for r in rows)
                and not _INFRA.search(text)
                and "ImportError" not in text
            ):
                observation = "ASSERTION_RED"
            elif (
                len(rows) == 1 and rows[0].exit_code == 1 and declared_missing_target(text, policy)
            ):
                observation = "DECLARED_TARGET_MISSING"
            else:
                observation = "UNVERIFIED"
                raise ValueError(
                    "Target failure is not the declared assertion or missing feature API"
                )
        if policy.task_kind == "bugfix" and observation != "ASSERTION_RED":
            raise ValueError("Bugfix needs a real failing target assertion on the original tree")
        if policy.task_kind == "feature" and observation not in {
            "GREEN",
            "ASSERTION_RED",
            "DECLARED_TARGET_MISSING",
        }:
            raise ValueError("Feature original target observation is unknown")
        status = "PASS"
        detail = (
            "Existing behavior verified; original target="
            + observation
            + "; final structure and service lifecycle were not run on the baseline"
        )
    except (DomainError, ValueError, KeyError, TypeError) as exc:
        detail = "Task-type baseline blocked: " + str(exc)[:500]
    return seal(
        BaselineAssessment,
        dict(
            policy_hash=policy.policy_hash,
            tree_hash=tree,
            status=status,
            behavior_ref=behavior_ref,
            target_ref=target_ref,
            target_observation=observation,
            detail=detail,
        ),
    )


def execute_baseline(
    runner: IndependentRegressionRunner,
    policy: TaskBaselinePolicy,
    source: Path,
    evidence: Path,
    *,
    tree: str,
    frozen_tests: Mapping[str, str],
) -> tuple[str, BaselineAssessment]:
    behavior_ref, _ = runner.execute(
        source,
        policy.behavior_plan,
        evidence / "behavior",
        phase="baseline",
        expected_tree=tree,
        frozen_tests=frozen_tests,
    )
    target_ref = None
    if policy.target_plan is not None:
        target_ref, _ = runner.execute(
            source,
            policy.target_plan,
            evidence / "target",
            phase="baseline",
            expected_tree=tree,
            frozen_tests=frozen_tests,
            role="target_behavior",
        )
    value = assess_baseline(
        runner.artifacts,
        policy,
        task_id=runner.task_id,
        experiment_id=runner.experiment_id,
        tree=tree,
        behavior_ref=behavior_ref,
        target_ref=target_ref,
    )
    ref = runner.artifacts.put(value.model_dump_json().encode())
    return ref, value
