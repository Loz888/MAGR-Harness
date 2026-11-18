"""Bounded public command observations, independent of private Judge results."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from mgar_v3.domain import Model, digest

if TYPE_CHECKING:
    from mgar_v3.harness.contracts import ExecutionReceipt, TaskBinding, ToolContext
    from mgar_v3.harness.regression_types import RegressionCommand

SHA = r"^[a-f0-9]{64}$"
PREFIX = "HARNESS_PUBLIC_TEST_FEEDBACK_V1 "


class PublicFeedbackPolicy(Model):
    version: Literal["harness-public-feedback-v1"] = "harness-public-feedback-v1"
    stagnant_batches: int = Field(default=3, ge=2, le=6)


class PublicTestFeedback(Model):
    version: Literal["harness-public-test-feedback-v1"] = "harness-public-test-feedback-v1"
    source: Literal["public-command"] = "public-command"
    task_id: str
    run_id: str
    workspace_id: str
    execution_epoch: int = Field(ge=1)
    acceptance_revision: str
    requirements_hash: str = Field(pattern=SHA)
    task_semantics_hash: str = Field(pattern=SHA)
    command_id: str
    argv: tuple[str, ...]
    receipt_id: str = Field(pattern=SHA)
    tree_hash: str = Field(pattern=SHA)
    log_sha256: str = Field(pattern=SHA)
    artifact_hash: str = Field(pattern=SHA)
    exit_code: int
    outcome: Literal["passed", "failed", "unverified"]
    category: Literal["PUBLIC_GREEN", "PUBLIC_ASSERTION", "PUBLIC_CHECK_ERROR", "UNVERIFIED"]
    log_complete: bool
    tests_run: int | None = Field(default=None, ge=0)
    tests_failed: int | None = Field(default=None, ge=0)
    tests_skipped: int | None = Field(default=None, ge=0)
    tests_xfailed: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    tests_xpassed: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    failure_ids: tuple[Annotated[str, Field(max_length=256)], ...] = Field(
        default=(), max_length=16
    )
    failure_ids_truncated: bool = False
    assertion_excerpt: str = Field(default="", max_length=4000)
    failure_signature: str = Field(pattern=SHA)


def task_semantics_hash(task: TaskBinding) -> str:
    return digest(task.model_dump(mode="json", exclude={"execution_epoch"}))


def failure_observation(output: str) -> tuple[tuple[str, ...], str]:
    """Reporter identities and assertion lines; timings/addresses are not progress."""
    lines = []
    ids = set()
    for raw in output.splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        if isinstance(record, dict):
            if isinstance(record.get("failures"), list):
                for failure in record["failures"]:
                    if not isinstance(failure, dict):
                        continue
                    identity = str(failure.get("file", "")).removeprefix("/workspace/")
                    title = str(failure.get("fullTitle", failure.get("title", "")))
                    if title:
                        ids.add((identity + "::" if identity else "") + title)
                    error = failure.get("err", {})
                    if isinstance(error, dict):
                        for key in ("message", "expected", "actual"):
                            if key in error:
                                lines.append(key + ": " + str(error[key])[:1000])
                continue
            if record.get("Action") == "fail" and record.get("Test"):
                ids.add(str(record.get("Package", "")) + "::" + str(record["Test"]))
            if isinstance(record.get("Output"), str):
                raw = record["Output"].rstrip()
            elif record.get("Action"):
                continue
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).strip()
        match = re.match(r"(?:FAILED|ERROR) (\S+?)(?:\s+-\s+|$)", line)
        if match:
            ids.add(match[1])
        match = re.match(r"(?:FAIL|ERROR): (.+)$|--- FAIL: (\S+)", line)
        if match:
            ids.add(next(g for g in match.groups() if g))
        match = re.match(r"not ok \d+\s*-?\s*(.+)", line)
        if match:
            ids.add(match[1])
        if re.search(
            r"assert|expected|actual|exception|error|^E\s|not ok|^--- FAIL|^FAIL:", line, re.I
        ):
            line = re.sub(r"0x[a-fA-F0-9]+", "<address>", line)
            line = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", line)
            line = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|s)\b", "<time>", line)
            if line not in lines:
                lines.append(line[:1000])
    # Bound the failure context before it can enlarge a checkpoint/model request.
    return tuple(sorted(ids)), "\n".join(lines)[:4000]


def observe_public_command(
    receipt: ExecutionReceipt,
    context: ToolContext,
    *,
    command_id: str,
    artifact_hash: str,
    complete: bool,
    command: RegressionCommand | None = None,
) -> PublicTestFeedback:
    from mgar_v3.harness.acceptance import requirements_hash
    from mgar_v3.harness.contracts import sha256_text
    from mgar_v3.harness.project_commands import parse_tests, test_project_kind

    output = receipt.stdout + receipt.stderr
    complete = (
        complete
        and sha256_text(output) == receipt.log_sha256
        and receipt.tree_hash == context.workspace_tree
    )
    ids, excerpt = failure_observation(output)
    ran, failed, skipped = None, None, None
    xfailed, xpassed = 0, 0
    status = "unverified"
    if complete and receipt.status in {"passed", "failed"}:
        if receipt.purpose == "build":
            status = receipt.status
        elif receipt.public_probe is not None:
            status = {"green": "passed", "red": "failed"}.get(
                str(receipt.public_probe.get("outcome")), "unverified"
            )
            ran, failed, skipped = receipt.tests_run, receipt.tests_failed, 0
            ids = tuple(
                sorted(
                    str(case["id"])
                    for case in receipt.public_probe.get("cases", [])
                    if case.get("status") == "assertion_failed"
                )
            )
        elif command is not None and command.argv == receipt.argv:
            from mgar_v3.harness.test_results import parse_independent_test

            native = parse_independent_test(command, output, receipt.exit_code, complete=complete)
            if native.parsed:
                ran, failed, skipped = native.tests_run, native.tests_failed, native.tests_skipped
                xfailed, xpassed = native.tests_xfailed, native.tests_xpassed
            if native.status in {"PASS", "FAIL"}:
                status = "passed" if native.status == "PASS" else "failed"
                ids = native.failure_ids or ()
        else:
            parsed, count, failures, exclusions, _ = parse_tests(
                test_project_kind(receipt.argv, fallback="unknown"), output, receipt.exit_code
            )
            if parsed in {"passed", "failed"} and parsed == receipt.status:
                ran, failed, skipped = count, failures, exclusions
                status = parsed if exclusions == 0 else "unverified"
    outcome: Literal["passed", "failed", "unverified"] = (
        "passed" if status == "passed" else "failed" if status == "failed" else "unverified"
    )
    if outcome == "passed":
        ids, excerpt = (), ""
    category: Literal["PUBLIC_GREEN", "PUBLIC_ASSERTION", "PUBLIC_CHECK_ERROR", "UNVERIFIED"] = (
        "UNVERIFIED"
        if outcome == "unverified"
        else "PUBLIC_GREEN"
        if outcome == "passed"
        else "PUBLIC_ASSERTION"
        if ids or re.search(r"AssertionError|assert |not ok", excerpt)
        else "PUBLIC_CHECK_ERROR"
    )
    signature = digest(
        {
            "outcome": outcome,
            "exit_code": receipt.exit_code,
            "tests_run": ran,
            "tests_failed": failed,
            "tests_skipped": skipped,
            "failure_ids": ids,
            "assertion_excerpt": excerpt,
        }
    )
    return PublicTestFeedback(
        task_id=context.task.task_id,
        run_id=str(context.task.run_id),
        workspace_id=context.task.workspace_id,
        execution_epoch=(
            receipt.provenance.execution_epoch
            if receipt.provenance is not None
            else context.task.execution_epoch
        ),
        acceptance_revision=context.task.acceptance_revision,
        requirements_hash=requirements_hash(context.task),
        task_semantics_hash=task_semantics_hash(context.task),
        command_id=command_id,
        argv=receipt.argv,
        receipt_id=receipt.receipt_id,
        tree_hash=receipt.tree_hash,
        log_sha256=receipt.log_sha256,
        artifact_hash=artifact_hash,
        exit_code=receipt.exit_code,
        outcome=outcome,
        category=category,
        log_complete=complete,
        tests_run=ran,
        tests_failed=failed,
        tests_skipped=skipped,
        tests_xfailed=xfailed,
        tests_xpassed=xpassed,
        failure_ids=tuple(i if len(i) <= 256 else i[:223] + ":" + digest(i)[:32] for i in ids[:16]),
        failure_ids_truncated=len(ids) > 16 or any(len(i) > 256 for i in ids),
        assertion_excerpt=excerpt,
        failure_signature=signature,
    )


def render_public_feedback(feedback: PublicTestFeedback) -> str:
    return PREFIX + feedback.model_dump_json() + "\n"
