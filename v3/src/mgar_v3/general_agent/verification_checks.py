"""Mechanical checks over immutable content and provider receipts."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from mgar_v3.domain import digest
from mgar_v3.general_agent.actions import ActionRecord
from mgar_v3.general_agent.contracts import TaskSnapshot
from mgar_v3.general_agent.verification_contracts import (
    BrowserState,
    Citation,
    CodingDiff,
    CodingRun,
    ExternalAction,
    Numeric,
    VerificationStep,
)
from mgar_v3.harness.contracts import ExecutionReceipt, WorkspaceDiff


def _finite_number(value: object) -> Decimal:
    if type(value) is int or isinstance(value, Decimal):
        number = Decimal(value)
        if number.is_finite() and abs(number.adjusted()) <= 100:
            return number
    raise ValueError("Invalid numeric data.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("Repeated JSON field.")
    return values


def _numeric(step: Numeric, raw: bytes, output: bytes) -> bool:
    rows = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_object)
    actual = json.loads(output, parse_float=Decimal, object_pairs_hook=_unique_object)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000 or not isinstance(actual, dict):
        return False
    if (
        set(actual) != {"sample_count", "value", "formula", "field"}
        or type(actual["sample_count"]) is not int
        or actual["sample_count"] != len(rows)
        or actual["formula"] != step.formula
        or actual["field"] != step.field
    ):
        return False
    if any(not isinstance(row, dict) or step.field not in row for row in rows):
        return False
    numbers = [_finite_number(row[step.field]) for row in rows]
    with localcontext() as context:
        context.prec = 50
        result = Decimal(len(numbers)) if step.formula == "count" else sum(numbers, Decimal(0))
        if step.formula == "mean":
            result /= len(numbers)
        return _finite_number(actual["value"]) == result


def check_content(
    task: TaskSnapshot, step: VerificationStep, observation: bytes, output: bytes, verifier: str
) -> bool:
    if isinstance(step, Numeric):
        return _numeric(step, observation, output)
    if isinstance(step, Citation):
        reference = step.source.reference.encode()
        excerpt = step.excerpt.encode()
        if step.kind == "citation" and output.startswith(b"%PDF-"):
            from mgar_v3.general_agent.research_pdf import verified_pdf_text

            output = verified_pdf_text(output)
        return excerpt in observation and excerpt in output and reference in output
    if isinstance(step, BrowserState):
        view = json.loads(observation)
        return (
            isinstance(view, dict)
            and view.get("url") == step.source.reference
            and isinstance(view.get("text"), str)
            and step.expected_text in view["text"]
            and step.expected_text.encode() in output
        )
    if isinstance(step, CodingDiff):
        diff = WorkspaceDiff.model_validate_json(observation)
        return (
            (diff.tree_before, diff.tree_after) == (step.before_sha256, step.tree_sha256)
            and diff.workspace_id == task.project_id
            and diff.tree_before != diff.tree_after
            and bool(diff.changes)
            and bool(diff.patch.strip())
            and diff.patch.encode() == output
        )
    if isinstance(step, CodingRun):
        receipt = ExecutionReceipt.model_validate_json(observation)
        expected = "test" if step.kind == "coding_test" else "build"
        return (
            receipt.purpose == expected
            and receipt.argv == step.argv
            and receipt.tree_hash == step.tree_sha256
            and receipt.environment_hash == step.environment_sha256
            and receipt.status == "passed"
            and receipt.exit_code == 0
            and receipt.tests_failed == 0
            and (expected == "build" or receipt.tests_run > 0)
            and receipt.provenance is not None
            and receipt.provenance.workspace_id == task.project_id
            and receipt.provenance.operation_id == receipt.operation_id
            and receipt.provenance.actor != verifier
        )
    raise TypeError("External actions require the authoritative action journal.")


def check_action(
    task: TaskSnapshot,
    step: ExternalAction,
    action: ActionRecord,
    output: bytes,
    now: datetime,
) -> bool:
    receipt = action.receipt
    intent = action.intent
    return (
        action.status == "succeeded"
        and receipt is not None
        and (
            intent.task_id,
            intent.task_revision,
            intent.plan_version,
            intent.scope.target.kind,
            intent.scope.operation,
        )
        == (task.task_id, task.revision, task.plan_version, step.kind, step.operation)
        and intent.scope.effect == "external_write"
        and 0 <= (now - receipt.recorded_at).total_seconds() <= step.max_age_seconds
        and receipt.model_dump_json().encode() == output
        and action.receipt_sha256 == digest(receipt.model_dump(mode="json"))
    )
