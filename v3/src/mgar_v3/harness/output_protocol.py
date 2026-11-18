"""Bounded model output facts; none of these records authorize a tool or a retry."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator
from pydantic import Field
from referencing import Registry

from mgar_v3.domain import DomainError, Model
from mgar_v3.evidence.artifacts import ArtifactStore

if TYPE_CHECKING:
    from mgar_v3.harness.contracts import HarnessToolCall, ModelRequest, ToolSpec

OutputStage = Literal["investigate", "implement", "finalize", "continuation"]
OutputObserver = Callable[[Any, dict[str, Any]], None]


class OutputBudget(Model):
    stage: OutputStage
    completion_tokens: int = Field(ge=1, le=131072)
    reasoning_bytes: int = Field(ge=128, le=262144)
    tool_argument_bytes: int = Field(ge=128, le=262144)
    answer_bytes: int = Field(ge=128, le=200000)
    reasoning_control: Literal["provider-effort-local-byte-bound"] = (
        "provider-effort-local-byte-bound"
    )


class OutputPolicy(Model):
    schema_version: Literal["harness-output-policy-v1"] = "harness-output-policy-v1"
    investigate_tokens: int = Field(default=4096, ge=1, le=131072)
    implement_tokens: int = Field(default=8192, ge=1, le=131072)
    finalize_tokens: int = Field(default=4096, ge=1, le=131072)
    continuation_tokens: int = Field(default=8192, ge=1, le=131072)
    max_continuations: int = Field(default=2, ge=0, le=4)

    def budget(self, stage: OutputStage, capability_limit: int) -> OutputBudget:
        tokens = min(getattr(self, stage + "_tokens"), capability_limit)
        return OutputBudget(
            stage=stage, completion_tokens=tokens,
            reasoning_bytes=max(128, min(262144, tokens * 16)),
            tool_argument_bytes=max(128, min(262144, tokens * 12)),
            answer_bytes=max(128, min(200000, tokens * 12)),
        )


class OutputContinuation(Model):
    parent_operation_id: str = Field(min_length=1, max_length=100)
    root_operation_id: str = Field(min_length=1, max_length=100)
    source_artifact: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_request_id: str | None = None
    count: int = Field(ge=1, le=4)
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    acceptance_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt_ids: tuple[str, ...] = ()
    remaining_budget: dict[str, Any]


class ToolOutputState(Model):
    tool_call_id: str
    name: str
    arguments_status: Literal["complete", "invalid_or_truncated", "not_completed"]
    execution_status: Literal["not_executed"] = "not_executed"


class ModelOutputReceipt(Model):
    schema_version: Literal["harness-model-output-v1"] = "harness-model-output-v1"
    operation_id: str
    provider_request_id: str | None = None
    finish_reason: str | None
    provider_finish_reason: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    response_status: Literal["complete", "incomplete", "invalid", "unknown"]
    usage_status: Literal["known", "unknown"]
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_calls: tuple[ToolOutputState, ...] = ()
    continuation_count: int = Field(default=0, ge=0, le=4)
    error_code: str = ""
    output_budget: OutputBudget | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation: OutputContinuation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def validate_output_result(result: Mapping[str, Any], profile: Any) -> None:
    """New frozen profiles cannot score a truncated or unbound final reply as PASS."""
    policy = profile.output_policy
    if policy is None:
        return
    try:
        if result["output_policy"] != policy.model_dump(mode="json"):
            raise ValueError("Frozen output policy differs")
        receipts = [ModelOutputReceipt.model_validate(r) for r in result["output_receipts"]]
        by_operation = {r.operation_id: r for r in receipts}
        positions = {r.operation_id: i for i, r in enumerate(receipts)}
        if not receipts or len(receipts) != len(by_operation):
            raise ValueError("Missing or duplicated output receipt")
        count = int(result["output_continuations"])
        if not 0 <= count <= policy.max_continuations:
            raise ValueError("Continuation allowance exceeded")
        for receipt in receipts:
            if receipt.output_budget is None or receipt.output_budget != policy.budget(
                receipt.output_budget.stage,
                min(profile.resources.max_output_tokens, profile.model["max_output_tokens"]),
            ):
                raise ValueError("Phase output budget differs")
            continuation = receipt.continuation
            if continuation is not None:
                parent = by_operation.get(continuation.parent_operation_id)
                if (parent is None or parent.artifact_hash != continuation.source_artifact
                        or parent.usage_status != "known"
                        or parent.response_status not in {"incomplete", "invalid"}
                        or continuation.count > count
                        or positions[parent.operation_id] >= positions[receipt.operation_id]):
                    raise ValueError("Unresolved or unbound continuation parent")
        parents = {r.continuation.parent_operation_id for r in receipts if r.continuation}
        observed_counts = {r.continuation.count for r in receipts if r.continuation}
        if result["status"] == "completed" and (
            receipts[-1].finish_reason != "stop"
            or receipts[-1].response_status != "complete"
            or any(r.usage_status != "known" for r in receipts)
            or any(r.response_status != "complete" and r.operation_id not in parents
                   for r in receipts)
            or observed_counts != set(range(1, count + 1))
        ):
            raise ValueError("No complete known final reply")
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("MODEL_OUTPUT_INTEGRITY_FAILED",
                          "Cannot verify the complete bounded output chain", 409) from exc


def strict_arguments(raw: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON object member")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError("Non-finite JSON value")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("Tool arguments must be an object")
    return value


def validate_tool_calls(calls: Sequence[HarnessToolCall], tools: Sequence[ToolSpec]) -> None:
    declared = {tool.name: tool for tool in tools}
    if len({call.call_id for call in calls}) != len(calls):
        raise ValueError("Duplicate tool call IDs")
    for call in calls:
        spec = declared.get(call.name)
        if spec is None:
            raise ValueError("Tool name is incomplete or undeclared")
        # Empty Registry never retrieves remote references or grants network access.
        validator = Draft202012Validator(spec.input_schema, registry=Registry())
        if next(validator.iter_errors(call.arguments), None) is not None:
            raise ValueError("Tool arguments do not satisfy the declared schema")


def numeric_argument_feedback(
    proposals: Sequence[dict[str, Any]], tools: Sequence[ToolSpec],
) -> str:
    """Report declared numeric bounds without copying untrusted argument values."""
    declared = {tool.name: tool for tool in tools}
    hints: list[str] = []
    for proposal in proposals[:8]:
        spec = declared.get(proposal.get("name", ""))
        if spec is None:
            continue
        try:
            arguments = strict_arguments(proposal.get("arguments") or proposal["initial_arguments"])
        except (KeyError, TypeError, ValueError, RecursionError):
            continue
        schema = spec.input_schema
        for field, rule in schema.get("properties", {}).items():
            value = arguments.get(field)
            if not isinstance(rule, dict) or type(value) not in (int, float):
                continue
            lower, upper = rule.get("minimum"), rule.get("maximum")
            if lower is not None and value < lower:
                limit = f"at least {lower}"
            elif upper is not None and value > upper:
                limit = f"at most {upper}"
            else:
                continue
            message = f"{spec.name}.{field} must be {limit}."
            if spec.capability == "read" and field not in schema.get("required", []):
                message += f" Omit {field} from the new {spec.name} call to use its default."
            if message not in hints:
                hints.append(message)
            if len(hints) == 8:
                return " ".join(hints)
    return " ".join(hints)


class OutputCapture:
    """Private immutable snapshots, indexed by the original Run's event stream."""

    def __init__(self, artifacts: ArtifactStore | None = None) -> None:
        self.artifacts = artifacts
        self.memory: dict[str, bytes] = {}
        self.observer: OutputObserver | None = None

    def put(self, request: ModelRequest, payload: Mapping[str, Any]) -> str:
        raw = json.dumps({
            "schema": "harness-private-model-output-v1",
            "task_id": request.task.task_id, "run_id": str(request.task.run_id),
            "workspace_id": request.task.workspace_id,
            "execution_epoch": request.task.execution_epoch,
            "operation_id": request.operation_id, "model_id": request.model_id,
            "price_version": request.price_version,
            "context_revision": request.context_revision,
            "max_output_tokens": request.max_output_tokens,
            "workspace_tree": request.workspace_tree,
            "acceptance_hash": request.acceptance_binding.acceptance_hash
            if request.acceptance_binding else None,
            "output_budget": request.output_budget.model_dump(mode="json")
            if request.output_budget else None,
            "continuation": request.output_continuation.model_dump(mode="json")
            if request.output_continuation else None,
            **payload,
        }, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > 1600000:
            raise DomainError("MODEL_OUTPUT_CAPTURE_LIMIT", "Output capture exceeds its bound", 413)
        key = hashlib.sha256(raw).hexdigest()
        if self.artifacts is not None:
            self.artifacts.put(raw)
        else:
            self.memory[key] = raw
        if self.observer is not None:
            self.observer(request, {
                "artifact_hash": key, "provider_request_id": payload.get("provider_request_id"),
                "finish_reason": payload.get("finish_reason"),
                "response_status": payload.get("response_status"),
                "usage_status": "known" if payload.get("usage", {}).get("known") else "unknown",
            })
        return key
