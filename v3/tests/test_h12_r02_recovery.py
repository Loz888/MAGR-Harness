"""R02 bounded continuation and original PG/CAS recovery with free adapters."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from test_harness_v4_h02 import _deepseek_sse
from test_harness_v4_h10 import ScriptedModel, _call, _runtime, _stop
from test_harness_v4_h11_service import _InjectedCrash, _request, _service

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import BudgetLimits, ModelResponse, ModelUsage, sha256_text
from mgar_v3.harness.model import DEEPSEEK_CHAT, DeepSeekChatAdapter
from mgar_v3.harness.output_protocol import OutputPolicy, validate_output_result
from mgar_v3.harness.service import (
    DETERMINISTIC_MODEL_ID,
    DETERMINISTIC_PRICE_VERSION,
    CodingTaskRequest,
)
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_runner import profile_for


def truncated(known=True):
    return ModelResponse(text="A partial patch before its closing JSON", finish_reason="length",
                         usage=ModelUsage(known=known, input_tokens=10, output_tokens=8))


def test_recovery_references_original_output_and_existing_tool_receipt(tmp_path):
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), truncated(), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy(max_continuations=2)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "completed", (result.reason, result.summary)
    assert result.output_continuations == 1 and result.tool_calls == 1
    continuation = model.requests[-1].output_continuation
    assert continuation.parent_operation_id == model.requests[1].operation_id
    assert continuation.count == 1 and continuation.receipt_ids
    assert continuation.source_artifact == result.output_receipts[1].artifact_hash
    assert continuation.remaining_budget["remaining"]["model_calls"] > 0
    assert result.output_receipts[1].response_status == "incomplete"
    assert len({r.operation_id for r in model.requests}) == 3
    projection = json.loads(next(m.content for m in reversed(model.requests[-1].messages)
                                 if m.name == "h10_context" and m.role == "user"))
    state = json.loads(projection["constraints"][0])["working_state"]
    assert state["resource_budget"]["output"]["stage"] == "continuation"
    assert state["resource_budget"]["tokens"]["output_reservation"] == (
        model.requests[-1].max_output_tokens)


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_continuation_limit_retains_partial_artifacts_and_never_passes(tmp_path, limit):
    model = ScriptedModel([truncated() for _ in range(limit + 1)])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy(max_continuations=limit)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "incomplete" and result.reason == "model_output_continuation_limit"
    assert result.output_continuations == limit and len(model.requests) == limit + 1
    assert result.tool_calls == 0 and len(result.output_receipts) == limit + 1
    assert all(loop.output_capture.artifacts.read(r.artifact_hash) for r in result.output_receipts)


def test_unknown_usage_keeps_reservation_and_does_not_continue(tmp_path):
    model = ScriptedModel([truncated(known=False)])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy()
    loop.reservation_micros = 100
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "unknown" and result.output_continuations == 0
    assert len(model.requests) == 1 and result.output_receipts[0].usage_status == "unknown"
    assert loop.budgets.snapshot(task.task_id).cost_micros == 100
    assert any(e.kind == "model_output_recorded" for e in loop.events.events(task.run_id))


def test_exhausted_call_budget_does_not_authorize_a_continuation(tmp_path):
    model = ScriptedModel([truncated()])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy()
    loop.budgets.limits = BudgetLimits(max_model_calls=1)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "incomplete" and result.reason == "model_output_budget_exhausted"
    assert len(model.requests) == 1 and result.output_continuations == 0


def test_custom_adapter_parallel_batch_is_checked_before_first_tool_execution(tmp_path):
    first = _call("read_file", {"path": "source.py"})
    invalid = _call("read_file", {"path": 7})
    model = ScriptedModel([
        first.model_copy(update={"tool_calls": first.tool_calls + invalid.tool_calls}),
        _call("read_file", {"path": "source.py"}), _stop(),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy()
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "completed", result.reason
    assert result.tool_calls == 1 and result.output_continuations == 1
    assert result.output_receipts[0].response_status == "invalid"
    assert len(result.output_receipts[0].tool_calls) == 2


def test_preexisting_contract_profile_can_be_read_without_rewriting_output_policy():
    task = h12.load_frozen_tasks("dev")[0]
    old = profile_for(task, "archived-r01", "candidate", output_policy=None)
    assert old.acceptance_protocol == "contract-v1"
    assert "output_policy" not in old.model_dump(mode="json")
    current = profile_for(task, "r02-frozen", "candidate")
    assert current.output_policy == OutputPolicy()


def test_pg_recovery_reuses_known_continuation_without_duplicate_tool(engine, settings, tmp_path):
    responses = [truncated(), _call("read_file", {"path": "source.py"}), _stop()]
    model = ScriptedModel([r.model_copy(update={"usage": ModelUsage(cost_micros=7)})
                           for r in responses])

    def crash(point, payload):
        if point == "model_after_settlement" and len(model.requests) == 2:
            raise _InjectedCrash("known continuation reply before its tool")

    service = _service(engine, settings, model, crash)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    before = engine.harness.tasks()[0]
    operation = model.requests[-1].operation_id
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        result = _service(replacement, settings, model).recover_task(before["task_id"])
        assert result["status"] == "completed", (result["reason"], result["summary"])
        assert result["output_continuations"] == 1 and result["tool_calls"] == 1
        assert result["model_calls"] == 3 and result["cost_micros"] == 21
        assert result["metered_process"]["model_calls"] == 1
        assert result["usage_known"] and result["reserved_micros"] == 0
        assert sum(r.operation_id == operation for r in model.requests) == 1
        assert len(model.requests) == 3
        with replacement.transaction() as conn:
            calls = conn.execute(
                "SELECT status,result,settled_micros FROM app.usage_calls WHERE run_id=%s",
                                 (before["run_id"],)).fetchall()
        assert len(calls) == 3 and all(c["status"] == "known" for c in calls)
        assert sum(c["settled_micros"] for c in calls) == result["cost_micros"]
        assert json.loads(replacement.artifacts.read(
            replacement.harness.get(before["task_id"])["result_hash"]))["output_continuations"] == 1
    finally:
        replacement.close()


@pytest.mark.parametrize("lost_stream", [False, True])
def test_pg_unknown_stream_or_missing_usage_retains_cas_and_reservation(
    engine, settings, tmp_path, lost_stream,
):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"id":"r02-interrupted","choices":[]}\n\n'
            yield _deepseek_sse(include_finish=False, include_usage=False).replace(
                b'data: [DONE]\n\n', b'')
            raise httpx.ReadTimeout("fixture stream lost after proposed tool")

    posts = []

    def handler(request):
        posts.append(request.method)
        return httpx.Response(200, stream=Broken()) if lost_stream else httpx.Response(
            200, content=_deepseek_sse(include_usage=False))

    capabilities = DEEPSEEK_CHAT.model_copy(update={
        "model_id": DETERMINISTIC_MODEL_ID,
        "price": DEEPSEEK_CHAT.price.model_copy(update={"version": DETERMINISTIC_PRICE_VERSION}),
    })
    adapter = DeepSeekChatAdapter(api_key="fixture-only", capabilities=capabilities,
                                 client=httpx.Client(transport=httpx.MockTransport(handler)),
                                 artifacts=engine.artifacts)
    service = _service(engine, settings, adapter)
    result = service.create_task(_request(service, tmp_path))
    assert result["status"] == "unknown" and result["tool_calls"] == 0
    assert posts == ["POST"] and result["output_continuations"] == 0
    task = engine.harness.get(result["task_id"])
    with engine.transaction() as conn:
        call = conn.execute("SELECT status,reserved_micros FROM app.usage_calls WHERE run_id=%s",
                            (task["run_id"],)).fetchone()
        held = conn.execute("SELECT reserved_micros FROM app.budgets WHERE mandate_id=%s",
                            (task["mandate_id"],)).fetchone()["reserved_micros"]
    assert call["status"] == "unknown" and held == call["reserved_micros"] > 0
    assert result["model_calls"] == 1 and result["reserved_micros"] == held
    assert result["usage_known"] is False
    events = [e for e in engine.harness.events(result["task_id"])
              if e.kind == "model_output_captured"]
    assert events
    assert all(engine.artifacts.read(e.payload["artifact_hash"]) for e in events)
    if lost_stream:
        receipt = result["output_receipts"][0]
        assert receipt["provider_request_id"] == "r02-interrupted"
        assert receipt["tool_calls"][0]["execution_status"] == "not_executed"
    try:
        service.recover_task(result["task_id"])
    except DomainError as exc:
        assert exc.code in {"RECOVERY_BLOCKED", "USAGE_UNKNOWN", "HARNESS_RECOVERY_BLOCKED"}
    assert posts == ["POST"]


def test_pg_partial_patch_then_known_output_recovery_does_not_reapply_write(
    engine, settings, tmp_path,
):
    def edit(request):
        return _call("apply_edits", {"expected_tree": request.workspace_tree, "edits": [{
            "path": "source.py", "expected_sha256": sha256_text("value = 1\n"),
            "replacements": [{"old_text": "value = 1", "new_text": "value = 2"}],
        }]})

    model = ScriptedModel([
        _call("read_file", {"path": "source.py"}), edit, truncated(),
        _call("run_tests", {"command_id": "tests"}),
        _stop("Fixed source.py and verified the current suite"),
    ])

    def crash(point, payload):
        if point == "model_after_settlement" and len(model.requests) == 4:
            raise _InjectedCrash("known post-patch continuation before tests")

    service = _service(engine, settings, model, crash)
    (tmp_path / "project").mkdir()
    (tmp_path / "project/source.py").write_text("value = 1\n")
    (tmp_path / "project/test_source.py").write_text(
        "import unittest\nfrom source import value\n"
        "class TestValue(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value, 2)\n")
    project = service.register_project(str(tmp_path / "project"))
    request = CodingTaskRequest(project_id=project["project_id"], objective="Fix source.py value",
                                task_kind="bugfix", target_path="source.py", old_text="value = 1",
                                new_text="value = 2", approved=True,
                                test_command="python3 -B -m unittest -v")
    with pytest.raises(_InjectedCrash):
        service.create_task(request)
    task = engine.harness.tasks()[0]
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        result = _service(replacement, settings, model).recover_task(task["task_id"])
        assert result["status"] == "completed", (result["reason"], result["summary"])
        assert result["output_continuations"] == 1 and len(model.requests) == 5
        assert result["model_calls"] == 5 and result["reserved_micros"] == 0
        writes = [e for e in replacement.harness.events(task["task_id"])
                  if e.kind == "tool_started" and e.payload.get("name") == "apply_edits"]
        assert len(writes) == 1
        assert model.requests[3].output_continuation.receipt_ids
        assert result["receipts"][0]["status"] == "failed"
        assert result["receipts"][-1]["status"] == "passed"
        assert {c["path"] for c in result["changes"]} == {"source.py"}
    finally:
        replacement.close()


def test_scorer_rejects_truncated_final_or_changed_output_policy(engine, settings, tmp_path):
    model = ScriptedModel([truncated(), _call("read_file", {"path": "source.py"}), _stop()])
    service = _service(engine, settings, model)
    result = service.create_task(_request(service, tmp_path))
    profile = SimpleNamespace(output_policy=OutputPolicy(),
                              resources=SimpleNamespace(max_output_tokens=2048),
                              model={"max_output_tokens": 8192})
    validate_output_result(result, profile)
    for variant in ("length", "policy", "parent", "missing", "orphan"):
        broken = copy.deepcopy(result)
        if variant == "length":
            broken["output_receipts"][-1].update(
                finish_reason="length", response_status="incomplete")
        elif variant == "policy":
            broken["output_policy"]["max_continuations"] = 4
        elif variant == "missing":
            broken["output_receipts"] = []
        elif variant == "orphan":
            broken["output_receipts"][1].pop("continuation")
        else:
            broken["output_receipts"][1]["continuation"]["source_artifact"] = "a" * 64
        with pytest.raises(DomainError, match="bounded output chain"):
            validate_output_result(broken, profile)
