"""Optimize recall after permission and lifecycle checks, with identical ranking."""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.memory.model import (
    EpisodicDetail,
    MemoryDetail,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    ProceduralDetail,
    SemanticDetail,
    WorkingDetail,
)
from mgar_v3.memory.store import Connection, MemoryStore, _corrupt

DETAILS = {
    "working": WorkingDetail,
    "episodic": EpisodicDetail,
    "semantic": SemanticDetail,
    "procedural": ProceduralDetail,
}
_DETAILS: ContextVar[dict[UUID, MemoryDetail] | None] = ContextVar("e5_r1_details", default=None)

INDEXES = """
CREATE INDEX e5_recall_order ON knowledge.memory_records
(project_id, repository, actor, created_at DESC, memory_id DESC)
WHERE status IN ('active','stale');
CREATE INDEX e5_scoped_recall_order ON knowledge.memory_records
(project_id, repository, actor, scope_kind, scope_key, created_at DESC, memory_id DESC)
WHERE status IN ('active','stale');
"""

ORDER = " ORDER BY m.created_at DESC, m.memory_id DESC"
CLAUSES = (
    "m.project_id=%s",
    "m.repository=%s",
    "m.actor=%s",
)
ACTIVE = "m.status='active'"
STALE = "m.status IN ('active','stale')"
WORKING = (
    "(m.memory_kind <> 'working' OR (SELECT w.expires_at FROM knowledge.memory_working w "
    "WHERE w.memory_id=m.memory_id) > now())"
)

BASE_SELECT = (
    "SELECT m.* FROM knowledge.memory_records m LEFT JOIN "
    "knowledge.memory_working w ON w.memory_id=m.memory_id WHERE "
)
BASE_WORKING = "(m.memory_kind <> 'working' OR w.expires_at > now())"

TOP = (
    "WITH selected AS MATERIALIZED ("
    "SELECT m.memory_id,m.created_at FROM knowledge.memory_records m WHERE "
)
RECORDS = " FROM selected c JOIN knowledge.memory_records m USING (memory_id)"
BATCH_COLUMNS = """
, CASE m.memory_kind
    WHEN 'working' THEN to_jsonb(w)
    WHEN 'episodic' THEN to_jsonb(e)
    WHEN 'semantic' THEN to_jsonb(s)
    WHEN 'procedural' THEN to_jsonb(p)
  END AS recalled_detail
"""
DETAIL_JOINS = """
LEFT JOIN knowledge.memory_working w ON m.memory_id=w.memory_id AND m.memory_kind='working'
LEFT JOIN knowledge.memory_episodic e ON m.memory_id=e.memory_id AND m.memory_kind='episodic'
LEFT JOIN knowledge.memory_semantic s ON m.memory_id=s.memory_id AND m.memory_kind='semantic'
LEFT JOIN knowledge.memory_procedural p ON m.memory_id=p.memory_id AND m.memory_kind='procedural'
"""


def recall_sql(scope_clause: Sequence[str], arm: str) -> str:
    if arm in {"baseline", "index"}:
        return BASE_SELECT + " AND ".join([*scope_clause, BASE_WORKING]) + ORDER + " LIMIT %s"
    query = TOP + " AND ".join([*scope_clause, WORKING]) + ORDER + " LIMIT %s) SELECT m.*"
    if arm == "topn":
        return query + RECORDS + ORDER
    if arm == "full":
        return query + BATCH_COLUMNS + RECORDS + DETAIL_JOINS + ORDER
    raise ValueError("Unknown candidate mode")


class CandidateStore(MemoryStore):
    def __init__(self, settings, arm):
        super().__init__(settings)
        self.arm = arm

    def recall(
        self,
        *,
        actor: str | None = None,
        memory_kinds: Sequence[MemoryKind] | None = None,
        scope: MemoryScope | None = None,
        commit: str | None = None,
        limit: int = 32,
        include_stale: bool = False,
    ) -> list[MemoryRecord]:
        actor = self._actor(actor)
        if not 1 <= limit <= 64:
            raise DomainError("MEMORY_BOUND_INVALID", "recall limit is out of range", 400)
        clauses = [*CLAUSES, STALE if include_stale else ACTIVE]
        params = [self.settings.project_id, self._repository, actor]
        if memory_kinds:
            clauses.append("m.memory_kind = ANY(%s)")
            params.append(list(memory_kinds))
        if scope is not None:
            clauses.extend(("m.scope_kind=%s", "m.scope_key=%s"))
            params.extend((scope, self._scope_key(scope, commit)))
        elif commit is not None:
            clauses.append("m.scope_kind='commit' AND m.scope_key=%s")
            params.append(commit)
        params.append(limit)
        with self._connection() as db:
            rows = db.execute(recall_sql(clauses, self.arm), params).fetchall()
            if self.arm != "full":
                return [self._record(db, row) for row in rows]
            details = {}
            for row in rows:
                key = row["memory_id"]
                detail = row["recalled_detail"]
                if detail is None:
                    raise _corrupt(key)
                details[key] = DETAILS[row["memory_kind"]].model_validate(
                    {k: value for k, value in detail.items() if k != "memory_id"}
                )
            token = _DETAILS.set(details)
            try:
                return [self._record(db, row) for row in rows]
            finally:
                _DETAILS.reset(token)

    def _detail(self, conn: Connection, memory_id: UUID, kind: MemoryKind) -> MemoryDetail:
        batch = _DETAILS.get()
        if batch is not None:
            return batch[memory_id]
        return super()._detail(conn, memory_id, kind)
