"""Authoritative General Agent task records on the Engine-owned PG connection."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.general_agent.contracts import (
    GeneralTaskSpec,
    OutcomeContract,
    OutcomeVerificationReceipt,
    RecentTask,
    TaskPlan,
    TaskSnapshot,
    TaskState,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row

_SELECT = (
    "SELECT t.task_id,t.project_id,t.membership_id,t.owner_user_id,"
    "r.revision,r.version,r.state,r.reason,"
    "r.spec,r.plan,r.plan_version,r.outcome FROM app.general_tasks t "
    "JOIN app.general_task_revisions r ON r.task_id=t.task_id "
)
_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    "draft": {"ready", "waiting", "blocked", "cancelled"},
    "ready": {"running", "waiting", "blocked", "cancelled"},
    "running": {"waiting", "blocked", "verifying", "failed", "cancelled"},
    "waiting": {"ready", "blocked", "failed", "cancelled"},
    "blocked": {"ready", "waiting", "failed", "cancelled"},
    "verifying": {"running", "waiting", "blocked", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def _admission(spec: GeneralTaskSpec, draft: bool) -> tuple[TaskState, str | None]:
    if spec.clarifications():
        return "waiting", "clarification_required"
    if draft:
        return "draft", None
    if spec.deadline is not None and spec.deadline <= datetime.now(UTC):
        return "blocked", "deadline_expired"
    return "ready", None


def _snapshot(row: Row | None) -> TaskSnapshot:
    if row is None:
        raise DomainError("GENERAL_TASK_NOT_FOUND", "任务不存在。", 404)
    spec = GeneralTaskSpec.model_validate(row["spec"])
    return TaskSnapshot.model_validate(
        {**row, "spec": spec, "clarifications": spec.clarifications()}
    )


def _fence(snapshot: TaskSnapshot, revision: int, version: int) -> None:
    if (snapshot.revision, snapshot.version) != (revision, version):
        raise DomainError("GENERAL_TASK_STALE", "任务已更新，请读取当前版本。", 409)


class GeneralTaskStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def recent(self, user_id: UUID | None, all_projects: bool, limit: int) -> list[RecentTask]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT t.task_id,t.project_id,LEFT(r.spec->>'goal',160) AS goal,"
                "r.state,r.updated_at "
                "FROM app.general_tasks t JOIN app.general_task_revisions r "
                "ON (r.task_id,r.revision)=(t.task_id,t.current_revision) "
                "WHERE (t.owner_user_id=%s AND t.project_id IS NULL) "
                "OR (t.project_id IS NOT NULL AND (%s OR EXISTS ("
                "SELECT 1 FROM app.harness_project_memberships m "
                "WHERE m.membership_id=t.membership_id AND m.workspace_id=t.project_id "
                "AND m.user_id=%s::uuid AND m.revoked_at IS NULL))) "
                "ORDER BY r.updated_at DESC,t.task_id DESC LIMIT %s",
                (user_id, all_projects, user_id, limit),
            ).fetchall()
        return [RecentTask.model_validate(row) for row in rows]

    def get(self, task_id: UUID, *, revision: int | None = None) -> TaskSnapshot:
        with self.engine.transaction() as conn:
            found = conn.execute(
                _SELECT + "WHERE t.task_id=%s AND r.revision=COALESCE(%s,t.current_revision)",
                (task_id, revision),
            ).fetchone()
        return _snapshot(found)

    def _current(
        self,
        conn: Connection,
        task_id: UUID,
        revision: int,
        version: int,
    ) -> TaskSnapshot:
        current = self.lock_current(conn, task_id)
        _fence(current, revision, version)
        return current

    @staticmethod
    def lock_current(conn: Connection, task_id: UUID) -> TaskSnapshot:
        return _snapshot(
            conn.execute(
                _SELECT + "WHERE t.task_id=%s AND r.revision=t.current_revision FOR UPDATE OF t,r",
                (task_id,),
            ).fetchone()
        )

    @staticmethod
    def _insert_revision(
        conn: Connection,
        task_id: UUID,
        revision: int,
        spec: GeneralTaskSpec,
        draft: bool,
    ) -> None:
        state, reason = _admission(spec, draft)
        conn.execute(
            "INSERT INTO app.general_task_revisions(task_id,revision,spec,state,reason) "
            "VALUES (%s,%s,%s,%s,%s)",
            (task_id, revision, Jsonb(spec.model_dump(mode="json")), state, reason),
        )

    def create(
        self,
        project_id: str | None,
        membership_id: UUID | None,
        request_id: UUID,
        spec: GeneralTaskSpec,
        *,
        draft: bool = False,
        owner_user_id: UUID | None = None,
    ) -> TaskSnapshot:
        request_hash = digest({"spec": spec.model_dump(mode="json"), "draft": draft})
        with self.engine.transaction() as conn:
            if owner_user_id is not None:
                existing = conn.execute(
                    "SELECT task_id,request_hash FROM app.general_tasks "
                    "WHERE owner_user_id=%s AND request_id=%s", (owner_user_id, request_id),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT task_id,request_hash FROM app.general_tasks WHERE project_id=%s "
                    "AND owner_user_id IS NULL "
                    "AND membership_id IS NOT DISTINCT FROM %s::uuid AND request_id=%s",
                    (project_id, membership_id, request_id),
                ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "请求标识已绑定其他任务内容。", 409)
                return self.get(existing["task_id"])
            task_id = uuid4()
            conn.execute(
                "INSERT INTO app.general_tasks("
                "task_id,project_id,membership_id,owner_user_id,request_id,request_hash,"
                "current_revision) "
                "VALUES (%s,%s,%s,%s,%s,%s,1)",
                (task_id, project_id, membership_id, owner_user_id, request_id, request_hash),
            )
            self._insert_revision(conn, task_id, 1, spec, draft)
            return self.get(task_id)

    def attach_project(
        self, task_id: UUID, revision: int, version: int,
        project_id: str, membership_id: UUID | None,
    ) -> TaskSnapshot:
        with self.engine.transaction() as conn:
            task = self._current(conn, task_id, revision, version)
            if task.project_id is not None or task.state not in {"draft", "ready", "waiting"}:
                raise DomainError("GENERAL_TASK_SCOPE_FIXED", "当前任务无法更换项目。", 409)
            next_revision = revision + 1
            self._insert_revision(conn, task_id, next_revision, task.spec, task.state == "draft")
            conn.execute(
                "UPDATE app.general_tasks SET project_id=%s,membership_id=%s,current_revision=%s "
                "WHERE task_id=%s", (project_id, membership_id, next_revision, task_id),
            )
            return self.get(task_id)

    def revise(
        self,
        task_id: UUID,
        revision: int,
        version: int,
        spec: GeneralTaskSpec,
        *,
        draft: bool = False,
    ) -> TaskSnapshot:
        with self.engine.transaction() as conn:
            self._current(conn, task_id, revision, version)
            next_revision = revision + 1
            self._insert_revision(conn, task_id, next_revision, spec, draft)
            conn.execute(
                "UPDATE app.general_tasks SET current_revision=%s WHERE task_id=%s",
                (next_revision, task_id),
            )
            return self.get(task_id)

    def save_plan(
        self,
        task_id: UUID,
        revision: int,
        version: int,
        plan: TaskPlan,
        *,
        replace_running: bool = False,
    ) -> TaskSnapshot:
        with self.engine.transaction() as conn:
            current = self._current(conn, task_id, revision, version)
            if plan.task_revision != revision:
                raise DomainError("GENERAL_PLAN_STALE", "计划与任务修订不匹配。", 409)
            accepted_phase = current.state == "ready" or (
                replace_running and current.state == "running"
            )
            if not accepted_phase or _admission(current.spec, False)[0] != "ready":
                raise DomainError("GENERAL_TASK_NOT_READY", "任务尚不能接受计划。", 409)
            conn.execute(
                "UPDATE app.general_task_revisions SET plan=%s,plan_version=version+1,"
                "version=version+1,updated_at=now() WHERE task_id=%s AND revision=%s",
                (Jsonb(plan.model_dump(mode="json")), task_id, revision),
            )
            return self.get(task_id)

    def transition(
        self,
        task_id: UUID,
        revision: int,
        version: int,
        state: TaskState,
        *,
        reason: str | None = None,
    ) -> TaskSnapshot:
        if reason is not None and not re.fullmatch(r"[a-zA-Z0-9_:-]{1,100}", reason):
            raise DomainError("GENERAL_TASK_REASON_INVALID", "任务状态原因无效。", 422)
        with self.engine.transaction() as conn:
            current = self._current(conn, task_id, revision, version)
            if state not in _TRANSITIONS[current.state]:
                raise DomainError("GENERAL_TASK_TRANSITION_DENIED", "任务状态转换无效。", 409)
            if state in {"ready", "running"} and _admission(current.spec, False)[0] != "ready":
                raise DomainError("GENERAL_TASK_NOT_READY", "任务仍需澄清或已过截止时间。", 409)
            if state == "running" and current.plan is None:
                raise DomainError("GENERAL_PLAN_REQUIRED", "开始运行前需要当前修订的计划。", 409)
            if state == "waiting" and current.clarifications:
                reason = "clarification_required"
            conn.execute(
                "UPDATE app.general_task_revisions SET state=%s,reason=%s,version=version+1,"
                "updated_at=now() WHERE task_id=%s AND revision=%s",
                (state, reason, task_id, revision),
            )
            return self.get(task_id)

    def finish(
        self,
        task_id: UUID,
        revision: int,
        version: int,
        outcome: OutcomeContract,
    ) -> TaskSnapshot:
        current = self.get(task_id)
        _fence(current, revision, version)
        if current.state != "verifying":
            raise DomainError("GENERAL_TASK_TRANSITION_DENIED", "任务不在验证状态。", 409)

        # Reading bounded CAS objects stays outside the Engine write transaction.
        artifacts = self._check_outcome(current, outcome)
        with self.engine.transaction() as conn:
            current = self._current(conn, task_id, revision, version)
            self.engine.general_verification.require_attestation(conn, current, outcome)
            for content_hash, size in artifacts.items():
                conn.execute(
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (content_hash, size),
                )
            conn.execute(
                "UPDATE app.general_task_revisions SET outcome=%s,state='completed',reason=NULL,"
                "version=version+1,updated_at=now() WHERE task_id=%s AND revision=%s",
                (Jsonb(outcome.model_dump(mode="json")), task_id, revision),
            )
            return self.get(task_id)

    def _check_outcome(self, current: TaskSnapshot, outcome: OutcomeContract) -> dict[str, int]:
        def invalid() -> DomainError:
            return DomainError("GENERAL_OUTCOME_UNVERIFIED", "交付物或证据未绑定当前任务。", 409)

        deliverables = {item.deliverable_id: item.sha256 for item in outcome.deliverables}
        criteria = {item.id: item for item in current.spec.success_criteria}
        if (
            outcome.task_revision != current.revision
            or outcome.plan_version != current.plan_version
            or set(deliverables) != {item.id for item in current.spec.deliverables}
            or {item.criterion_id for item in outcome.evidence} != set(criteria)
        ):
            raise invalid()

        sizes: dict[str, int] = {}

        def checked_bytes(content_hash: str) -> bytes:
            try:
                content = self.engine.artifacts.read(content_hash)
            except DomainError as exc:
                raise invalid() from exc
            sizes[content_hash] = len(content)
            return content

        for item in outcome.evidence:
            criterion = criteria[item.criterion_id]
            try:
                receipt = OutcomeVerificationReceipt.model_validate_json(
                    checked_bytes(item.receipt_sha256)
                )
            except ValidationError as exc:
                raise invalid() from exc
            if (
                receipt.task_id != current.task_id
                or receipt.task_revision != current.revision
                or receipt.plan_version != current.plan_version
                or receipt.verification_version != current.version
                or receipt.criterion_id != criterion.id
                or receipt.criterion_sha256 != digest(criterion.model_dump(mode="json"))
                or {entry.deliverable_id: entry.sha256 for entry in receipt.deliverables}
                != {key: deliverables[key] for key in criterion.deliverable_ids}
            ):
                raise invalid()
            for evidence_hash in receipt.evidence_sha256:
                if evidence_hash not in sizes:
                    checked_bytes(evidence_hash)
        for artifact_hash in deliverables.values():
            if artifact_hash not in sizes:
                checked_bytes(artifact_hash)
        return sizes
