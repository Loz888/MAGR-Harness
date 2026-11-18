"""Harness GitHub Action/Outbox journal on the original Engine connection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.coordination.engine import Engine, Row
from mgar_v3.domain import DomainError, digest


class HarnessGitHubLedger:
    """PublicationJournal for the existing leased PublicationDispatcher.

    A business mutation is always made under ``engine.transaction``. Unknown
    writes remain reserved; a negative lookup cannot clear or requeue them.
    """

    def __init__(self, engine: Engine, validate: Callable[[Row], None]) -> None:
        self.engine, self.validate = engine, validate

    def publication(self, publication_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT a.*,o.lease_token,o.lease_until,o.attempts "
                "FROM app.harness_github_actions a LEFT JOIN app.harness_github_outbox o "
                "USING(publication_id) WHERE a.publication_id=%s",
                (publication_id,),
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "GitHub 交付记录不存在。", 404)
        return row

    def review(
        self,
        *,
        session: Row,
        task_id: str,
        bundle: dict[str, Any],
        action_type: str,
        mode: str,
        parent_id: UUID | None = None,
    ) -> Row:
        payload_hash = bundle.get("payload_hash") or digest(bundle)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.harness_github_actions(publication_id,parent_id,session_id,"
                "task_id,project_id,actor,action_type,execution_mode,payload_hash,bundle,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'review') "
                "ON CONFLICT(task_id,action_type,payload_hash) DO NOTHING",
                (
                    uuid4(),
                    parent_id,
                    session["session_id"],
                    task_id,
                    session["project_id"],
                    session["actor"],
                    action_type,
                    mode,
                    payload_hash,
                    Jsonb(bundle),
                ),
            )
            row = conn.execute(
                "SELECT * FROM app.harness_github_actions WHERE task_id=%s "
                "AND action_type=%s AND payload_hash=%s",
                (task_id, action_type, payload_hash),
            ).fetchone()
        assert row is not None
        return row

    def approve(self, identifier: UUID, payload_hash: str, approval: Row, revision: int) -> Row:
        with self.engine.transaction() as conn:
            row = self.publication(identifier)
            if row["payload_hash"] != payload_hash:
                raise DomainError("APPROVAL_HASH_MISMATCH", "批准内容与当前完整提案不同。", 409)
            if row["status"] in {"dispatching", "succeeded", "outcome_unknown"}:
                return row
            # An explicit exact-payload approval can renew a queued action or
            # repair a rejection that happened before any remote step. Never
            # erase sent/unknown step history to make an action retryable.
            steps = self.steps(identifier)
            if row["status"] == "failed" and any(
                step["status"] not in {"succeeded", "rejected"} for step in steps
            ):
                raise DomainError(
                    "GITHUB_NEW_REVIEW_REQUIRED", "已有派发步骤，需重新生成完整审查提案。", 409
                )
            if row["status"] not in {"review", "ready", "failed"}:
                raise DomainError("INVALID_ACTION_STATE", "请为当前代码重新生成审查提案。", 409)
            candidate = {**row, "approval": approval, "grant_revision": revision}
            self.validate(candidate)
            conn.execute(
                "UPDATE app.harness_github_actions SET status='ready',approval=%s,"
                "grant_revision=%s,updated_at=now() WHERE publication_id=%s",
                (Jsonb(approval), revision, identifier),
            )
            conn.execute(
                "INSERT INTO app.harness_github_outbox(publication_id,status) VALUES (%s,'ready') "
                "ON CONFLICT(publication_id) DO UPDATE SET status='ready',lease_token=NULL,"
                "lease_until=NULL",
                (identifier,),
            )
        return self.publication(identifier)

    def recover(self) -> int:
        """Fence expired leases; never resend an interrupted or timed-out write."""
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "UPDATE app.harness_github_outbox SET status='outcome_unknown',lease_token=NULL,"
                "lease_until=NULL WHERE status='dispatching' AND lease_until<=now() "
                "RETURNING publication_id",
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE app.harness_github_actions SET status='outcome_unknown',"
                    "reason_code='LEASE_EXPIRED',updated_at=now() WHERE publication_id=%s",
                    (row["publication_id"],),
                )
                conn.execute(
                    "UPDATE app.harness_github_steps SET status='outcome_unknown' "
                    "WHERE publication_id=%s AND status='dispatching'",
                    (row["publication_id"],),
                )
                conn.execute(
                    "UPDATE app.harness_github_step_attempts SET status='outcome_unknown' "
                    "WHERE publication_id=%s AND status='dispatching'",
                    (row["publication_id"],),
                )
        return len(rows)

    def claim_publication(self, execution_mode: str) -> Row | None:
        self.recover()
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT a.* FROM app.harness_github_outbox o "
                "JOIN app.harness_github_actions a USING(publication_id) "
                "WHERE o.status='ready' AND a.status='ready' AND a.execution_mode=%s "
                "ORDER BY o.created_at FOR UPDATE OF o SKIP LOCKED LIMIT 1",
                (execution_mode,),
            ).fetchone()
            if row is None:
                return None
            lease = uuid4()
            conn.execute(
                "UPDATE app.harness_github_outbox SET status='dispatching',lease_token=%s,"
                "lease_until=now()+interval '5 minutes',attempts=attempts+1 "
                "WHERE publication_id=%s",
                (lease, row["publication_id"]),
            )
            conn.execute(
                "UPDATE app.harness_github_actions SET status='dispatching',updated_at=now() "
                "WHERE publication_id=%s",
                (row["publication_id"],),
            )
        return {**row, "lease_token": lease}

    def publication_result(
        self,
        publication_id: UUID,
        lease_token: UUID,
        *,
        external_id: str | None = None,
        unknown: bool = False,
        failed: bool = False,
        reason_code: str = "",
        reason: str = "",
    ) -> str:
        if sum((external_id is not None, unknown, failed)) != 1:
            raise ValueError("Exactly one remote outcome is required")
        status = "outcome_unknown" if unknown else "failed" if failed else "succeeded"
        with self.engine.transaction() as conn:
            row = self.publication(publication_id)
            if row["status"] != "dispatching" or row["lease_token"] != lease_token:
                raise DomainError("FENCED", "GitHub 派发租约已失效。", 409)
            conn.execute(
                "UPDATE app.harness_github_actions SET status=%s,external_id=%s,reason_code=%s,"
                "reason=%s,updated_at=now() WHERE publication_id=%s",
                (status, external_id, reason_code[:100], reason[:1000], publication_id),
            )
            conn.execute(
                "UPDATE app.harness_github_outbox SET status=%s,lease_token=NULL,lease_until=NULL "
                "WHERE publication_id=%s",
                (status, publication_id),
            )
            if unknown:
                conn.execute(
                    "UPDATE app.harness_github_steps SET status='outcome_unknown' "
                    "WHERE publication_id=%s AND status='dispatching'",
                    (publication_id,),
                )
                conn.execute(
                    "UPDATE app.harness_github_step_attempts SET status='outcome_unknown' "
                    "WHERE publication_id=%s AND status='dispatching'",
                    (publication_id,),
                )
        return status

    def reconcile_publication(self, publication_id: UUID, external_id: str | None) -> bool:
        if external_id is None:
            return False
        with self.engine.transaction() as conn:
            if self.publication(publication_id)["status"] != "outcome_unknown":
                raise DomainError("NOT_UNKNOWN", "只有结果未知的交付需要对账。", 409)
            conn.execute(
                "UPDATE app.harness_github_actions SET status='succeeded',external_id=%s,"
                "reason_code='RECONCILED',updated_at=now() WHERE publication_id=%s",
                (external_id, publication_id),
            )
            conn.execute(
                "UPDATE app.harness_github_outbox SET status='succeeded',lease_token=NULL,"
                "lease_until=NULL WHERE publication_id=%s",
                (publication_id,),
            )
            conn.execute(
                "UPDATE app.harness_github_steps SET status='succeeded',"
                "receipt=coalesce(receipt,'{}'::jsonb)||'{\"reconciled\":true}'::jsonb "
                "WHERE publication_id=%s",
                (publication_id,),
            )
            conn.execute(
                "UPDATE app.harness_github_step_attempts SET status='succeeded',"
                "receipt=coalesce(receipt,'{}'::jsonb)||'{\"reconciled\":true}'::jsonb "
                "WHERE publication_id=%s AND status IN ('dispatching','outcome_unknown')",
                (publication_id,),
            )
        return True

    def begin_step(self, action: Row, ordinal: int, kind: str, request: Row) -> bool:
        with self.engine.transaction() as conn:
            row = self.publication(action["publication_id"])
            if row["status"] != "dispatching" or row["lease_token"] != action["lease_token"]:
                raise DomainError("FENCED", "GitHub 派发租约已失效。", 409)
            self.validate(row)
            old = conn.execute(
                "SELECT * FROM app.harness_github_steps WHERE publication_id=%s AND ordinal=%s",
                (row["publication_id"], ordinal),
            ).fetchone()
            if old:
                if old["request_hash"] != digest(request):
                    raise DomainError("PAYLOAD_CHANGED", "远端步骤请求与原审查内容不同。", 409)
                if old["status"] == "succeeded":
                    return False
                if old["status"] != "rejected":
                    raise OSError(
                        "A previous remote step is unknown; only reconciliation is allowed"
                    )
                attempt = old["attempt"] + 1
                conn.execute(
                    "UPDATE app.harness_github_steps SET status='dispatching',receipt=NULL,"
                    "attempt=%s,updated_at=now() WHERE publication_id=%s AND ordinal=%s",
                    (attempt, row["publication_id"], ordinal),
                )
            else:
                attempt = 1
                conn.execute(
                    "INSERT INTO app.harness_github_steps(publication_id,ordinal,kind,status,"
                    "request_hash) VALUES (%s,%s,%s,'dispatching',%s)",
                    (row["publication_id"], ordinal, kind, digest(request)),
                )
            conn.execute(
                "INSERT INTO app.harness_github_step_attempts(publication_id,ordinal,attempt,"
                "request_hash,status) VALUES (%s,%s,%s,%s,'dispatching')",
                (row["publication_id"], ordinal, attempt, digest(request)),
            )
        return True

    def receipt(self, action: Row, ordinal: int, receipt: Mapping[str, Any]) -> None:
        with self.engine.transaction() as conn:
            current = self.publication(action["publication_id"])
            if current["lease_token"] != action["lease_token"]:
                raise DomainError("FENCED", "GitHub 回执租约已失效。", 409)
            conn.execute(
                "UPDATE app.harness_github_steps SET status='succeeded',receipt=%s,"
                "updated_at=now() "
                "WHERE publication_id=%s AND ordinal=%s AND status='dispatching'",
                (Jsonb(dict(receipt)), action["publication_id"], ordinal),
            )
            conn.execute(
                "UPDATE app.harness_github_step_attempts a SET status='succeeded',receipt=%s "
                "FROM app.harness_github_steps s WHERE a.publication_id=s.publication_id "
                "AND a.ordinal=s.ordinal AND a.attempt=s.attempt AND a.status='dispatching' "
                "AND a.publication_id=%s AND a.ordinal=%s",
                (Jsonb(dict(receipt)), action["publication_id"], ordinal),
            )

    def reject(self, action: Row, ordinal: int, reason: str) -> None:
        with self.engine.transaction() as conn:
            current = self.publication(action["publication_id"])
            if current["lease_token"] != action["lease_token"]:
                raise DomainError("FENCED", "GitHub 拒绝回执的租约已失效。", 409)
            receipt = Jsonb({"definitive_rejection": True, "reason": reason[:500]})
            conn.execute(
                "UPDATE app.harness_github_steps SET status='rejected',receipt=%s,"
                "updated_at=now() WHERE publication_id=%s AND ordinal=%s AND status='dispatching'",
                (receipt, action["publication_id"], ordinal),
            )
            conn.execute(
                "UPDATE app.harness_github_step_attempts a SET status='rejected',receipt=%s "
                "FROM app.harness_github_steps s WHERE a.publication_id=s.publication_id "
                "AND a.ordinal=s.ordinal AND a.attempt=s.attempt AND a.status='dispatching' "
                "AND a.publication_id=%s AND a.ordinal=%s",
                (receipt, action["publication_id"], ordinal),
            )

    def attach_pull(self, identifier: UUID, pull: Row) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.harness_github_actions SET pull_request=%s,updated_at=now() "
                "WHERE publication_id=%s",
                (Jsonb(pull), identifier),
            )

    def steps(self, identifier: UUID) -> list[Row]:
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT ordinal,kind,status,attempt,receipt FROM app.harness_github_steps "
                "WHERE publication_id=%s ORDER BY ordinal",
                (identifier,),
            ).fetchall()

    def resume(self, identifier: UUID, proof: dict[int, Row], approval: Row, revision: int) -> None:
        """Explicitly queue only unsent steps after proving every sent step."""
        with self.engine.transaction() as conn:
            action = self.publication(identifier)
            if action["status"] != "outcome_unknown" or action["action_type"] != "draft_pr":
                raise DomainError("NOT_UNKNOWN", "仅支持对账后的 Draft PR 分步续发。", 409)
            steps = self.steps(identifier)
            if any(step["ordinal"] not in proof for step in steps):
                raise DomainError("GITHUB_STEP_UNCONFIRMED", "尚不能证明原已发送步骤的结果。", 409)
            candidate = {**action, "approval": approval, "grant_revision": revision}
            self.validate(candidate)
            for step in steps:
                conn.execute(
                    "UPDATE app.harness_github_steps SET status='succeeded',receipt=%s,"
                    "updated_at=now() WHERE publication_id=%s AND ordinal=%s",
                    (
                        Jsonb({"reconciled": True, **proof[step["ordinal"]]}),
                        identifier,
                        step["ordinal"],
                    ),
                )
                conn.execute(
                    "UPDATE app.harness_github_step_attempts SET status='succeeded',receipt=%s "
                    "WHERE publication_id=%s AND ordinal=%s "
                    "AND status IN ('dispatching','outcome_unknown')",
                    (
                        Jsonb({"reconciled": True, **proof[step["ordinal"]]}),
                        identifier,
                        step["ordinal"],
                    ),
                )
            conn.execute(
                "UPDATE app.harness_github_actions SET status='ready',approval=%s,"
                "grant_revision=%s,"
                "reason_code='EXPLICIT_CONTINUATION',reason='',updated_at=now() "
                "WHERE publication_id=%s",
                (Jsonb(approval), revision, identifier),
            )
            conn.execute(
                "UPDATE app.harness_github_outbox SET status='ready',lease_token=NULL,"
                "lease_until=NULL "
                "WHERE publication_id=%s",
                (identifier,),
            )
