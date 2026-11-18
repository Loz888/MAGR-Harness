"""Thinking/max wire, private continuation, context accounting and real PG recovery."""

import json
from uuid import uuid4

import httpx
import pytest
from test_harness_product_models import request
from test_harness_v4_h10 import _context_fixture
from test_harness_v4_h11_service import _InjectedCrash, _request, _service

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import CancellationToken, ModelMessage, ModelResponse
from mgar_v3.harness.long_context import _message_tokens
from mgar_v3.harness.model import DEEPSEEK_CHAT, DeepSeekChatAdapter
from mgar_v3.harness.product_models import (
    ProductDeepSeekChatAdapter,
    product_model_capabilities,
    product_request_reservation,
)
from mgar_v3.harness.provider_context import ProviderContextStore
from mgar_v3.models.deepseek_policy import deepseek_request_options
from mgar_v3.quality_eval.h12_live import FLASH, RecordedFlashModel

PRIVATE = "PRIVATE_PROVIDER_CONTINUATION_中文🙂"


def response_wire(*, tool=False, reasoning=PRIVATE, text="source.py defines value = 1"):
    delta = {"tool_calls": [{"index": 0, "id": "read-source", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"source.py"}'}}]
             } if tool else {"content": text}
    chunks = [{"choices": [{"delta": {"reasoning_content": part}}]}
              for part in (reasoning[:8], reasoning[8:])]
    chunks += [{"choices": [{"delta": delta}]},
               {"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
               {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                "completion_tokens_details": {"reasoning_tokens": 30}}}]
    return ("".join("data: " + json.dumps(row) + "\n\n" for row in chunks)
            + "data: [DONE]\n\n").encode()


@pytest.mark.parametrize("kind", ["base", "product", "h12"])
def test_every_harness_adapter_explicitly_enables_max_without_changing_output_cap(tmp_path, kind):
    sent = []
    def handle(wire):
        sent.append(json.loads(wire.content))
        return httpx.Response(200, content=response_wire(tool=True))
    client = httpx.Client(transport=httpx.MockTransport(handle))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    if kind == "h12":
        adapter = RecordedFlashModel(api_key="fixture-key", client=client, artifacts=artifacts,
                                    evidence=tmp_path / "receipts")
        q = request().model_copy(update={"price_version": FLASH.price.version})
    elif kind == "product":
        adapter = ProductDeepSeekChatAdapter(api_key="fixture-key", client=client,
                                            artifacts=artifacts)
        q = request()
    else:
        adapter = DeepSeekChatAdapter(api_key="fixture-key", client=client, artifacts=artifacts,
                                     capabilities=product_model_capabilities())
        q = request()
    result = adapter.complete(q, CancellationToken())
    assert result.provider_context is not None
    assert result.usage.output_tokens == 40  # Includes the 30 reasoning tokens, counted once.
    assert sent[0]["thinking"] == {"type": "enabled"}
    assert sent[0]["reasoning_effort"] == "max"
    assert sent[0]["max_tokens"] == q.max_output_tokens == 8192
    assert PRIVATE not in result.model_dump_json()
    if kind == "h12":
        receipt = next((tmp_path / "receipts").glob("*.json")).read_text()
        assert PRIVATE not in receipt
        assert json.loads(receipt)["request_options"] == deepseek_request_options()


def test_private_context_survives_serialization_new_adapter_and_all_assistant_turns(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    sent = []
    def handle(wire):
        body = json.loads(wire.content)
        sent.append(body)
        if len(sent) > 1:
            assistants = [m for m in body["messages"] if m["role"] == "assistant"]
            assert len(assistants) == len(sent) - 1
            assert all(m["reasoning_content"] == PRIVATE for m in assistants)
        return httpx.Response(200, content=response_wire(tool=len(sent) == 1))
    client = httpx.Client(transport=httpx.MockTransport(handle))
    q = request()
    history = list(q.messages)
    for index in range(3):
        adapter = ProductDeepSeekChatAdapter(api_key="fixture-key", client=client,
                                            artifacts=ArtifactStore(artifacts.root))
        result = adapter.complete(q.model_copy(update={"messages": tuple(history)}),
                                  CancellationToken())
        # This is the same JSON boundary used by durable known-response recovery.
        result = ModelResponse.model_validate_json(result.model_dump_json())
        message = ModelMessage(role="assistant", content=result.text, tool_calls=result.tool_calls,
                               provider_context=result.provider_context)
        history.append(ModelMessage.model_validate_json(message.model_dump_json()))
        if index == 0:
            history.append(ModelMessage(role="tool", tool_call_id="read-source",
                                        content="value = 1"))
        else:
            history.append(ModelMessage(role="user", content="Continue with the same evidence"))
    assert len(sent) == 3
    assert PRIVATE not in json.dumps([m.model_dump(mode="json") for m in history])


@pytest.mark.parametrize("change", [
    "run", "workspace", "actor", "future-epoch", "missing", "corrupt",
])
def test_foreign_or_lost_private_context_is_rejected_before_network(tmp_path, change):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    q = request()
    store = ProviderContextStore(artifacts)
    ref = store.put(PRIVATE, q)
    if change == "missing":
        artifacts.path(ref.artifact_hash).unlink()
    elif change == "corrupt":
        artifacts.path(ref.artifact_hash).write_bytes(b"corrupt")
    else:
        field, value = {"run": ("run_id", uuid4()), "workspace": ("workspace_id", "other"),
                        "actor": ("actor", "other"), "future-epoch": ("execution_epoch", 0)}[change]
        q = q.model_copy(update={"task": q.task.model_copy(update={field: value})})
    called = []
    adapter = ProductDeepSeekChatAdapter(api_key="fixture-key", artifacts=artifacts,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: called.append(r))))
    q = q.model_copy(update={"messages": (*q.messages, ModelMessage(role="assistant", content="",
                                                                  provider_context=ref))})
    with pytest.raises(DomainError, match="provider continuation"):
        adapter.complete(q, CancellationToken())
    assert not called


def test_private_bytes_enter_budget_quotes_and_small_window_compaction(tmp_path):
    manager, task, state = _context_fixture(tmp_path)
    q = request().model_copy(update={"task": task})
    store = ProviderContextStore(manager.artifacts)
    plain = ModelMessage(role="assistant", content="Observed evidence")
    private = plain.model_copy(update={"provider_context": store.put("r" * 2000, q)})
    assert _message_tokens((private,)) >= _message_tokens((plain,)) + 2000
    without = q.model_copy(update={"messages": (plain,)})
    with_private = q.model_copy(update={"messages": (private,)})
    assert product_request_reservation(with_private, product_model_capabilities()) > (
        product_request_reservation(without, product_model_capabilities()))
    history = []
    for index in range(8):
        history += [private, ModelMessage(role="user", content=f"Next evidence {index}")]
    view = manager.prepare(task, (), history, state)
    record = manager.metrics[-1]
    assert record.compacted and record.input_tokens <= manager.max_tokens
    assert len(view.messages) < len(history) + 2
    assert PRIVATE not in json.dumps(record.model_dump(mode="json"))
    for message in view.messages:
        if message.provider_context:
            assert store.read(message.provider_context, q) == "r" * 2000


@pytest.mark.parametrize("first_tool", [True, False])
def test_real_pg_recovery_reuses_known_reply_and_roundtrips_private_context(
    engine, settings, tmp_path, first_tool
):
    wires = []
    def handle(wire):
        body = json.loads(wire.content)
        wires.append(body)
        if len(wires) > 1:
            assistants = [m for m in body["messages"] if m["role"] == "assistant"]
            assert assistants and all(m["reasoning_content"] == PRIVATE for m in assistants)
        tool = len(wires) == 1 if first_tool else len(wires) == 2
        text = "source.py defines value = 1" if first_tool or len(wires) > 2 else "Need source"
        return httpx.Response(200, content=response_wire(tool=tool, text=text))

    class Model:
        def __init__(self, artifacts):
            self.artifacts = artifacts
        def complete(self, q, cancellation):
            caps = DEEPSEEK_CHAT.model_copy(update={"model_id": q.model_id,
                "price": DEEPSEEK_CHAT.price.model_copy(update={"version": q.price_version})})
            adapter = DeepSeekChatAdapter(api_key="fixture-key", capabilities=caps,
                artifacts=self.artifacts, max_attempts=1,
                client=httpx.Client(transport=httpx.MockTransport(handle)))
            return adapter.complete(q, cancellation)

    def crash(point, payload):
        if point == "model_after_settlement":
            raise _InjectedCrash("after known response, before the tool exchange")
    service = _service(engine, settings, Model(engine.artifacts), crash)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    before = engine.harness.tasks()[0]
    assert len(wires) == 1
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        resumed = _service(replacement, settings, Model(replacement.artifacts)).recover_task(
            before["task_id"])
        assert resumed["status"] == "completed", resumed
        assert len(wires) == (2 if first_tool else 3)
        assert replacement.harness.get(before["task_id"])["run_id"] == before["run_id"]
        assert PRIVATE not in json.dumps(resumed, default=str)
        assert PRIVATE not in json.dumps([e.model_dump(mode="json") for e in
                                         replacement.harness.events(before["task_id"])])
    finally:
        replacement.close()
