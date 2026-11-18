"""Complete oversized SSE must keep known usage and public bounded receipts."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from test_analysis_budget_policy import config, request
from test_analysis_budget_policy import resolved as resolved
from test_web_access import browser, signup

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.domain import AgentDecision, FollowUpInput
from mgar_v3.repository_analysis.model import DeepSeekAnalysisModel
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.repository_analysis.tools import RepositoryAnalysisTools, ToolOutcome

SOURCE = '[project]\nrequires-python = ">=3.12"\ndependencies = ["fastapi", "uvicorn"]\n'
PRIVATE = "PRIVATE_PROVIDER_REASONING" * 12000


def stream(model, content, *, usage=True, private=True, tokens=False, finish_reason="stop"):
    frames = []

    def emit(value):
        frames.append(b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n")

    if private:
        emit({"model": model, "choices": [{"delta": {"reasoning_content": PRIVATE}}]})
    for fragment in list(content) if tokens else [content]:
        frame = {"model": model, "choices": [{"index": 0, "delta": {"content": fragment}}]}
        if tokens:
            frame.update({"id": "chatcmpl-" + "c" * 64, "object": "chat.completion.chunk",
                          "created": 1700000000, "system_fingerprint": "f" * 320})
        emit(frame)
    emit({"model": model, "choices": [{"delta": {}, "finish_reason": finish_reason}]})
    if usage:
        emit({"model": model, "choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 100,
        }})
    frames.append(b"data: [DONE]\n\n")
    return b"".join(frames)


def environment(settings, engine, monkeypatch):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        assert client.put("/api/v3/settings/analysis", json=config("cost")).status_code == 200
    value = service.create(request().model_copy(update={"task": "这个项目用的什么框架"}))

    async def execute(tools, call):
        return ToolOutcome(
            tool="read_file", text=SOURCE, content=SOURCE.encode(), evidence_kind="file",
            reference=f"github://{value.repository}@{value.commit}/pyproject.toml",
            source_version="controlled-source-blob",
        )

    monkeypatch.setattr(RepositoryAnalysisTools, "execute", execute)
    return service, value


def test_truncated_analysis_retries_with_compact_copy_of_exact_evidence(
    settings, engine, resolved, monkeypatch,
):
    service, value = environment(settings, engine, monkeypatch)
    requests = []
    read = {"action": "tool", "thought": "brief",
            "tool_calls": [{"tool": "read_file", "path": "pyproject.toml"}]}

    def provider(req):
        body = json.loads(req.content)
        payload = json.loads(body["messages"][1]["content"])
        requests.append(payload)
        finish = "stop"
        if len(requests) <= 2:
            content = json.dumps(read)
        elif len(requests) == 3:
            finish = "length"
            content = '{"action":"final","report":'
            assert payload["progress_instruction"]
            assert sum(item.get("tool") == "read_file" for item in payload["history"]) == 1
        elif len(requests) == 4:
            assert payload["task"] == requests[-2]["task"]
            assert payload["commit"] == value.commit
            assert len(json.dumps(payload)) < len(json.dumps(requests[-2]))
            observation = next(item for item in payload["history"] if item.get("evidence_id"))
            quote = 'dependencies = ["fastapi", "uvicorn"]'
            assert quote in observation["content"]
            claim = {"statement": "Web 框架是 FastAPI。", "evidence_id": observation["evidence_id"],
                     "quote": quote}
            content = json.dumps({"action": "final", "thought": "brief", "report": {
                "conclusion": "Web 框架是 FastAPI。",
                "structure": "依赖在 pyproject.toml 声明。",
                "key_files": [claim], "findings": [claim],
            }}, ensure_ascii=False)
        else:
            assert len(requests) == 5 and "source_checks" in payload
            content = json.dumps({"accepted": True, "summary": "完整", "missing_requirements": []})
        assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "max"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=stream(body["model"], content, private=False, finish_reason=finish),
        )

    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        monkeypatch.setattr(
            "mgar_v3.repository_analysis.service.DeepSeekAnalysisModel",
            lambda settings, model: DeepSeekAnalysisModel(
                settings, model, client=remote, rate_limit=lambda *args: None,
            ),
        )
        service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert detail["run"]["run_status"] == "succeeded", detail["run"]["stop_reason"]
    assert "FastAPI" in detail["report"] and len(requests) == 5
    with engine.transaction() as conn:
        receipts = conn.execute(
            "SELECT safe_payload FROM app.run_events WHERE run_id=%s "
            "AND kind='model_response_recorded' ORDER BY event_id", (value.run_id,),
        ).fetchall()
        usage = conn.execute(
            "SELECT status,count(*) AS n FROM app.usage_calls WHERE run_id=%s GROUP BY status",
            (value.run_id,),
        ).fetchall()
    failed = json.loads(engine.artifacts.read(receipts[2]["safe_payload"]["content_hash"]))
    assert failed["output_issue"] == "MODEL_OUTPUT_TRUNCATED"
    assert usage == [{"status": "known", "n": 5}]
    assert detail["budget"]["reserved_micros"] == 0


@pytest.mark.parametrize("wire_kind", ["private_frame", "public_token_frames"])
def test_framework_question_completes_with_oversized_wire_known_usage_and_exact_evidence(
    settings, engine, resolved, monkeypatch, wire_kind,
):
    service, value = environment(settings, engine, monkeypatch)
    requests = []
    wires = []

    def provider(req):
        body = json.loads(req.content)
        payload = json.loads(body["messages"][1]["content"])
        requests.append(payload)
        if "source_checks" in payload:
            decision = {"accepted": True, "missing_requirements": [], "summary": "问题已回答。"}
        elif not any(item.get("evidence_id") for item in payload["history"]):
            decision = {"action": "tool", "thought": "INTERNAL_THOUGHT",
                        "tool_calls": [{"tool": "read_file", "path": "pyproject.toml"}]}
        else:
            observation = next(item for item in payload["history"] if item.get("evidence_id"))
            claim = {"statement": "使用FastAPI与uvicorn。",
                     "evidence_id": observation["evidence_id"],
                     "quote": 'dependencies = ["fastapi", "uvicorn"]'}
            decision = {"action": "final", "thought": "INTERNAL_THOUGHT", "report": {
                "conclusion": "Python 3.12 后端，Web框架为FastAPI，由uvicorn运行。",
                "structure": "pyproject.toml声明运行时与依赖。",
                "key_files": [claim], "findings": [claim],
            }}
        wire = stream(body["model"], json.dumps(decision, ensure_ascii=False),
                      private=wire_kind == "private_frame",
                      tokens=wire_kind == "public_token_frames")
        assert len(wire) < 524288
        wires.append(wire)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        monkeypatch.setattr(
            "mgar_v3.repository_analysis.service.DeepSeekAnalysisModel",
            lambda settings, model: DeepSeekAnalysisModel(
                settings, model, client=remote, rate_limit=lambda *args: None,
            ),
        )
        service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert detail["run"]["run_status"] == "succeeded", detail["run"]["stop_reason"]
    assert "FastAPI" in detail["report"]
    assert len(requests) == 3  # Evidence collection, answer, independent acceptance.
    assert any(len(wire) > 262144 for wire in wires)
    assert len(detail["claims"]) == 2
    assert detail["budget"]["reserved_micros"] == detail["budget"]["unknown_calls"] == 0
    assert detail["budget"]["settled_micros"] == 450
    response_events = [e for e in detail["events"] if e["kind"] == "model_response_recorded"]
    assert len(response_events) == 3
    for event, wire in zip(response_events, wires, strict=True):
        recorded = engine.artifacts.read(event["payload"]["content_hash"])
        receipt = json.loads(recorded)
        assert len(recorded) <= 262144
        assert "PRIVATE_PROVIDER_REASONING" not in recorded.decode()
        assert "INTERNAL_THOUGHT" not in recorded.decode()
        assert receipt["transport"]["wire_bytes"] == len(wire)
        assert receipt["transport"]["wire_sha256"] == hashlib.sha256(wire).hexdigest()
        assert receipt["usage"] == {"prompt_tokens": 100, "completion_tokens": 100}
    with engine.transaction() as conn:
        calls = conn.execute(
            "SELECT status,usage FROM app.usage_calls WHERE run_id=%s", (value.run_id,),
        ).fetchall()
    assert all(call["status"] == "known" for call in calls)
    assert service.follow_up(value.run_id, FollowUpInput(
        client_request_id=str(uuid4()), body="继续说明入口。",
    )).thread_id == value.thread_id


@pytest.mark.parametrize("status,expected", [(200, 150), (400, 0), (500, None)])
def test_local_receipt_recording_failure_keeps_known_or_unknown_usage_correct(
    settings, engine, resolved, monkeypatch, status, expected,
):
    service, value = environment(settings, engine, monkeypatch)
    service.store.begin_run(value.run_id)
    value = service.store.spec(value.run_id)
    sent = []

    def provider(req):
        sent.append(req)
        decision = {"action": "insufficient", "thought": "brief",
                    "insufficient_reason": "Missing controlled source."}
        return httpx.Response(status, json={
            "model": value.model.model_id,
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(decision),
            }}],
        })

    def recording_failure(*args):
        raise RuntimeError("controlled local recording failure")

    monkeypatch.setattr(service.store, "record_model_response", recording_failure)
    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        model = DeepSeekAnalysisModel(service.settings, value.model, client=remote,
                                      rate_limit=lambda *args: None)
        with pytest.raises(DomainError) as error:
            model.complete(service.store, value.run_id, value.execution_epoch, "controlled",
                           value.profile_hash, "system", {"task": value.task}, AgentDecision,
                           call_limit=None, deadline_check=lambda: None)
    assert error.value.code == (
        "USAGE_UNKNOWN" if expected is None else "MODEL_RESPONSE_RECORDING_FAILED"
    )
    with engine.transaction() as conn:
        call = conn.execute("SELECT * FROM app.usage_calls WHERE run_id=%s", (value.run_id,))
        row = call.fetchone()
    assert len(sent) == 1
    assert row["status"] == ("unknown" if expected is None else "known")
    assert row["settled_micros"] == expected
    if expected is not None:
        assert service.detail(value.run_id)["budget"]["reserved_micros"] == 0


def test_complete_usage_survives_final_preview_failure(settings, engine, resolved, monkeypatch):
    service, value = environment(settings, engine, monkeypatch)
    service.store.begin_run(value.run_id)
    value = service.store.spec(value.run_id)
    sent = []

    def provider(req):
        sent.append(req)
        return httpx.Response(200, json={
            "model": value.model.model_id,
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "action": "insufficient", "thought": "brief", "insufficient_reason": "missing",
            })}}],
        })

    def preview_failure(self):
        raise RuntimeError("controlled preview failure after complete usage")

    monkeypatch.setattr("mgar_v3.repository_analysis.model.PreviewRelay.flush", preview_failure)
    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        model = DeepSeekAnalysisModel(service.settings, value.model, client=remote,
                                      rate_limit=lambda *args: None)
        with pytest.raises(DomainError) as error:
            model.complete(service.store, value.run_id, value.execution_epoch, "preview",
                           value.profile_hash, "system", {"task": value.task}, AgentDecision,
                           call_limit=None, deadline_check=lambda: None)
    assert error.value.code == "MODEL_RESPONSE_RECORDING_FAILED"
    assert len(sent) == 1
    assert service.detail(value.run_id)["budget"]["settled_micros"] == 150
    assert service.detail(value.run_id)["budget"]["reserved_micros"] == 0


@pytest.mark.parametrize("close_error", [httpx.ReadError, httpx.ConnectError])
def test_complete_usage_survives_response_context_close_failure(
    settings, engine, resolved, monkeypatch, close_error,
):
    service, value = environment(settings, engine, monkeypatch)
    service.store.begin_run(value.run_id)
    value = service.store.spec(value.run_id)
    sent = []

    def provider(req):
        sent.append(req)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream(
            value.model.model_id, json.dumps({
                "action": "insufficient", "thought": "brief", "insufficient_reason": "missing",
            }), private=False,
        ))

    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        original_stream = remote.stream

        @contextmanager
        def close_failure(*args, **kwargs):
            with original_stream(*args, **kwargs) as response:
                yield response
            raise close_error("controlled close failure after complete response")

        monkeypatch.setattr(remote, "stream", close_failure)
        model = DeepSeekAnalysisModel(service.settings, value.model, client=remote,
                                      rate_limit=lambda *args: None)
        with pytest.raises(DomainError) as error:
            model.complete(service.store, value.run_id, value.execution_epoch, "close",
                           value.profile_hash, "system", {"task": value.task}, AgentDecision,
                           call_limit=None, deadline_check=lambda: None)
    assert error.value.code == "MODEL_RESPONSE_RECORDING_FAILED"
    assert len(sent) == 1
    detail = service.detail(value.run_id)
    assert detail["budget"]["settled_micros"] == 150
    assert detail["budget"]["reserved_micros"] == detail["budget"]["unknown_calls"] == 0
    with engine.transaction() as conn:
        call = conn.execute("SELECT status,usage FROM app.usage_calls WHERE run_id=%s",
                            (value.run_id,)).fetchone()
    assert call["status"] == "known"
    assert call["usage"] == {"prompt_tokens": 100, "completion_tokens": 100}


def test_failed_legacy_reservation_is_unresolved_and_not_an_infinite_wait(
    settings, engine, resolved, monkeypatch,
):
    service, value = environment(settings, engine, monkeypatch)
    service.store.begin_run(value.run_id)
    value = service.store.spec(value.run_id)
    service.store.reserve_repository_call(
        value.run_id, value.execution_epoch, "legacy", value.profile_hash, 16137, None, 29213,
    )
    service.store.fail(value.run_id, "MODEL_RESPONSE_LIMIT")
    detail = service.detail(value.run_id)
    assert detail["continuation"]["state"] == "unresolved"
    assert detail["budget"]["legacy_reserved_calls"] == 1
    assert detail["budget"]["unresolved_calls"] == 1
    assert detail["budget"]["inflight_calls"] == 0
    follow = service.follow_up(value.run_id, FollowUpInput(
        client_request_id=str(uuid4()), body="继续。",
    ))
    assert follow.thread_id == value.thread_id and follow.ticket_id != value.ticket_id
    assert follow.parent_run_id == value.run_id
    assert follow.limits.max_cost_micros == value.limits.max_cost_micros - 16137
    assert service.detail(value.run_id)["budget"]["reserved_micros"] == 16137
    assert service.store.spec(value.run_id).reserved_tokens == 29213
    assert len(service.list()) == 2


def test_missing_usage_keeps_unknown_without_retry_but_allows_explicit_followup(
    settings, engine, resolved, monkeypatch,
):
    service, value = environment(settings, engine, monkeypatch)
    sent = []

    def provider(req):
        sent.append(req)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream(
            value.model.model_id, '{}', usage=False, private=False,
        ))

    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        monkeypatch.setattr(
            "mgar_v3.repository_analysis.service.DeepSeekAnalysisModel",
            lambda settings, model: DeepSeekAnalysisModel(
                settings, model, client=remote, rate_limit=lambda *args: None,
            ),
        )
        service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert len(sent) == 1 and detail["run"]["stop_reason"] == "USAGE_UNKNOWN"
    assert detail["continuation"]["state"] == "unresolved"
    assert detail["budget"]["unknown_calls"] == 1
    assert detail["budget"]["reserved_micros"] > 0
    follow = service.follow_up(value.run_id, FollowUpInput(
        client_request_id=str(uuid4()), body="继续。",
    ))
    assert follow.thread_id == value.thread_id and follow.ticket_id != value.ticket_id
    assert follow.limits.max_cost_micros == (
        value.limits.max_cost_micros - detail["budget"]["reserved_micros"]
    )
    assert service.detail(value.run_id)["budget"]["unknown_calls"] == 1
    assert service.detail(value.run_id)["budget"]["reserved_micros"] == (
        detail["budget"]["reserved_micros"]
    )
    assert len(sent) == 1  # A new user message queues work; it never replays the old request.
    assert len(service.list()) == 2
