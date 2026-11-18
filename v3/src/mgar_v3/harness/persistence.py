"""Engine backed adapters for the public harness service.

CAS contains bytes; only the Engine's task row selects a recovery checkpoint.
An unselected file never grants an epoch, a budget or a replay permission.
"""

from __future__ import annotations

import json
import threading
import time
from hashlib import sha256
from typing import Any, Literal, cast
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    EventDraft,
    EventEnvelope,
    ModelRequest,
    ModelResponse,
)
from mgar_v3.harness.events import STREAM_VERSION
from mgar_v3.harness.inbox import TaskInbox

_CHUNK_BYTES = 512_000
_MANIFEST_BYTES = 64_000
_MAX_CHUNKS = 128
_HASH = r"^[a-f0-9]{64}$"


class _CheckpointSection(Model):
    task_id: str
    run_id: str
    name: str = Field(pattern=r"^(state\.)?[a-z][a-z0-9_]{0,63}$")
    json_type: Literal["object", "array", "string", "number", "boolean", "null"]
    sha256: str = Field(pattern=_HASH)
    bytes: int = Field(strict=True, ge=1, le=_MAX_CHUNKS * _CHUNK_BYTES)
    chunks: tuple[str, ...] = Field(min_length=1, max_length=_MAX_CHUNKS)


class _CheckpointManifest(Model):
    schema_version: Literal["harness-checkpoint-v2"] = "harness-checkpoint-v2"
    task_id: str
    run_id: str
    # Recovery needs only this bounded operation hint before opening the tree.
    continuation: dict[str, Any]
    sections: dict[str, _CheckpointSection] = Field(min_length=1, max_length=64)


def _json_type(value: Any) -> str:
    return {
        dict: "object", list: "array", str: "string", int: "number", float: "number",
        bool: "boolean", type(None): "null",
    }[type(value)]


def _checkpoint_bytes(artifacts: Any, task_id: str, run_id: str, payload: dict[str, Any]) -> bytes:
    """CAS sections keep source, receipts and history out of the selected index.

    Chunks are immutable data, not recovery authorities. Only the PG-selected
    manifest can select their names, types, hashes and task/run scope.
    """
    state = payload.get("state", {})
    if not isinstance(state, dict):
        raise DomainError("CHECKPOINT_INVALID", "Checkpoint state is not an object", 409)
    fields = {key: value for key, value in payload.items() if key != "state"}
    fields.update({"state." + key: value for key, value in state.items()})
    sections: dict[str, _CheckpointSection] = {}
    chunk_count = 0
    for name, value in fields.items():
        content = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        parts = [
            content[offset:offset + _CHUNK_BYTES]
            for offset in range(0, len(content), _CHUNK_BYTES)
        ]
        chunk_count += len(parts)
        if chunk_count > _MAX_CHUNKS:
            raise DomainError(
                "CHECKPOINT_BOUNDS", "Checkpoint section index exceeds its bound", 413
            )
        sections[name] = _CheckpointSection(
            task_id=task_id, run_id=run_id, name=name, json_type=_json_type(value),
            sha256=sha256(content).hexdigest(), bytes=len(content),
            chunks=tuple(artifacts.put(part) for part in parts),
        )
    request = payload.get("request")
    hint = {"operation_id": request["operation_id"]} if isinstance(request, dict) else None
    manifest = _CheckpointManifest(
        task_id=task_id, run_id=run_id, continuation={"request": hint}, sections=sections,
    ).model_dump(mode="json")
    content = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    if len(content) > _MANIFEST_BYTES:
        raise DomainError("CHECKPOINT_BOUNDS", "Checkpoint manifest exceeds its byte bound", 413)
    return content


def _checkpoint_payload(
    artifacts: Any, content: bytes, task_id: str, run_id: str,
) -> dict[str, Any]:
    try:
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("checkpoint is not an object")
        if value.get("task_id") != task_id or value.get("run_id") != run_id:
            raise DomainError("CHECKPOINT_SCOPE_DENIED", "Checkpoint belongs to another run", 409)
        if "schema_version" not in value:
            # Preserve recovery of already selected, historical inline objects.
            if not isinstance(value.get("continuation"), dict):
                raise ValueError("legacy continuation is not an object")
            return cast(dict[str, Any], value["continuation"])
        if len(content) > _MANIFEST_BYTES:
            raise ValueError("manifest exceeds byte bound")
        manifest = _CheckpointManifest.model_validate(value)
        if sum(len(section.chunks) for section in manifest.sections.values()) > _MAX_CHUNKS:
            raise ValueError("manifest exceeds chunk bound")
        payload: dict[str, Any] = {"state": {}}
        for name, section in manifest.sections.items():
            if (section.task_id, section.run_id, section.name) != (task_id, run_id, name):
                raise DomainError(
                    "CHECKPOINT_SCOPE_DENIED", "Checkpoint section scope differs", 409
                )
            parts = []
            for index, content_hash in enumerate(section.chunks):
                part = artifacts.read(content_hash)
                if not 1 <= len(part) <= _CHUNK_BYTES or (
                    index < len(section.chunks) - 1 and len(part) != _CHUNK_BYTES
                ):
                    raise ValueError("invalid checkpoint chunk size")
                parts.append(part)
            raw = b"".join(parts)
            if len(raw) != section.bytes or sha256(raw).hexdigest() != section.sha256:
                raise ValueError("checkpoint section hash or size differs")
            item = json.loads(raw)
            if _json_type(item) != section.json_type:
                raise ValueError("checkpoint section type differs")
            if name.startswith("state."):
                payload["state"][name[6:]] = item
            else:
                if name == "state":
                    raise ValueError("ambiguous state section")
                payload[name] = item
        request = payload.get("request")
        hint = {"operation_id": request["operation_id"]} if isinstance(request, dict) else None
        if manifest.continuation != {"request": hint}:
            raise ValueError("recovery operation hint differs")
        return payload
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        raise DomainError(
            "CHECKPOINT_INVALID", "Invalid checkpoint manifest or section", 409
        ) from exc


class DurableModel:
    def __init__(self, inner: Any, store: Any, task_id: str, epoch: int, fault: Any) -> None:
        self.inner, self.store, self.task_id, self.epoch = inner, store, task_id, epoch
        self.fault = fault

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        self.store.guard(self.task_id, self.epoch)
        payload = request.model_dump(mode="json")
        fingerprint = {
            **payload,
            "task": {
                key: value for key, value in payload["task"].items() if key != "execution_epoch"
            },
        }
        operation = self.store.operation(self.task_id, request.operation_id)
        if operation is not None and operation["request_hash"] not in (None, digest(fingerprint)):
            raise DomainError("IDEMPOTENCY_CONFLICT", "Known model request changed", 409)
        known = self.store.model_result(self.task_id, self.epoch, request.operation_id)
        if known is not None:
            if operation is None or operation["request_hash"] != digest(fingerprint):
                raise DomainError("IDEMPOTENCY_CONFLICT", "Model request identity is missing", 409)
            return cast(ModelResponse, known)
        self.store.start_operation(
            self.task_id,
            self.epoch,
            request.operation_id,
            "model",
            digest(fingerprint),
            intent=payload,
        )
        self.fault("model_before_dispatch", {"operation_id": request.operation_id})
        cancellation.ensure_active()
        self.store.dispatch_operation(self.task_id, self.epoch, request.operation_id)
        self.fault("model_dispatched", {"operation_id": request.operation_id})
        observer = getattr(self.inner, "set_output_observer", None)
        if callable(observer):
            def capture(observed: ModelRequest, payload: dict[str, Any]) -> None:
                if (observed.operation_id != request.operation_id
                        or observed.task.run_id != request.task.run_id
                        or observed.task.execution_epoch != self.epoch):
                    raise DomainError("MODEL_OUTPUT_SCOPE", "Partial output scope differs", 409)
                self.store.emit(self.task_id, self.epoch, EventDraft(
                    run_id=request.task.run_id, operation_id=request.operation_id,
                    execution_epoch=self.epoch, revision=0, kind="model_output_captured",
                    payload=payload,
                ))
            # Child partial output must stay separate from the main answer.
            from mgar_v3.harness.delegation import EXPERT_OPERATION

            observer(None if EXPERT_OPERATION in request.operation_id else capture)
        started = time.monotonic()
        try:
            response = self.inner.complete(request, cancellation)
        except BaseException:
            self.store.unknown_operation(
                self.task_id,
                self.epoch,
                request.operation_id,
                "provider outcome was not durably recorded",
            )
            raise
        self.fault("model_before_settlement", {"operation_id": request.operation_id})
        self.store.save_model_result(
            self.task_id, self.epoch, request.operation_id, response,
            provider_ms=round((time.monotonic() - started) * 1000),
        )
        self.fault("model_after_settlement", {"operation_id": request.operation_id})
        return cast(ModelResponse, response)


class DurableContinuation:
    def __init__(self, store: Any, task_id: str, epoch: int, workspace: Any, fault: Any) -> None:
        self.store, self.task_id, self.epoch = store, task_id, epoch
        self.workspace, self.fault = workspace, fault

    def load(self) -> dict[str, Any] | None:
        row = self.store.guard(self.task_id, self.epoch)
        if not row["checkpoint_hash"]:
            return None
        payload = _checkpoint_payload(
            self.store.engine.artifacts, self.store.engine.artifacts.read(row["checkpoint_hash"]),
            self.task_id, str(row["run_id"]),
        )
        return {**payload, "expected_tree": row["tree_hash"]}

    def save(self, payload: dict[str, Any]) -> None:
        from mgar_v3.harness.recovery_io import workspace_lock

        with workspace_lock(self.workspace.root):
            row = self.store.guard(self.task_id, self.epoch)
            tree = self.workspace.snapshot().tree_hash
            if payload.get("expected_tree") != tree:
                raise DomainError(
                    "CHECKPOINT_TREE_STALE", "Tree moved before checkpoint commit", 409
                )
            content_hash = self.store.engine.artifacts.put(
                _checkpoint_bytes(
                    self.store.engine.artifacts, self.task_id, str(row["run_id"]), payload
                )
            )
            self.fault("checkpoint_before_commit", {"content_hash": content_hash})
            revision = int(row["checkpoint_revision"]) + 1
            self.store.save_checkpoint(
                self.task_id,
                self.epoch,
                content_hash,
                revision,
                tree_hash=tree,
                inbox_revision=int(payload.get("state", {}).get("instruction_revision", 0)),
            )
            self.fault("checkpoint_after_commit", {"content_hash": content_hash})


class _InboxLock:
    def __init__(self, inbox: EngineInbox) -> None:
        self.inbox = inbox
        self.local = threading.local()

    def __enter__(self) -> None:
        context = self.inbox.store.engine.transaction()
        conn = context.__enter__()
        try:
            row = self.inbox.store._row(conn, self.inbox.task_id)
        except BaseException as exc:
            context.__exit__(type(exc), exc, exc.__traceback__)
            raise
        # Publish shared adapter state only after acquiring Engine's mutex.
        # Each thread retains its own exact transaction context while waiting.
        stack = getattr(self.local, "stack", [])
        stack.append((context, self.inbox._connection, self.inbox._row_value))
        self.local.stack = stack
        self.inbox._connection = conn
        self.inbox._row_value = row

    def __exit__(self, *args: Any) -> Any:
        context, previous_connection, previous_row = self.local.stack.pop()
        self.inbox._connection = previous_connection
        self.inbox._row_value = previous_row
        return context.__exit__(*args)


class EngineInbox(TaskInbox):
    """TaskInbox validation with an Engine row lock instead of file authority.

    Applied messages stay replayable until their containing loop checkpoint is
    committed. This closes the receipt/checkpoint crash window.
    """

    def __init__(self, store: Any, task_id: str, *, epoch: int | None = None) -> None:
        self.store, self.task_id = store, task_id
        self.epoch = epoch
        self._connection: Any = None
        self._row_value: dict[str, Any] = {}
        self._lock: Any = _InboxLock(self)

    def _load(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(self._row_value["inbox"])))

    def _save(self, payload: dict[str, Any]) -> None:
        assert self._connection is not None
        if self.epoch is not None:
            self.store._guard(self._connection, self.task_id, self.epoch)
        self._connection.execute(
            "UPDATE app.harness_tasks SET inbox=%s,updated_at=now() WHERE task_id=%s",
            (Jsonb(payload), self.task_id),
        )
        self._row_value["inbox"] = payload

    def post(self, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        if self.epoch is not None:
            # Posting is an actor operation, not a worker acknowledgement.
            return EngineInbox(self.store, self.task_id).post(**kwargs)
        # The transaction stays open across both terminal check and insertion.
        with self.store.engine.transaction() as conn:
            row = self.store._row(conn, self.task_id)
            if row["public_status"] not in {"queued", "running", "paused", "waiting_input"}:
                raise DomainError("HARNESS_TASK_FINISHED", "Task cannot accept messages", 409)
            return super().post(**kwargs)

    def pending_instructions(self) -> list[dict[str, Any]]:
        with self._lock:
            watermark = self._row_value["checkpoint_inbox_revision"]
            return [
                message
                for message in self._load()["messages"]
                if message["kind"] == "instruction"
                and (
                    message["status"] == "received"
                    or (message["status"] == "applied" and message["applied_revision"] > watermark)
                )
            ]


class EngineEvents:
    def __init__(self, store: Any, task_id: str, epoch: int) -> None:
        self.store, self.task_id, self.epoch = store, task_id, epoch

    @property
    def last_cursor(self) -> int:
        events = self.events()
        return events[-1].cursor if events else 0

    def emit(self, draft: EventDraft) -> EventEnvelope:
        return cast(EventEnvelope, self.store.emit(self.task_id, self.epoch, draft))

    def events(
        self, run_id: UUID | None = None, after_cursor: int = 0
    ) -> tuple[EventEnvelope, ...]:
        del run_id
        return tuple(self.store.events(self.task_id, after_cursor=after_cursor))

    def page(
        self, after_cursor: int = 0, limit: int = 200
    ) -> tuple[list[dict[str, Any]], int, bool]:
        limit = max(1, min(limit, 1000))
        records = self.store.events(self.task_id, after_cursor=after_cursor, limit=limit + 1)
        page = records[:limit]
        return (
            [item.model_dump(mode="json") for item in page],
            page[-1].cursor if page else after_cursor,
            len(records) > limit,
        )

    def summary(self) -> dict[str, Any]:
        records = self.events()
        counts: dict[str, int] = {}
        for record in records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
        return {
            "stream_version": STREAM_VERSION,
            "event_counts": counts,
            "events_cursor": self.last_cursor,
            "last_cursor": self.last_cursor,
            "last_event": records[-1].kind if records else "",
        }
