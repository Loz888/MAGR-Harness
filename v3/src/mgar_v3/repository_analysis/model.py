"""DeepSeek JSON-mode client constrained by the repository-analysis ledger."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

import httpx
from pydantic import ValidationError

from mgar_v3.context.policy import ContextPolicy, message_input_bound
from mgar_v3.domain import DomainError, Model
from mgar_v3.models.deepseek_policy import deepseek_request_options
from mgar_v3.rate_limit import acquire
from mgar_v3.repository_analysis.domain import AnalysisModelConfig
from mgar_v3.repository_analysis.streaming import (
    PreviewRelay,
    StreamReadError,
    WireReceipt,
    read_completion,
)
from mgar_v3.settings import Settings

T = TypeVar("T", bound=Model)
MAX_RECEIPT_BYTES = 256 * 1024


def public_usage(value: dict[str, Any]) -> dict[str, Any]:
    """Keep provider usage counts; never persist arbitrary nested response text."""
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key, names in (
        ("prompt_tokens_details", ("cached_tokens",)),
        ("completion_tokens_details", ("reasoning_tokens", "accepted_prediction_tokens",
                                      "rejected_prediction_tokens")),
    ):
        details = value.get(key)
        if isinstance(details, dict):
            result[key] = {name: details[name] for name in names
                           if type(details.get(name)) is int and details[name] >= 0}
    return result


def completion_receipt(
    value: dict[str, Any], raw: bytes | WireReceipt, result: Model | None,
) -> tuple[bytes, bool]:
    """Persist bounded public semantics rather than SSE transport overhead.

    Only a strictly validated result enters this artifact, without agent thought
    or provider reasoning. Invalid output retains a digest and checked usage.
    """
    public = result.model_dump(mode="json", exclude={"thought"}) if result is not None else {}
    content = json.dumps(public, ensure_ascii=False, separators=(",", ":"))
    receipt: dict[str, Any] = {
        "schema_version": "analysis-provider-receipt-v1",
        "model": value["model"], "usage": public_usage(value["usage"]),
        "content_validated": result is not None,
        "choices": [{"message": {"content": content}}],
        "transport": (
            {"wire_bytes": raw.wire_bytes, "wire_sha256": raw.wire_sha256,
             "frames": raw.frames, "content_bytes": raw.content_bytes,
             "completion_boundary": raw.completion_boundary}
            if isinstance(raw, WireReceipt) else
            {"wire_bytes": len(raw), "wire_sha256": hashlib.sha256(raw).hexdigest()}
        ),
    }

    def encoded() -> bytes:
        return json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode()

    body = encoded()
    oversized = len(body) > MAX_RECEIPT_BYTES
    if oversized:
        receipt["choices"][0]["message"] = {"content": "", "content_omitted": True}
        receipt["transport"]["public_content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        body = encoded()
    return body, oversized


class AnalysisLedger(Protocol):
    def repository_model_result(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        schema: type[T],
    ) -> T | None: ...

    def reserve_repository_call(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        reservation_micros: int,
        call_limit: int | None,
        token_reservation: int = 0,
    ) -> UUID: ...

    def settle_call(
        self,
        call_id: UUID,
        cost_micros: int | None,
        usage: dict[str, Any] | None = None,
        result: Model | None = None,
    ) -> None: ...

    def record_model_input(
        self, run_id: UUID, epoch: int, step_key: str, body: dict[str, Any]
    ) -> None: ...

    def record_model_response(self, call_id: UUID, status: int, body: bytes) -> None: ...

    def note_input_tokens(self, run_id: UUID, tokens: int) -> None: ...


class DeepSeekAnalysisModel:
    def __init__(
        self,
        settings: Settings,
        config: AnalysisModelConfig,
        *,
        client: httpx.Client | None = None,
        rate_limit: Any = acquire,
    ):
        self.settings = settings
        self.config = config
        self.client = client
        self.rate_limit = rate_limit

    def complete(
        self,
        ledger: AnalysisLedger,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        system: str,
        payload: dict[str, Any],
        schema: type[T],
        *,
        call_limit: int | None,
        deadline_check: Callable[[], None],
    ) -> T:
        key = self.settings.deepseek_api_key
        if key is None:
            raise DomainError("MODEL_CREDENTIAL_REQUIRED", "DeepSeek credential is missing", 403)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": encoded}]
        phase = "analysis" if schema.__name__ == "AgentDecision" else "validation"

        def emit(kind: str, **data: Any) -> None:
            recorder = getattr(ledger, "stream_event", None)
            if callable(recorder):
                recorder(run_id, epoch, kind, **data)

        for attempt in range(3):
            attempt_key = step_key + ":retry" * attempt
            existing = ledger.repository_model_result(
                run_id, epoch, attempt_key, profile_hash, schema
            )
            if existing is not None:
                return existing
            deadline_check()
            input_bound = message_input_bound(messages)
            maximum_output = self.config.max_output_tokens
            spec_reader = getattr(ledger, "spec", None)
            if callable(spec_reader):
                spec = spec_reader(run_id)
                if input_bound > spec.limits.max_input_tokens - spec.input_tokens_used:
                    raise DomainError("INPUT_TOKEN_BUDGET", "Input token budget exhausted")
                if spec.limits.max_total_tokens is not None:
                    remaining = (
                        spec.limits.max_total_tokens
                        - spec.input_tokens_used
                        - spec.output_tokens_used
                        - spec.reserved_tokens
                    )
                    maximum_output = min(maximum_output, remaining - input_bound)
                    if maximum_output < 512:
                        raise DomainError("TOKEN_BUDGET_EXHAUSTED", "Token budget exhausted")
            window = (self.config.context_policy or ContextPolicy()).window(
                self.config.context_tokens, self.config.max_output_tokens
            )
            if input_bound + maximum_output > window:
                raise DomainError("MODEL_INPUT_LIMIT", "Request exceeds the model context window")
            reservation = (
                input_bound * self.config.input_micros_per_million
                + maximum_output * self.config.output_micros_per_million
                + 999999
            ) // 1000000
            body = {
                "model": self.config.model_id,
                "max_tokens": maximum_output,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": messages,
                **deepseek_request_options(),
            }
            ledger.record_model_input(run_id, epoch, attempt_key, body)
            for waiting in range(13):
                try:
                    self.rate_limit(
                        self.settings.redis_url.get_secret_value(),
                        "repo-model:" + profile_hash + ":" + str(run_id),
                    )
                    break
                except DomainError as exc:
                    if exc.code != "RATE_LIMITED":
                        raise
                    if waiting == 12:
                        raise DomainError(
                            "MODEL_RATE_LIMITED", "Model service is temporarily unavailable"
                        ) from None
                    deadline_check()
                    if callable(spec_reader):
                        current = spec_reader(run_id)
                        if current.run_status != "running" or current.execution_epoch != epoch:
                            raise DomainError(
                                "FENCED", "Analysis was paused or cancelled"
                            ) from None
                    if waiting == 0:
                        emit("analysis_waiting")
                    time.sleep(5)
            try:
                client = self.client or httpx.Client(timeout=60)
            except (OSError, httpx.HTTPError):
                raise DomainError(
                    "MODEL_CLIENT_UNAVAILABLE", "Model client is unavailable"
                ) from None
            call_id: UUID | None = None
            requested = False
            response_opened = False
            cost: int | None = None
            usage: dict[str, Any] | None = None
            accepted_result: T | None = None
            try:
                call_id = ledger.reserve_repository_call(
                    run_id, epoch, attempt_key, profile_hash, reservation, call_limit,
                    token_reservation=input_bound + maximum_output,
                )
                emit("model_stream_started", step_key=attempt_key, phase=phase)

                def send_preview(
                    field: str, text: str, offset: int, step: str = attempt_key,
                ) -> None:
                    emit("answer_delta", step_key=step, field=field, text=text, offset=offset)

                relay = PreviewRelay(send_preview)
                requested = True
                with client.stream(
                    "POST", "https://api.deepseek.com/v1/chat/completions", json=body,
                    headers={"Authorization": "Bearer " + key.get_secret_value()},
                ) as response:
                    response_opened = True
                    status = response.status_code
                    if not response.is_success:
                        if status in {400, 401, 403, 404, 422, 429}:
                            cost = 0  # Definitive rejection before model generation.
                        # Provider error bodies can echo private inputs. A status
                        # receipt is sufficient; do not persist unbounded text.
                        ledger.record_model_response(call_id, status, json.dumps({
                            "schema_version": "analysis-provider-rejection-v1",
                            "http_status": status,
                            "usage_known": cost == 0,
                        }).encode())
                        if status == 429 and attempt < 2:
                            try:
                                delay = float(response.headers.get("Retry-After", "1"))
                            except ValueError:
                                delay = 1.0
                            if not 0 <= delay <= 10:
                                raise DomainError("MODEL_RATE_LIMITED", "Provider is rate limited")
                            time.sleep(delay)
                            continue
                        if cost == 0:
                            raise DomainError("MODEL_PROVIDER_ERROR", "Provider rejected request")
                        raise DomainError("USAGE_UNKNOWN", "Provider outcome is unknown")
                    value, raw = read_completion(
                        response, relay.feed, deadline_check, max_output_tokens=maximum_output,
                    )
                    received_usage = value.get("usage")
                    if (
                        value.get("model") != self.config.model_id
                        or not isinstance(received_usage, dict)
                        or any(
                            type(received_usage.get(k)) is not int or received_usage[k] < 0
                            for k in ("prompt_tokens", "completion_tokens")
                        )
                    ):
                        emit(
                            "model_stream_failed", step_key=attempt_key,
                            reason=("model_mismatch" if value.get("model") != self.config.model_id
                                    else "invalid_usage"),
                            wire_bytes=raw.wire_bytes, wire_sha256=raw.wire_sha256,
                            completion_boundary=raw.completion_boundary,
                        )
                        raise DomainError("USAGE_UNKNOWN", "Model usage is unavailable")
                    checked_cost = (
                        received_usage["prompt_tokens"] * self.config.input_micros_per_million
                        + received_usage["completion_tokens"]
                        * self.config.output_micros_per_million
                        + 999999
                    ) // 1000000
                    if (
                        checked_cost > reservation
                        or received_usage["prompt_tokens"] > input_bound
                        or received_usage["completion_tokens"] > maximum_output
                    ):
                        emit(
                            "model_stream_failed", step_key=attempt_key,
                            reason="usage_exceeds_reservation", wire_bytes=raw.wire_bytes,
                            wire_sha256=raw.wire_sha256,
                        )
                        raise DomainError("USAGE_UNKNOWN", "Provider exceeded reserved price bound")
                    # Preserve checked usage before HTTP context cleanup, which
                    # can itself fail after the complete response has arrived.
                    # Local failures cannot erase it; finally settles once.
                    cost = checked_cost
                    usage = public_usage(received_usage)
                result = None
                try:
                    choice = value["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("Truncated output")
                    result = schema.model_validate_json(choice["message"]["content"])
                except (
                    KeyError, IndexError, TypeError, AttributeError, ValueError, ValidationError,
                ):
                    pass
                receipt, content_oversized = completion_receipt(value, raw, result)
                ledger.record_model_response(call_id, status, receipt)
                if content_oversized:
                    raise DomainError(
                        "MODEL_RESPONSE_LIMIT", "Public completion exceeds byte bound"
                    )
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    try:
                        relay.feed(value["choices"][0]["message"]["content"])
                    except (KeyError, IndexError, TypeError):
                        pass
                relay.flush()
                if not getattr(ledger, "accounts_usage", False):
                    ledger.note_input_tokens(run_id, cast(int, usage["prompt_tokens"]))
                if result is None:
                    emit("draft_rejected", reason="MODEL_INVALID_RESPONSE")
                    if attempt < 2:
                        messages = messages[:2] + [{
                            "role": "user", "content": (
                                "Return one complete JSON object matching the schema. "
                                "Use every required field. " + (
                                    "An answer needs key_files and findings with exact quotes."
                                    if phase == "analysis"
                                    else "Use accepted, missing_requirements and summary only."
                                )
                            ),
                        }]
                        continue
                    raise DomainError("MODEL_INVALID_RESPONSE", "Output failed schema validation")
                accepted_result = result
                return result
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if response_opened:
                    code = (
                        "MODEL_RESPONSE_RECORDING_FAILED" if cost is not None else "USAGE_UNKNOWN"
                    )
                    raise DomainError(
                        code, "Provider connection failed during or after the response"
                    ) from None
                cost = 0  # The provider did not receive this request.
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise DomainError(
                    "MODEL_CLIENT_UNAVAILABLE", "Cannot connect to provider"
                ) from None
            except StreamReadError as exc:
                emit("model_stream_failed", step_key=attempt_key, **exc.diagnostics)
                raise DomainError(
                    "USAGE_UNKNOWN", "Stream ended without a complete usage receipt: " + exc.reason,
                ) from None
            except (httpx.HTTPError, ValueError, OSError) as exc:
                emit(
                    "model_stream_failed", step_key=attempt_key,
                    reason="transport_or_local_error", error_type=type(exc).__name__,
                    complete_usage_received=cost is not None,
                )
                code = "MODEL_RESPONSE_RECORDING_FAILED" if cost is not None else "USAGE_UNKNOWN"
                message = (
                    "Model response could not be completed or saved"
                    if cost is not None else "Stream ended without a usage receipt"
                )
                raise DomainError(code, message) from None
            except DomainError as exc:
                if exc.code == "DEADLINE_EXCEEDED" and cost is None:
                    raise DomainError("USAGE_UNKNOWN", "Stream deadline exceeded") from None
                raise
            except Exception as exc:
                emit(
                    "model_stream_failed", step_key=attempt_key,
                    reason="local_processing_error", error_type=type(exc).__name__,
                    complete_usage_received=cost is not None,
                )
                code = "MODEL_RESPONSE_RECORDING_FAILED" if cost is not None else "USAGE_UNKNOWN"
                raise DomainError(code, "Model response receipt could not be processed") from None
            finally:
                try:
                    if call_id is not None:
                        ledger.settle_call(
                            call_id, cost if requested else 0, usage, accepted_result,
                        )
                finally:
                    if self.client is None:
                        client.close()

        raise DomainError("MODEL_INVALID_RESPONSE", "Model retry policy stopped the request")
