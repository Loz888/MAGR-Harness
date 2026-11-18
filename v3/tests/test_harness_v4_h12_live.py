"""Physical paid-call boundaries tested using a controlled HTTP transport."""

import json
from uuid import uuid4

import httpx
import pytest

from mgar_v3.harness.contracts import (
    CancellationToken,
    ModelMessage,
    ModelRequest,
    TaskBinding,
    VerificationRequirement,
)
from mgar_v3.harness.model import ModelProviderRejected
from mgar_v3.quality_eval.h12_live import FLASH, RecordedFlashModel


def request():
    return ModelRequest(
        operation_id="h12-live-test:model:1",
        task=TaskBinding(
            task_id="h12-live-test",
            thread_id=uuid4(),
            run_id=uuid4(),
            execution_epoch=1,
            actor="owner",
            repository="local/fixture",
            base_commit="a" * 40,
            workspace_id="fixture",
            objective="Inspect",
            acceptance_revision="1",
            task_kind="inspect",
            requirements=(VerificationRequirement(requirement_id="inspect"),),
        ),
        messages=(ModelMessage(role="user", content="Inspect fixture"),),
        model_id=FLASH.model_id,
        price_version=FLASH.price.version,
        max_output_tokens=64,
        reservation_micros=10000,
        context_revision="1",
        workspace_tree="b" * 64,
    )


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_h12_never_sends_an_unbudgeted_second_http_attempt(tmp_path, status):
    calls = []

    def reject(http_request):
        calls.append(http_request)
        return httpx.Response(status, json={"error": "controlled refusal"})

    adapter = RecordedFlashModel(
        api_key="fixture-private-key",
        evidence=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(reject)),
    )
    with pytest.raises(ModelProviderRejected):
        adapter.complete(request(), CancellationToken())
    assert len(calls) == 1
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["physical_attempts"] == 1
    assert record["error_code"] == "MODEL_PROVIDER_REJECTED"
    with pytest.raises(FileExistsError):
        adapter.complete(request(), CancellationToken())
    assert len(calls) == 1


def test_h12_receipt_keeps_supplier_identity_usage_and_no_reasoning_or_key(tmp_path):
    calls = []

    def respond(http_request):
        calls.append(json.loads(http_request.content))
        chunks = [
            {
                "id": "supplier-1",
                "model": "DeepSeek-V4.1-Flash-1017",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "done", "reasoning_content": "private-thought"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {
                "id": "supplier-1",
                "model": "DeepSeek-V4.1-Flash-1017",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            },
        ]
        body = "".join("data: " + json.dumps(part) + "\n\n" for part in chunks)
        return httpx.Response(200, text=body + "data: [DONE]\n\n")

    adapter = RecordedFlashModel(
        api_key="fixture-private-key",
        evidence=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = adapter.complete(request(), CancellationToken())
    assert result.text == "done" and result.usage.known
    assert calls[0]["thinking"] == {"type": "enabled"}
    assert calls[0]["reasoning_effort"] == "max"
    text = next(tmp_path.glob("*.json")).read_text()
    assert "private-thought" not in text and "fixture-private-key" not in text
    receipt = json.loads(text)
    assert receipt["provider_receipts"][-1]["usage"]["total_tokens"] == 11
    assert receipt["provider_receipts"][-1]["id"] == "supplier-1"
