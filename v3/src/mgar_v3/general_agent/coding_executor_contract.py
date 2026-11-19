"""Immutable hand-off from a GA02 claim to the original Harness service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from pydantic import model_validator

from mgar_v3.domain import DomainError, Model
from mgar_v3.general_agent.capabilities import CapabilityAccess
from mgar_v3.general_agent.contracts import Sha256, TaskSnapshot
from mgar_v3.general_agent.graph_store import NodeClaim
from mgar_v3.harness.coding_loop import ServicePlan
from mgar_v3.harness.contracts import BudgetLimits

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


INSPECT_CAPABILITY = "coding.execute.inspect"
CODING_CAPABILITY = "coding.execute"
CODING_CAPABILITIES = {INSPECT_CAPABILITY, CODING_CAPABILITY}


class CodingDispatch(Model):
    claim: NodeClaim
    access: CapabilityAccess
    request_sha256: Sha256
    limits: BudgetLimits
    service_plan: ServicePlan | None = None

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        if self.claim.capability_id not in CODING_CAPABILITIES:
            raise ValueError("A Coding executor claim is required")
        if (self.access.task_id, self.access.task_revision) != (
            self.claim.task_id,
            self.claim.task_revision,
        ):
            raise ValueError("Coding grant differs from the TaskGraph claim")
        return self

    @property
    def harness_task_id(self) -> str:
        # The full claim in the immutable Harness request fences a prefix collision.
        return f"h08-{self.claim.operation_id.hex[:12]}"

    def require_current(self, engine: Engine) -> TaskSnapshot:
        if not engine.general_graphs.model_tools(self.claim, self.access, context_tokens=8192):
            raise DomainError("GENERAL_CODING_SCOPE", "Coding authority is no longer current.", 403)
        task = engine.general_tasks.get(self.claim.task_id)
        if (
            task.revision != self.claim.task_revision
            or task.project_id != self.access.project_id
            or task.spec.coding_policy is None
            or (task.spec.coding_policy == "inspect")
            != (self.claim.capability_id == INSPECT_CAPABILITY)
        ):
            raise DomainError("GENERAL_CODING_SCOPE", "Coding policy differs from its claim.", 403)
        return task

    def require_resources(self, engine: Engine) -> None:
        binding = engine.general_graphs.registry.definition(self.claim.capability_id)
        seconds = int((self.claim.expires_at - datetime.now(UTC)).total_seconds())
        if (
            binding is None
            or self.limits.max_model_calls > binding.upper_bound.model_calls
            or self.limits.max_tool_calls > binding.upper_bound.tool_calls
            or self.limits.max_cost_micros != 0
            or self.limits.max_seconds > binding.timeout_seconds
            or self.limits.max_seconds > seconds + 1
            or seconds < 1
            or (self.service_plan and self.service_plan.timeout_seconds > self.limits.max_seconds)
        ):
            raise DomainError("GENERAL_CODING_BUDGET", "Coding execution exceeds its claim.", 409)
