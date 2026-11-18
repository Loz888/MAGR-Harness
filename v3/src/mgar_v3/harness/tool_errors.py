"""Bounded error feedback. These receipts describe authority; they never grant it."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

from pydantic import Field

from mgar_v3.domain import Model, digest

if TYPE_CHECKING:
    from mgar_v3.harness.contracts import HarnessToolCall, HarnessToolResult, ToolContext, ToolSpec

ErrorCategory = Literal[
    "PATH_OUT_OF_SCOPE", "SYMLINK_ESCAPE", "INVALID_ARGUMENT", "MISSING_DEPENDENCY",
    "CAPABILITY_NOT_GRANTED", "RESOURCE_LIMIT", "EPOCH_STALE", "NON_RECOVERABLE_POLICY_DENIAL",
]


class ToolErrorPolicy(Model):
    schema_version: Literal["harness-tool-errors-v1"] = "harness-tool-errors-v1"
    # Additional attempts after the first known denial, across all tools and restarts.
    max_corrections: int = Field(default=3, ge=0, le=8)


class ToolErrorReceipt(Model):
    schema_version: Literal["harness-tool-error-v1"] = "harness-tool-error-v1"
    operation_id: str
    call_id: str
    tool: str
    code: str
    category: ErrorCategory
    task_id: str
    run_id: str
    workspace_id: str
    execution_epoch: int
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    acceptance_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    diagnostic_hash: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )
    allowed_read_roots: tuple[str, ...] = ()
    allowed_write_roots: tuple[str, ...] = ()
    allowed_command_ids: tuple[str, ...] = ()
    argument_constraints: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False
    remaining_corrections: int = Field(ge=0, le=8)
    requires_approval: bool = False
    requires_reconciliation: bool = False
    # None means effects cannot be ruled out. It must never become an automatic retry.
    executed: bool | None = None
    next_action: str


class ToolCorrectionReceipt(Model):
    operation_id: str
    denial_operation_id: str
    task_id: str
    run_id: str
    execution_epoch: int
    request_hash: str
    tree_hash: str


def ambiguous_path(path: str) -> bool:
    """Reject encoded traversal rather than interpreting it as a host path hint."""
    decoded = path
    for _ in range(3):
        value = unquote(decoded)
        if value == decoded:
            break
        decoded = value
    return ("\x00" in decoded or "\\" in decoded or decoded.startswith("/")
            or bool(re.match(r"^[A-Za-z]:", decoded)) or ".." in decoded.split("/"))


def effective_roots(granted: tuple[str, ...], leased: tuple[str, ...],
                    protected: tuple[str, ...]) -> tuple[str, ...]:
    def within(path: str, root: str) -> bool:
        return not root or path == root or path.startswith(root + "/")

    roots = set()
    for left in granted:
        for right in leased:
            root = left if within(left, right) else right if within(right, left) else None
            if root is not None and not ambiguous_path(root) and not any(
                within(root, entry) for entry in protected
            ):
                roots.add(root)
    return tuple(sorted(roots))[:64]


def argument_constraints(spec: ToolSpec | None) -> dict[str, Any]:
    """Only registry-owned types/bounds, never errors, defaults or free-text descriptions."""
    if spec is None:
        return {}
    schema = spec.input_schema
    fields = {}
    for key, value in list(schema.get("properties", {}).items())[:32]:
        if isinstance(value, dict):
            fields[key] = {k: value[k] for k in (
                "type", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"
            ) if k in value}
    return {"required": list(schema.get("required", []))[:32], "fields": fields,
            "additional_properties": schema.get("additionalProperties", True)}


_FATAL = {
    "HARNESS_PROTECTED_PATH_DENIED", "CONTEXT_ARTIFACT_SCOPE_DENIED", "CONTEXT_AUTHORITY_CHANGED",
    "HARNESS_ACTOR_MISMATCH", "HARNESS_WORKSPACE_MISMATCH", "HARNESS_REPOSITORY_MISMATCH",
    "HARNESS_AUTHORITY_REQUIRED", "HARNESS_LEASE_DENIED", "HARNESS_LEASE_REVOKED",
    "HARNESS_LEASE_EXPIRED", "HARNESS_DEPENDENCY_SOURCE_DENIED", "HARNESS_AUTHORITY_DENIED",
}
_ARGUMENT = {
    "HARNESS_TOOL_ARGUMENT", "CONTEXT_CURSOR_INVALID", "WORKSPACE_SEARCH_LIMIT",
    "WORKSPACE_FILE_NOT_FOUND", "WORKSPACE_PATH_NOT_FOUND", "HARNESS_SYMBOL_AMBIGUOUS",
    "HARNESS_SYMBOL_NOT_FOUND", "PATCH_STALE", "PATCH_INVALID", "PATCH_PREIMAGE_MISMATCH",
    "WORKSPACE_EDIT_STALE",
}
_NO_EFFECT = _ARGUMENT | _FATAL | {
    "HARNESS_PATH_DENIED", "HARNESS_READ_DENIED", "HARNESS_WRITE_DENIED",
    "HARNESS_COMMAND_DENIED", "HARNESS_TOOL_UNDECLARED", "HARNESS_NETWORK_DENIED",
    "HARNESS_EXECUTE_DENIED", "HARNESS_EXTERNAL_WRITE_DENIED", "WORKSPACE_SYMLINK_DENIED",
    "HARNESS_EPOCH_MISMATCH", "STALE_EPOCH", "FENCED", "RUN_NOT_ACTIVE",
}


def error_receipt(
    call: HarnessToolCall, result: HarnessToolResult, context: ToolContext, *,
    policy: ToolErrorPolicy, prior_errors: int, acceptance_hash: str,
    spec: ToolSpec | None, scope: dict[str, Any], allow_write: bool, allow_execute: bool,
) -> ToolErrorReceipt:
    code = result.code
    reconcile = code in {
        "RECOVERY_BLOCKED", "RECOVERY_OPERATION_CONFLICT", "WORKSPACE_STALE",
        "HARNESS_EXPERT_MODEL_UNKNOWN",
    }
    if code in {"HARNESS_EPOCH_MISMATCH", "STALE_EPOCH", "FENCED", "RUN_NOT_ACTIVE"}:
        category: ErrorCategory = "EPOCH_STALE"
    elif code == "WORKSPACE_SYMLINK_DENIED" or (
        code == "HARNESS_PATH_DENIED" and "symlink" in result.content.lower()
    ):
        category = "SYMLINK_ESCAPE"
    elif code in _FATAL or reconcile:
        category = "NON_RECOVERABLE_POLICY_DENIAL"
    elif code in {"MISSING_DEPENDENCY", "DEPENDENCY_MISSING", "SANDBOX_IMAGE_REQUIRED"}:
        category = "MISSING_DEPENDENCY"
    elif code in {"WORKSPACE_LIMIT", "HARNESS_CALL_LIMIT", "BUDGET_EXCEEDED",
                  "HARNESS_BUDGET_EXHAUSTED",
                  "HARNESS_RESOURCE_LIMIT", "COMMAND_RESOURCE_LIMIT"}:
        category = "RESOURCE_LIMIT"
    elif (code == "HARNESS_TOOL_UNDECLARED"
          or (code == "HARNESS_WRITE_DENIED" and not allow_write)
          or (code == "HARNESS_COMMAND_DENIED" and not allow_execute)
          or code in {"HARNESS_EXECUTE_DENIED", "HARNESS_NETWORK_DENIED",
                      "HARNESS_EXTERNAL_WRITE_DENIED"}):
        category = "CAPABILITY_NOT_GRANTED"
    elif code == "HARNESS_COMMAND_DENIED":
        category = (
            "INVALID_ARGUMENT" if result.data.get("correctable") else "CAPABILITY_NOT_GRANTED"
        )
    elif code in {"HARNESS_PATH_DENIED", "HARNESS_READ_DENIED", "HARNESS_WRITE_DENIED"}:
        category = "PATH_OUT_OF_SCOPE"
    elif code in _ARGUMENT:
        category = "INVALID_ARGUMENT"
    else:
        # Unknown failures cannot prove that an edit/process did not occur.
        category = "NON_RECOVERABLE_POLICY_DENIAL"
        reconcile = True
    remaining = max(0, policy.max_corrections - prior_errors)
    correctable = category in {"PATH_OUT_OF_SCOPE", "INVALID_ARGUMENT"}
    approved = category == "CAPABILITY_NOT_GRANTED"
    next_action = (
        "Use only the listed relative roots and declared argument types; issue a new tool call."
        if correctable and remaining else
        "Correction limit reached; keep the denial and ask the operator for revised input."
        if correctable else
        "Ask the operator through the existing inbox; remain blocked until formal authorization. "
        "A text reply cannot expand this task or its acceptance scope."
        if approved else
        "Reconcile the original operation before any further execution."
        if reconcile else
        "Operator environment preparation is required; do not install, mount or enable network."
        if category in {"MISSING_DEPENDENCY", "RESOURCE_LIMIT"} else
        "Stop this operation; obtain a current authorized task through the original controller."
    )
    safe_code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "TOOL_FAILURE"
    return ToolErrorReceipt(
        operation_id=context.operation_id, call_id=call.call_id, tool=call.name,
        code=safe_code, category=category, task_id=context.task.task_id,
        run_id=str(context.task.run_id), workspace_id=context.task.workspace_id,
        execution_epoch=context.task.execution_epoch, tree_hash=context.workspace_tree,
        acceptance_hash=acceptance_hash, request_hash=digest(call.model_dump(mode="json")),
        policy_hash=digest(policy.model_dump(mode="json")),
        allowed_read_roots=scope.get("read_roots", ()),
        allowed_write_roots=scope.get("write_roots", ()) if allow_write else (),
        allowed_command_ids=scope.get("command_ids", ()) if allow_execute else (),
        argument_constraints=argument_constraints(spec), recoverable=correctable and remaining > 0,
        remaining_corrections=remaining, requires_approval=approved,
        requires_reconciliation=reconcile, executed=False if code in _NO_EFFECT else None,
        next_action=next_action,
    )


def safe_error_result(result: HarnessToolResult, receipt: ToolErrorReceipt) -> HarnessToolResult:
    payload = receipt.model_dump(mode="json")
    feedback: dict[str, Any] = {"tool_error": payload}
    if receipt.recoverable and result.data.get("correctable"):
        # These fields come only from registry command/page/symbol recovery.
        for key in ("retry_arguments", "next_arguments"):
            args = result.data.get(key)
            if isinstance(args, dict) and (
                (set(args) == {"command_id"}
                 and args["command_id"] in receipt.allowed_command_ids)
                or (set(args) <= {"path", "artifact_hash", "cursor"}
                    and isinstance(args.get("path"), str)
                    and not ambiguous_path(args["path"])
                    and any(not root or args["path"] == root
                            or args["path"].startswith(root + "/")
                            for root in receipt.allowed_read_roots))
                or (receipt.tool == "read_symbol" and receipt.code == "HARNESS_SYMBOL_NOT_FOUND"
                    and set(args) == {"path", "symbol"}
                    and isinstance(args.get("symbol"), str)
                    and 0 < len(args["symbol"]) <= 300
                    and all(part.isidentifier() for part in args["symbol"].split("."))
                    and isinstance(args.get("path"), str)
                    and not ambiguous_path(args["path"])
                    and any(not root or args["path"] == root
                            or args["path"].startswith(root + "/")
                            for root in receipt.allowed_read_roots))
            ):
                feedback[key] = args
    return result.model_copy(update={
        "content": json.dumps(feedback, ensure_ascii=False),
        "data": {**result.data, "tool_error": payload},
    })
