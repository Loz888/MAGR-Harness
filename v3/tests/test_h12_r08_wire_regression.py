"""Synthetic replay of the R08 wire-limit failure; never contacts a provider."""

import base64
import json

import httpx
import pytest
from test_harness_v4_h02 import _client, _request

from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.model import DeepSeekChatAdapter, ModelProtocolError
from mgar_v3.quality_eval.h12_live import FLASH


def frame(payload):
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class SyntheticStream(httpx.SyncByteStream):
    def __init__(self, padding=False):
        self.padding = padding

    def __iter__(self):
        if self.padding:
            for _ in range(40):
                yield frame({"choices": [], "padding": "x" * 65000})
            return
        reasoning = frame({
            "id": "synthetic-r08-wire-limit-00000000000000",
            "object": "chat.completion.chunk", "model": FLASH.model_id, "created": 0,
            "system_fingerprint": "synthetic-not-a-provider-receipt",
            "choices": [{"index": 0, "delta": {"reasoning_content": "x"},
                         "finish_reason": None}],
        })
        assert len(reasoning) * 8191 > 2 * 1024 * 1024
        for _ in range(8191):
            yield reasoning
        yield frame({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        yield frame({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 8192,
                     "completion_tokens_details": {"reasoning_tokens": 8191}}})
        yield b"data: [DONE]\n\n"


def request(tokens):
    return _request().model_copy(update={
        "model_id": FLASH.model_id, "price_version": FLASH.price.version,
        "max_output_tokens": tokens,
    })


def test_legal_8192_token_stream_crosses_2mib_and_keeps_final_usage_without_retry():
    posts = []
    adapter = DeepSeekChatAdapter(
        api_key="synthetic", capabilities=FLASH, max_attempts=1,
        client=_client(lambda req: posts.append(req.method)
                       or httpx.Response(200, stream=SyntheticStream())),
    )
    result = adapter.complete(request(8192), CancellationToken())
    assert result.text == "ok" and result.finish_reason == "stop"
    assert result.usage.known and result.usage.output_tokens == 8192
    assert result.output_receipt.usage_status == "known"
    snapshots = [json.loads(v) for v in adapter.output_capture.memory.values()]
    parts = sorted((s for s in snapshots if "wire_base64" in s), key=lambda s: s["wire_sequence"])
    wire = b"".join(base64.b64decode(s["wire_base64"]) for s in parts)
    assert len(wire) > 2 * 1024 * 1024
    assert b'"completion_tokens": 8192' in wire and wire.endswith(b"data: [DONE]\n\n")
    assert posts == ["POST"]


def test_excessive_framing_still_stops_under_the_small_output_envelope_without_retry():
    posts = []
    adapter = DeepSeekChatAdapter(
        api_key="synthetic", capabilities=FLASH, max_attempts=1,
        client=_client(lambda req: posts.append(req.method)
                       or httpx.Response(200, stream=SyntheticStream(padding=True))),
    )
    with pytest.raises(ModelProtocolError, match="wire output exceeded"):
        adapter.complete(request(128), CancellationToken())
    snapshots = [json.loads(v) for v in adapter.output_capture.memory.values()]
    assert any(s.get("response_status") == "unknown" for s in snapshots)
    assert posts == ["POST"]
