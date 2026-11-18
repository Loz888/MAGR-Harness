"""R03 feedback must preserve the original canonical path and lease checks."""

import json

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _runtime, _stop

from mgar_v3.harness.contracts import (
    CancellationToken,
    HarnessToolCall,
    HarnessToolResult,
    ToolContext,
)
from mgar_v3.harness.tool_errors import ToolErrorPolicy, error_receipt, safe_error_result


def invoke_error(loop, task, grant, call):
    context = ToolContext(task=task, grant=grant, operation_id="r03-security-operation",
                          workspace_tree=loop.tools.workspace.manifest.tree_hash)
    spec = next(item for item in loop.tools.specs() if item.name == call.name)

    def transform(result):
        assert result.status in {"error", "denied"}
        receipt = error_receipt(
            call, result, context, policy=ToolErrorPolicy(), prior_errors=0,
            acceptance_hash="a" * 64, spec=spec, scope=loop.tools.error_scope(context),
            allow_write=False, allow_execute=False,
        )
        return safe_error_result(result, receipt)

    return loop.tools.invoke_guarded(call, context, CancellationToken(), lambda: None, transform)


@pytest.mark.parametrize("race", [False, True])
def test_external_symlink_and_check_open_replacement_cannot_escape(tmp_path, monkeypatch, race):
    loop, task, grant, session = _runtime(tmp_path, ScriptedModel([]), max_tokens=32768)
    outside = tmp_path / "private.txt"
    outside.write_text("HOST-SECRET-MUST-NOT-BE-READ")
    target = loop.tools.workspace.root / "source.py"

    def replace():
        target.unlink()
        target.symlink_to(outside)

    if race:
        original = loop.tools.workspace.resolve_read_path
        changed = False

        def swap(path):
            nonlocal changed
            relative = original(path)
            if not changed:
                changed = True
                replace()
            return relative

        monkeypatch.setattr(loop.tools.workspace, "resolve_read_path", swap)
    else:
        replace()
    try:
        call = HarnessToolCall(call_id="escape", name="read_file", arguments={"path": "source.py"})
        result = invoke_error(loop, task, grant, call)
    finally:
        session.close()
    receipt = result.data["tool_error"]
    assert receipt["category"] == "SYMLINK_ESCAPE" and not receipt["recoverable"]
    assert "HOST-SECRET" not in result.content and str(outside) not in result.content
    assert outside.read_text() == "HOST-SECRET-MUST-NOT-BE-READ"


@pytest.mark.parametrize("failure", ["dependency", "resource", "unknown"])
def test_environment_or_unknown_effects_do_not_become_automatic_retries(
    tmp_path, monkeypatch, failure,
):
    code, category = {
        "dependency": ("MISSING_DEPENDENCY", "MISSING_DEPENDENCY"),
        "resource": ("HARNESS_BUDGET_EXHAUSTED", "RESOURCE_LIMIT"),
        "unknown": ("RECOVERY_BLOCKED", "NON_RECOVERABLE_POLICY_DENIAL"),
    }[failure]
    model = ScriptedModel([_call("read_file", {"path": "source.py"})])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    monkeypatch.setattr(
        loop.tools, "_invoke", lambda call, context, cancellation: HarnessToolResult(
            call_id=call.call_id, status="error", code=code,
            content="private environment /host/secret KEY=must-not-leak",
        ),
    )
    loop.tool_error_policy = ToolErrorPolicy()
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == ("unknown" if failure == "unknown" else "blocked")
    receipt = result.tool_error_receipts[0]
    assert receipt.category == category and not receipt.recoverable
    assert receipt.requires_reconciliation == (failure == "unknown")
    assert len(model.requests) == 1 and "must-not-leak" not in result.summary


@pytest.mark.parametrize("cursor", ["0", "offset:20000"])
def test_bad_cursor_feedback_includes_legal_bound_continuation(tmp_path, cursor):
    runtime = {}
    reference = {}

    def invalid(request):
        # The initial read issued this exact path/hash under current grant and lease.
        reference.update(runtime["loop"].tools.context_tools.references()[0])
        return _call("artifact_page", {"path": "source.py",
                     "artifact_hash": reference["artifact_hash"], "cursor": cursor})

    def correct(request):
        message = next(m for m in reversed(request.messages) if m.role == "tool")
        feedback = json.loads(message.content)
        assert feedback["tool_error"]["category"] == "INVALID_ARGUMENT"
        assert feedback["tool_error"]["executed"] is False
        assert feedback["retry_arguments"]["artifact_hash"] == reference["artifact_hash"]
        args = feedback["next_arguments"]
        assert args["path"] == "source.py" and args["cursor"] not in {"0", "offset:20000"}
        return _call("artifact_page", args)

    model = ScriptedModel([_call("read_file", {"path": "source.py"}), invalid, correct, _stop()])
    loop, task, grant, session = _runtime(
        tmp_path, model, files={"source.py": "value = 1\n" * 200}, max_tokens=32768,
        page_bytes=128,
    )
    runtime["loop"] = loop
    loop.tool_error_policy = ToolErrorPolicy()
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "completed", result.reason
    assert result.tool_calls == 3 and len(result.tool_error_receipts) == 1
    assert len(result.tool_correction_receipts) == 1


def test_foreign_hash_with_bad_cursor_is_fatal_not_a_correction(tmp_path):
    model = ScriptedModel([_call("artifact_page", {
        "path": "source.py", "artifact_hash": "f" * 64, "cursor": "0",
    })])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    loop.tool_error_policy = ToolErrorPolicy()
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "denied"
    assert result.tool_error_receipts[0].code == "CONTEXT_ARTIFACT_SCOPE_DENIED"
    assert not result.tool_error_receipts[0].recoverable and len(model.requests) == 1


def test_private_diagnostic_is_auditable_but_never_an_issued_model_artifact(tmp_path, monkeypatch):
    hashes = []

    def request_private_page(request):
        message = next(m for m in reversed(request.messages) if m.role == "tool")
        assert "HOST-SECRET" not in message.content
        error = json.loads(message.content)["tool_error"]
        hashes.append(error["diagnostic_hash"])
        return _call("artifact_page", {
            "path": "source.py", "artifact_hash": hashes[0], "cursor": "0",
        })

    model = ScriptedModel([_call("read_file", {"path": "source.py"}), request_private_page])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    original = loop.tools._invoke

    def once(call, context, cancellation):
        monkeypatch.setattr(loop.tools, "_invoke", original)
        return HarnessToolResult(call_id=call.call_id, status="error", code="HARNESS_TOOL_ARGUMENT",
                                 content="HOST-SECRET at /private/server.env")

    monkeypatch.setattr(loop.tools, "_invoke", once)
    loop.tool_error_policy = ToolErrorPolicy()
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "denied" and len(result.tool_error_receipts) == 2
    assert result.tool_error_receipts[-1].code == "CONTEXT_ARTIFACT_SCOPE_DENIED"
    raw = json.loads(loop.output_capture.artifacts.read(hashes[0]))
    assert raw["content"] == "HOST-SECRET at /private/server.env"
    assert raw["task_id"] == task.task_id and raw["run_id"] == str(task.run_id)
    assert raw["operation_id"] == result.tool_error_receipts[0].operation_id
