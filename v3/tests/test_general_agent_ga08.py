"""Office drafts, scoped read adapters, timezone checks and the GA04 outbox."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_general_agent_ga01 import create, spec
from test_general_agent_ga02 import capability, grant, graph, node
from test_general_agent_ga10 import seed

from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import GeneralActionDispatcher
from mgar_v3.general_agent.actions import ActionApproval, ActionScope
from mgar_v3.general_agent.capabilities import CapabilityRegistry
from mgar_v3.general_agent.contracts import SourceRef, TaskPlan
from mgar_v3.general_agent.office import (
    CalendarEvent,
    ChangeCalendar,
    DeleteOfficeItem,
    DraftMail,
    OfficeExecutor,
    OfficeGrant,
    OfficeResource,
    OfficeScope,
    OfficeSnapshot,
    Recurrence,
    SendMail,
    ShareOfficeFile,
)

PROVIDER = "office-fixture"
ACCOUNT = "dedicated-fixture-account"
OUTBOUND = OfficeResource(kind="email", external_id="outbound")
CALENDAR = OfficeResource(kind="calendar", external_id="team-calendar")
DOCUMENT = OfficeResource(kind="document", external_id="selected-document")
VERSION = digest("resource-v1")


class Connector:
    mode = "fixture"
    account = ACCOUNT
    provider_id = PROVIDER

    def __init__(self, *, timeout=False):
        self.timeout = timeout
        self.reads = []
        self.writes = []
        self.external = {}

    def read(self, resource):
        self.reads.append(resource)
        return OfficeSnapshot(
            resource=resource,
            observed_at=datetime.now(UTC),
            version="v1",
            content={"title": "Fixture office record", "kind": resource.kind},
        )

    def observe(self, intent):
        return VERSION

    def write(self, change, intent, draft):
        self.writes.append((change, intent.idempotency_key, draft))
        value = self.external.setdefault(intent.idempotency_key, f"external-{change.operation}")
        if self.timeout:
            raise TimeoutError("fixture lost its response")
        return value

    def find(self, intent):
        return self.external.get(intent.idempotency_key)


def scoped(*granted, actor="owner", membership_id=None, project_id="project"):
    return OfficeGrant(
        account=ACCOUNT,
        actor=actor,
        project_id=project_id,
        membership_id=membership_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        scopes=tuple(OfficeScope(operation=op, resource=target) for op, target in granted),
    )


def lease_for(engine, operation, resource, *, effect="external_write", compensable=True):
    key = "office." + operation
    descriptor = capability(key, provider_id=PROVIDER, effect="external_write")
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    value = spec("office").model_copy(
        update={
            "allowed_capabilities": (key,),
            "approval_policy": "manual",
        }
    )
    task = create(engine.general_tasks, value)
    planned = engine.general_tasks.save_plan(
        task.task_id,
        1,
        1,
        TaskPlan(task_revision=1, steps=("Prepare a reviewed change",)),
    )
    task = engine.general_tasks.transition(task.task_id, 1, planned.version, "running")
    scope = ActionScope(
        actor="owner",
        capability=key,
        provider_id=PROVIDER,
        target=resource.target(ACCOUNT),
        operation=operation,
        effect=effect,
        mode="fixture",
        compensable=compensable,
    )
    return engine.general_actions.issue_lease(scope, grant(task, descriptor), task.plan_version)


def approve(engine, action, lease, *, level="operator"):
    return engine.general_actions.approve(
        action.intent.action_id,
        ActionApproval(
            intent_sha256=action.intent_sha256,
            lease_id=lease.lease_id,
            epoch=lease.epoch,
            reviewer="fixture-reviewer",
            level=level,
        ),
    )


@pytest.mark.parametrize("kind", ["email", "calendar", "document", "spreadsheet"])
def test_read_is_exact_scoped_and_does_not_write(engine, kind):
    connector = Connector()
    executor = OfficeExecutor(engine, connector)
    resource = OfficeResource(kind=kind, external_id="granted")
    access = scoped(("read", resource))
    item = executor.read(access, resource)
    assert item.resource == resource and item.version == "v1"
    with pytest.raises(DomainError):
        executor.read(access, resource.model_copy(update={"external_id": "outside"}))
    assert connector.writes == [] and connector.reads == [resource]


def test_graph_read_checks_declared_source_version_and_produces_a_bound_snapshot(engine):
    provider = Connector()
    executor = OfficeExecutor(engine, provider)
    descriptor = capability("office.read", provider_id=PROVIDER)
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    value = spec("office").model_copy(
        update={
            "allowed_capabilities": (descriptor.id,),
            "source_refs": (
                SourceRef(
                    kind="document", reference=DOCUMENT.target(ACCOUNT).reference, version="v1"
                ),
            ),
        }
    )
    task = create(engine.general_tasks, value)
    state = engine.general_graphs.install(
        task.task_id,
        1,
        1,
        graph(node("read", domain="office", capabilities=(descriptor.id,), outputs=("result",))),
    )
    result = engine.general_executors.run(
        task.task_id,
        1,
        state.graph_revision,
        "read",
        grant(task, descriptor),
        execute=executor.read_handler(scoped(("read", DOCUMENT)), DOCUMENT),
    )
    snapshot = OfficeSnapshot.model_validate_json(
        engine.artifacts.read(result.progress["read"].outputs["result"]),
    )
    assert snapshot.resource == DOCUMENT and provider.writes == []


def test_draft_is_local_and_broker_approval_and_reconciliation_control_send(engine):
    connector = Connector(timeout=True)
    executor = OfficeExecutor(engine, connector)
    only_draft = scoped(("draft", OUTBOUND))
    draft = DraftMail(subject="Review", body="Fixture message", to=("alice@example.test",))
    draft_hash = executor.draft(only_draft, OUTBOUND, draft)
    lease = lease_for(engine, "send", OUTBOUND)
    change = SendMail(resource=OUTBOUND, draft_sha256=draft_hash)
    with pytest.raises(DomainError):
        executor.queue(only_draft, lease, change, idempotency_key="fixture-send")
    can_send = scoped(("send", OUTBOUND))
    queued = executor.queue(can_send, lease, change, idempotency_key="fixture-send")
    dispatch = GeneralActionDispatcher(engine, executor)
    assert dispatch.tick() is None and connector.writes == []
    approve(engine, queued, lease)
    assert dispatch.tick() == queued.intent.action_id and len(connector.writes) == 1
    assert connector.writes[0][2] == draft
    assert engine.general_actions.get(queued.intent.action_id).status == "unknown"
    assert dispatch.tick() is None
    assert dispatch.reconcile(queued.intent.action_id)
    result = engine.general_actions.get(queued.intent.action_id)
    assert result.receipt.external_id == "external-send" and len(connector.writes) == 1


def test_calendar_dst_attendee_dedupe_and_idempotency(engine):
    event = CalendarEvent(
        title="Across the clock change",
        start="2026-03-08T01:30:00-05:00",
        end="2026-03-08T03:30:00-04:00",
        timezone="America/New_York",
        attendees=("person@example.test",),
        recurrence=Recurrence(frequency="WEEKLY", count=3),
    )
    with pytest.raises(ValidationError):
        CalendarEvent.model_validate(
            {
                **event.model_dump(mode="json"),
                "start": "2026-03-08T02:30:00-05:00",
            }
        )
    with pytest.raises(ValidationError):
        CalendarEvent.model_validate(
            {
                **event.model_dump(mode="json"),
                "attendees": ["person@example.test", "PERSON@example.test"],
            }
        )
    connector = Connector()
    executor = OfficeExecutor(engine, connector)
    access = scoped(("create_event", CALENDAR))
    lease = lease_for(engine, "create_event", CALENDAR)
    change = ChangeCalendar(operation="create_event", resource=CALENDAR, event=event)
    action = executor.queue(access, lease, change, idempotency_key="calendar-once")
    assert executor.queue(access, lease, change, idempotency_key="calendar-once") == action
    approve(engine, action, lease)
    assert GeneralActionDispatcher(engine, executor).tick() == action.intent.action_id
    assert connector.writes == [(change, "calendar-once", None)]


def test_mutations_require_distinct_operation_scope_current_version_and_higher_delete_approval(
    engine,
):
    provider = Connector()
    executor = OfficeExecutor(engine, provider)
    access = scoped(("share", DOCUMENT), ("delete", DOCUMENT))
    share = ShareOfficeFile(resource=DOCUMENT, recipient="reader@example.test")
    lease = lease_for(engine, "share", DOCUMENT)
    with pytest.raises(DomainError) as denied:
        executor.queue(access, lease, share, idempotency_key="file-share")
    assert denied.value.code == "OFFICE_PRECONDITION_REQUIRED"
    queued = executor.queue(
        access, lease, share, idempotency_key="file-share", precondition_sha256=VERSION
    )
    approve(engine, queued, lease)
    assert GeneralActionDispatcher(engine, executor).tick() == queued.intent.action_id
    deleted = DeleteOfficeItem(resource=DOCUMENT)
    lease = lease_for(engine, "delete", DOCUMENT, effect="delete", compensable=False)
    action = executor.queue(
        access,
        lease,
        deleted,
        idempotency_key="file-delete",
        precondition_sha256=VERSION,
    )
    assert action.required_approval == "elevated"
    with pytest.raises(DomainError):
        approve(engine, action, lease)
    approve(engine, action, lease, level="elevated")
    assert GeneralActionDispatcher(engine, executor).tick() == action.intent.action_id


def test_office_resource_access_fences_revoked_project_membership(engine):
    alice, _, memberships = seed(engine)
    membership = memberships[alice, "north"]
    access = scoped(
        ("read", DOCUMENT), actor=str(alice), membership_id=membership, project_id="north"
    )
    connector = Connector()
    executor = OfficeExecutor(engine, connector)
    executor.read(access, DOCUMENT)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.harness_project_memberships SET revoked_at=now() WHERE membership_id=%s",
            (membership,),
        )
    with pytest.raises(DomainError):
        executor.read(access, DOCUMENT)
    assert connector.reads == [DOCUMENT]


def test_a_grant_revoked_during_read_does_not_release_the_snapshot(engine):
    alice, _, memberships = seed(engine)
    membership = memberships[alice, "north"]
    access = scoped(
        ("read", DOCUMENT),
        actor=str(alice),
        membership_id=membership,
        project_id="north",
    )

    class RevokingConnector(Connector):
        def read(self, resource):
            item = super().read(resource)
            with engine.transaction() as conn:
                conn.execute(
                    "UPDATE app.harness_project_memberships SET revoked_at=now() "
                    "WHERE membership_id=%s",
                    (membership,),
                )
            return item

    provider = RevokingConnector()
    with pytest.raises(DomainError):
        OfficeExecutor(engine, provider).read(access, DOCUMENT)
    assert provider.reads == [DOCUMENT] and provider.writes == []


def test_same_event_id_in_another_calendar_cannot_satisfy_graph_source(engine):
    correct = OfficeResource(kind="calendar", external_id="shared-event-id", parent_id="north")
    other = correct.model_copy(update={"parent_id": "south"})
    provider = Connector()
    executor = OfficeExecutor(engine, provider)
    descriptor = capability("office.read", provider_id=PROVIDER)
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    value = spec("office").model_copy(
        update={
            "allowed_capabilities": (descriptor.id,),
            "source_refs": (
                SourceRef(
                    kind="document", reference=correct.target(ACCOUNT).reference, version="v1"
                ),
            ),
        }
    )
    task = create(engine.general_tasks, value)
    state = engine.general_graphs.install(
        task.task_id,
        1,
        1,
        graph(
            node(
                "read",
                domain="office",
                capabilities=(descriptor.id,),
                outputs=("result",),
            )
        ),
    )
    with pytest.raises(DomainError, match="Office operation"):
        engine.general_executors.run(
            task.task_id,
            1,
            state.graph_revision,
            "read",
            grant(task, descriptor),
            execute=executor.read_handler(scoped(("read", other)), other),
        )
    assert (
        provider.reads == []
        and engine.general_graphs.get(task.task_id).progress["read"].state == "failed"
    )
