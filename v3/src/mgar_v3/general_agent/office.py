"""Draft-first Office workflows over per-account adapters and the GA04 action broker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    model_validator,
)

from mgar_v3.domain import DomainError, Model
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.general_agent.actions import (
    ActionIntent,
    ActionLease,
    ActionMode,
    ActionReceipt,
    ActionRecord,
    ActionTarget,
    EffectKind,
)
from mgar_v3.general_agent.contracts import Identifier, Sha256, Text, unique
from mgar_v3.general_agent.executors import ExecutorHandler
from mgar_v3.general_agent.graph import BranchValue, GraphNode
from mgar_v3.general_agent.graph_store import NodeClaim
from mgar_v3.harness.tenancy import require_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.general_agent.contracts import TaskSnapshot

Kind = Literal["email", "calendar", "document", "spreadsheet"]
WriteOperation = Literal["send", "create_event", "update_event", "share", "delete"]
Operation = Literal["read", "draft", "send", "create_event", "update_event", "share", "delete"]
Email = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=254,
        pattern=r"^[A-Za-z0-9_+.%'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    ),
]
_EVENTS = {"create_event", "update_event"}


class OfficeResource(Model):
    kind: Kind
    external_id: Identifier
    parent_id: Identifier | None = None
    cell_range: Text | None = None

    @model_validator(mode="after")
    def scoped_options(self) -> Self:
        if self.parent_id is not None and self.kind != "calendar":
            raise ValueError("Only calendar events have a parent ID.")
        if self.cell_range is not None and self.kind != "spreadsheet":
            raise ValueError("Only spreadsheets use cell ranges.")
        return self

    def target(self, account: str) -> ActionTarget:
        reference = "/".join((self.kind, account, self.parent_id or "-", self.external_id))
        if self.cell_range:
            reference += "/" + self.cell_range
        return ActionTarget(
            kind=self.kind if self.kind in {"email", "calendar"} else "file",
            reference=reference,
        )


class OfficeScope(Model):
    operation: Operation
    resource: OfficeResource


class OfficeGrant(Model):
    """Supplied by a trusted account adapter, using exact operation and target scopes."""

    account: Identifier
    actor: Identifier
    project_id: Identifier
    membership_id: UUID | None = None
    scopes: Annotated[tuple[OfficeScope, ...], Field(min_length=1, max_length=50)]
    expires_at: AwareDatetime


class OfficeSnapshot(Model):
    resource: OfficeResource
    version: Identifier
    observed_at: AwareDatetime
    content: JsonValue


class DraftMail(Model):
    subject: Annotated[
        str, StringConstraints(min_length=1, max_length=240, pattern=r"^[^\r\n\x00]+$")
    ]
    body: Annotated[str, StringConstraints(min_length=1, max_length=40000, pattern=r"^[^\x00]+$")]
    to: Annotated[tuple[Email, ...], Field(min_length=1, max_length=30)]
    cc: Annotated[tuple[Email, ...], Field(max_length=20)] = ()
    bcc: Annotated[tuple[Email, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def unique_recipients(self) -> Self:
        unique(
            tuple(address.casefold() for address in (*self.to, *self.cc, *self.bcc)), "recipients"
        )
        return self


class StoredOfficeDraft(Model):
    schema_version: Literal["office-draft-v1"] = "office-draft-v1"
    account: Identifier
    actor: Identifier
    project_id: Identifier
    resource: OfficeResource
    draft: DraftMail


class Recurrence(Model):
    frequency: Literal["DAILY", "WEEKLY"]
    count: Annotated[int, Field(strict=True, ge=2, le=52)]


class CalendarEvent(Model):
    title: Text
    start: AwareDatetime
    end: AwareDatetime
    timezone: Text
    attendees: Annotated[tuple[Email, ...], Field(max_length=50)] = ()
    recurrence: Recurrence | None = None

    @model_validator(mode="after")
    def check_time(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Unknown IANA timezone.") from exc
        duration = self.end.astimezone(UTC) - self.start.astimezone(UTC)
        if not timedelta(0) < duration <= timedelta(days=366):
            raise ValueError("Event end must follow its start.")
        if any(
            value.astimezone(zone).utcoffset() != value.utcoffset()
            for value in (self.start, self.end)
        ):
            raise ValueError("Event offsets must match its timezone at each instant.")
        unique(tuple(address.casefold() for address in self.attendees), "attendees")
        return self


class SendMail(Model):
    operation: Literal["send"] = "send"
    resource: OfficeResource
    draft_sha256: Sha256


class ChangeCalendar(Model):
    operation: Literal["create_event", "update_event"]
    resource: OfficeResource
    event: CalendarEvent


class ShareOfficeFile(Model):
    operation: Literal["share"] = "share"
    resource: OfficeResource
    recipient: Email
    role: Literal["reader", "writer"] = "reader"


class DeleteOfficeItem(Model):
    operation: Literal["delete"] = "delete"
    resource: OfficeResource


OfficeChange = Annotated[
    SendMail | ChangeCalendar | ShareOfficeFile | DeleteOfficeItem,
    Field(discriminator="operation"),
]


class OfficeChangeEnvelope(RootModel[OfficeChange]):
    pass


class OfficeConnector(Protocol):
    """Account-bound OAuth or trusted MCP client. Version checks are read-only."""

    provider_id: str
    account: str
    mode: ActionMode

    def read(self, resource: OfficeResource) -> OfficeSnapshot: ...
    def observe(self, intent: ActionIntent) -> str | None: ...
    def write(self, change: OfficeChange, intent: ActionIntent, draft: DraftMail | None) -> str: ...
    def find(self, intent: ActionIntent) -> str | None: ...


def _denied() -> DomainError:
    return DomainError("OFFICE_SCOPE_DENIED", "Office operation is outside the account grant.", 403)


def _shape(change: OfficeChange) -> None:
    resource = change.resource
    op = change.operation
    if (
        (op == "send" and (resource.kind != "email" or resource.external_id != "outbound"))
        or (op in _EVENTS and resource.kind != "calendar")
        or (op == "create_event" and resource.parent_id is not None)
        or (op == "update_event" and resource.parent_id is None)
        or (op == "share" and resource.kind not in {"document", "spreadsheet"})
        or (op == "delete" and resource.kind == "calendar" and resource.parent_id is None)
        or resource.cell_range is not None
    ):
        raise _denied()


def _effect(change: OfficeChange) -> EffectKind:
    return "delete" if change.operation == "delete" else "external_write"


class OfficeExecutor:
    def __init__(self, engine: Engine, connector: OfficeConnector) -> None:
        self.engine, self.connector = engine, connector

    def _require(self, grant: OfficeGrant, operation: Operation, resource: OfficeResource) -> None:
        if (
            grant.account != self.connector.account
            or grant.expires_at <= datetime.now(UTC)
            or OfficeScope(operation=operation, resource=resource) not in grant.scopes
        ):
            raise _denied()
        if grant.membership_id is None:
            if grant.actor != self.engine.settings.local_actor:
                raise _denied()
        else:
            with self.engine.transaction() as conn:
                member = require_membership(
                    conn,
                    grant.project_id,
                    str(grant.membership_id),
                    write=operation != "read",
                )
                if str(member["user_id"]) != grant.actor:
                    raise _denied()

    def read(self, grant: OfficeGrant, resource: OfficeResource) -> OfficeSnapshot:
        self._require(grant, "read", resource)
        snapshot = self.connector.read(resource)
        self._require(grant, "read", resource)
        if snapshot.resource != resource or snapshot.observed_at > datetime.now(UTC):
            raise DomainError(
                "OFFICE_RECEIPT_SCOPE", "Provider returned a different resource.", 502
            )
        return snapshot

    def draft(self, grant: OfficeGrant, resource: OfficeResource, mail: DraftMail) -> str:
        self._require(grant, "draft", resource)
        if resource.kind != "email" or resource.external_id != "outbound":
            raise _denied()
        draft = StoredOfficeDraft(
            account=grant.account,
            actor=grant.actor,
            project_id=grant.project_id,
            resource=resource,
            draft=mail,
        )
        return self.engine.artifacts.put(draft.model_dump_json().encode())

    def _draft(
        self,
        change: SendMail,
        *,
        account: str,
        actor: str,
        project_id: str,
    ) -> DraftMail:
        value = StoredOfficeDraft.model_validate_json(
            self.engine.artifacts.read(change.draft_sha256)
        )
        if (value.account, value.actor, value.project_id, value.resource) != (
            account,
            actor,
            project_id,
            change.resource,
        ):
            raise _denied()
        return value.draft

    def queue(
        self,
        grant: OfficeGrant,
        lease: ActionLease,
        change: OfficeChange,
        *,
        idempotency_key: str,
        precondition_sha256: str | None = None,
    ) -> ActionRecord:
        self._require(grant, change.operation, change.resource)
        _shape(change)
        if (
            (lease.scope.actor, lease.access.project_id, lease.scope.provider_id, lease.scope.mode)
            != (grant.actor, grant.project_id, self.connector.provider_id, self.connector.mode)
            or lease.scope.target != change.resource.target(grant.account)
            or lease.scope.operation != change.operation
            or lease.scope.effect != _effect(change)
            or lease.expires_at > grant.expires_at
        ):
            raise _denied()
        if change.operation in {"update_event", "share", "delete"} and precondition_sha256 is None:
            raise DomainError("OFFICE_PRECONDITION_REQUIRED", "Resource version is required.", 422)
        if isinstance(change, SendMail):
            self._draft(
                change, account=grant.account, actor=grant.actor, project_id=grant.project_id
            )
        descriptor = self.engine.general_graphs.registry.definition(lease.scope.capability)
        if descriptor is None:
            raise _denied()
        payload = self.engine.artifacts.put(change.model_dump_json().encode())
        intent = ActionIntent(
            action_id=uuid4(),
            task_id=lease.access.task_id,
            task_revision=lease.access.task_revision,
            plan_version=lease.plan_version,
            scope=lease.scope,
            idempotency_key=idempotency_key,
            risk=descriptor.risk,
            approval="operator",
            expected_effect="Execute approved Office " + change.operation,
            compensation="Review reversal separately" if lease.scope.compensable else None,
            precondition_sha256=precondition_sha256,
            payload_sha256=payload,
        )
        return self.engine.general_actions.submit(intent, lease.lease_id, lease.epoch)

    def read_handler(self, grant: OfficeGrant, resource: OfficeResource) -> ExecutorHandler:
        def handle(task: TaskSnapshot, node: GraphNode, claim: NodeClaim) -> dict[str, BranchValue]:
            bound_reference = resource.target(grant.account).reference
            sources = tuple(
                item
                for item in task.spec.source_refs
                if item.kind in {"document", "artifact"} and item.reference == bound_reference
            )
            if (
                claim.capability_id != "office.read"
                or task.project_id != grant.project_id
                or task.membership_id != grant.membership_id
                or len(node.outputs) != 1
                or not sources
            ):
                raise _denied()
            snapshot = self.read(grant, resource)
            if not any(
                source.version is None or source.version == snapshot.version for source in sources
            ):
                raise DomainError(
                    "OFFICE_SOURCE_VERSION", "The Office source version changed.", 409
                )
            return {node.outputs[0]: self.engine.artifacts.put(snapshot.model_dump_json().encode())}

        return handle

    def draft_handler(
        self, grant: OfficeGrant, resource: OfficeResource, mail: DraftMail
    ) -> ExecutorHandler:
        def handle(task: TaskSnapshot, node: GraphNode, claim: NodeClaim) -> dict[str, BranchValue]:
            if (
                claim.capability_id != "office.draft"
                or task.project_id != grant.project_id
                or task.membership_id != grant.membership_id
                or len(node.outputs) != 1
                or not any(
                    item.id == node.outputs[0] and item.kind == "draft"
                    for item in task.spec.deliverables
                )
            ):
                raise _denied()
            return {node.outputs[0]: self.draft(grant, resource, mail)}

        return handle

    def send(self, intent: ActionIntent, payload: bytes | None) -> ActionReceipt:
        if payload is None:
            raise DefinitiveFailure("Office action has no payload")
        try:
            change = OfficeChangeEnvelope.model_validate_json(payload).root
            _shape(change)
            if (
                intent.scope.target != change.resource.target(self.connector.account)
                or intent.scope.operation != change.operation
                or intent.scope.effect != _effect(change)
                or intent.scope.provider_id != self.connector.provider_id
                or intent.scope.mode != self.connector.mode
            ):
                raise _denied()
            draft = None
            if isinstance(change, SendMail):
                task = self.engine.general_tasks.get(intent.task_id, revision=intent.task_revision)
                draft = self._draft(
                    change,
                    account=self.connector.account,
                    actor=intent.scope.actor,
                    project_id=task.project_id,
                )
        except (DomainError, ValueError) as exc:
            raise DefinitiveFailure("Office payload does not match the approved action") from exc
        return self._receipt(intent, self.connector.write(change, intent, draft))

    @property
    def mode(self) -> ActionMode:
        return self.connector.mode

    @property
    def provider_id(self) -> str:
        return self.connector.provider_id

    def observe(self, intent: ActionIntent) -> str | None:
        return self.connector.observe(intent)

    def find(self, intent: ActionIntent) -> ActionReceipt | None:
        external = self.connector.find(intent)
        return None if external is None else self._receipt(intent, external)

    @staticmethod
    def _receipt(intent: ActionIntent, external: str) -> ActionReceipt:
        return ActionReceipt(
            action_id=intent.action_id,
            intent_sha256=intent.identity(),
            provider_id=intent.scope.provider_id,
            target=intent.scope.target,
            idempotency_key=intent.idempotency_key,
            external_id=external,
            recorded_at=datetime.now(UTC),
        )
