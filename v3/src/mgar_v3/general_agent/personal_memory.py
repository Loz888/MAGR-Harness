"""Versioned account memory on the Engine-owned PostgreSQL connection."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.general_agent.contracts import Identifier, Revision, Sha256, Text
from mgar_v3.harness.tenancy import active_membership
from mgar_v3.memory.model import MemoryKind
from mgar_v3.memory.write import contains_memory_secret

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row

Kind = Literal["preference", "project_fact", "experience"]
RecallPolicy = Literal["scope_only", "explicit_projects"]
_LEGACY: dict[Kind, MemoryKind] = {
    "preference": "procedural",
    "project_fact": "semantic",
    "experience": "episodic",
}
_SENSITIVE_MARKER = re.compile(
    r"(?i)(?:\b(?:password|token|secret|access[_ -]?key|pin|ssn)\s*[:=：]|"
    r"(?:密码|密钥|身份证号|银行卡号|访问令牌)\s*[:=：]|"
    r"\bya29\.[A-Za-z0-9_.-]{10,}|\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\b\d{3}-\d{2}-\d{4}\b|\b\d{17}[0-9Xx]\b|\b(?:\d[ -]?){13,19}\b)"
)
_CURRENT = (
    "SELECT i.memory_id,i.tenant_id,i.user_id,i.project_id,i.kind,i.subject,"
    "i.created_at,v.version,v.content,v.source,v.recall_policy,v.status,v.valid_until,"
    "v.created_at AS version_created_at FROM app.personal_memory_items i "
    "JOIN app.personal_memory_versions v ON v.memory_id=i.memory_id "
)
_OWNER = "i.tenant_id=%s AND i.user_id=%s"


class MemorySource(Model):
    kind: Literal["user_confirmed", "verified_outcome"]
    task_id: UUID | None = None
    task_revision: Revision | None = None
    outcome_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def check_reference(self) -> Self:
        bound = (self.task_id, self.task_revision, self.outcome_sha256)
        if self.kind == "verified_outcome" and any(value is None for value in bound):
            raise ValueError("Verified memories require durable provenance.")
        if self.kind == "user_confirmed" and any(value is not None for value in bound):
            raise ValueError("Manual memories cannot claim task provenance.")
        return self


class MemoryContent(Model):
    content: Text
    valid_until: AwareDatetime | None = None
    recall_policy: RecallPolicy = "scope_only"


class CreatePersonalMemory(MemoryContent):
    project_id: Identifier | None = None
    kind: Kind
    subject: Identifier

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        if self.project_id is None and (
            self.kind != "preference" or self.recall_policy != "scope_only"
        ):
            raise ValueError("Personal scope is available for preferences.")
        return self


class RevisePersonalMemory(MemoryContent):
    expected_version: Revision


class PersonalMemory(Model):
    schema_version: Literal["personal-memory-v1"] = "personal-memory-v1"
    memory_id: UUID
    tenant_id: Identifier
    user_id: UUID
    project_id: Identifier | None
    kind: Kind
    legacy_kind: MemoryKind
    subject: Identifier
    created_at: AwareDatetime
    version: Revision
    version_created_at: AwareDatetime
    content: Text
    source: MemorySource
    recall_policy: RecallPolicy
    valid_until: AwareDatetime | None
    status: Literal["active", "revoked"]
    trust: Literal["user_confirmed", "verified_outcome"]


def _view(row: Row) -> PersonalMemory:
    return PersonalMemory.model_validate(
        {**row, "legacy_kind": _LEGACY[row["kind"]], "trust": row["source"]["kind"]}
    )


def _missing() -> DomainError:
    return DomainError("PERSONAL_MEMORY_NOT_FOUND", "找不到个人记忆。", 404)


def _source_unavailable() -> DomainError:
    return DomainError("PERSONAL_MEMORY_SOURCE", "任务结果与当前记忆范围不匹配。", 403)


def _safe(*values: str) -> None:
    for value in values:
        if contains_memory_secret(value) or _SENSITIVE_MARKER.search(value):
            raise DomainError("PERSONAL_MEMORY_SENSITIVE", "记忆包含凭据或敏感标识。", 422)


class PersonalMemoryStore:
    def __init__(self, engine: Engine, tenant_id: str) -> None:
        self.engine = engine
        self.tenant_id = tenant_id

    @staticmethod
    def _account(conn: Connection, user: UUID) -> bool:
        account = conn.execute(
            "SELECT is_owner FROM app.web_accounts WHERE user_id=%s",
            (user,),
        ).fetchone()
        if account is None:
            raise DomainError("AUTH_REQUIRED", "请登录后继续。", 401)
        return bool(account["is_owner"])

    @staticmethod
    def _project(conn: Connection, user: UUID, project: str | None, owner: bool) -> None:
        if project is None or active_membership(conn, project, user_id=user) is not None:
            return
        if (
            owner
            and conn.execute(
                "SELECT 1 FROM app.harness_managed_projects WHERE workspace_id=%s",
                (project,),
            ).fetchone()
            is not None
        ):
            return
        raise DomainError("HARNESS_PROJECT_UNKNOWN", "项目不存在。", 404)

    @staticmethod
    def _version(
        conn: Connection,
        key: UUID,
        version: int,
        value: MemoryContent,
        source: MemorySource,
        status: Literal["active", "revoked"],
    ) -> None:
        conn.execute(
            "INSERT INTO app.personal_memory_versions "
            "(memory_id,version,content,source,recall_policy,status,valid_until) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                key,
                version,
                value.content,
                Jsonb(source.model_dump(mode="json")),
                value.recall_policy,
                status,
                value.valid_until,
            ),
        )

    def _row(
        self,
        conn: Connection,
        user: UUID,
        key: UUID,
        *,
        version: int | None = None,
    ) -> Row:
        row = conn.execute(
            _CURRENT + "WHERE " + _OWNER + " AND i.memory_id=%s "
            "AND v.version=COALESCE(%s,i.current_version) FOR UPDATE OF i",
            (self.tenant_id, user, key, version),
        ).fetchone()
        if row is None:
            raise _missing()
        return row

    def create(
        self,
        user: UUID,
        value: CreatePersonalMemory,
        *,
        completed_task: UUID | None = None,
    ) -> PersonalMemory:
        _safe(value.subject, value.content)
        if value.valid_until is not None and value.valid_until <= datetime.now(UTC):
            raise DomainError("PERSONAL_MEMORY_EXPIRED", "记忆有效期已过。", 422)
        with self.engine.transaction() as conn:
            owner = self._account(conn, user)
            self._project(conn, user, value.project_id, owner)
            source = MemorySource(kind="user_confirmed")
            if completed_task is not None:
                try:
                    task = self.engine.general_tasks.lock_current(conn, completed_task)
                except DomainError as exc:
                    if exc.code != "GENERAL_TASK_NOT_FOUND":
                        raise
                    raise _source_unavailable() from None
                membership = (
                    active_membership(conn, task.require_project(), user_id=user)
                    if task.membership_id is not None
                    else None
                )
                if (
                    task.state != "completed"
                    or task.outcome is None
                    or value.kind != "experience"
                    or task.project_id != value.project_id
                    or (task.membership_id is None and not owner)
                    or (
                        task.membership_id is not None
                        and (
                            membership is None or membership["membership_id"] != task.membership_id
                        )
                    )
                ):
                    raise _source_unavailable()
                # The verified label only covers the exact completed summary.
                if value.content != task.outcome.summary:
                    raise DomainError("PERSONAL_MEMORY_SOURCE", "结果摘要与记忆内容不一致。", 409)
                source = MemorySource(
                    kind="verified_outcome",
                    task_id=task.task_id,
                    task_revision=task.revision,
                    outcome_sha256=digest(task.outcome.model_dump(mode="json")),
                )
            row = conn.execute(
                "INSERT INTO app.personal_memory_items "
                "(memory_id,tenant_id,user_id,project_id,kind,subject,current_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,1) "
                "ON CONFLICT (tenant_id,user_id,project_id,kind,subject) DO NOTHING "
                "RETURNING memory_id",
                (uuid4(), self.tenant_id, user, value.project_id, value.kind, value.subject),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "PERSONAL_MEMORY_CONFLICT",
                    "已有同名记忆，请按当前版本修正。",
                    409,
                )
            key = row["memory_id"]
            self._version(conn, key, 1, value, source, "active")
            return _view(self._row(conn, user, key))

    def get(self, user: UUID, key: UUID, *, version: int | None = None) -> PersonalMemory:
        with self.engine.transaction() as conn:
            owner = self._account(conn, user)
            row = self._row(conn, user, key, version=version)
            self._project(conn, user, row["project_id"], owner)
            return _view(row)

    def revise(self, user: UUID, key: UUID, value: RevisePersonalMemory) -> PersonalMemory:
        _safe(value.content)
        if value.valid_until is not None and value.valid_until <= datetime.now(UTC):
            raise DomainError("PERSONAL_MEMORY_EXPIRED", "记忆有效期已过。", 422)
        return self._change(user, key, value.expected_version, value)

    def revoke(self, user: UUID, key: UUID, expected_version: int) -> PersonalMemory:
        return self._change(user, key, expected_version, None)

    def _change(
        self,
        user: UUID,
        key: UUID,
        expected_version: int,
        replacement: MemoryContent | None,
    ) -> PersonalMemory:
        with self.engine.transaction() as conn:
            owner = self._account(conn, user)
            current = _view(self._row(conn, user, key))
            self._project(conn, user, current.project_id, owner)
            if current.version != expected_version:
                raise DomainError("PERSONAL_MEMORY_STALE", "记忆版本已更新。", 409)
            if replacement is None:
                if current.status == "revoked":
                    return current
                value = MemoryContent(
                    content=current.content,
                    valid_until=current.valid_until,
                    recall_policy=current.recall_policy,
                )
                source = current.source
                status: Literal["active", "revoked"] = "revoked"
            else:
                if current.project_id is None and replacement.recall_policy != "scope_only":
                    raise DomainError("PERSONAL_MEMORY_SCOPE", "个人偏好已有独立范围。", 422)
                value, source, status = replacement, MemorySource(kind="user_confirmed"), "active"
            version = current.version + 1
            self._version(conn, key, version, value, source, status)
            conn.execute(
                "UPDATE app.personal_memory_items SET current_version=%s WHERE memory_id=%s",
                (version, key),
            )
            return _view(self._row(conn, user, key))

    def delete(self, user: UUID, key: UUID, expected_version: int) -> None:
        with self.engine.transaction() as conn:
            self._account(conn, user)
            current = self._row(conn, user, key)
            if current["version"] != expected_version:
                raise DomainError("PERSONAL_MEMORY_STALE", "记忆版本已更新。", 409)
            # Personal erasure stays available even after project access is revoked.
            conn.execute("DELETE FROM app.personal_memory_items WHERE memory_id=%s", (key,))

    def recall(
        self,
        user: UUID,
        *,
        project: str | None = None,
        source_project: str | None = None,
        include_personal: bool = False,
        query: str = "",
        limit: int = 20,
    ) -> list[PersonalMemory]:
        if (
            source_project is not None
            and (project is None or source_project == project)
            or not 1 <= limit <= 100
            or len(query) > 200
        ):
            raise DomainError("PERSONAL_MEMORY_SCOPE", "记忆检索范围无效。", 422)
        with self.engine.transaction() as conn:
            owner = self._account(conn, user)
            self._project(conn, user, project, owner)
            self._project(conn, user, source_project, owner)
            rows = conn.execute(
                _CURRENT + "WHERE " + _OWNER + " AND v.version=i.current_version "
                "AND v.status='active' AND (v.valid_until IS NULL OR v.valid_until>now()) "
                "AND (i.project_id IS NOT DISTINCT FROM %s OR "
                "(%s AND i.project_id IS NULL) OR "
                "(i.project_id=%s AND v.recall_policy='explicit_projects')) "
                "AND (strpos(lower(i.subject),lower(%s))>0 OR "
                "strpos(lower(v.content),lower(%s))>0 OR %s='') "
                "ORDER BY v.created_at DESC,i.memory_id LIMIT %s",
                (
                    self.tenant_id,
                    user,
                    project,
                    include_personal,
                    source_project,
                    query,
                    query,
                    query,
                    limit,
                ),
            ).fetchall()
            return [_view(row) for row in rows]
