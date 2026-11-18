"""R01 integrity failures must stop before a model or workspace side effect."""

import json

import pytest
from pydantic import ValidationError
from test_harness_v4_h10 import ScriptedModel, _context_fixture, _runtime

from mgar_v3.context.builder import ContextBuilder
from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.acceptance import AcceptanceContract, contract_for
from mgar_v3.harness.coding_loop import AcceptanceProposal, _RunState
from mgar_v3.harness.contracts import CancellationToken, ModelMessage

PUBLIC = "Print the actually observed HTTP status before closing the local server."


def fixture(tmp_path):
    model = ScriptedModel([])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    task = task.model_copy(update={"public_requirements": (PUBLIC,)})
    return loop, task, grant, session, model


@pytest.mark.parametrize("field", [
    "acceptance_id", "acceptance_revision", "acceptance_hash", "required_checks",
    "required_artifacts", "scope", "baseline_commit", "task_type",
])
def test_required_contract_fields_cannot_default_to_pass(tmp_path, field):
    loop, task, _, session, _ = fixture(tmp_path)
    try:
        raw = loop.plan(task).acceptance_contract.model_dump(mode="json")
        raw.pop(field)
        with pytest.raises(ValidationError):
            AcceptanceContract.model_validate(raw)
    finally:
        session.close()


@pytest.mark.parametrize("variant", ["missing", "hash", "revision", "required", "planner"])
def test_plan_tampering_is_rejected_before_any_execution(tmp_path, variant):
    loop, task, grant, session, model = fixture(tmp_path)
    try:
        plan = loop.plan(task)
        before = loop.workspace.snapshot().tree_hash
        if variant == "missing":
            plan = plan.model_copy(update={"acceptance_contract": None})
        elif variant == "planner":
            plan = plan.model_copy(update={"acceptance_binding": None})
        elif variant == "required":
            items = tuple(i.model_copy(update={"required": False}) for i in plan.checklist.items)
            plan = plan.model_copy(update={
                "checklist": plan.checklist.model_copy(update={"items": items}),
            })
        else:
            key, value = ("acceptance_hash", "a" * 64) if variant == "hash" else (
                "acceptance_revision", 99)
            plan = plan.model_copy(update={
                "acceptance_contract": plan.acceptance_contract.model_copy(update={key: value}),
            })
        with pytest.raises(DomainError):
            loop.run(task, grant, plan)
        assert not model.requests
        assert loop.workspace.snapshot().tree_hash == before
        assert loop.budgets.snapshot(task.task_id).model_calls == 0
    finally:
        session.close()


def test_self_consistent_contract_cannot_remove_trusted_policy(tmp_path):
    loop, task, grant, session, _ = fixture(tmp_path)
    try:
        plan = loop.plan(task)
        raw = plan.acceptance_contract.model_dump(mode="json")
        raw["required_checks"] = [c for c in raw["required_checks"] if c["id"] != "evidence"]
        raw["affected_check_ids"] = [c["id"] for c in raw["required_checks"]]
        raw["acceptance_hash"] = digest({k: v for k, v in raw.items() if k != "acceptance_hash"})
        with pytest.raises((DomainError, ValidationError)):
            changed = AcceptanceContract.model_validate(raw)
            loop.run(task, grant, plan.model_copy(update={
                "acceptance_contract": changed, "acceptance_binding": changed.binding("planner"),
            }))
    finally:
        session.close()


def test_self_consistent_hash_cannot_weaken_required_artifact_policy(tmp_path):
    loop, task, grant, session, model = fixture(tmp_path)
    try:
        plan = loop.plan(task)
        raw = plan.acceptance_contract.model_dump(mode="json")
        raw["required_artifacts"] = ["verification_report"]
        raw["acceptance_hash"] = digest({k: v for k, v in raw.items() if k != "acceptance_hash"})
        changed = AcceptanceContract.model_validate(raw)
        with pytest.raises(DomainError):
            loop.run(task, grant, plan.model_copy(update={
                "acceptance_contract": changed, "acceptance_binding": changed.binding("planner"),
            }))
        assert not model.requests
    finally:
        session.close()


def test_rehashed_revision_and_planner_receipt_cannot_mint_unapproved_history(tmp_path):
    loop, task, grant, session, model = fixture(tmp_path)
    try:
        plan = loop.plan(task)
        raw = plan.acceptance_contract.model_dump(mode="json")
        raw.update(acceptance_revision=2, previous_hash=raw["acceptance_hash"])
        raw["acceptance_hash"] = digest({k: v for k, v in raw.items() if k != "acceptance_hash"})
        changed = AcceptanceContract.model_validate(raw)
        with pytest.raises(DomainError, match="trusted planning event"):
            loop.run(task, grant, plan.model_copy(update={
                "acceptance_contract": changed, "acceptance_binding": changed.binding("planner"),
            }))
        assert not model.requests
    finally:
        session.close()


def test_model_can_add_optional_checks_but_cannot_replace_existing_required(tmp_path):
    loop, task, _, session, _ = fixture(tmp_path)
    try:
        plan = loop.plan(task)
        with pytest.raises(DomainError):
            loop.accept_proposal(task, plan, [
                AcceptanceProposal(item_id="evidence", description="Skip the real evidence",
                                   kind="evidence"),
            ])
        new = loop.accept_proposal(task, plan, [
            AcceptanceProposal(item_id="extra.note", description="Optional additional explanation",
                               kind="evidence"),
        ])
        assert new.acceptance_contract.previous_hash == plan.acceptance_contract.acceptance_hash
        assert new.acceptance_contract.acceptance_revision == 2
        assert set(plan.acceptance_contract.required_ids).issubset(
            new.acceptance_contract.required_ids)
        loop._acceptance(task, new)
    finally:
        session.close()


@pytest.mark.parametrize("stage", ["prompt", "compactor"])
def test_projection_or_compactor_field_loss_blocks_before_dispatch(tmp_path, monkeypatch, stage):
    loop, task, grant, session, model = fixture(tmp_path)
    try:
        if stage == "compactor":
            original = ContextBuilder.build

            def build(*args, **kwargs):
                bundle = original(*args, **kwargs)
                body = json.loads(bundle.constraints[0])
                body["working_state"].pop("acceptance_contract")
                return bundle.model_copy(update={"constraints": [json.dumps(body)]})

            monkeypatch.setattr(ContextBuilder, "build", build)
        else:
            original = loop.context.prepare

            def prepare(*args, **kwargs):
                view = original(*args, **kwargs)
                messages = []
                for message in view.messages:
                    if message.name == "h10_context" and message.role == "user":
                        body = json.loads(message.content)
                        state = json.loads(body["constraints"][0])
                        state["working_state"].pop("acceptance_contract")
                        body["constraints"][0] = json.dumps(state)
                        message = message.model_copy(update={"content": json.dumps(body)})
                    messages.append(message)
                return view.model_copy(update={"messages": tuple(messages)})

            monkeypatch.setattr(loop.context, "prepare", prepare)
        result = loop.run(task, grant, loop.plan(task))
        assert result.status == "blocked"
        assert not model.requests
        assert result.model_calls == 0
    finally:
        session.close()


def test_three_compactions_preserve_every_public_id_and_contract_hash(tmp_path):
    context, task, state = _context_fixture(tmp_path)
    task = task.model_copy(update={"public_requirements": (PUBLIC,)})
    contract = contract_for(task, state.checklist["items"])
    state = state.model_copy(update={"acceptance_contract": contract})
    history = []
    for number in range(4):
        history += [ModelMessage(role="user", content="Historical observation\n" * 2400)]
        state = state.model_copy(update={"sequence": number + 1})
        view = context.prepare(task, (), history, state)
        body = json.loads(next(m.content for m in view.messages
                               if m.name == "h10_context" and m.role == "user"))
        projected = json.loads(body["constraints"][0])["working_state"]["acceptance_contract"]
        assert projected["acceptance_hash"] == contract.acceptance_hash
        assert {c["id"] for c in projected["required_checks"]} == set(contract.required_ids)
        assert PUBLIC in json.dumps(projected)
        history = list(view.messages)
    assert context.compaction_count >= 3


@pytest.mark.parametrize("variant", ["missing", "revision"])
def test_verifier_cannot_omit_or_change_acceptance_binding(tmp_path, variant):
    from mgar_v3.harness.adapters import LocalVerifier

    loop, task, grant, session, _ = fixture(tmp_path)
    try:
        task = task.model_copy(update={"task_kind": "feature"})
        state = _RunState(
            task=task, grant=grant, plan=loop.plan(task),
            policy=loop._task_policy(task.task_kind), token=CancellationToken(),
            baseline_files=dict(loop.workspace.snapshot().files),
        )

        class WrongVerifier:
            def verify(self, request):
                result = LocalVerifier().verify(request)
                binding = None if variant == "missing" else result.acceptance_binding.model_copy(
                    update={"acceptance_revision": 999})
                return result.model_copy(update={"acceptance_binding": binding})

        loop.verifier = WrongVerifier()
        with pytest.raises(DomainError, match="acceptance"):
            loop._assess(state, "Ready for verification", finalize=True)
    finally:
        session.close()
