"""Persistent, cursor-addressable harness event log.

The log is deliberately append-only and file backed so a slow or disconnected
client can never block the coding loop.  Each record uses the versioned
``harness-event-v1`` envelope from :mod:`mgar_v3.harness.contracts`.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from mgar_v3.harness.contracts import EventDraft, EventEnvelope

STREAM_VERSION = "harness-stream-v1"
_PRIVATE_KEYS = {
    "reasoning",
    "reasoning_content",
    "provider_context",
    "chain_of_thought",
    "internal_reasoning",
    "hidden_reasoning",
    "thoughts",
}


def _public(value: Any) -> Any:
    """Strip internal reasoning keys before anything leaves the process."""

    if isinstance(value, dict):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


class PersistentEventSink:
    """Append-only JSONL event log with monotonically increasing cursors."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cursor = self._scan_cursor()

    def _scan_cursor(self) -> int:
        if not self.path.is_file():
            return 0
        cursor = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    value = payload.get("cursor")
                    if isinstance(value, int) and value > cursor:
                        cursor = value
        except OSError:
            return 0
        return cursor

    @property
    def last_cursor(self) -> int:
        with self._lock:
            return self._cursor

    def emit(self, draft: EventDraft) -> EventEnvelope:
        with self._lock:
            self._cursor += 1
            envelope = EventEnvelope(
                run_id=draft.run_id,
                operation_id=draft.operation_id,
                execution_epoch=draft.execution_epoch,
                revision=draft.revision,
                cursor=self._cursor,
                kind=draft.kind,
                payload=_public(draft.payload),
            )
            record = {
                **envelope.to_run_event_safe_payload(),
                "run_id": str(envelope.run_id),
                "cursor": envelope.cursor,
                "kind": envelope.kind,
                "occurred_at": envelope.occurred_at.isoformat(),
            }
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return envelope

    def events(
        self,
        run_id: UUID | None = None,
        after_cursor: int = 0,
    ) -> tuple[EventEnvelope, ...]:
        del run_id  # one sink is scoped to exactly one task
        envelopes: list[EventEnvelope] = []
        for record in self._read(after_cursor, None):
            try:
                envelopes.append(
                    EventEnvelope(
                        envelope_id=UUID(str(record["envelope_id"])),
                        run_id=UUID(str(record["run_id"])),
                        operation_id=str(record["operation_id"]),
                        execution_epoch=int(record["execution_epoch"]),
                        revision=int(record["revision"]),
                        cursor=int(record["cursor"]),
                        kind=str(record["kind"]),
                        payload=dict(record.get("payload") or {}),
                        occurred_at=datetime.fromisoformat(str(record["occurred_at"])),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(envelopes)

    def page(
        self,
        after_cursor: int = 0,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Return ``(records, next_cursor, has_more)`` without blocking writers."""

        bounded = max(1, min(int(limit), 1000))
        records = self._read(after_cursor, bounded + 1)
        has_more = len(records) > bounded
        records = records[:bounded]
        next_cursor = records[-1]["cursor"] if records else max(0, after_cursor)
        return records, next_cursor, has_more

    def _read(self, after_cursor: int, limit: int | None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.path.is_file():
            return records
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    cursor = record.get("cursor")
                    if not isinstance(cursor, int) or cursor <= after_cursor:
                        continue
                    records.append(record)
                    if limit is not None and len(records) >= limit:
                        break
        except OSError:
            return records
        return records

    def summary(self) -> dict[str, Any]:
        """Public progress projection derived only from emitted events."""

        records = self._read(0, None)
        counts: dict[str, int] = {}
        for record in records:
            kind = str(record.get("kind", ""))
            counts[kind] = counts.get(kind, 0) + 1
        last = records[-1] if records else {}
        return {
            "stream_version": STREAM_VERSION,
            "events_cursor": self.last_cursor,
            "event_counts": counts,
            "last_event": last.get("kind", ""),
            "last_cursor": last.get("cursor", 0),
        }


def iter_frames(records: Iterable[dict[str, Any]]) -> Iterable[str]:
    """Render records as SSE ``id``/``event``/``data`` frames."""

    for record in records:
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        yield f"id: {record.get('cursor', 0)}\nevent: update\ndata: {data}\n\n"
