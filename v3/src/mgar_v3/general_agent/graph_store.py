"""Persistent graph orchestration. Execution and approvals enter via trusted ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, Field, ValidationError

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.general_agent.capabilities import (
    CapabilityAccess,
    CapabilityDescriptor,
    CapabilityRegistry,
    Resources,
    RouteDecision,
)
from mgar_v3.general_agent.contracts import (
    Identifier,
    Revision,
    Sha256,
    TaskPlan,
    TaskSnapshot,
    Text,
)
from mgar_v3.general_agent.graph import (
    BranchValue,
    GraphNode,
    NodeState,
    ReplanTrigger,
    TaskGraph,
    model_plan,
)
from mgar_v3.harness.tenancy import require_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.harness.service import CodingTaskRequest

_TERMINAL = {"succeeded", "failed", "skipped", "blocked"}
_ACTIVE = {"running", "unknown"}


class NodeClaim(Model):
    operation_id: UUID
    task_id: UUID
    task_revision: Revision
    node_id: Identifier
    node_sha256: Sha256
    descriptor_sha256: Sha256
    capability_id: Identifier
    inputs: dict[Identifier, BranchValue]
    idempotent: bool
    expires_at: AwareDatetime


class NodeReceipt(Model):
    schema_version: Literal["general-node-receipt-v1"] = "general-node-receipt-v1"
    operation_id: UUID
    task_id: UUID
    task_revision: Revision
    node_id: Identifier
    node_sha256: Sha256
    descriptor_sha256: Sha256
    status: Literal["succeeded", "failed", "unknown"]
    outputs: Annotated[dict[Identifier, BranchValue], Field(max_length=16)] = Field(
        default_factory=dict
    )


class NodeProgress(Model):
    state: NodeState = "pending"
    attempts: int = Field(default=0, ge=0)
    claim: NodeClaim | None = None
    receipt_sha256: Sha256 | None = None
    signal_reference: Text | None = None
    outputs: dict[Identifier, BranchValue] = Field(default_factory=dict)
    reason: Identifier | None = None


class GraphSnapshot(Model):
    task_id: UUID
    graph_revision: Revision
    version: Revision
    plan_version: Revision
    graph: TaskGraph
    created_at: AwareDatetime
    progress: dict[Identifier, NodeProgress]
    spent: Resources = Field(default_factory=Resources)


def _update[T: Model](value: T, **changes: Any) -> T:
    return value.model_copy(update=changes)


def _expired_state(state: GraphSnapshot, now: datetime) -> dict[str, NodeProgress]:
    return {
        key: _update(item, state="unknown", reason="receipt_reconciliation_required")
        if item.state == "running" and item.claim is not None and item.claim.expires_at <= now
        else item
        for key, item in state.progress.items()
    }


def _dependency_state(node: GraphNode, progress: dict[str, NodeProgress]) -> NodeState:
    for dep in node.dependencies:
        parent = progress[dep.node_id]
        if parent.state in {"failed", "blocked"}:
            return "blocked"
    for dep in node.dependencies:
        if progress[dep.node_id].state == "skipped" and not dep.accept_skipped:
            return "skipped"
    if any(
        progress[dep.node_id].state not in {"succeeded", "skipped"} for dep in node.dependencies
    ):
        return "waiting"
    for dep in node.dependencies:
        if dep.output_id is not None and digest(progress[dep.node_id].outputs[dep.output_id]) != (
            digest(dep.equals)
        ):
            return "skipped"
    return "pending"


def _propagate(graph: TaskGraph, progress: dict[str, NodeProgress]) -> None:
    # Pending nodes only. A skipped/failed prerequisite can affect a whole
    # descendant chain independent of node declaration order.
    for _ in graph.nodes:
        changed = False
        for node in graph.nodes:
            item = progress[node.id]
            if item.state not in {"pending", "waiting"}:
                continue
            state = _dependency_state(node, progress)
            if state in {"blocked", "skipped"}:
                progress[node.id] = _update(item, state=state, reason="prerequisite_" + state)
                changed = True
        if not changed:
            return


def _resource_reservation(
    state: GraphSnapshot,
    node: GraphNode,
    task: TaskSnapshot,
    access: CapabilityAccess,
    cost: Resources,
    action_spent: Resources,
) -> Resources | None:
    limits = task.spec.budget
    task_caps = {
        "cost_micros": limits.max_cost_micros,
        "model_calls": limits.max_model_calls,
        "tool_calls": limits.max_tool_calls,
        "tokens": limits.max_total_tokens,
    }
    total = {}
    for field, amount in cost.model_dump().items():
        cap = task_caps[field]
        used = getattr(state.spent, field) + amount
        overall = used + getattr(action_spent, field)
        if (
            amount > getattr(node.budget, field)
            or overall > getattr(access.resources, field)
            or (cap is not None and overall > cap)
        ):
            return None
        total[field] = used
    return Resources.model_validate(total)


def _deadline(task: TaskSnapshot, state: GraphSnapshot) -> datetime | None:
    deadline = task.spec.deadline
    seconds = task.spec.budget.max_seconds
    remaining = (datetime.max.replace(tzinfo=UTC) - state.created_at).total_seconds()
    if seconds is not None and seconds < remaining:
        budget_deadline = state.created_at + timedelta(seconds=seconds)
        return min(deadline, budget_deadline) if deadline is not None else budget_deadline
    return deadline


class GeneralGraphStore:
    def __init__(self, engine: Engine, registry: CapabilityRegistry) -> None:
        self.engine, self.registry = engine, registry

    @staticmethod
    def _read(
        conn: Connection, task_id: UUID, revision: int, *, lock: bool = False
    ) -> GraphSnapshot:
        row = conn.execute(
            "SELECT snapshot FROM app.general_task_graphs WHERE task_id=%s AND task_revision=%s"
            + (" FOR UPDATE" if lock else ""),
            (task_id, revision),
        ).fetchone()
        if row is None:
            raise DomainError("GENERAL_GRAPH_NOT_FOUND", "TaskGraph was not found.", 404)
        return GraphSnapshot.model_validate(row["snapshot"])

    def get(self, task_id: UUID) -> GraphSnapshot:
        with self.engine.transaction() as conn:
            task = self.engine.general_tasks.get(task_id)
            state = self._read(conn, task_id, task.revision)
            self._check_plan(task, state)
            return state

    @staticmethod
    def _check_plan(task: TaskSnapshot, state: GraphSnapshot) -> None:
        if task.plan_version != state.plan_version:
            raise DomainError("GENERAL_GRAPH_PLAN_STALE", "The task plan changed.", 409)

    def _current(
        self,
        conn: Connection,
        task_id: UUID,
        revision: int,
    ) -> tuple[TaskSnapshot, GraphSnapshot]:
        task = self.engine.general_tasks.lock_current(conn, task_id)
        if task.revision != revision:
            raise DomainError("GENERAL_GRAPH_TASK_STALE", "Current task revision changed.", 409)
        state = self._read(conn, task_id, revision, lock=True)
        self._check_plan(task, state)
        return task, state

    @staticmethod
    def _event(conn: Connection, state: GraphSnapshot, kind: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO app.general_graph_events("
            "task_id,task_revision,graph_revision,version,kind,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                state.task_id,
                state.graph.task_revision,
                state.graph_revision,
                state.version,
                kind,
                Jsonb(payload),
            ),
        )

    def _save(
        self,
        conn: Connection,
        task: TaskSnapshot,
        state: GraphSnapshot,
        *,
        kind: str,
        payload: dict[str, Any],
        **changes: Any,
    ) -> GraphSnapshot:
        updated: GraphSnapshot = _update(state, version=state.version + 1, **changes)
        cursor = conn.execute(
            "UPDATE app.general_task_graphs SET graph_revision=%s,version=%s,snapshot=%s "
            "WHERE task_id=%s AND task_revision=%s AND version=%s",
            (
                updated.graph_revision,
                updated.version,
                Jsonb(updated.model_dump(mode="json")),
                task.task_id,
                task.revision,
                state.version,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError("GENERAL_GRAPH_STALE", "Graph version changed.", 409)
        self._event(conn, updated, kind, payload)
        return updated

    def install(
        self,
        task_id: UUID,
        revision: int,
        task_version: int,
        graph: TaskGraph,
    ) -> GraphSnapshot:
        with self.engine.transaction() as conn:
            task = self.engine.general_tasks.lock_current(conn, task_id)
            if task.revision != revision or task.version != task_version:
                raise DomainError("GENERAL_TASK_STALE", "Task version changed.", 409)
            graph.check_task(task.spec, revision)
            present = conn.execute(
                "SELECT 1 FROM app.general_task_graphs WHERE task_id=%s AND task_revision=%s",
                (task_id, revision),
            ).fetchone()
            if present:
                raise DomainError("GENERAL_GRAPH_EXISTS", "Use graph CAS to replace the plan.", 409)
            planned = self.engine.general_tasks.save_plan(
                task_id,
                revision,
                task_version,
                TaskPlan(task_revision=revision, steps=tuple(node.goal for node in graph.nodes)),
            )
            state = GraphSnapshot(
                task_id=task_id,
                graph_revision=1,
                version=1,
                plan_version=planned.version,
                graph=graph,
                created_at=datetime.now(UTC),
                progress={node.id: NodeProgress() for node in graph.nodes},
            )
            conn.execute(
                "INSERT INTO app.general_task_graphs("
                "task_id,task_revision,graph_revision,version,snapshot) VALUES (%s,%s,1,1,%s)",
                (task_id, revision, Jsonb(state.model_dump(mode="json"))),
            )
            self._event(conn, state, "graph_installed", {"graph": graph.model_dump(mode="json")})
            return state

    def plan_with_model(
        self,
        task_id: UUID,
        revision: int,
        task_version: int,
        propose: Callable[[dict[str, Any]], str],
    ) -> GraphSnapshot:
        """Accept an already authorized and metered planner port."""
        task = self.engine.general_tasks.get(task_id)
        if (task.revision, task.version) != (revision, task_version):
            raise DomainError("GENERAL_TASK_STALE", "Task version changed.", 409)
        if task.state != "ready":
            raise DomainError("GENERAL_GRAPH_STATE", "Task is not ready for planning.", 409)
        deadline = task.spec.deadline
        if deadline is not None and deadline <= datetime.now(UTC):
            raise DomainError("GENERAL_GRAPH_DEADLINE", "Task time budget expired.", 409)
        with self.engine.transaction() as conn:
            present = conn.execute(
                "SELECT 1 FROM app.general_task_graphs WHERE task_id=%s AND task_revision=%s",
                (task_id, revision),
            ).fetchone()
        if present:
            raise DomainError("GENERAL_GRAPH_EXISTS", "Use graph CAS to replace the plan.", 409)
        return self.install(
            task_id, revision, task_version, model_plan(task.spec, revision, propose)
        )

    def replan(
        self,
        task_id: UUID,
        revision: int,
        graph_revision: int,
        version: int,
        graph: TaskGraph,
        trigger: ReplanTrigger,
    ) -> GraphSnapshot:
        with self.engine.transaction() as conn:
            task, state = self._current(conn, task_id, revision)
            if (state.graph_revision, state.version) != (graph_revision, version):
                raise DomainError("GENERAL_GRAPH_STALE", "Graph version changed.", 409)
            if task.state not in {"ready", "running", "blocked"}:
                raise DomainError(
                    "GENERAL_GRAPH_STATE", "Task phase cannot accept a new plan.", 409
                )
            graph.check_task(task.spec, revision)
            before = {node.id: node for node in state.graph.nodes}
            after = {node.id: node for node in graph.nodes}
            dirty = {
                key
                for key in before.keys() | after.keys()
                if key not in before
                or key not in after
                or before[key].model_dump_json() != after[key].model_dump_json()
            }
            for _ in graph.nodes:
                dirty.update(
                    node.id
                    for node in graph.nodes
                    if any(dep.node_id in dirty for dep in node.dependencies)
                )
            for key in dirty & state.progress.keys():
                item = state.progress[key]
                if item.state in _ACTIVE or (item.claim is not None and not item.claim.idempotent):
                    raise DomainError(
                        "GENERAL_GRAPH_FENCED_EFFECT",
                        "A claimed effect or unreconciled operation cannot be replaced.",
                        409,
                    )
            progress = {
                key: state.progress[key] if key not in dirty else NodeProgress() for key in after
            }
            _propagate(graph, progress)
            if task.state == "blocked":
                task = self.engine.general_tasks.transition(
                    task_id, revision, task.version, "ready"
                )
            planned = self.engine.general_tasks.save_plan(
                task_id,
                revision,
                task.version,
                TaskPlan(task_revision=revision, steps=tuple(node.goal for node in graph.nodes)),
                replace_running=True,
            )
            updated = self._save(
                conn,
                task,
                state,
                kind="graph_replanned",
                payload={
                    "trigger": trigger.model_dump(mode="json"),
                    "changed": sorted(dirty),
                    "graph": graph.model_dump(mode="json"),
                },
                graph=graph,
                progress=progress,
                graph_revision=state.graph_revision + 1,
                plan_version=planned.version,
            )
            self._advance_task(planned, updated)
            return updated

    def claim(
        self,
        task_id: UUID,
        revision: int,
        graph_revision: int,
        node_id: str,
        access: CapabilityAccess,
        *,
        context_tokens: int,
    ) -> NodeClaim | None:
        with self.engine.transaction() as conn:
            task, state = self._current(conn, task_id, revision)
            if state.graph_revision != graph_revision:
                raise DomainError("GENERAL_GRAPH_STALE", "Graph revision changed.", 409)
            if (task_id, task.project_id, revision) != (
                access.task_id,
                access.project_id,
                access.task_revision,
            ):
                raise DomainError("GENERAL_GRAPH_ACCESS", "Grant is outside task scope.", 403)
            if task.membership_id is not None:
                require_membership(conn, task.project_id, str(task.membership_id), write=True)
            if task.state not in {"ready", "running"}:
                raise DomainError("GENERAL_GRAPH_STATE", "Task cannot claim new work.", 409)
            node = self._node(state, node_id)
            if node.kind != "execute":
                raise DomainError("GENERAL_GRAPH_SIGNAL_REQUIRED", "This node waits for a signal.")
            now = datetime.now(UTC)
            progress = _expired_state(state, now)
            _propagate(state.graph, progress)
            active = {key for key, item in progress.items() if item.state in _ACTIVE}
            current = progress[node_id]
            group_full = (
                node.parallel_group is not None
                and sum(
                    item.id in active and item.parallel_group == node.parallel_group
                    for item in state.graph.nodes
                )
                >= state.graph.group_limits[node.parallel_group]
            )
            if (
                current.state not in {"pending", "waiting"}
                or _dependency_state(node, progress) != "pending"
                or len(active) >= state.graph.max_parallel
                or group_full
            ):
                if progress != state.progress:
                    self._save(
                        conn,
                        task,
                        state,
                        kind="graph_reconciled",
                        payload={"node_id": node_id},
                        progress=progress,
                    )
                return None
            deadline = _deadline(task, state)
            if deadline is not None and deadline <= now:
                raise DomainError("GENERAL_GRAPH_DEADLINE", "Task time budget expired.", 409)

            inputs = {
                item.name: progress[item.node_id].outputs.get(item.output_id)
                for item in node.inputs
            }
            action_spent = self.engine.general_actions.spent(conn, task_id, revision)

            def eligible(descriptor: CapabilityDescriptor) -> str | None:
                if descriptor.evidence_contract != "general-node-receipt-v1":
                    return "unsupported_evidence_contract"
                if not Draft202012Validator(descriptor.input_schema).is_valid(inputs):
                    return "input_schema"
                if (
                    _resource_reservation(
                        state, node, task, access, descriptor.upper_bound, action_spent
                    )
                    is None
                ):
                    return "resource_budget"
                if current.attempts and not descriptor.idempotent:
                    return "non_idempotent_retry_denied"
                return None

            decision = self.registry.route(
                task.spec,
                node.domain,
                node.capabilities,
                access,
                context_tokens=context_tokens,
                eligible=eligible,
            )
            route = {"node_id": node_id, "decision": decision.model_dump(mode="json")}
            descriptor = (
                self.registry.definition(decision.chosen_id) if decision.chosen_id else None
            )
            if descriptor is None:
                reason = "no_capability"
                progress[node_id] = _update(current, state="waiting", reason=reason)
                self._save(
                    conn,
                    task,
                    state,
                    kind="graph_route_blocked",
                    payload={**route, "reason": reason},
                    progress=progress,
                )
                return None
            reserved = _resource_reservation(
                state, node, task, access, descriptor.upper_bound, action_spent
            )
            assert reserved is not None  # The route admitted this exact immutable descriptor.
            idempotent = descriptor.idempotent and (
                current.claim is None or current.claim.idempotent
            )
            if current.attempts >= node.max_attempts or (current.attempts and not idempotent):
                raise DomainError(
                    "GENERAL_GRAPH_FENCED_EFFECT", "This operation cannot repeat.", 409
                )
            expires = now + timedelta(seconds=min(node.timeout_seconds, descriptor.timeout_seconds))
            if deadline is not None:
                expires = min(expires, deadline)
            claim = NodeClaim(
                operation_id=uuid4(),
                task_id=task_id,
                task_revision=revision,
                node_id=node_id,
                node_sha256=digest(node.model_dump(mode="json")),
                capability_id=descriptor.id,
                descriptor_sha256=descriptor.identity(),
                idempotent=idempotent,
                expires_at=expires,
                inputs=inputs,
            )
            progress[node_id] = NodeProgress(
                state="running", attempts=current.attempts + 1, claim=claim
            )
            if task.state == "ready":
                self.engine.general_tasks.transition(task_id, revision, task.version, "running")
            self._save(
                conn,
                task,
                state,
                kind="node_claimed",
                payload={**route, "operation_id": str(claim.operation_id)},
                progress=progress,
                spent=reserved,
            )
            return claim

    @staticmethod
    def _node(state: GraphSnapshot, node_id: str) -> GraphNode:
        node = next((node for node in state.graph.nodes if node.id == node_id), None)
        if node is None:
            raise DomainError("GENERAL_GRAPH_NODE_UNKNOWN", "Graph node was not found.", 404)
        return node

    def signal(
        self,
        task_id: UUID,
        revision: int,
        graph_revision: int,
        node_id: str,
        *,
        kind: Literal["wait_event", "approval"],
        reference: str,
        accepted: bool,
    ) -> GraphSnapshot:
        with self.engine.transaction() as conn:
            task, state = self._current(conn, task_id, revision)
            if state.graph_revision != graph_revision:
                raise DomainError("GENERAL_GRAPH_STALE", "Graph revision changed.", 409)
            node = self._node(state, node_id)
            current = state.progress[node_id]
            if (
                node.kind != kind
                or node.signal_key != reference
                or (kind == "wait_event" and not accepted)
            ):
                raise DomainError(
                    "GENERAL_GRAPH_SIGNAL_MISMATCH", "Signal differs from graph binding.", 422
                )
            next_state: NodeState = "succeeded" if accepted else "failed"
            if (current.signal_reference, current.state) == (reference, next_state):
                return state
            deadline = _deadline(task, state)
            if deadline is not None and deadline <= datetime.now(UTC):
                raise DomainError("GENERAL_GRAPH_DEADLINE", "Task time budget expired.", 409)
            if (
                task.state not in {"ready", "running"}
                or current.state not in {"pending", "waiting"}
                or _dependency_state(node, state.progress) != "pending"
            ):
                raise DomainError("GENERAL_GRAPH_SIGNAL_NOT_READY", "Signal is not ready.", 409)
            progress = dict(state.progress)
            progress[node_id] = NodeProgress(state=next_state, signal_reference=reference)
            _propagate(state.graph, progress)
            updated = self._save(
                conn,
                task,
                state,
                kind="node_signal",
                payload={
                    "node_id": node_id,
                    "kind": kind,
                    "reference": reference,
                    "accepted": accepted,
                },
                progress=progress,
            )
            self._advance_task(task, updated)
            return updated

    def settle(self, receipt_sha256: str) -> GraphSnapshot:
        raw = self.engine.artifacts.read(receipt_sha256)
        if len(raw) > 32768:
            raise DomainError(
                "GENERAL_GRAPH_RECEIPT_INVALID", "Receipt exceeds its byte limit.", 422
            )
        try:
            receipt = NodeReceipt.model_validate_json(raw)
        except ValidationError as exc:
            raise DomainError(
                "GENERAL_GRAPH_RECEIPT_INVALID", "Receipt schema is invalid.", 422
            ) from exc
        with self.engine.transaction() as conn:
            task, state = self._current(conn, receipt.task_id, receipt.task_revision)
            node = self._node(state, receipt.node_id)
            current = state.progress[node.id]
            binding = current.claim
            if current.receipt_sha256 == receipt_sha256:
                return state
            if (
                task.state != "running"
                or current.state not in _ACTIVE
                or binding is None
                or (receipt.operation_id, receipt.node_sha256, receipt.descriptor_sha256)
                != (binding.operation_id, binding.node_sha256, binding.descriptor_sha256)
            ):
                raise DomainError(
                    "GENERAL_GRAPH_RECEIPT_STALE", "Receipt does not own this claim.", 409
                )
            descriptor = self.registry.definition(binding.capability_id)
            if (
                descriptor is None
                or descriptor.identity() != binding.descriptor_sha256
                or (
                    set(receipt.outputs) != set(node.outputs)
                    if receipt.status == "succeeded"
                    else bool(receipt.outputs)
                )
                or (
                    receipt.status == "succeeded"
                    and not Draft202012Validator(descriptor.output_schema).is_valid(receipt.outputs)
                )
            ):
                raise DomainError(
                    "GENERAL_GRAPH_RECEIPT_INVALID", "Receipt output is outside the binding.", 422
                )
            next_state: NodeState = receipt.status
            if (
                receipt.status == "failed"
                and binding.idempotent
                and current.attempts < node.max_attempts
            ):
                next_state = "pending"
            progress = dict(state.progress)
            progress[node.id] = _update(
                current,
                state=next_state,
                outputs=receipt.outputs,
                receipt_sha256=receipt_sha256,
                reason="receipt_reconciliation_required" if next_state == "unknown" else None,
            )
            _propagate(state.graph, progress)
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (receipt_sha256, len(raw)),
            )
            updated = self._save(
                conn,
                task,
                state,
                kind="node_receipt",
                payload={"node_id": node.id, "sha256": receipt_sha256, "state": next_state},
                progress=progress,
            )
            self._advance_task(task, updated)
            return updated

    def _advance_task(self, task: TaskSnapshot, state: GraphSnapshot) -> None:
        if not all(item.state in _TERMINAL for item in state.progress.values()):
            return
        produced = {
            key
            for item in state.progress.values()
            if item.state == "succeeded"
            for key in item.outputs
        }
        complete = {item.id for item in task.spec.deliverables} <= produced
        if complete and task.state == "running":
            self.engine.general_tasks.transition(
                task.task_id, task.revision, task.version, "verifying"
            )
        elif not complete and task.state in {"ready", "running"}:
            self.engine.general_tasks.transition(
                task.task_id,
                task.revision,
                task.version,
                "blocked",
                reason="graph_missing_deliverables",
            )

    def model_tools(
        self,
        claim: NodeClaim,
        access: CapabilityAccess,
        *,
        context_tokens: int,
    ) -> list[dict[str, Any]]:
        """Recheck current claim, grants, and readiness before model projection."""
        with self.engine.transaction() as conn:
            task, state = self._current(conn, claim.task_id, claim.task_revision)
            node = self._node(state, claim.node_id)
            current = state.progress[node.id]
            if (
                task.state != "running"
                or current.state != "running"
                or current.claim != claim
                or claim.expires_at <= datetime.now(UTC)
                or (claim.task_id, task.project_id, claim.task_revision)
                != (access.task_id, access.project_id, access.task_revision)
            ):
                raise DomainError(
                    "GENERAL_GRAPH_ACCESS", "Model projection is outside active claim.", 403
                )
            if task.membership_id is not None:
                require_membership(conn, task.project_id, str(task.membership_id), write=True)
            action_spent = self.engine.general_actions.spent(
                conn, claim.task_id, claim.task_revision
            )
            if any(
                getattr(state.spent, key) + getattr(action_spent, key) > limit
                for key, limit in access.resources.model_dump().items()
            ):
                return []
            decision: RouteDecision = self.registry.route(
                task.spec,
                node.domain,
                (claim.capability_id,),
                access,
                context_tokens=context_tokens,
            )
            if decision.descriptor_sha256 != claim.descriptor_sha256:
                return []
            return self.registry._model_tools(decision)

    def events(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            task = self.engine.general_tasks.get(task_id)
            self._read(conn, task_id, task.revision)
            return list(
                conn.execute(
                    "SELECT event_id,graph_revision,version,kind,payload,occurred_at "
                    "FROM app.general_graph_events WHERE task_id=%s AND task_revision=%s "
                    "ORDER BY event_id DESC LIMIT 200",
                    (task_id, task.revision),
                ).fetchall()
            )

    def coding_request(
        self, claim: NodeClaim, access: CapabilityAccess, *, inspect_path: str = ""
    ) -> CodingTaskRequest:
        """Project a current selection into the existing CodingLoop admission contract."""
        from mgar_v3.general_agent.coding import to_coding_request

        with self.engine.transaction():
            if claim.capability_id not in {
                "coding.harness",
                "coding.inspect",
            } or not self.model_tools(
                claim,
                access,
                context_tokens=8192,
            ):
                raise DomainError("GENERAL_CODING_ROUTE_REQUIRED", "No current Coding claim.", 403)
            task = self.engine.general_tasks.get(claim.task_id)
            if claim.capability_id == "coding.inspect" and task.spec.coding_policy != "inspect":
                raise DomainError("GENERAL_CODING_ROUTE_REQUIRED", "Coding inspection only.", 403)
            request = to_coding_request(task.spec, project_id=task.project_id)
            if inspect_path and request.task_kind != "inspect":
                raise DomainError("GENERAL_CODING_ROUTE_REQUIRED", "Coding inspection only.", 403)
            return replace(request, target_path=inspect_path)
