"""R02 wire faults use synthetic chunks and never contact a supplier."""

import base64
import json

import httpx
import pytest
from test_harness_v4_h02 import _anthropic_sse, _client, _deepseek_sse, _request

from mgar_v3.harness.contracts import CancellationToken, ToolSpec
from mgar_v3.harness.model import (
    DEEPSEEK_CHAT,
    AnthropicMessagesAdapter,
    DeepSeekChatAdapter,
    ModelProtocolError,
    ModelProviderRejected,
)
from mgar_v3.harness.output_protocol import OutputPolicy
from mgar_v3.harness.runtime import UnknownModelOutcome


def spec():
    return ToolSpec(name="read_file", description="Read a relative file", input_schema={
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False,
    })


def raw(adapter, receipt):
    capture = adapter.output_capture
    return json.loads(capture.memory[receipt.artifact_hash])


@pytest.mark.parametrize("arguments", [
    '{"path":', '{"path":"file', '{}', '{"path":7}', '{"path":null}',
    '{"path":"a","path":"b"}', '{"path":NaN}', '{"path":"x","extra":true}',
    '["source.py"]',
])
def test_incomplete_or_schema_invalid_tool_is_never_executable(arguments):
    payload = _deepseek_sse(arguments=arguments)
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    response = adapter.complete(_request(tools=(spec(),)), CancellationToken())
    assert response.finish_reason == "error" and not response.tool_calls
    assert response.output_receipt.response_status == "invalid"
    captured = raw(adapter, response.output_receipt)
    assert captured["raw_tool_calls"][0]["arguments"] == arguments
    assert captured["execution_status"] == "not_executed"


@pytest.mark.parametrize("arguments", ['{"path":', '{"path":"source.py"}'])
def test_length_preserves_original_finish_and_partial_patch(arguments):
    payload = _deepseek_sse(arguments=arguments, finish="length")
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    response = adapter.complete(_request(tools=(spec(),)), CancellationToken())
    receipt = response.output_receipt
    assert receipt.finish_reason == "length" and receipt.response_status != "complete"
    assert receipt.usage_status == "known" and not response.tool_calls
    assert all(c.execution_status == "not_executed" for c in receipt.tool_calls)
    assert raw(adapter, receipt)["raw_tool_calls"][0]["arguments"] == arguments


def test_fragmented_name_and_arguments_must_reassemble_before_schema_validation():
    frames = [
        {"id": "provider-r02", "choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "r02-call", "function": {"name": "read_", "arguments": '{"pa'},
        }]}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"name": "file", "arguments": 'th":"source.py"}'},
        }]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                   "completion_tokens_details": {"reasoning_tokens": 12}}},
    ]
    payload = b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    response = adapter.complete(_request(tools=(spec(),)), CancellationToken())
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "source.py"}
    assert response.output_receipt.provider_request_id == "provider-r02"
    assert response.usage.reasoning_tokens == 12 and response.usage.output_tokens == 20
    assert response.usage.cost_micros == 54  # reasoning is already in completion, not billed twice.


def test_parallel_batch_rejects_all_when_one_call_is_invalid():
    frames = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "valid", "function": {
                "name": "read_file", "arguments": '{"path":"source.py"}'}},
            {"index": 1, "id": "invalid", "function": {
                "name": "read_file", "arguments": '{"path":'}},
        ]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 10}},
    ]
    payload = b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    response = adapter.complete(_request(tools=(spec(),)), CancellationToken())
    assert not response.tool_calls and response.finish_reason == "error"
    assert len(response.output_receipt.tool_calls) == 2


def test_incomplete_sse_json_retains_exact_wire_bytes_without_retry():
    payload = b'data: {"id":"lost","choices":[{"delta":{"content":"partial'
    posts = []
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda request: posts.append(request.method) or httpx.Response(200, content=payload)))
    with pytest.raises(UnknownModelOutcome):
        adapter.complete(_request(), CancellationToken())
    snapshots = [json.loads(value) for value in adapter.output_capture.memory.values()]
    wire = sorted((v for v in snapshots if "wire_base64" in v), key=lambda v: v["wire_sequence"])
    assert b"".join(base64.b64decode(v["wire_base64"]) for v in wire) == payload
    assert any(v.get("response_status") == "unknown" for v in snapshots)
    assert posts == ["POST"]


def test_bad_sse_json_keeps_capture_and_raises_protocol_error():
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=b"data: {not-json}\n\n")))
    with pytest.raises(ModelProtocolError):
        adapter.complete(_request(), CancellationToken())
    assert any(json.loads(v).get("response_status") == "unknown"
               for v in adapter.output_capture.memory.values())


@pytest.mark.parametrize("name,arguments", [("read_f", '{"path":"x"}'), ("read_file", "")])
def test_incomplete_name_or_missing_arguments_cannot_default_to_executable(name, arguments):
    payload = _deepseek_sse(arguments=arguments).replace(b'"read_file"', json.dumps(name).encode())
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    response = adapter.complete(_request(tools=(spec(),)), CancellationToken())
    assert response.finish_reason == "error" and not response.tool_calls


def test_nested_patch_cut_mid_replacement_is_retained_without_execution():
    patch = '{"edits":[{"path":"source.py","replacements":[{"new_text":"value = 2'
    payload = _deepseek_sse(arguments=patch, finish="length").replace(
        b'"read_file"', b'"apply_edits"')
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    request = _request(tools=(ToolSpec(name="apply_edits", description="Incremental edit",
                                     input_schema={"type": "object", "required": ["edits"]}),))
    response = adapter.complete(request, CancellationToken())
    assert not response.tool_calls and response.output_receipt.finish_reason == "length"
    assert raw(adapter, response.output_receipt)["raw_tool_calls"][0]["arguments"] == patch


def test_anthropic_native_token_stop_is_preserved_separately_from_normalized_finish():
    payload = _anthropic_sse().replace(b'"stop_reason": "tool_use"',
                                       b'"stop_reason": "max_tokens"')
    adapter = AnthropicMessagesAdapter(api_key="fixture", client=_client(
        lambda _: httpx.Response(200, content=payload)))
    response = adapter.complete(_request("anthropic", tools=(spec(),)), CancellationToken())
    assert response.finish_reason == "length" and not response.tool_calls
    assert response.output_receipt.finish_reason == "length"
    assert response.output_receipt.provider_finish_reason == "max_tokens"


def test_500_without_usage_is_not_automatically_reposted():
    posts = []
    adapter = DeepSeekChatAdapter(api_key="fixture", client=_client(
        lambda request: posts.append(request.method) or httpx.Response(500, text="lost")))
    with pytest.raises(ModelProviderRejected):
        adapter.complete(_request(), CancellationToken())
    assert posts == ["POST"]


def test_phase_limits_are_configurable_and_bound_to_declared_provider_capability():
    policy = OutputPolicy(investigate_tokens=1024, implement_tokens=16384,
                          finalize_tokens=2048, continuation_tokens=32768)
    assert policy.budget("investigate", 32768).completion_tokens == 1024
    assert policy.budget("implement", 32768).completion_tokens == 16384
    assert policy.budget("finalize", 32768).completion_tokens == 2048
    assert policy.budget("continuation", 8192).completion_tokens == 8192
    bodies = []
    adapter = DeepSeekChatAdapter(api_key="fixture", capabilities=DEEPSEEK_CHAT.model_copy(
        update={"max_output_tokens": 32768}), client=_client(
            lambda request: bodies.append(json.loads(request.content)) or httpx.Response(
                200, content=b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n')))
    request = _request(max_output_tokens=16384).model_copy(update={
        "output_budget": policy.budget("implement", 32768),
    })
    response = adapter.complete(request, CancellationToken())
    assert response.finish_reason == "stop" and bodies[0]["max_tokens"] == 16384
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "max"
