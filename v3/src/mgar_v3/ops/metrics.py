"""Bounded, redacted operational export from the original Harness ledger."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from mgar_v3.settings import Settings

_ID = re.compile(r"[A-Za-z0-9_:.-]{1,128}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_EVENTS = (
    "harness_dispatch_started", "verification_result", "command_completed",
    "harness_stream_disconnected", "tool_completed", "harness_provider_completed",
)
_JOIN = (
    " JOIN app.runs r USING(run_id) JOIN app.tickets t USING(ticket_id) "
    "JOIN app.threads h USING(thread_id) "
)


def _tag(value: Any, regex: re.Pattern[str] = _ID) -> str | None:
    return value if isinstance(value, str) and regex.fullmatch(value) else None


def export_metrics(
    settings: Settings, *, within_hours: int, limit: int, run_id: UUID | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    after = now - timedelta(hours=within_hours)
    params = (settings.project_id, after, run_id, run_id)
    with psycopg.connect(
        settings.database_url.get_secret_value(), row_factory=dict_row, connect_timeout=5,
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        operations = conn.execute(
            "SELECT a.task_id,o.run_id,o.operation_id,o.kind,o.budget_kind,o.state,"
            "o.updated_at,EXTRACT(EPOCH FROM (o.updated_at-o.created_at)) AS journal_seconds,"
            "o.intent->>'model_id' AS model_id,u.status AS usage_status,"
            "u.settled_micros,u.reserved_micros "
            "FROM app.harness_operations o JOIN app.harness_tasks a USING(run_id) " + _JOIN +
            "LEFT JOIN app.usage_calls u ON u.call_id=o.model_call_id "
            "WHERE h.project_id=%s AND o.updated_at>=%s AND (%s::uuid IS NULL OR o.run_id=%s) "
            "ORDER BY o.updated_at DESC,o.operation_id LIMIT %s", (*params, limit + 1),
        ).fetchall()
        events = conn.execute(
            "SELECT a.task_id,e.run_id,e.kind,e.created_at,e.safe_payload "
            "FROM app.run_events e JOIN app.harness_tasks a USING(run_id) " + _JOIN +
            "WHERE h.project_id=%s AND e.created_at>=%s AND (%s::uuid IS NULL OR e.run_id=%s) "
            "AND e.kind=ANY(%s) ORDER BY e.event_id DESC LIMIT %s",
            (*params, list(_EVENTS), limit + 1),
        ).fetchall()
    operation_items = []
    for row in operations[:limit]:
        usage = row["usage_status"]
        operation_items.append({
            "request_id": row["task_id"], "run_id": str(row["run_id"]),
            "operation_id": _tag(row["operation_id"]),
            "kind": row["budget_kind"] or row["kind"], "state": row["state"],
            "recorded_at": row["updated_at"].isoformat(),
            "journal_ms": max(0, round(float(row["journal_seconds"]) * 1000)),
            "model_id": _tag(row["model_id"]),
            "usage_status": usage,
            "settled_micros": row["settled_micros"] if usage == "known" else None,
            "reserved_micros": row["reserved_micros"] if usage in {"reserved", "unknown"} else None,
        })
    event_items = []
    for row in events[:limit]:
        envelope = row["safe_payload"]
        fields = envelope.get("payload", envelope)
        entry = {
            "request_id": row["task_id"], "run_id": str(row["run_id"]),
            "operation_id": _tag(fields.get("operation_id") or envelope.get("operation_id")),
            "event": row["kind"], "recorded_at": row["created_at"].isoformat(),
            "status": _tag(fields.get("status") or fields.get("finish_reason")),
            "failure_code": _tag(fields.get("code"), _CODE),
        }
        for key in ("queued_ms", "duration_ms", "exit_code", "cursor"):
            if type(fields.get(key)) is int:
                entry[key] = fields[key]
        if row["kind"] == "harness_stream_disconnected":
            entry["stream_request_id"] = _tag(fields.get("stream_request_id"))
        event_items.append(entry)
    return {
        "schema": "magr-harness-ops-v1", "from": after.isoformat(), "to": now.isoformat(),
        "truncated": len(events) > limit or len(operations) > limit,
        "operations": operation_items, "events": event_items,
    }
