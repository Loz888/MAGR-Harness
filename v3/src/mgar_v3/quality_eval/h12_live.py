"""H12's explicitly frozen DeepSeek Flash model and provider receipts.

This adapter does not grant a budget. The Engine reserves the experiment and
task budgets before every call. A provider attempt is never retried here.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgar_v3.harness.contracts import CancellationToken, ModelRequest, ModelResponse, canonical_hash
from mgar_v3.harness.model import DeepSeekChatAdapter, ModelCapabilities, ModelPrice
from mgar_v3.models.deepseek_policy import (
    DEEPSEEK_REASONING_POLICY,
    deepseek_request_options,
)

PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
MODEL_OBSERVATION = "experiments/harness-v4/h12-model-v2.json"
FLASH = ModelCapabilities(
    provider="deepseek",
    model_id="deepseek-flash",
    protocol="openai-chat-sse",
    context_tokens=1_048_576,
    max_output_tokens=8192,
    supports_structured_output=True,
    thinking="enabled",
    reasoning_effort="max",
    price=ModelPrice(
        version="DeepSeek-V4.1-Flash-1017-peak-20261111",
        input_micros_per_million=300_000,
        output_micros_per_million=1_200_000,
    ),
)


class RecordedFlashModel(DeepSeekChatAdapter):
    """Retain supplier identity and raw usage without keys or reasoning text."""

    def __init__(self, *, api_key: str, evidence: Path, **kwargs: Any) -> None:
        super().__init__(
            api_key=api_key, capabilities=FLASH, endpoint=ENDPOINT, max_attempts=1, **kwargs
        )
        self.evidence = evidence
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.provider_receipts: list[dict[str, Any]] = []

    def _parse(self, payload: dict[str, Any], request: ModelRequest, state: Any) -> Any:
        if isinstance(payload, dict):
            value = payload
            receipt = {
                key: value[key] for key in ("id", "model", "created", "usage") if key in value
            }
            if receipt and receipt not in self.provider_receipts:
                self.provider_receipts.append(receipt)
        return super()._parse(payload, request, state)

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        self.provider_receipts = []
        target = self.evidence / (canonical_hash(request.operation_id) + ".json")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        record: dict[str, Any] = {
            "operation_id": request.operation_id,
            "request_sha256": canonical_hash(request.model_dump(mode="json")),
            "endpoint": ENDPOINT,
            "capabilities": FLASH.model_dump(mode="json"),
            "started_at": datetime.now(UTC).isoformat(),
            "physical_attempts": 1,
            "reasoning_policy": DEEPSEEK_REASONING_POLICY,
            "request_options": deepseek_request_options(),
        }
        json.dump({**record, "state": "dispatching"}, stream)
        stream.flush()
        os.fsync(stream.fileno())
        try:
            response = super().complete(request, cancellation)
            record.update(
                finish_reason=response.finish_reason,
                normalized_usage=response.usage.model_dump(mode="json"),
                response_sha256=canonical_hash(response.model_dump(mode="json")),
            )
            return response
        except BaseException as exc:
            record["error_code"] = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            record.update(
                finished_at=datetime.now(UTC).isoformat(), provider_receipts=self.provider_receipts
            )
            # A second attempt under the same operation id cannot overwrite a
            # first physical-send record, even when its usage was unknown.
            with stream:
                stream.seek(0)
                stream.truncate()
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
