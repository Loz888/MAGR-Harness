"""Universal action authorization, idempotency and fault recovery on PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from test_general_agent_ga01 import create, spec
from test_general_agent_ga02 import capability, claim, grant, graph, node

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import DefinitiveFailure, GeneralActionDispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.actions import (
    ActionApproval,
    ActionIntent,
    ActionReceipt,
    ActionScope,
    ActionTarget,
)
from mgar_v3.general_agent.capabilities import CapabilityRegistry, Resources
from mgar_v3.general_agent.contracts import TaskBudget, TaskPlan

CAPABILITY = "office.email"
SOURCE = "office.read"
PROVIDER = "mail-fixture"
INITIAL_VERSION = "a" * 64


def prepare(
    engine,
    *,
    effect="external_write",
    compensable=True,
    tool_budget=5,
    action_cost=1,
    plan_graph=False,
):
    descriptor = capability(
        CAPABILITY,
        provider="executor",
        provider_id=PROVIDER,
        effect={"read": "read_only", "local_write": "workspace_write"}.get(
            effect, "external_write"
        ),
        upper_bound=Resources(tool_calls=action_cost),
        idempotent=False,
    )
    source = capability(SOURCE)
    engine.general_graphs.registry = CapabilityRegistry([descriptor, source])
    value = spec("office").model_copy(
        update={
            "approval_policy": "manual",
            "allowed_capabilities": (CAPABILITY, SOURCE),
            "budget": TaskBudget(max_tool_calls=tool_budget, max_cost_micros=0, max_model_calls=0),
        }
    )
    task = create(engine.general_tasks, value)
    if plan_graph:
        engine.general_graphs.install(
            task.task_id,
            1,
            1,
            graph(
                node("report", domain="office", capabilities=(SOURCE,), outputs=("result",)),
                node("other", domain="office", capabilities=(SOURCE,)),
                max_parallel=2,
            ),
        )
    else:
        engine.general_tasks.save_plan(
            task.task_id, 1, 1, TaskPlan(task_revision=1, steps=("Act",))
        )
    task = engine.general_tasks.transition(task.task_id, 1, 2, "running")
    scope = ActionScope(
        actor="owner",
        capability=CAPABILITY,
        provider_id=PROVIDER,
        target=ActionTarget(kind="email", reference="dedicated-fixture-mailbox/item-1"),
        operation="send",
        effect=effect,
        mode="fixture",
        compensable=compensable,
    )
    access = grant(task, descriptor, source, workspace_write=True)
    lease = engine.general_actions.issue_lease(scope, access, task.plan_version)
    intent = ActionIntent(
        action_id=uuid4(),
        idempotency_key="dedicated-fixture-key",
        scope=scope,
        task_id=task.task_id,
        task_revision=1,
        plan_version=task.plan_version,
        risk="low",
        expected_effect="Record a provider receipt for the dedicated fixture mailbox",
        precondition_sha256=INITIAL_VERSION,
        payload_sha256=engine.artifacts.put(b"fixture payload"),
        compensation="Use the separately authorized undo operation" if compensable else None,
    )
    return descriptor, task, lease, intent


def approve(journal, record, lease, level="operator"):
    return journal.approve(
        record.intent.action_id,
        ActionApproval(
            intent_sha256=record.intent_sha256,
            lease_id=lease.lease_id,
            epoch=lease.epoch,
            reviewer="reviewer",
            level=level,
        ),
    )


def bound_receipt(intent, external_id="fixture-object"):
    return ActionReceipt(
        action_id=intent.action_id,
        intent_sha256=intent.identity(),
        provider_id=intent.scope.provider_id,
        target=intent.scope.target,
        idempotency_key=intent.idempotency_key,
        external_id=external_id,
        recorded_at=datetime.now(UTC),
    )


class Transport:
    mode = "fixture"
    provider_id = PROVIDER
    observed = INITIAL_VERSION

    def __init__(self, *, loses_receipt=False, rejects=False, barrier=None):
        self.loses_receipt = loses_receipt
        self.rejects = rejects
        self.barrier = barrier
        self.sent = 0
        self.found = 0
        self.targets = {}

    def observe(self, intent):
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        return self.observed

    def send(self, intent, payload):
        assert payload == b"fixture payload"
        self.sent += 1
        if self.rejects:
            raise DefinitiveFailure()
        value = self.targets.setdefault(intent.idempotency_key, bound_receipt(intent))
        if self.loses_receipt:
            raise TimeoutError("fixture lost the response after writing")
        return value

    def find(self, intent):
        self.found += 1
        return self.targets.get(intent.idempotency_key)


def test_unapproved_action_cannot_send_and_receipt_is_auditable(engine):
    _, task, lease, intent = prepare(engine)
    journal = engine.general_actions
    transport = Transport()
    dispatcher = GeneralActionDispatcher(engine, transport)
    staged = journal.submit(intent, lease.lease_id, lease.epoch)
    assert staged.status == "review" and staged.required_approval == "operator"
    assert dispatcher.tick() is None and transport.sent == 0
    assert approve(journal, staged, lease).status == "ready"
    assert dispatcher.tick() == intent.action_id
    result = journal.get(intent.action_id)
    assert result.status == "succeeded" and result.receipt.external_id == "fixture-object"
    assert result.receipt_sha256 == digest(result.receipt.model_dump(mode="json"))
    assert [entry["kind"] for entry in journal.events(intent.action_id)] == [
        "submitted",
        "approved",
        "dispatching",
        "succeeded",
    ]
    assert (
        journal.submit(
            intent.model_copy(update={"action_id": uuid4()}), lease.lease_id, lease.epoch
        )
        == result
    )
    with pytest.raises(DomainError) as conflict:
        journal.submit(
            intent.model_copy(update={"expected_effect": "changed"}), lease.lease_id, lease.epoch
        )
    assert conflict.value.code == "GENERAL_ACTION_KEY_CONFLICT"
    assert dispatcher.tick() is None and transport.sent == 1
    with engine.transaction() as conn:
        assert journal.spent(conn, task.task_id, 1).tool_calls == 1


@pytest.mark.parametrize("effect", ["publish", "delete", "financial", "external_write"])
def test_irreversible_actions_require_elevated_approval(engine, effect):
    _, _, lease, intent = prepare(engine, effect=effect, compensable=False)
    journal = engine.general_actions
    staged = journal.submit(intent, lease.lease_id, lease.epoch)
    assert staged.required_approval == "elevated"
    with pytest.raises(DomainError):
        approve(journal, staged, lease)
    assert approve(journal, staged, lease, "elevated").status == "ready"


def test_scope_epoch_authorization_and_pinned_intent_are_checked_at_dispatch(engine):
    _, _, lease, intent = prepare(engine)
    journal = engine.general_actions
    for wrong in (
        intent.model_copy(
            update={"scope": intent.scope.model_copy(update={"operation": "delete"})}
        ),
        intent.model_copy(update={"task_revision": 2}),
    ):
        with pytest.raises(DomainError):
            journal.submit(wrong, lease.lease_id, lease.epoch)
    with pytest.raises(DomainError):
        journal.submit(intent, lease.lease_id, lease.epoch + 1)
    with pytest.raises(DomainError):
        journal.issue_lease(
            lease.scope.model_copy(update={"mode": "live"}), lease.access, lease.plan_version
        )
    record = journal.submit(intent, lease.lease_id, lease.epoch)
    forged = ActionApproval(
        intent_sha256="b" * 64,
        lease_id=lease.lease_id,
        epoch=lease.epoch,
        reviewer="reviewer",
        level="operator",
    )
    with pytest.raises(DomainError):
        journal.approve(intent.action_id, forged)
    approve(journal, record, lease)
    fresh = journal.issue_lease(lease.scope, lease.access, lease.plan_version)
    assert fresh.epoch == lease.epoch + 1
    transport = Transport()
    dispatcher = GeneralActionDispatcher(engine, transport)
    assert dispatcher.tick() is None and transport.sent == 0
    assert journal.get(intent.action_id).status == "cancelled"
    newer = intent.model_copy(update={"action_id": uuid4(), "idempotency_key": "revocable"})
    record = journal.submit(newer, fresh.lease_id, fresh.epoch)
    approve(journal, record, fresh)
    journal.revoke(fresh.lease_id)
    assert dispatcher.tick() is None and journal.get(newer.action_id).status == "cancelled"


def test_precondition_and_shared_graph_budget_are_enforced(engine):
    _, task, lease, intent = prepare(engine, plan_graph=True, tool_budget=1)
    journal = engine.general_actions
    transport = Transport()
    dispatcher = GeneralActionDispatcher(engine, transport)
    first = journal.submit(intent, lease.lease_id, lease.epoch)
    approve(journal, first, lease)
    transport.observed = "b" * 64
    assert dispatcher.tick() is None and transport.sent == 0
    assert journal.get(intent.action_id).status == "cancelled"

    second = intent.model_copy(update={"action_id": uuid4(), "idempotency_key": "next"})
    approve(journal, journal.submit(second, lease.lease_id, lease.epoch), lease)
    transport.observed = INITIAL_VERSION
    assert dispatcher.tick() == second.action_id
    graphs = engine.general_graphs
    state = graphs.get(task.task_id)
    assert claim(graphs, state, "report", lease.access) is None
    assert graphs.events(task.task_id)[0]["payload"]["decision"]["checks"][0]["reason"] == (
        "resource_budget"
    )


def test_graph_reservation_is_charged_before_action_dispatch(engine):
    _, task, lease, intent = prepare(engine, plan_graph=True, tool_budget=1)
    graphs = engine.general_graphs
    assert claim(graphs, graphs.get(task.task_id), "report", lease.access) is not None
    journal = engine.general_actions
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
    transport = Transport()
    assert GeneralActionDispatcher(engine, transport).tick() is None and transport.sent == 0
    assert journal.get(intent.action_id).status == "cancelled"


def test_concurrent_pollers_send_once_and_fault_before_commit_sends_nothing(engine, monkeypatch):
    _, task, lease, intent = prepare(engine)
    journal = engine.general_actions
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
    transport = Transport()
    dispatcher = GeneralActionDispatcher(engine, transport)
    original = journal._event

    def injected(conn, action_id, kind, **payload):
        if kind == "dispatching":
            raise RuntimeError("injected outbox audit failure")
        original(conn, action_id, kind, **payload)

    with monkeypatch.context() as fault:
        fault.setattr(journal, "_event", injected)
        with pytest.raises(RuntimeError, match="injected"):
            dispatcher.tick()
    assert journal.get(intent.action_id).status == "ready" and transport.sent == 0
    with engine.transaction() as conn:
        assert journal.spent(conn, task.task_id, 1).tool_calls == 0
    transport.barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = (pool.submit(dispatcher.tick), pool.submit(dispatcher.tick))
        results = [future.result() for future in pending]
    assert results.count(intent.action_id) == 1 and transport.sent == 1


def test_unknown_fences_same_target_and_reconciles_without_resending(engine):
    _, _, lease, intent = prepare(engine)
    journal = engine.general_actions
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
    transport = Transport(loses_receipt=True)
    dispatcher = GeneralActionDispatcher(engine, transport)
    assert dispatcher.tick() == intent.action_id
    unknown = journal.get(intent.action_id)
    assert unknown.status == "unknown" and unknown.receipt is None
    duplicate = intent.model_copy(update={"action_id": uuid4(), "idempotency_key": "second"})
    approve(journal, journal.submit(duplicate, lease.lease_id, lease.epoch), lease)
    assert dispatcher.tick() is None
    with pytest.raises(DomainError):
        journal.result(intent.action_id, unknown.attempt_id, receipt=bound_receipt(duplicate))
    assert dispatcher.reconcile(intent.action_id)
    assert journal.get(intent.action_id).receipt == transport.targets[intent.idempotency_key]
    assert transport.sent == 1 and transport.found == 1


def test_controller_restart_marks_abandoned_dispatch_unknown(settings):
    artifacts = ArtifactStore(settings.artifact_root)
    first = Engine(settings, artifacts)
    try:
        _, _, lease, intent = prepare(first)
        registry = first.general_graphs.registry
        journal = first.general_actions
        approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
        attempt = journal.claim(intent.action_id, INITIAL_VERSION)
        assert attempt is not None
        with first.transaction() as conn:
            conn.execute(
                "UPDATE app.general_action_outbox "
                "SET attempt_expires_at=now() - interval '1 second' WHERE action_id=%s",
                (intent.action_id,),
            )
    finally:
        first.close()
    other = Engine(settings, artifacts, general_capabilities=registry)
    try:
        transport = Transport()
        dispatcher = GeneralActionDispatcher(other, transport)
        assert dispatcher.tick() is None
        assert other.general_actions.get(intent.action_id).status == "unknown"
        assert not dispatcher.reconcile(intent.action_id)
        assert other.general_actions.get(intent.action_id).attempt_id == attempt
        assert transport.sent == 0 and transport.found == 1
    finally:
        other.close()


def test_definitive_rejection_is_terminal_and_cancellation_fences_pending_actions(engine):
    _, task, lease, intent = prepare(engine)
    journal = engine.general_actions
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
    transport = Transport(rejects=True)
    dispatcher = GeneralActionDispatcher(engine, transport)
    assert dispatcher.tick() == intent.action_id
    assert journal.get(intent.action_id).status == "failed"
    assert dispatcher.tick() is None
    more = intent.model_copy(update={"action_id": uuid4(), "idempotency_key": "cancelled"})
    approve(journal, journal.submit(more, lease.lease_id, lease.epoch), lease)
    engine.general_tasks.transition(task.task_id, 1, task.version, "cancelled")
    assert dispatcher.tick() is None and journal.get(more.action_id).status == "cancelled"
    assert transport.sent == 1


def test_ephemeral_preflight_observation_does_not_keep_revoked_authority(engine):
    _, _, lease, intent = prepare(engine)
    journal = engine.general_actions
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)

    class RevokedDuringObservation(Transport):
        def observe(self, intent):
            journal.revoke(lease.lease_id)
            return super().observe(intent)

    transport = RevokedDuringObservation()
    assert GeneralActionDispatcher(engine, transport).tick() is None and transport.sent == 0
    assert journal.get(intent.action_id).status == "cancelled"


def test_registered_external_capability_has_no_direct_graph_route(engine):
    _, task, lease, intent = prepare(engine, plan_graph=True)
    registry = engine.general_graphs.registry
    result = registry.route(task.spec, "office", (CAPABILITY,), lease.access, context_tokens=4096)
    assert result.status == "blocked" and result.checks[0].reason == "action_outbox_required"
    assert engine.general_actions.submit(intent, lease.lease_id, lease.epoch).status == "review"


def test_live_lease_needs_scoped_authorization_and_the_deployment_write_gate(engine):
    _, _, lease, _ = prepare(engine)
    journal = engine.general_actions
    live = lease.scope.model_copy(update={"mode": "live"})
    with pytest.raises(DomainError):
        journal.issue_lease(live, lease.access, lease.plan_version, live_authorized=True)
    engine.settings.allow_external_write = True
    with pytest.raises(DomainError):
        journal.issue_lease(live, lease.access, lease.plan_version)
    approved = journal.issue_lease(live, lease.access, lease.plan_version, live_authorized=True)
    assert approved.scope.mode == "live"


def test_missing_approved_payload_does_not_block_another_target(engine):
    _, task, first_lease, first = prepare(engine)
    journal = engine.general_actions
    payload = engine.artifacts.put(b"missing payload")
    first = first.model_copy(update={"payload_sha256": payload})
    approve(journal, journal.submit(first, first_lease.lease_id, first_lease.epoch), first_lease)

    _, _, second_lease, second = prepare(engine)
    scope = second.scope.model_copy(
        update={"target": ActionTarget(kind="email", reference="dedicated-fixture-mailbox/item-2")}
    )
    second_lease = journal.issue_lease(scope, second_lease.access, second_lease.plan_version)
    second = second.model_copy(update={"scope": scope, "idempotency_key": "following-action"})
    approve(
        journal, journal.submit(second, second_lease.lease_id, second_lease.epoch), second_lease
    )
    engine.artifacts.path(payload).unlink()

    observed = []

    class RecordingTransport(Transport):
        def observe(self, intent):
            observed.append(intent.action_id)
            return super().observe(intent)

    transport = RecordingTransport()
    dispatcher = GeneralActionDispatcher(engine, transport)
    assert dispatcher.tick() == first.action_id
    assert journal.get(second.action_id).status == "ready"
    assert transport.sent == 0 and observed == []

    assert dispatcher.tick() == second.action_id
    assert dispatcher.tick() is None
    assert transport.sent == 1 and observed == [second.action_id]
    assert journal.get(second.action_id).status == "succeeded"
    failed = journal.get(first.action_id)
    assert failed.status == "failed" and failed.attempt_id is None and failed.receipt is None
    events = journal.events(first.action_id)
    assert [item["kind"] for item in events] == ["submitted", "approved", "failed"]
    assert events[-1]["payload"] == {
        "stage": "payload_preflight",
        "reason": "ARTIFACT_MISSING",
        "payload_sha256": payload,
    }
    with engine.transaction() as conn:
        assert journal.spent(conn, task.task_id, task.revision) == Resources()
