"""Bounded GA12 fixtures. No provider access, shell commands or real writes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.actions import ActionIntent, ActionReceipt, ActionScope, ActionTarget
from mgar_v3.general_agent.capabilities import (
    CapabilityAccess,
    CapabilityDescriptor,
    CapabilityRegistry,
)
from mgar_v3.general_agent.contracts import (
    Deliverable,
    GeneralTaskSpec,
    SuccessCriterion,
    TaskDomain,
)
from mgar_v3.general_agent.graph import Dependency, GraphNode, NodeInput, TaskGraph
from mgar_v3.harness.adapters import LocalWorkspace
from mgar_v3.harness.contracts import TextReplacement, WorkspaceEdit, sha256_text
from mgar_v3.quality_eval.ga12_contracts import (
    Arm,
    AutonomyState,
    CodingState,
    CrossDomainState,
    DataState,
    FixtureInput,
    OfficeState,
    ResearchState,
    Sequences,
    Status,
    Tool,
    Value,
)

_TOOL_DOMAIN: dict[Tool, TaskDomain] = {
    "coding.patch": "coding",
    "research.read": "research",
    "data.aggregate": "data",
    "office.send": "office",
    "autonomy.resume": "office",
}


class Proof(Model):
    id: str
    tool: Tool
    artifact_sha256: str


class Effect(Model):
    action_id: str
    message_sha256: str


class Observation(Model):
    status: Status
    result: Value
    plan: tuple[Tool, ...]
    executed_tools: tuple[Tool, ...]
    evidence: tuple[Proof, ...]
    effects: tuple[Effect, ...]
    trace: tuple[str, ...]
    reason: str | None


def _descriptor(tool: Tool) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=tool,
        version="fixture-1",
        domain=_TOOL_DOMAIN[tool],
        description="GA12 fixture port",
        provider="native",
        provider_id="ga12-mock",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=(f"fixture.{tool}",),
        effect="workspace_write" if tool == "coding.patch" else "read_only",
        timeout_seconds=10,
        evidence_contract="ga12-fixture-v1",
    )


def _spec(case: FixtureInput, tools: tuple[Tool, ...]) -> GeneralTaskSpec:
    return GeneralTaskSpec(
        goal=case.goal,
        domain=_TOOL_DOMAIN[tools[0]],
        coding_policy="bugfix" if case.domain == "coding" else None,
        deliverables=(Deliverable(id="result", kind="file", description="Fixture result"),),
        success_criteria=(
            SuccessCriterion(
                id="verified", description="Independent check", deliverable_ids=("result",)
            ),
        ),
        allowed_capabilities=tools,
    )


def _plan(spec: GeneralTaskSpec, tools: tuple[Tool, ...]) -> tuple[Tool, ...]:
    nodes = []
    for index, tool in enumerate(tools):
        inputs: tuple[NodeInput, ...] = ()
        dependencies: tuple[Dependency, ...] = ()
        if index:
            dependencies = (Dependency(node_id="step-1"),)
            inputs = (NodeInput(name="prior", node_id="step-1", output_id="intermediate"),)
        nodes.append(
            GraphNode(
                id=f"step-{index + 1}",
                domain=_TOOL_DOMAIN[tool],
                goal=f"Use {tool}",
                dependencies=dependencies,
                inputs=inputs,
                capabilities=(tool,),
                outputs=("result" if index == len(tools) - 1 else "intermediate",),
                completion_condition="Bound fixture evidence",
            ),
        )
    graph = TaskGraph(task_revision=1, nodes=tuple(nodes))
    graph.check_task(spec, 1)
    return tuple(
        tool for tool, node in zip(tools, graph.nodes, strict=True) if tool in node.capabilities
    )


class _Fixture:
    def __init__(self, case: FixtureInput, arm: Arm, directory: Path) -> None:
        self.case, self.arm, self.directory = case, arm, directory
        self.artifacts = ArtifactStore(directory / "cas")
        self.trace: list[str] = []
        self.executed: list[Tool] = []
        self.evidence: list[Proof] = []
        # A separate mock provider acceptance log, so an unknown acknowledgement does not
        # erase an effect and reconciliation does not add another effect.
        self.effects: list[Effect] = []
        self.intermediate: int | None = None

    def _prove(self, tool: Tool, key: str, payload: dict[str, Any]) -> None:
        import json

        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        sha = self.artifacts.put(raw)
        self.evidence.append(Proof(id=key, tool=tool, artifact_sha256=sha))

    def coding(self, state: CodingState) -> tuple[Status, Value, str | None]:
        if state.memory_hint and self.arm == "no_memory":
            return "unavailable", None, "scoped_memory_unavailable"
        root = self.directory / "workspace"
        root.mkdir()
        path = "fixture.txt"
        file = root / path
        # LocalWorkspace presents universal newlines. Escape carriage returns
        # using one code point absent from the whole bounded edit, so matching
        # remains one-to-one and the deliverable preserves literal bytes.
        characters = set(state.text + state.old + state.new)
        marker = next(chr(point) for point in range(0xE000, 0xF900) if chr(point) not in characters)
        staged = state.text.replace("\r", marker)
        file.write_bytes(staged.encode("utf-8"))
        workspace = LocalWorkspace(root, workspace_id=self.case.id)
        before = workspace.snapshot()
        after = workspace.apply(
            [
                WorkspaceEdit(
                    path=path,
                    expected_sha256=sha256_text(staged),
                    replacements=[
                        TextReplacement(
                            old_text=state.old.replace("\r", marker),
                            new_text=state.new.replace("\r", marker),
                        )
                    ],
                )
            ],
            expected_tree=before.tree_hash,
        )
        value = after.files[path].replace(marker, "\r")
        final_bytes = value.encode("utf-8")
        if file.read_bytes() != final_bytes:
            file.write_bytes(final_bytes)
        self._prove(
            "coding.patch",
            "coding-output",
            {
                "value": value,
                "before": digest({path: sha256_text(before.files[path].replace(marker, "\r"))}),
                "after": digest({path: sha256_text(value)}),
            },
        )
        return "completed", value, None

    def research(self, state: ResearchState) -> tuple[Status, Value, str | None]:
        sources = [item for item in state.sources if item.trusted]
        if not sources:
            return "unavailable", None, "trusted_source_unavailable"
        if self.arm != "no_reranker":
            sources.sort(key=lambda item: (-item.rank, item.id))
        selected = sources[0]
        self.intermediate = selected.value
        self._prove("research.read", selected.id, selected.model_dump(mode="json"))
        return "completed", selected.value, None

    def data(self, state: DataState | CrossDomainState) -> tuple[Status, Value, str | None]:
        if isinstance(state, CrossDomainState) and self.intermediate is None:
            return "unavailable", None, "dependency_missing"
        formula = state.formula if isinstance(state, DataState) else "sum"
        values = state.values
        if isinstance(state, CrossDomainState):
            assert self.intermediate is not None
            values += (self.intermediate,)
        if formula == "max":
            value = max(values)
        elif formula == "count":
            value = len(values)
        else:
            value = sum(values)
        self._prove(
            "data.aggregate",
            "data-result",
            {
                "formula": formula,
                "values": values,
                "value": value,
            },
        )
        return "completed", value, None

    def office(self, state: OfficeState) -> tuple[Status, Value, str | None]:
        if not state.approved:
            self.trace.append("office_approval_denied")
            return "denied", None, "approval_required"
        action = ActionIntent(
            action_id=UUID(digest({"case": self.case.id, "action": state.action_id})[:32]),
            task_id=UUID(digest(self.case.id)[:32]),
            task_revision=1,
            plan_version=1,
            scope=ActionScope(
                actor="fixture",
                capability="office.send",
                provider_id="ga12-mock",
                target=ActionTarget(kind="email", reference=state.action_id),
                operation="send",
                effect="external_write",
                mode="fixture",
            ),
            expected_effect="One fixture acceptance",
            idempotency_key=state.action_id,
            risk="low",
            payload_sha256=sha256_text(state.message),
        )
        # This is the fixture provider boundary. A lookup below must always be read-only.
        self.effects.append(
            Effect(action_id=state.action_id, message_sha256=sha256_text(state.message))
        )
        self.trace.append("fixture_provider_accept")
        if state.ack == "lost":
            self.trace.append("fixture_provider_lookup")
            if not state.lookup_available:
                return "unknown", None, "fixture_ack_unconfirmed"
        receipt = ActionReceipt(
            action_id=action.action_id,
            intent_sha256=action.identity(),
            provider_id="ga12-mock",
            target=action.scope.target,
            idempotency_key=action.idempotency_key,
            external_id=state.action_id,
            recorded_at=datetime(2026, 11, 19, tzinfo=UTC),
        )
        self._prove("office.send", "office-receipt", receipt.model_dump(mode="json"))
        return "completed", receipt.external_id, None

    def autonomy(self, state: AutonomyState) -> tuple[Status, Value, str | None]:
        self.trace.append("checkpoint_loaded")
        if state.receipt == "unknown":
            return "unknown", None, "checkpoint_ack_unconfirmed"
        self.trace.append("known_receipt_reused")
        self._prove("autonomy.resume", "checkpoint-receipt", state.model_dump(mode="json"))
        return "completed", state.value, None

    def execute(self, tool: Tool) -> tuple[Status, Value, str | None]:
        state = self.case.initial_state
        if tool == "coding.patch" and isinstance(state, CodingState):
            return self.coding(state)
        if tool == "research.read" and isinstance(state, ResearchState):
            return self.research(state)
        if tool == "data.aggregate" and isinstance(state, (DataState, CrossDomainState)):
            return self.data(state)
        if tool == "office.send" and isinstance(state, OfficeState):
            return self.office(state)
        if tool == "autonomy.resume" and isinstance(state, AutonomyState):
            return self.autonomy(state)
        raise ValueError("Fixture tool input does not match its declared type.")


def execute(case: FixtureInput, arm: Arm, directory: Path) -> Observation:
    """Run a fresh fixture, without loading or receiving any judge answers."""
    tools = Sequences[case.domain]
    spec = _spec(case, tools)
    planned = () if arm == "no_planner" else _plan(spec, tools)
    selected = planned or tools[:1]
    if arm == "single_domain":
        selected = selected[:1]
    descriptors = [_descriptor(tool) for tool in tools]
    registry = CapabilityRegistry(descriptors)
    granted = tuple(item for item in descriptors if item.id in case.authorized_tools)
    scope = CapabilityAccess(
        project_id="benchmark-fixture",
        task_id=UUID(digest(case.id)[:32]),
        task_revision=1,
        capability_pins=tuple(item.identity() for item in granted),
        permissions=tuple(permission for item in granted for permission in item.permissions),
        workspace_write="coding.patch" in case.authorized_tools,
    )
    fixture = _Fixture(case, arm, directory)
    status: Status = "unavailable"
    result: Value = None
    reason: str | None = None
    for tool in selected:
        decision = registry.route(spec, _TOOL_DOMAIN[tool], (tool,), scope, context_tokens=2048)
        fixture.trace.append(f"{tool}:{decision.checks[0].reason}")
        if not decision.chosen_id:
            status, result, reason = "denied", None, decision.checks[0].reason
            break
        fixture.executed.append(tool)
        try:
            status, result, reason = fixture.execute(tool)
        except DomainError as exc:
            status, result, reason = "unavailable", None, exc.code
        if status != "completed":
            break
    else:
        if len(selected) != len(tools):
            status, result, reason = "unavailable", None, "incomplete_workflow"
    return Observation(
        status=status,
        result=result,
        plan=planned,
        executed_tools=tuple(fixture.executed),
        evidence=tuple(fixture.evidence),
        effects=tuple(fixture.effects),
        trace=tuple(fixture.trace),
        reason=reason,
    )
