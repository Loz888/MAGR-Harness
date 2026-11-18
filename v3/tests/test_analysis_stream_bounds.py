"""Large protocol envelopes and terminal receipts must not lose known usage."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from test_analysis_response_receipts import SOURCE, environment
from test_analysis_response_receipts import resolved as resolved

from mgar_v3.repository_analysis.model import DeepSeekAnalysisModel
from mgar_v3.repository_analysis.streaming import StreamReadError, read_completion


def event(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


def wire(model, content, *, usage=True, done=True, finish=True, metadata=0):
    parts = []
    for index in range(0, len(content), 2):
        parts.append(event({
            "model": model, "id": "provider-envelope-" + "x" * metadata,
            "choices": [{"index": 0, "delta": {"content": content[index:index + 2]}}],
        }))
    if finish:
        parts.append(event({"model": model, "choices": [{"finish_reason": "stop"}]}))
    if usage:
        parts.append(event({"model": model, "choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 100,
        }}))
    if done:
        parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


def test_framework_run_survives_metadata_wire_above_old_limit(
    settings, engine, resolved, monkeypatch,
):
    service, value = environment(settings, engine, monkeypatch)
    wires = []

    def provider(req):
        body = json.loads(req.content)
        payload = json.loads(body["messages"][1]["content"])
        if "source_checks" in payload:
            decision = {"accepted": True, "missing_requirements": [], "summary": "已回答。"}
        elif not any(item.get("evidence_id") for item in payload["history"]):
            decision = {"action": "tool", "thought": "read the manifest",
                        "tool_calls": [{"tool": "read_file", "path": "pyproject.toml"}]}
        else:
            observed = next(x for x in payload["history"] if x.get("evidence_id"))
            claim = {"statement": "使用FastAPI与uvicorn", "evidence_id": observed["evidence_id"],
                     "quote": 'dependencies = ["fastapi", "uvicorn"]'}
            decision = {"action": "final", "thought": "the source answers the question",
                        "report": {"conclusion": "FastAPI + uvicorn", "structure": SOURCE,
                                   "key_files": [claim], "findings": [claim]}}
        raw = wire(body["model"], json.dumps(decision, ensure_ascii=False), metadata=3000)
        wires.append(raw)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)

    original = service._runtime

    def runtime(spec):
        result = original(spec)
        result.model = DeepSeekAnalysisModel(
            service.settings, spec.model,
            client=httpx.Client(transport=httpx.MockTransport(provider)),
            rate_limit=lambda *args: None,
        )
        return result

    monkeypatch.setattr(service, "_runtime", runtime)
    service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert max(map(len, wires)) > 512 * 1024
    assert detail["run"]["run_status"] == "succeeded", detail["run"]["stop_reason"]
    assert detail["budget"]["unknown_calls"] == 0
    assert detail["budget"]["reserved_micros"] == 0
    assert detail["budget"]["settled_micros"] > 0
    assert "FastAPI" in detail["report"]


def test_terminal_usage_is_sufficient_without_optional_done_marker():
    raw = wire("declared-model", '{"answer":"ready"}', done=False)
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)
    value, receipt = read_completion(response, lambda text: None, lambda: None)
    assert value["usage"] == {"prompt_tokens": 100, "completion_tokens": 100}
    assert value["choices"][0]["message"]["content"] == '{"answer":"ready"}'
    assert receipt.wire_bytes == len(raw)
    assert receipt.wire_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("missing", ["usage", "finish"])
def test_missing_terminal_facts_remain_unconfirmed(missing):
    raw = wire("declared-model", '{"answer":"ready"}', **{missing: False})
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)
    with pytest.raises(ValueError):
        read_completion(response, lambda text: None, lambda: None)


class Disconnect(httpx.SyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    def __iter__(self):
        yield self.raw
        raise httpx.ReadError("PRIVATE_TRANSPORT_DETAIL")


def test_disconnect_after_terminal_usage_keeps_receipt_but_missing_usage_does_not():
    for usage in [True, False]:
        raw = wire("declared-model", '{"answer":"ready"}', usage=usage, done=False)
        response = httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Disconnect(raw),
        )
        if usage:
            value, receipt = read_completion(response, lambda text: None, lambda: None)
            assert value["usage"]["completion_tokens"] == 100
            assert receipt.completion_boundary == "terminal_usage_transport_end"
        else:
            with pytest.raises(StreamReadError) as error:
                read_completion(response, lambda text: None, lambda: None)
            assert error.value.reason == "transport_error"
            assert "PRIVATE_TRANSPORT_DETAIL" not in json.dumps(error.value.diagnostics)


@pytest.mark.parametrize("boundary", ["wire", "frame", "content"])
def test_stream_resource_limits_report_safe_diagnostics(boundary):
    secret = "PRIVATE_PROVIDER_REASONING"
    if boundary == "wire":
        raw = event({"choices": [], "private_metadata": secret + "x" * 4096}) * 600
    elif boundary == "frame":
        raw = event({"choices": [], "private_metadata": secret + "x" * (600 * 1024)})
    else:
        raw = event({"choices": [{"delta": {"content": "x" * 2048}}]}) * 260
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)
    with pytest.raises(StreamReadError) as error:
        read_completion(response, lambda text: None, lambda: None, max_output_tokens=512)
    assert error.value.reason == boundary + "_limit"
    diagnostic = error.value.diagnostics
    assert diagnostic["wire_bytes"] == len(raw)
    assert diagnostic["wire_sha256"] == hashlib.sha256(raw).hexdigest()
    assert secret not in json.dumps(diagnostic)
    assert secret not in str(error.value)


def test_intermediate_usage_before_finish_is_not_a_terminal_receipt():
    raw = (
        event({"model": "declared-model", "usage": {
            "prompt_tokens": 100, "completion_tokens": 1,
        }, "choices": [{"delta": {"content": '{"answer":"ready"}'}}]})
        + event({"choices": [{"finish_reason": "stop"}]})
        + b"data: [DONE]\n\n"
    )
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)
    with pytest.raises(StreamReadError) as error:
        read_completion(response, lambda text: None, lambda: None)
    assert error.value.reason == "usage_before_finish"


def test_unknown_model_call_persists_reason_without_retry_or_private_body(settings):
    from pydantic import SecretStr
    from test_analysis_streaming import Chunks, Ledger, complete, completion_text

    from mgar_v3.domain import DomainError

    raw = wire("deepseek-chat", completion_text(), usage=False)
    source = Chunks([raw])
    ledger = Ledger(source)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=source,
    ))
    with pytest.raises(DomainError) as error:
        complete(settings.model_copy(update={"deepseek_api_key": SecretStr("test-key")}),
                 transport, ledger)
    assert error.value.code == "USAGE_UNKNOWN"
    assert ledger.reservations == 1 and ledger.settlements == [None]
    diagnostic = next(payload for kind, payload in ledger.events if kind == "model_stream_failed")
    assert diagnostic["reason"] == "missing_usage"
    assert diagnostic["wire_bytes"] == len(raw)
    assert "content" not in diagnostic and "raw" not in diagnostic


def test_dense_protocol_overhead_is_hashed_without_retaining_the_wire():
    class DenseMetadata(httpx.SyncByteStream):
        def __init__(self):
            self.received = 0
            self.digest = hashlib.sha256()

        def __iter__(self):
            def frame(text):
                return event({
                    "model": "declared-model", "provider_metadata": "DENSE_METADATA_ONLY" * 170,
                    "choices": [{"delta": {"content": text}}],
                })

            def record(chunk):
                self.received += len(chunk)
                self.digest.update(chunk)
                return chunk

            yield record(frame('{"answer":"'))
            for _ in range(3000):
                yield record(frame("xy"))
            yield record(frame('"}'))
            yield record(event({"model": "declared-model", "choices": [{
                "delta": {}, "finish_reason": "stop",
            }]}))
            yield record(event({"model": "declared-model", "choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 2000,
            }}))
            yield record(b"data: [DONE]\n\n")

    source = DenseMetadata()
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
    value, receipt = read_completion(response, lambda text: None, lambda: None,
                                     max_output_tokens=8192)
    assert 9 * 1024 * 1024 < source.received < 32 * 1024 * 1024
    assert value["choices"][0]["message"]["content"] == '{"answer":"' + "xy" * 3000 + '"}'
    assert receipt.wire_bytes == source.received
    assert receipt.wire_sha256 == source.digest.hexdigest()
    assert "DENSE_METADATA_ONLY" not in repr(receipt)
