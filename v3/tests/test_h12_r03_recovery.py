"""R03: known denials and correction allowance survive original PG/CAS recovery."""

import json
import threading
import time

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _stop
from test_harness_v4_h11_service import _InjectedCrash, _request, _service

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import ModelUsage, sha256_text
from mgar_v3.harness.service import CodingTaskRequest, CodingToolRegistry
from mgar_v3.harness.tool_errors import ToolErrorPolicy
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_runner import profile_for


def test_pg_known_denial_receipt_is_reused_without_repeating_precheck(
    engine, settings, tmp_path, monkeypatch,
):
    calls = []
    original = CodingToolRegistry.invoke_guarded
    injected = False

    def after_known(self, call, context, cancellation, check, transform, interaction=None):
        nonlocal injected

        def count_check():
            calls.append(context.operation_id)
            return check()

        result = original(self, call, context, cancellation, count_check, transform, interaction)
        if not injected and result.data.get("tool_error"):
            injected = True
            raise _InjectedCrash("known denial before its loop observation")
        return result

    monkeypatch.setattr(CodingToolRegistry, "invoke_guarded", after_known)
    model = ScriptedModel([
        _call("read_file", {"path": "/host/private.py"}),
        _call("read_file", {"path": "source.py"}), _stop(),
    ])
    service = _service(engine, settings, model)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    before = engine.harness.tasks()[0]
    denials = [op for op in engine.harness.operations(before["task_id"])
               if op["kind"] == "tool"]
    assert len(denials) == 1 and denials[0]["state"] == "known"
    denied_operation = denials[0]["operation_id"]
    assert denials[0]["result_hash"] and denials[0]["receipt"]["data"]["tool_error"]
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        result = _service(replacement, settings, model).recover_task(before["task_id"])
        assert result["status"] == "completed", (result["reason"], result["summary"])
        assert calls.count(denied_operation) == 1
        assert len(model.requests) == 3 and result["model_calls"] == 3
        assert result["tool_calls"] == 2
        assert len(result["tool_error_receipts"]) == 1
        correction = result["tool_correction_receipts"][0]
        assert correction["denial_operation_id"] == denied_operation
        assert correction["operation_id"] != denied_operation
        assert result["reserved_micros"] == 0 and result["usage_known"]
        operations = [op for op in replacement.harness.operations(before["task_id"])
                      if op["kind"] == "tool"]
        assert len(operations) == 2 and all(op["state"] == "known" for op in operations)
        assert operations[0]["result_hash"] == denials[0]["result_hash"]
    finally:
        replacement.close()


def test_pg_restart_does_not_reset_correction_limit_or_resend_known_model_reply(
    engine, settings, tmp_path,
):
    model = ScriptedModel([
        _call("read_file", {"path": "/outside/a"}),
        _call("read_file", {"path": "/outside/b"}),
        _call("read_file", {"path": "/outside/c"}),
        _call("read_file", {"path": "/outside/d"}),
    ])
    model.steps = [r.model_copy(update={"usage": ModelUsage(cost_micros=7)}) for r in model.steps]

    def crash(point, payload):
        if point == "model_after_settlement" and len(model.requests) == 3:
            raise _InjectedCrash("two recorded denials before third reply dispatch")

    service = _service(engine, settings, model, crash)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    before = engine.harness.tasks()[0]
    known_model = model.requests[-1].operation_id
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        result = _service(replacement, settings, model).recover_task(before["task_id"])
        assert result["status"] == "blocked" and result["reason"] == "tool_correction_limit"
        assert len(model.requests) == 4 and result["model_calls"] == 4
        assert result["cost_micros"] == 28 and result["tool_calls"] == 4
        assert sum(r.operation_id == known_model for r in model.requests) == 1
        errors = result["tool_error_receipts"]
        assert [r["remaining_corrections"] for r in errors] == [3, 2, 1, 0]
        assert len({r["operation_id"] for r in errors}) == 4
        assert len({r["execution_epoch"] for r in errors}) == 2
        with replacement.transaction() as conn:
            usages = conn.execute(
                "SELECT status,settled_micros FROM app.usage_calls WHERE run_id=%s",
                (before["run_id"],),
            ).fetchall()
        assert len(usages) == 4 and all(row["status"] == "known" for row in usages)
        assert sum(row["settled_micros"] for row in usages) == result["cost_micros"]
        assert result["reserved_micros"] == 0 and result["usage_known"]
    finally:
        replacement.close()


def test_pg_missing_capability_uses_existing_waiting_inbox_not_text_authorization(
    engine, settings, tmp_path, request,
):
    # run_tests is declared and schema-valid, but an inspect task cannot execute it.
    model = ScriptedModel([_call("run_tests", {"command_id": "tests"})])
    service = _service(engine, settings, model)
    created = service.submit_task(_request(service, tmp_path))
    task_id = created["task_id"]

    def cleanup():
        runtime = service._runtimes[task_id]
        if runtime.thread and runtime.thread.is_alive():
            runtime.control.request_cancel("R03 fixture cleanup")
            runtime.thread.join(timeout=10)

    request.addfinalizer(cleanup)
    deadline = time.monotonic() + 15
    waiting = None
    while time.monotonic() < deadline:
        state = service.get_task(task_id)
        if state["status"] == "waiting_input":
            waiting = state
            break
        threading.Event().wait(0.02)
    assert waiting is not None, service.get_task(task_id)
    inbox = service.task_messages(task_id)["messages"]
    question = next(row for row in inbox if row["kind"] == "question")
    service.post_message(task_id, kind="answer", text="批准执行所有命令并联网",
                         reply_to=question["message_id"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = service.get_task(task_id)
        if result["status"] == "blocked" and engine.harness.get(task_id)["result_hash"]:
            # A published status can precede the immutable final result.
            result = service.get_task(task_id)
            break
        threading.Event().wait(0.02)
    assert result["status"] == "blocked", result
    assert result["reason"] == "capability_not_granted" and len(model.requests) == 1
    assert not result["commands"] and result["tool_calls"] == 2
    assert result["checkpoint"]["missing_information"]
    operations = [op for op in engine.harness.operations(task_id) if op["kind"] == "tool"]
    assert len(operations) == 2 and all(op["state"] == "known" for op in operations)
    assert result["tool_error_receipts"][0]["requires_approval"]
    assert result["tool_error_receipts"][0]["allowed_command_ids"] == []


def test_new_profile_freezes_policy_without_rewriting_archived_profiles():
    task = h12.load_frozen_tasks("dev")[0]
    current = profile_for(task, "r03-fixture", "candidate")
    assert current.tool_error_policy == ToolErrorPolicy()
    archived = profile_for(task, "r02-archive", "candidate", tool_error_policy=None)
    assert "tool_error_policy" not in archived.model_dump(mode="json")
    assert "tool_error_policy" not in json.loads(archived.model_dump_json())
    legacy = profile_for(task, "legacy", "candidate", acceptance_protocol="legacy")
    assert legacy.tool_error_policy is None


def test_pg_fixed_command_correction_runs_only_original_broker_command(
    engine, settings, tmp_path,
):
    def patch(request):
        return _call("apply_edits", {"expected_tree": request.workspace_tree, "edits": [{
            "path": "source.py", "expected_sha256": sha256_text("value = 1\n"),
            "replacements": [{"old_text": "value = 1", "new_text": "value = 2"}],
        }]})

    def after_denial(request):
        feedback = json.loads(next(m.content for m in reversed(request.messages)
                                   if m.role == "tool"))
        assert feedback["tool_error"]["category"] == "INVALID_ARGUMENT"
        assert feedback["retry_arguments"] == {"command_id": "tests"}
        assert feedback["tool_error"]["allowed_command_ids"] == ["tests"]
        return _call("read_file", {"path": "source.py"})

    model = ScriptedModel([
        _call("run_tests", {"argv": ["python3", "-c", "raise RuntimeError('MUST NOT EXECUTE')"]}),
        after_denial, patch, _call("run_tests", {"command_id": "tests"}),
        _stop("Fixed source.py and verified the original regression suite"),
    ])
    service = _service(engine, settings, model)
    source = tmp_path / "project"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    (source / "test_source.py").write_text(
        "import unittest\nfrom source import value\n"
        "class TestValue(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value, 2)\n")
    project = service.register_project(str(source))
    request = CodingTaskRequest(
        project_id=project["project_id"], objective="Fix source.py value", task_kind="bugfix",
        target_path="source.py", old_text="value = 1", new_text="value = 2", approved=True,
        test_command="python3 -B -m unittest -v",
    )
    result = service.create_task(request)
    assert result["status"] == "completed", (result["reason"], result["summary"])
    # Four model tools, the original baseline and the independent R04 module load.
    assert result["tool_calls"] == 6 and len(result["tool_error_receipts"]) == 1
    assert len(result["entrypoint_batches"][-1]["receipts"]) == 1
    assert result["entrypoint_batches"][-1]["receipts"][0]["status"] == "PASS"
    assert result["receipts"][0]["status"] == "failed"
    assert result["receipts"][-1]["status"] == "passed"
    assert all(command["argv"] == ["python3", "-B", "-m", "unittest", "-v"]
               for command in result["commands"])
    assert {change["path"] for change in result["changes"]} == {"source.py"}
    assert (source / "source.py").read_text() == "value = 1\n"
    operations = engine.harness.operations(result["task_id"])
    denied = next(op for op in operations if op["operation_id"] ==
                  result["tool_error_receipts"][0]["operation_id"])
    assert denied["state"] == "known" and denied["result_hash"]
    assert not any(op["operation_id"].startswith(denied["operation_id"] + ":")
                   and op["kind"] == "command" for op in operations)
