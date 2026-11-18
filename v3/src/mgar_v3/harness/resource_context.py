"""Read-only model guidance from frozen limits and current ledger observations."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from mgar_v3.harness.contracts import BudgetLimits, BudgetSnapshot


def project_resources(
    ledger: Any,
    task_id: str,
    used: BudgetSnapshot,
    *,
    elapsed_seconds: float,
    max_seconds: int,
    input_context_limit: int | None,
    context_window_limit: int | None,
    max_output_tokens: int,
) -> dict[str, Any]:
    """An advisory projection never reserves, releases, retries or grants money.

    Native Harness budgets cap calls/cost/time and per-request context. They
    contain no cumulative token ceiling: do not invent one from a window size.
    Unknown usage remains charged through the ledger's reserved amounts.
    """
    reader = getattr(ledger, "resource_snapshot", None)
    observed = reader(task_id) if callable(reader) else {}
    raw_limits = observed.get("limits") or getattr(ledger, "limits", None)
    limits = (
        raw_limits
        if isinstance(raw_limits, BudgetLimits)
        else BudgetLimits.model_validate(raw_limits)
        if raw_limits is not None
        else None
    )
    consumed = observed.get("used") or used.model_dump(mode="json")
    limit = {
        "model_calls": limits.max_model_calls if limits else None,
        "tool_calls": limits.max_tool_calls if limits else None,
        "cost_micros": limits.max_cost_micros if limits else None,
        "seconds": min(max_seconds, limits.max_seconds) if limits else max_seconds,
    }
    usage = {key: int(consumed[key]) for key in ("model_calls", "tool_calls", "cost_micros")}
    remaining: dict[str, int | None] = {
        key: max(0, int(value) - usage[key]) if value is not None else None
        for key, value in limit.items()
        if key != "seconds"
    }
    remaining["seconds"] = max(0, int(limit["seconds"] or 0) - math.ceil(elapsed_seconds))
    deadline = observed.get("deadline_at")
    if deadline:
        deadline_remaining = (datetime.fromisoformat(deadline) - datetime.now(UTC)).total_seconds()
        remaining["seconds"] = min(
            remaining["seconds"] or 0,
            max(0, math.floor(deadline_remaining)),
        )
    shared = observed.get("shared")
    if shared:
        shared = dict(shared)
        shared["remaining_micros"] = max(
            0,
            shared["limit_micros"] - shared["settled_micros"] - shared["reserved_micros"],
        )
        shared["remaining_calls"] = (
            max(0, shared["call_limit"] - shared["used_calls"])
            if shared["call_limit"] is not None
            else None
        )
        remaining["cost_micros"] = min(remaining["cost_micros"] or 0, shared["remaining_micros"])
        if shared["remaining_calls"] is not None:
            remaining["model_calls"] = min(remaining["model_calls"] or 0, shared["remaining_calls"])
    unknown = shared.get("unknown_calls", 0) if shared else observed.get("unknown_calls")
    stage = "investigate"
    guidance = ["These are observations of existing authority, not a new spending grant."]
    if unknown:
        stage = "blocked"
        remaining["model_calls"] = 0
        guidance.append(
            "An earlier model outcome is unknown. Preserve its reservation; do not "
            "resend it or start another paid model call before reconciliation."
        )
    elif remaining["model_calls"] is not None and remaining["model_calls"] <= 1:
        stage = "finalize"
        guidance.append(
            "This is the last available model turn. If all required verification "
            "already passes on the current tree, return the concise final result "
            "without further tools so the verifier can evaluate it. Otherwise "
            "prioritize essential correction/testing and report unresolved work "
            "truthfully; never claim unverified success."
        )
    elif (
        (
            remaining["model_calls"] is not None
            and limit["model_calls"] is not None
            and remaining["model_calls"] <= max(3, math.ceil(limit["model_calls"] * 0.5))
        )
        or (remaining["seconds"] is not None and remaining["seconds"] <= 60)
        or (remaining["tool_calls"] is not None and remaining["tool_calls"] <= 3)
        or (
            remaining["cost_micros"] is not None
            and limit["cost_micros"] is not None
            and remaining["cost_micros"] <= math.ceil(limit["cost_micros"] * 0.25)
        )
    ):
        stage = "prioritize_verification"
        guidance.append(
            "Resources are running low. Focus on the current failure and a concrete "
            "hypothesis; batch relevant reads, make the smallest justified edit, "
            "run required verification, and reserve a final model turn to report "
            "the verified outcome. Avoid broad exploratory paging."
        )
    elif (
        remaining["model_calls"] is not None
        and limit["model_calls"] is not None
        and remaining["model_calls"] <= math.ceil(limit["model_calls"] * 0.7)
    ):
        stage = "test_hypothesis"
        guidance.append(
            "A substantial part of the model-call allowance has been used. Identify "
            "the specific evidence still needed for a causal explanation and batch "
            "those reads. Test a justified minimal correction while there is time "
            "to revise it; do not make a speculative edit just to satisfy a timer. "
            "Leave turns for required verification and a final result."
        )
    return {
        "schema_version": "harness-resource-budget-v1",
        "source": "engine_budget_ledger" if observed else "runtime_budget_ledger",
        "limits": limit,
        "used_or_reserved": usage,
        "remaining": remaining,
        "reserved_micros": observed.get("reserved_micros"),
        "unknown_calls": unknown,
        "shared": shared,
        "deadline_at": deadline,
        "tokens": {
            "cumulative_limit": None,
            "cumulative_remaining": None,
            "input_context_limit": input_context_limit,
            "context_window_limit": context_window_limit,
            "output_reservation": max_output_tokens,
            "observed_input": (observed.get("tokens") or {}).get("input_tokens"),
            "observed_output": (observed.get("tokens") or {}).get("output_tokens"),
            "accounting": "per-request context; token receipts do not reset or grant money",
        },
        "stage": stage,
        "guidance": guidance,
    }
