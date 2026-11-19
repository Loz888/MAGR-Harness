"""Universal action contracts and the Engine-backed action broker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.general_agent.capabilities import (
    CapabilityAccess,
    CapabilityDescriptor,
    Resources,
    Risk,
)
from mgar_v3.general_agent.contracts import Identifier, Revision, Sha256, TaskSnapshot, Text
from mgar_v3.harness.tenancy import require_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row

EffectKind = Literal["read", "local_write", "external_write", "publish", "delete", "financial"]
ApprovalLevel = Literal["none", "operator", "elevated"]
ActionMode = Literal["fixture", "live"]
ActionStatus = Literal[
    "review", "ready", "dispatching", "unknown", "succeeded", "failed", "cancelled"
]
_APPROVAL = {"none": 0, "operator": 1, "elevated": 2}
_RISK = {"low": 0, "medium": 1, "high": 2}
_DESCRIPTOR_EFFECT = {
    "read": "read_only",
    "local_write": "workspace_write",
    "external_write": "external_write",
    "publish": "external_write",
    "delete": "external_write",
    "financial": "external_write",
}
_ACTIVE = {"dispatching", "unknown"}
_ACTION = (
    "SELECT i.*,o.status,o.attempt_id,o.attempt_expires_at,o.reservation "
    "FROM app.general_action_intents i JOIN app.general_action_outbox o USING (action_id) "
)


class ActionTarget(Model):
    kind: Literal["file", "email", "calendar", "browser", "github", "ledger"]
    reference: Text


class ActionScope(Model):
    actor: Identifier
    capability: Identifier
    provider_id: Identifier
    target: ActionTarget
    operation: Identifier
    effect: EffectKind
    mode: ActionMode
    compensable: Annotated[bool, Field(strict=True)] = False


class ActionIntent(Model):
    schema_version: Literal["general-action-v1"] = "general-action-v1"
    action_id: UUID
    task_id: UUID
    task_revision: Revision
    plan_version: Revision
    scope: ActionScope
    expected_effect: Text
    idempotency_key: Identifier
    risk: Risk
    approval: ApprovalLevel = "none"
    precondition_sha256: Sha256 | None = None
    payload_sha256: Sha256 | None = None
    compensation: Text | None = None

    def identity(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"action_id"}))


class ActionLease(Model):
    lease_id: UUID
    epoch: Revision
    plan_version: Revision
    access: CapabilityAccess
    scope: ActionScope
    expires_at: AwareDatetime


class ActionApproval(Model):
    intent_sha256: Sha256
    lease_id: UUID
    epoch: Revision
    reviewer: Identifier
    level: Literal["operator", "elevated"]


class ActionReceipt(Model):
    schema_version: Literal["general-action-receipt-v1"] = "general-action-receipt-v1"
    action_id: UUID
    intent_sha256: Sha256
    provider_id: Identifier
    target: ActionTarget
    idempotency_key: Identifier
    external_id: Text
    recorded_at: AwareDatetime


class ActionRecord(Model):
    intent: ActionIntent
    intent_sha256: Sha256
    lease_id: UUID
    required_approval: ApprovalLevel
    status: ActionStatus
    attempt_id: UUID | None
    receipt: ActionReceipt | None
    receipt_sha256: Sha256 | None


def _record(row: Row) -> ActionRecord:
    return ActionRecord.model_validate(
        {
            key: row[key]
            for key in (
                "intent",
                "intent_sha256",
                "lease_id",
                "required_approval",
                "status",
                "attempt_id",
                "receipt",
                "receipt_sha256",
            )
        }
    )


def _denied(message: str) -> DomainError:
    return DomainError("GENERAL_ACTION_DENIED", message, 403)


class GeneralActionStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _row(conn: Connection, action_id: UUID) -> Row:
        row = conn.execute(_ACTION + "WHERE i.action_id=%s", (action_id,)).fetchone()
        if row is None:
            raise DomainError("GENERAL_ACTION_NOT_FOUND", "Action was not found.", 404)
        return row

    @staticmethod
    def _event(conn: Connection, action_id: UUID, kind: str, **payload: Any) -> None:
        conn.execute(
            "INSERT INTO app.general_action_events(action_id,kind,payload) VALUES (%s,%s,%s)",
            (action_id, kind, Jsonb(payload)),
        )

    @staticmethod
    def _state(conn: Connection, action_id: UUID, status: ActionStatus) -> None:
        conn.execute(
            "UPDATE app.general_action_outbox SET status=%s WHERE action_id=%s", (status, action_id)
        )

    def get(self, action_id: UUID) -> ActionRecord:
        with self.engine.transaction() as conn:
            return _record(self._row(conn, action_id))

    def events(self, action_id: UUID) -> list[Row]:
        with self.engine.transaction() as conn:
            self._row(conn, action_id)
            return list(
                conn.execute(
                    "SELECT kind,payload,occurred_at FROM app.general_action_events "
                    "WHERE action_id=%s ORDER BY event_id",
                    (action_id,),
                ).fetchall()
            )

    def _authorize(
        self,
        conn: Connection,
        scope: ActionScope,
        access: CapabilityAccess,
        plan_version: int,
    ) -> tuple[TaskSnapshot, CapabilityDescriptor]:
        task = self.engine.general_tasks.lock_current(conn, access.task_id)
        now = datetime.now(UTC)
        if (
            (task.project_id, task.revision, task.plan_version)
            != (access.project_id, access.task_revision, plan_version)
            or task.state != "running"
            or (task.spec.deadline is not None and task.spec.deadline <= now)
        ):
            raise _denied("Task, revision, plan or deadline changed.")
        seconds = task.spec.budget.max_seconds
        if seconds is not None:
            started = conn.execute(
                "SELECT created_at FROM app.general_task_revisions "
                "WHERE task_id=%s AND revision=%s",
                (task.task_id, task.revision),
            ).fetchone()
            assert started is not None
            if (now - started["created_at"]).total_seconds() >= seconds:
                raise _denied("Task time budget expired.")
        actor = self.engine.settings.local_actor
        if task.membership_id is not None:
            member = require_membership(conn, task.project_id, str(task.membership_id), write=True)
            actor = str(member["user_id"])
        if actor != scope.actor:
            raise _denied("Action actor is outside the task scope.")
        descriptor = self.engine.general_graphs.registry.definition(scope.capability)
        if (
            descriptor is None
            or descriptor.id not in task.spec.allowed_capabilities
            or descriptor.provider_id != scope.provider_id
            or descriptor.identity() not in access.capability_pins
            or not set(descriptor.permissions) <= set(access.permissions)
            or descriptor.effect != _DESCRIPTOR_EFFECT[scope.effect]
            or _RISK[descriptor.risk] > _RISK[access.max_risk]
            or (
                task.spec.risk_level is not None
                and _RISK[descriptor.risk] > _RISK[task.spec.risk_level]
            )
            or (scope.effect == "local_write" and not access.workspace_write)
            or (
                descriptor.effect == "external_write"
                and task.spec.approval_policy == "deny_external_writes"
            )
            or self.engine.general_graphs.registry.health(descriptor.id) != "ready"
        ):
            raise _denied("Capability, permission or effect is outside the grant.")
        return task, descriptor

    def issue_lease(
        self,
        scope: ActionScope,
        access: CapabilityAccess,
        plan_version: int,
        *,
        ttl_seconds: int = 300,
        live_authorized: bool = False,
    ) -> ActionLease:
        """Called by a trusted owner service with a task-bound grant."""
        if not 1 <= ttl_seconds <= 3600 or (scope.mode == "live" and not live_authorized):
            raise _denied("Lease expiry or live authorization is invalid.")
        with self.engine.transaction() as conn:
            task, _ = self._authorize(conn, scope, access, plan_version)
            prior = conn.execute(
                "SELECT COALESCE(max(epoch),0) AS epoch FROM app.general_action_leases "
                "WHERE task_id=%s AND task_revision=%s AND capability=%s",
                (access.task_id, access.task_revision, scope.capability),
            ).fetchone()
            assert prior is not None
            expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
            if task.spec.deadline is not None:
                expires = min(expires, task.spec.deadline)
            lease = ActionLease(
                lease_id=uuid4(),
                epoch=int(prior["epoch"]) + 1,
                plan_version=plan_version,
                access=access,
                scope=scope,
                expires_at=expires,
            )
            conn.execute(
                "UPDATE app.general_action_leases SET revoked_at=now() WHERE task_id=%s "
                "AND task_revision=%s AND capability=%s AND revoked_at IS NULL",
                (task.task_id, task.revision, scope.capability),
            )
            conn.execute(
                "INSERT INTO app.general_action_leases "
                "(lease_id,task_id,task_revision,capability,epoch,grant_snapshot,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    lease.lease_id,
                    task.task_id,
                    task.revision,
                    scope.capability,
                    lease.epoch,
                    Jsonb(lease.model_dump(mode="json")),
                    expires,
                ),
            )
            return lease

    def revoke(self, lease_id: UUID) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.general_action_leases SET revoked_at=now() "
                "WHERE lease_id=%s AND revoked_at IS NULL",
                (lease_id,),
            )

    def _lease(
        self,
        conn: Connection,
        intent: ActionIntent,
        lease_id: UUID,
        epoch: int | None = None,
    ) -> tuple[TaskSnapshot, CapabilityDescriptor, ActionLease]:
        row = conn.execute(
            "SELECT grant_snapshot FROM app.general_action_leases WHERE lease_id=%s "
            "AND revoked_at IS NULL AND expires_at>now()",
            (lease_id,),
        ).fetchone()
        if row is None:
            raise _denied("Action lease expired or was revoked.")
        lease = ActionLease.model_validate(row["grant_snapshot"])
        if (intent.task_id, intent.task_revision, intent.plan_version, intent.scope) != (
            lease.access.task_id,
            lease.access.task_revision,
            lease.plan_version,
            lease.scope,
        ) or (epoch is not None and epoch != lease.epoch):
            raise _denied("Action does not match the lease, scope and epoch.")
        task, descriptor = self._authorize(conn, lease.scope, lease.access, lease.plan_version)
        if (
            _RISK[intent.risk] < _RISK[descriptor.risk]
            or _RISK[intent.risk] > _RISK[lease.access.max_risk]
            or (
                task.spec.risk_level is not None
                and _RISK[intent.risk] > _RISK[task.spec.risk_level]
            )
        ):
            raise _denied("Action risk is outside the grant.")
        return task, descriptor, lease

    @staticmethod
    def _required(intent: ActionIntent) -> ApprovalLevel:
        scope = intent.scope
        if scope.effect in {"publish", "delete", "financial"} or (
            scope.effect != "read" and (not scope.compensable or not intent.compensation)
        ):
            return "elevated"
        if scope.effect != "read" and intent.approval == "none":
            return "operator"
        return intent.approval

    def submit(self, intent: ActionIntent, lease_id: UUID, epoch: int) -> ActionRecord:
        with self.engine.transaction() as conn:
            task, _, _ = self._lease(conn, intent, lease_id, epoch)
            previous = conn.execute(
                _ACTION + "WHERE project_id=%s AND provider_id=%s AND execution_mode=%s "
                "AND idempotency_key=%s",
                (
                    task.project_id,
                    intent.scope.provider_id,
                    intent.scope.mode,
                    intent.idempotency_key,
                ),
            ).fetchone()
            identity = intent.identity()
            if previous is not None:
                if previous["intent_sha256"] != identity:
                    raise DomainError(
                        "GENERAL_ACTION_KEY_CONFLICT", "Idempotency key already bound.", 409
                    )
                return _record(previous)
            if conn.execute(
                "SELECT 1 FROM app.general_action_intents WHERE action_id=%s",
                (intent.action_id,),
            ).fetchone():
                raise DomainError("GENERAL_ACTION_ID_CONFLICT", "Action ID already bound.", 409)
            approval = self._required(intent)
            conn.execute(
                "INSERT INTO app.general_action_intents "
                "(action_id,task_id,task_revision,plan_version,project_id,provider_id,"
                "execution_mode,target_sha256,idempotency_key,intent_sha256,intent,lease_id,"
                "required_approval) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    intent.action_id,
                    task.task_id,
                    task.revision,
                    intent.plan_version,
                    task.project_id,
                    intent.scope.provider_id,
                    intent.scope.mode,
                    digest(intent.scope.target.model_dump(mode="json")),
                    intent.idempotency_key,
                    identity,
                    Jsonb(intent.model_dump(mode="json")),
                    lease_id,
                    approval,
                ),
            )
            conn.execute(
                "INSERT INTO app.general_action_outbox(action_id,status) VALUES (%s,%s)",
                (intent.action_id, "ready" if approval == "none" else "review"),
            )
            self._event(conn, intent.action_id, "submitted", intent_sha256=identity, epoch=epoch)
            return _record(self._row(conn, intent.action_id))

    def approve(self, action_id: UUID, approval: ActionApproval) -> ActionRecord:
        """The trusted reviewer port supplies the operator identity and approval level."""
        with self.engine.transaction() as conn:
            row = self._row(conn, action_id)
            record = _record(row)
            if (approval.intent_sha256, approval.lease_id) != (
                record.intent_sha256,
                record.lease_id,
            ):
                raise _denied("Approval does not bind this intent and lease.")
            self._lease(conn, record.intent, record.lease_id, approval.epoch)
            if (
                record.status != "review"
                or _APPROVAL[approval.level] < _APPROVAL[record.required_approval]
            ):
                raise _denied("Action requires a current approval with the correct level.")
            conn.execute(
                "UPDATE app.general_action_intents SET approval=%s WHERE action_id=%s",
                (Jsonb(approval.model_dump(mode="json")), action_id),
            )
            self._state(conn, action_id, "ready")
            self._event(conn, action_id, "approved", approval=approval.model_dump(mode="json"))
            return _record(self._row(conn, action_id))

    @staticmethod
    def spent(conn: Connection, task_id: UUID, revision: int) -> Resources:
        columns = ", ".join(
            f"COALESCE(sum((reservation->>'{field}')::numeric),0) AS {field}"
            for field in Resources.model_fields
        )
        row = conn.execute(
            f"SELECT {columns} FROM app.general_action_outbox JOIN app.general_action_intents "
            "USING (action_id) WHERE task_id=%s AND task_revision=%s",
            (task_id, revision),
        ).fetchone()
        assert row is not None
        return Resources.model_validate({key: int(amount) for key, amount in row.items()})

    def _reserve(
        self,
        conn: Connection,
        task: TaskSnapshot,
        lease: ActionLease,
        descriptor: CapabilityDescriptor,
    ) -> bool:
        spent = self.spent(conn, task.task_id, task.revision)
        graph = conn.execute(
            "SELECT snapshot->'spent' AS spent FROM app.general_task_graphs "
            "WHERE task_id=%s AND task_revision=%s",
            (task.task_id, task.revision),
        ).fetchone()
        node_spent = Resources.model_validate(graph["spent"]) if graph else Resources()
        limits = dict(
            zip(
                ("cost_micros", "model_calls", "tool_calls", "tokens"),
                (
                    task.spec.budget.max_cost_micros,
                    task.spec.budget.max_model_calls,
                    task.spec.budget.max_tool_calls,
                    task.spec.budget.max_total_tokens,
                ),
                strict=True,
            )
        )
        for key, cost in descriptor.upper_bound.model_dump().items():
            total = cost + getattr(spent, key) + getattr(node_spent, key)
            if total > getattr(lease.access.resources, key) or (
                limits[key] is not None and total > limits[key]
            ):
                return False
        return True

    def _expire(self, conn: Connection, mode: ActionMode, provider_id: str) -> None:
        rows = conn.execute(
            "UPDATE app.general_action_outbox o SET status='unknown' "
            "FROM app.general_action_intents i "
            "WHERE i.action_id=o.action_id AND i.execution_mode=%s AND i.provider_id=%s "
            "AND o.status='dispatching' AND o.attempt_expires_at<=now() RETURNING o.action_id",
            (mode, provider_id),
        ).fetchall()
        for row in rows:
            self._event(conn, row["action_id"], "unknown", reason="dispatch_lease_expired")

    def candidate(self, mode: ActionMode, provider_id: str) -> ActionIntent | None:
        with self.engine.transaction() as conn:
            self._expire(conn, mode, provider_id)
            rows = conn.execute(
                _ACTION + "WHERE i.execution_mode=%s AND i.provider_id=%s AND o.status='ready' "
                "AND NOT EXISTS (SELECT 1 FROM app.general_action_intents pending "
                "JOIN app.general_action_outbox p USING (action_id) "
                "WHERE pending.project_id=i.project_id AND pending.provider_id=i.provider_id "
                "AND pending.execution_mode=i.execution_mode "
                "AND pending.target_sha256=i.target_sha256 "
                "AND p.status IN ('dispatching','unknown')) "
                "ORDER BY i.created_at,i.action_id LIMIT 32",
                (mode, provider_id),
            ).fetchall()
            for row in rows:
                record = _record(row)
                try:
                    self._lease(conn, record.intent, record.lease_id)
                except DomainError as exc:
                    self._state(conn, record.intent.action_id, "cancelled")
                    self._event(conn, record.intent.action_id, "cancelled", reason=exc.code)
                else:
                    return record.intent
            return None

    def claim(self, action_id: UUID, observed_sha256: str | None) -> UUID | None:
        with self.engine.transaction() as conn:
            row = self._row(conn, action_id)
            record = _record(row)
            if record.status != "ready":
                return None
            try:
                task, descriptor, lease = self._lease(conn, record.intent, record.lease_id)
                approval = (
                    ActionApproval.model_validate(row["approval"]) if row["approval"] else None
                )
                if record.required_approval != "none" and (
                    approval is None
                    or (approval.intent_sha256, approval.lease_id, approval.epoch)
                    != (record.intent_sha256, record.lease_id, lease.epoch)
                    or _APPROVAL[approval.level] < _APPROVAL[record.required_approval]
                ):
                    raise _denied("Action approval expired or differs from the intent.")
                if record.intent.precondition_sha256 != observed_sha256:
                    raise _denied("Action precondition changed.")
                if not self._reserve(conn, task, lease, descriptor):
                    raise _denied("Action budget is exhausted.")
            except DomainError as exc:
                self._state(conn, action_id, "cancelled")
                self._event(conn, action_id, "cancelled", reason=exc.code, detail=str(exc))
                return None
            if conn.execute(
                _ACTION + "WHERE project_id=%s AND provider_id=%s AND execution_mode=%s "
                "AND target_sha256=%s AND o.status IN ('dispatching','unknown') LIMIT 1",
                (
                    row["project_id"],
                    row["provider_id"],
                    row["execution_mode"],
                    row["target_sha256"],
                ),
            ).fetchone():
                return None
            attempt = uuid4()
            conn.execute(
                "UPDATE app.general_action_outbox SET status='dispatching',attempt_id=%s,"
                "attempt_expires_at=%s,reservation=%s WHERE action_id=%s",
                (
                    attempt,
                    datetime.now(UTC) + timedelta(seconds=descriptor.timeout_seconds),
                    Jsonb(descriptor.upper_bound.model_dump(mode="json")),
                    action_id,
                ),
            )
            self._event(conn, action_id, "dispatching", attempt_id=str(attempt), epoch=lease.epoch)
            return attempt

    def result(
        self,
        action_id: UUID,
        attempt_id: UUID,
        *,
        receipt: ActionReceipt | None = None,
        definitive_failure: bool = False,
    ) -> ActionRecord:
        with self.engine.transaction() as conn:
            record = _record(self._row(conn, action_id))
            intent = record.intent
            if record.attempt_id != attempt_id:
                raise _denied("Dispatch receipt does not own the attempt.")
            if record.status == "succeeded" and receipt == record.receipt:
                return record
            if record.status not in _ACTIVE or (definitive_failure and receipt is not None):
                raise _denied("The action has no matching pending attempt.")
            if receipt is not None:
                if (
                    receipt.action_id,
                    receipt.intent_sha256,
                    receipt.provider_id,
                    receipt.target,
                    receipt.idempotency_key,
                ) != (
                    action_id,
                    record.intent_sha256,
                    intent.scope.provider_id,
                    intent.scope.target,
                    intent.idempotency_key,
                ):
                    raise _denied("Receipt differs from the original action.")
                payload = receipt.model_dump(mode="json")
                binding = digest(payload)
                conn.execute(
                    "UPDATE app.general_action_intents SET receipt=%s,receipt_sha256=%s "
                    "WHERE action_id=%s",
                    (Jsonb(payload), binding, action_id),
                )
                self._state(conn, action_id, "succeeded")
                self._event(conn, action_id, "succeeded", receipt_sha256=binding)
            else:
                status: ActionStatus = "failed" if definitive_failure else "unknown"
                self._state(conn, action_id, status)
                self._event(conn, action_id, status, attempt_id=str(attempt_id))
            return _record(self._row(conn, action_id))

    def reconcilable(self, action_id: UUID, mode: ActionMode, provider_id: str) -> ActionRecord:
        with self.engine.transaction() as conn:
            self._expire(conn, mode, provider_id)
            record = _record(self._row(conn, action_id))
            scope = record.intent.scope
            if (scope.mode, scope.provider_id) != (mode, provider_id) or record.status != "unknown":
                raise _denied("Reconciliation can only inspect an unknown action on its provider.")
            return record
