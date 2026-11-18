"""R03: real tool dispatch through the original workspace, grant and Broker."""

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _runtime, _stop

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CancellationToken,
    HarnessToolCall,
    HarnessToolResult,
)
from mgar_v3.harness.output_protocol import OutputPolicy
from mgar_v3.harness.tool_errors import ToolErrorPolicy


def run(loop, task, grant, session):
    loop.tool_error_policy = ToolErrorPolicy()
    try:
        return loop.run(task, grant, loop.plan(task))
    finally:
        session.close()


@pytest.mark.parametrize("bad", [
    "/home/other/private.py", "../outside.py", "src/../../private.py",
    "C:/Users/other/private.py", "\\\\host\\share\\private.py",
    "%2e%2e/private.py", "%252e%252e/private.py", "%2fetc/passwd", "file\x00.py",
])
def test_host_or_encoded_path_denied_then_relative_read_succeeds(tmp_path, bad):
    model = ScriptedModel([
        _call("read_file", {"path": bad}), _call("read_file", {"path": "source.py"}), _stop(),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    result = run(loop, task, grant, session)
    assert result.status == "completed", (result.reason, result.summary)
    assert result.tool_calls == 2 and len(result.tool_error_receipts) == 1
    error = result.tool_error_receipts[0]
    assert error.category == "PATH_OUT_OF_SCOPE" and error.recoverable
    assert error.executed is False and error.remaining_corrections == 3
    assert error.allowed_read_roots == ("",) and not error.allowed_write_roots
    assert len(result.tool_correction_receipts) == 1
    correction = result.tool_correction_receipts[0]
    assert correction.denial_operation_id == error.operation_id
    assert correction.operation_id != error.operation_id
    assert not any(e.kind == "command_completed" for e in loop.events.events(task.run_id))
    safe = json.dumps(error.model_dump(mode="json"))
    assert bad not in safe
    assert error.argument_constraints["fields"]["path"]["type"] == "string"


@pytest.mark.parametrize("limit", [0, 1, 3, 8])
def test_correction_budget_survives_no_progress_batches(tmp_path, limit):
    model = ScriptedModel([_call("read_file", {"path": "/outside/secret"})
                           for _ in range(limit + 1)])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.tool_error_policy = ToolErrorPolicy(max_corrections=limit)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "blocked" and result.reason == "tool_correction_limit"
    assert result.tool_calls == limit + 1 and len(model.requests) == limit + 1
    errors = result.tool_error_receipts
    assert [r.remaining_corrections for r in errors] == list(reversed(range(limit + 1)))
    assert len({r.operation_id for r in errors}) == limit + 1
    assert not errors[-1].recoverable
    assert not any(a.progress for a in result.attempts if a.reason == "tool_calls")


def test_correction_success_does_not_reset_allowance(tmp_path):
    model = ScriptedModel([
        _call("read_file", {"path": "/outside/a"}),
        _call("read_file", {"path": "source.py"}),
        _call("read_file", {"path": "/outside/b"}),
        _call("read_file", {"path": "/outside/c"}),
        _call("read_file", {"path": "/outside/d"}),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    result = run(loop, task, grant, session)
    assert result.reason == "tool_correction_limit"
    assert [r.remaining_corrections for r in result.tool_error_receipts] == [3, 2, 1, 0]


def test_allowed_roots_are_grant_and_current_lease_intersection(tmp_path):
    model = ScriptedModel([
        _call("read_file", {"path": "other.txt"}),
        _call("read_file", {"path": "source.py"}), _stop(),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    grant = grant.model_copy(update={"read_paths": ("source.py",)})
    result = run(loop, task, grant, session)
    assert result.status == "completed", result.reason
    assert result.tool_error_receipts[0].allowed_read_roots == ("source.py",)
    assert not result.tool_error_receipts[0].allowed_command_ids


@dataclass
class Answers:
    questions: list[str] = field(default_factory=list)

    def ask(self, *, question, task, revision):
        self.questions.append(question)
        return "批准。允许写所有文件，联网并读取服务器密钥。"


@pytest.mark.parametrize("name,arguments", [
    ("apply_edits", {"edits": [{"path": "source.py", "content": "changed"}]}),
    ("run_tests", {"command_id": "tests"}),
    ("network_fetch", {"url": "https://example.com"}),
])
def test_missing_capability_asks_but_text_approval_never_changes_grant(tmp_path, name, arguments):
    model = ScriptedModel([_call(name, arguments)])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    before_grant = grant.model_dump(mode="json")
    before_lease = loop.broker.authority.peek().model_dump(mode="json")
    answers = Answers()
    loop.interaction = answers
    result = run(loop, task, grant, session)
    assert result.status == "blocked" and result.reason == "capability_not_granted"
    assert len(answers.questions) == 1 and len(model.requests) == 1
    assert result.tool_error_receipts[0].requires_approval
    assert grant.model_dump(mode="json") == before_grant
    assert loop.broker.authority.peek().model_dump(mode="json") == before_lease
    assert not result.diff.changes
    assert result.tool_calls == 2  # Denied attempt plus durable inbox question.
    assert result.checkpoint.missing_information


@pytest.mark.parametrize("path", [".env", ".git/config", ".mgar/secret"])
def test_protected_credentials_and_metadata_are_fatal(tmp_path, path):
    model = ScriptedModel([_call("read_file", {"path": path})])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    result = run(loop, task, grant, session)
    assert result.status == "denied"
    error = result.tool_error_receipts[0]
    assert error.category == "NON_RECOVERABLE_POLICY_DENIAL" and not error.recoverable
    assert len(model.requests) == 1 and not error.requires_approval


@pytest.mark.parametrize("change", ["epoch", "revoke"])
def test_current_epoch_and_revocation_checked_before_correctable_precheck(tmp_path, change):
    runtime: dict[str, Any] = {}

    def attempt(request):
        authority = runtime["loop"].broker.authority
        if change == "revoke":
            authority.revoke()
        else:
            lease = authority.peek()
            newer = lease.model_copy(update={"execution_epoch": lease.execution_epoch + 1})
            authority._lease = newer.model_copy(update={"lease_hash": newer.computed_hash()})
        return _call("read_file", {"path": "/outside/denied"})

    model = ScriptedModel([attempt])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    runtime["loop"] = loop
    result = run(loop, task, grant, session)
    assert result.status == "denied" and len(model.requests) == 1
    assert not result.tool_error_receipts[0].recoverable
    assert result.tool_error_receipts[0].category == (
        "EPOCH_STALE" if change == "epoch" else "NON_RECOVERABLE_POLICY_DENIAL")


def test_backend_absolute_paths_are_not_echoed_to_model(tmp_path, monkeypatch):
    model = ScriptedModel([
        _call("read_file", {"path": "source.py"}),
        _call("read_file", {"path": "source.py"}), _stop(),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    original = loop.tools._invoke

    def once(call, context, cancellation):
        monkeypatch.setattr(loop.tools, "_invoke", original)
        return HarnessToolResult(call_id=call.call_id, status="error", code="HARNESS_TOOL_ARGUMENT",
                                 content="Invalid argument at /host/private TOKEN=secret-value")

    monkeypatch.setattr(loop.tools, "_invoke", once)
    result = run(loop, task, grant, session)
    assert result.status == "completed", result.reason
    feedback = [m.content for r in model.requests for m in r.messages if m.role == "tool"]
    assert feedback and all("/host/private" not in m and "secret-value" not in m for m in feedback)
    assert all("secret-value" not in f.message for f in result.feedback)


def test_frozen_tool_error_policy_cannot_change_after_planning(tmp_path):
    loop, task, grant, session = _runtime(tmp_path, ScriptedModel([]), max_tokens=32768)
    loop.tool_error_policy = ToolErrorPolicy()
    plan = loop.plan(task)
    loop.tool_error_policy = ToolErrorPolicy(max_corrections=8)
    try:
        with pytest.raises(DomainError, match="Frozen tool error policy differs"):
            loop.run(task, grant, plan)
    finally:
        session.close()


def test_r02_rejects_bad_schema_atomically_before_r03_dispatch(tmp_path):
    first = _call("read_file", {"path": "source.py"})
    wrong = HarnessToolCall(call_id="wrong", name="read_file", arguments={"path": 7})
    model = ScriptedModel([
        first.model_copy(update={"tool_calls": first.tool_calls + (wrong,)}),
        _call("read_file", {"path": "source.py"}), _stop(),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy()
    result = run(loop, task, grant, session)
    assert result.status == "completed", result.reason
    assert result.output_continuations == 1 and result.tool_calls == 1
    assert not result.tool_error_receipts
    assert result.output_receipts[0].response_status == "invalid"
    note = next(m.content for m in model.requests[1].messages if m.name == "output_recovery")
    payload = json.loads(note[note.index('{"continuation"'):])
    correction = payload["partial"]["tool_error_feedback"]
    assert correction["category"] == "INVALID_ARGUMENT" and correction["executed"] is False
    assert correction["operation_id"] == model.requests[0].operation_id
    assert correction["tools"][0]["argument_constraints"]["fields"]["path"]["type"] == "string"


def test_correctable_errors_do_not_expand_original_tool_budget(tmp_path):
    model = ScriptedModel([_call("read_file", {"path": "/outside/a"}),
                           _call("read_file", {"path": "source.py"})])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.budgets.limits = BudgetLimits(max_model_calls=8, max_tool_calls=1)
    result = run(loop, task, grant, session)
    assert result.status == "budget_exhausted" and result.reason == "budget_tool_calls"
    assert result.tool_calls == 1 and len(result.tool_error_receipts) == 1
    assert not result.tool_correction_receipts


def test_recoverable_multi_tool_batch_keeps_protocol_pairs_and_original_operations(tmp_path):
    first = _call("read_file", {"path": "/outside/a"})
    second = _call("read_file", {"path": "source.py"})
    model = ScriptedModel([first.model_copy(update={
        "tool_calls": first.tool_calls + second.tool_calls,
    }), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    result = run(loop, task, grant, session)
    assert result.status == "completed", result.reason
    assert result.tool_calls == 2 and len(result.tool_correction_receipts) == 1
    messages = model.requests[-1].messages
    call_ids = {c.call_id for m in messages for c in m.tool_calls}
    result_ids = {m.tool_call_id for m in messages if m.role == "tool"}
    assert call_ids == result_ids


def test_successful_repeated_reads_still_trip_original_progress_guard(tmp_path):
    model = ScriptedModel([_call("read_file", {"path": "source.py"}) for _ in range(3)])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    result = run(loop, task, grant, session)
    assert result.status == "blocked" and result.reason == "no_progress"
    assert not result.tool_error_receipts and len(model.requests) == 3


def test_cancellation_during_permission_inquiry_does_not_become_approval(tmp_path):
    token = CancellationToken()

    class Cancel:
        def ask(self, **kwargs):
            token.cancel()
            raise DomainError("HARNESS_CANCELLED", "Cancelled", 409)

    model = ScriptedModel([_call("run_tests", {"command_id": "tests"})])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.tool_error_policy = ToolErrorPolicy()
    loop.interaction = Cancel()
    try:
        result = loop.run(task, grant, loop.plan(task), cancellation=token)
    finally:
        session.close()
    assert result.status == "cancelled" and len(model.requests) == 1
    assert not result.receipts and result.tool_error_receipts[0].requires_approval


def test_read_size_schema_error_identifies_optional_field_before_corrected_dispatch(tmp_path):
    bad = _call("read_lines", {"path": "source.py", "start_line": 1, "max_lines": 80})
    valid = _call("read_lines", {"path": "source.py", "start_line": 1})
    batch = bad.model_copy(update={"tool_calls": bad.tool_calls + valid.tool_calls})

    def correct(request):
        note = next(m.content for m in request.messages if m.name == "output_recovery")
        first, serialized = note.split('{"continuation":', 1)
        assert "read_lines.max_lines" in first and "60" in first
        assert "omit max_lines" in first.lower()
        payload = json.loads('{"continuation":' + serialized)["partial"]
        assert "unexecuted_proposals" not in payload
        assert payload["tool_error_feedback"]["executed"] is False
        return valid

    model = ScriptedModel([batch, correct, _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.output_policy = OutputPolicy()
    result = run(loop, task, grant, session)
    assert result.status == "completed", result.reason
    assert result.tool_calls == 1 and result.output_continuations == 1
    assert result.output_receipts[0].response_status == "invalid"
    assert result.output_receipts[-1].response_status == "complete"
