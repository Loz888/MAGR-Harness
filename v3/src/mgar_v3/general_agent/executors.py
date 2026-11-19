"""Dispatch bounded read-only executor handlers under the current graph claim."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.general_agent.capabilities import CapabilityAccess
from mgar_v3.general_agent.contracts import TaskSnapshot
from mgar_v3.general_agent.graph import BranchValue, GraphNode
from mgar_v3.general_agent.graph_store import GraphSnapshot, NodeClaim, NodeReceipt

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

ExecutorHandler = Callable[[TaskSnapshot, GraphNode, NodeClaim], dict[str, BranchValue]]


class GeneralExecutorService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def run(
        self,
        task_id: UUID,
        revision: int,
        graph_revision: int,
        node_id: str,
        access: CapabilityAccess,
        *,
        execute: ExecutorHandler,
        context_tokens: int = 8192,
        accepted_capabilities: tuple[str, ...] | None = None,
    ) -> GraphSnapshot | None:
        """Trusted application boundary. Handler selection and grants are server-owned."""
        graphs = self.engine.general_graphs
        with self.engine.transaction():
            snapshot = graphs.get(task_id)
            if (
                snapshot.graph.task_revision != revision
                or snapshot.graph_revision != graph_revision
            ):
                raise DomainError("GENERAL_GRAPH_STALE", "Graph revision changed.", 409)
            node = next((item for item in snapshot.graph.nodes if item.id == node_id), None)
            if node is None:
                raise DomainError("GENERAL_GRAPH_NODE_UNKNOWN", "Graph node was not found.", 404)
            candidates = (
                graphs.registry.definition(key)
                for key in node.capabilities
                if accepted_capabilities is None or key in accepted_capabilities
            )
            pins = {
                item.identity()
                for item in candidates
                if item is not None and item.effect == "read_only"
            }
            safe = access.model_copy(
                update={
                    "capability_pins": tuple(pin for pin in access.capability_pins if pin in pins)
                }
            )
            claim = graphs.claim(
                task_id,
                revision,
                graph_revision,
                node_id,
                safe,
                context_tokens=context_tokens,
            )
            if claim is None:
                return None
            task = self.engine.general_tasks.get(task_id)

        try:
            if not graphs.model_tools(claim, safe, context_tokens=context_tokens):
                raise DomainError("GENERAL_EXECUTOR_GRANT", "Executor access changed.", 403)
            outputs = execute(task, node, claim)
            if set(outputs) != set(node.outputs):
                raise DomainError("GENERAL_EXECUTOR_OUTPUT", "Executor returned invalid outputs.")
        except DomainError:
            self._settle(claim, "failed", {})
            raise
        return self._settle(claim, "succeeded", outputs)

    def _settle(
        self,
        claim: NodeClaim,
        status: Literal["succeeded", "failed"],
        outputs: dict[str, BranchValue],
    ) -> GraphSnapshot:
        receipt = NodeReceipt(
            operation_id=claim.operation_id,
            task_id=claim.task_id,
            task_revision=claim.task_revision,
            node_id=claim.node_id,
            node_sha256=claim.node_sha256,
            descriptor_sha256=claim.descriptor_sha256,
            status=status,
            outputs=outputs,
        )
        receipt_sha256 = self.engine.artifacts.put(receipt.model_dump_json().encode())
        return self.engine.general_graphs.settle(receipt_sha256)
