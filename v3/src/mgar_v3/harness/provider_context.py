"""Bounded, task-bound provider continuation storage outside public messages."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import ModelRequest, ProviderContextRef


def provider_context_bytes(messages: Any) -> int:
    """Conservative wire/token allowance for referenced private provider text."""
    return sum(message.provider_context.wire_bytes for message in messages
               if message.provider_context is not None)


class ProviderContextStore:
    def __init__(self, artifacts: ArtifactStore | None = None) -> None:
        self.artifacts = artifacts
        # Standalone adapters can continue in memory. Engine-backed adapters
        # always receive the original durable CAS, including after recovery.
        self._memory: dict[str, bytes] = {}

    @staticmethod
    def _scope(request: ModelRequest) -> dict[str, Any]:
        task = request.task
        return {"task_id": task.task_id, "run_id": str(task.run_id),
                "workspace_id": task.workspace_id, "repository": task.repository,
                "actor": task.actor, "execution_epoch": task.execution_epoch,
                "model_id": request.model_id}

    def put(self, text: str, request: ModelRequest) -> ProviderContextRef:
        wire_bytes = len(json.dumps(text, ensure_ascii=False).encode())
        if wire_bytes > 262144:
            raise DomainError("MODEL_PROVIDER_CONTEXT_LIMIT", "Provider continuation is too large")
        raw = json.dumps({"schema": "deepseek-provider-context-v1",
                          "scope": self._scope(request), "reasoning_content": text},
                         ensure_ascii=False, separators=(",", ":")).encode()
        key = hashlib.sha256(raw).hexdigest()
        if self.artifacts is not None:
            self.artifacts.put(raw)
        else:
            self._memory[key] = raw
        return ProviderContextRef(artifact_hash=key, wire_bytes=wire_bytes)

    def read(self, reference: ProviderContextRef, request: ModelRequest) -> str:
        try:
            raw = (self.artifacts.read(reference.artifact_hash) if self.artifacts is not None
                   else self._memory[reference.artifact_hash])
            value = json.loads(raw)
        except (DomainError, KeyError, ValueError, TypeError) as exc:
            raise DomainError("MODEL_PROVIDER_CONTEXT_UNAVAILABLE",
                              "Private provider continuation is unavailable", 409) from exc
        expected_scope = self._scope(request)
        scope = value.get("scope") if isinstance(value, dict) else None
        source_epoch = scope.get("execution_epoch") if isinstance(scope, dict) else None
        if (hashlib.sha256(raw).hexdigest() != reference.artifact_hash
                or not isinstance(value, dict)
                or value.get("schema") != "deepseek-provider-context-v1"
                or not isinstance(scope, dict)
                or {k: v for k, v in scope.items() if k != "execution_epoch"}
                != {k: v for k, v in expected_scope.items() if k != "execution_epoch"}
                or type(source_epoch) is not int
                or not 1 <= source_epoch <= request.task.execution_epoch
                or not isinstance(value.get("reasoning_content"), str)
                or len(json.dumps(value["reasoning_content"], ensure_ascii=False).encode())
                != reference.wire_bytes):
            raise DomainError("MODEL_PROVIDER_CONTEXT_MISMATCH",
                              "Private provider continuation belongs to another request", 409)
        return str(value["reasoning_content"])
