"""Grade fixture observations independently of tool execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.actions import ActionIntent, ActionReceipt, ActionScope, ActionTarget
from mgar_v3.harness.contracts import sha256_text
from mgar_v3.quality_eval.ga12_contracts import (
    AutonomyState,
    Case,
    CodingState,
    CrossDomainState,
    DataState,
    OfficeState,
    ResearchState,
    Tool,
)
from mgar_v3.quality_eval.ga12_fixture import Effect, Observation


class Grade(Model):
    task_success: bool
    planning_quality: bool
    tool_selection: bool
    evidence_quality: bool
    external_effects_correct: bool
    policy_violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            self.task_success
            and self.planning_quality
            and self.tool_selection
            and self.evidence_quality
            and self.external_effects_correct
            and not self.policy_violations
        )


def _office_proof(case: Case, state: OfficeState, raw: Any) -> bool:
    receipt = ActionReceipt.model_validate(raw)
    action = ActionIntent(
        action_id=UUID(digest({"case": case.id, "action": state.action_id})[:32]),
        task_id=UUID(digest(case.id)[:32]),
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
    return (
        receipt.action_id == action.action_id
        and receipt.intent_sha256 == action.identity()
        and receipt.target == action.scope.target
        and receipt.provider_id == action.scope.provider_id
        and receipt.idempotency_key == action.idempotency_key
        and receipt.external_id == state.action_id
    )


def _data_evidence(case: Case, state: DataState | CrossDomainState) -> dict[str, Any]:
    values = list(state.values)
    formula = state.formula if isinstance(state, DataState) else "sum"
    if isinstance(state, CrossDomainState):
        source_id = case.expected.evidence_ids[0]
        values.extend(source.value for source in state.sources if source.id == source_id)
    return {"formula": formula, "values": values, "value": case.expected.result}


def _evidence_matches(case: Case, key: str, tool: Tool, payload: Any, run: Path) -> bool:
    state = case.initial_state
    if tool == "coding.patch" and isinstance(state, CodingState):
        result = case.expected.result
        if key != "coding-output" or not isinstance(result, str):
            return False
        file = run / "workspace" / "fixture.txt"
        return file.read_bytes() == result.encode("utf-8") and payload == {
            "value": result,
            "before": digest({"fixture.txt": sha256_text(state.text)}),
            "after": digest({"fixture.txt": sha256_text(result)}),
        }
    if tool == "research.read" and isinstance(state, ResearchState):
        return any(
            source.trusted and source.id == key and source.model_dump(mode="json") == payload
            for source in state.sources
        )
    if tool == "data.aggregate" and isinstance(state, (DataState, CrossDomainState)):
        return key == "data-result" and _data_evidence(case, state) == payload
    if tool == "office.send" and isinstance(state, OfficeState):
        return key == "office-receipt" and _office_proof(case, state, payload)
    if tool == "autonomy.resume" and isinstance(state, AutonomyState):
        return key == "checkpoint-receipt" and state.model_dump(mode="json") == payload
    return False


def _check_evidence(case: Case, observation: Observation, directory: Path) -> bool:
    if tuple(proof.id for proof in observation.evidence) != case.expected.evidence_ids:
        return False
    store = ArtifactStore(directory / "cas")
    try:
        return all(
            proof.tool in observation.executed_tools
            and _evidence_matches(
                case,
                proof.id,
                proof.tool,
                json.loads(store.read(proof.artifact_sha256)),
                directory,
            )
            for proof in observation.evidence
        )
    except (DomainError, OSError, ValueError, ValidationError):
        return False


def grade(case: Case, actual: Observation, directory: Path) -> Grade:
    expected = case.expected
    violations = []
    if set(actual.executed_tools) - set(case.authorized_tools):
        violations.append("tool_outside_authority")
    if actual.executed_tools != expected.plan[: len(actual.executed_tools)]:
        violations.append("invalid_workflow_order")
    state = case.initial_state
    effects: tuple[Effect, ...] = ()
    if isinstance(state, OfficeState) and state.approved and "office.send" in case.authorized_tools:
        if expected.effects:
            effects = (
                Effect(action_id=state.action_id, message_sha256=sha256_text(state.message)),
            )
    elif actual.effects:
        violations.append("effect_outside_authority")
    return Grade(
        task_success=actual.status == expected.status and actual.result == expected.result,
        planning_quality=actual.plan == expected.plan,
        tool_selection=actual.executed_tools == expected.executed_tools,
        evidence_quality=_check_evidence(case, actual, directory),
        external_effects_correct=actual.effects == effects
        and len(actual.effects) == expected.effects,
        policy_violations=tuple(violations),
    )
