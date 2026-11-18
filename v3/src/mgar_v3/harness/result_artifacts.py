"""Bounded final-result and receipt indexes over immutable CAS sections.

Only the ledger-selected outer artifact is authoritative. Task/run ownership
belongs to its envelope and never overwrites fields inside an arbitrary result.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal, cast

from pydantic import Field, ValidationError

from mgar_v3.domain import DomainError, Model
from mgar_v3.harness.persistence import _checkpoint_bytes, _checkpoint_payload

_ENVELOPE = "__harness_result_cas__"
_INLINE_BYTES = 512_000
_INDEX_BYTES = 64_000
_PAYLOAD_BYTES = 128 * 512_000
_PROJECTED = frozenset(
    {
        "task_id",
        "run_id",
        "evaluation_profile",
        "status",
        "candidate_status",
        "tree_after",
        "independent_gate",
    }
)


class _ResultIndex(Model):
    schema_version: Literal["harness-result-index-v1"] = "harness-result-index-v1"
    task_id: str
    run_id: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_bytes: int = Field(strict=True, ge=2, le=_PAYLOAD_BYTES)


class _ResultManifest(Model):
    schema_version: Literal["harness-result-manifest-v1"] = "harness-result-manifest-v1"
    task_id: str
    run_id: str
    checkpoint: dict[str, Any]


def _bytes(value: Any) -> bytes:
    content = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif type(item) not in {str, int, float, bool, type(None)}:
            raise ValueError("unsupported JSON value type")
    return content


def _ownership(task_id: str, run_id: str) -> None:
    if not isinstance(task_id, str) or not isinstance(run_id, str) or not task_id or not run_id:
        raise DomainError("RESULT_SCOPE_DENIED", "Result scope is invalid", 409)


def dump_result(
    artifacts: Any,
    payload: dict[str, Any],
    task_id: str,
    run_id: str,
) -> bytes:
    """Return inline JSON or a bounded index; preserve the recovered object exactly."""
    _ownership(task_id, run_id)
    if not isinstance(payload, dict):
        raise DomainError("RESULT_INVALID", "Result payload must be an object", 409)
    try:
        raw = _bytes(payload)
        conflicting_scope = (
            payload.get("task_id", task_id) != task_id or payload.get("run_id", run_id) != run_id
        )
        if len(raw) <= _INLINE_BYTES and _ENVELOPE not in payload and not conflicting_scope:
            return raw
        if len(raw) > _PAYLOAD_BYTES:
            raise DomainError("RESULT_BOUNDS", "Result exceeds its section bound", 413)
        projection = {key: payload[key] for key in sorted(_PROJECTED & payload.keys())}
        # Bound the directly inspectable business fields before writing chunks.
        # They must remain exact because store.finish validates the outer index.
        if len(_bytes(projection)) > _INDEX_BYTES - 2048:
            raise DomainError("RESULT_BOUNDS", "Result projection exceeds its index bound", 413)
        checkpoint = json.loads(
            _checkpoint_bytes(
                artifacts,
                task_id,
                run_id,
                {"result": payload, "state": {}},
            )
        )
        manifest = _bytes(
            _ResultManifest(
                task_id=task_id,
                run_id=run_id,
                checkpoint=checkpoint,
            ).model_dump(mode="json")
        )
        if len(manifest) > _INDEX_BYTES:
            raise DomainError("RESULT_BOUNDS", "Result manifest exceeds its bound", 413)
        metadata = _ResultIndex(
            task_id=task_id,
            run_id=run_id,
            manifest_sha256=artifacts.put(manifest),
            payload_sha256=sha256(raw).hexdigest(),
            payload_bytes=len(raw),
        )
        index = _bytes({_ENVELOPE: metadata.model_dump(mode="json"), **projection})
        if len(index) > _INDEX_BYTES:
            raise DomainError("RESULT_BOUNDS", "Result index exceeds its bound", 413)
        return index
    except (TypeError, ValueError, UnicodeError, ValidationError, RecursionError) as exc:
        raise DomainError("RESULT_INVALID", "Result contains invalid JSON data", 409) from exc


def load_result(
    artifacts: Any,
    raw: bytes,
    task_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Verify all sections and ownership before exposing a recovered result."""
    _ownership(task_id, run_id)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("result is not an object")
        if _ENVELOPE not in value:
            # Previously selected inline results carry no envelope. Preserve
            # their representation and check scope fields when they exist.
            if value.get("task_id", task_id) != task_id or value.get("run_id", run_id) != run_id:
                raise DomainError(
                    "RESULT_SCOPE_DENIED", "Inline result belongs to another run", 409
                )
            if len(raw) > 2_000_000:
                raise ValueError("legacy inline result exceeds the CAS bound")
            _bytes(value)
            return cast(dict[str, Any], value)
        if len(raw) > _INDEX_BYTES or set(value) - {_ENVELOPE} - _PROJECTED:
            raise ValueError("invalid result index fields or size")
        metadata = _ResultIndex.model_validate(value[_ENVELOPE])
        if (metadata.task_id, metadata.run_id) != (task_id, run_id):
            raise DomainError("RESULT_SCOPE_DENIED", "Result index belongs to another run", 409)
        manifest_raw = artifacts.read(metadata.manifest_sha256)
        if (
            len(manifest_raw) > _INDEX_BYTES
            or sha256(manifest_raw).hexdigest() != metadata.manifest_sha256
        ):
            raise ValueError("result manifest hash or size differs")
        manifest = _ResultManifest.model_validate_json(manifest_raw)
        if (manifest.task_id, manifest.run_id) != (task_id, run_id):
            raise DomainError("RESULT_SCOPE_DENIED", "Result manifest belongs to another run", 409)
        restored = _checkpoint_payload(artifacts, _bytes(manifest.checkpoint), task_id, run_id)
        if set(restored) != {"state", "result"} or restored["state"] != {}:
            raise ValueError("result section names differ")
        payload = restored["result"]
        if not isinstance(payload, dict):
            raise ValueError("result section is not an object")
        payload_raw = _bytes(payload)
        if (
            len(payload_raw) != metadata.payload_bytes
            or sha256(payload_raw).hexdigest() != metadata.payload_sha256
        ):
            raise ValueError("result payload hash or size differs")
        projection = {key: payload[key] for key in sorted(_PROJECTED & payload.keys())}
        indexed = {key: item for key, item in value.items() if key != _ENVELOPE}
        if _bytes(projection) != _bytes(indexed):
            raise ValueError("result projection differs from verified payload")
        return cast(dict[str, Any], payload)
    except (ValueError, TypeError, KeyError, UnicodeError, ValidationError, RecursionError) as exc:
        raise DomainError(
            "RESULT_INVALID", "Invalid result index, manifest or section", 409
        ) from exc
