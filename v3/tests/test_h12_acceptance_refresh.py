"""Current tool receipts must reach the next model context before finalization."""

import json

import pytest
from test_h12_budget_context import working_state
from test_h12_build_command import BUILD, assert_candidate, case, receipt
from test_harness_v4_h07 import (
    TEST_TOTAL,
    TOTAL_LOOP,
    FakeApplication,
    ScriptedModel,
    _grant,
    _loop,
    _python_argv,
    _run,
    _seed,
    _task,
)
from test_harness_v4_h10 import _call, _stop

from mgar_v3.domain import DomainError
from mgar_v3.harness.coding_loop import ServicePlan, _RunState
from mgar_v3.harness.contracts import CancellationToken, VerificationRequirement


def test_same_tree_build_after_rejected_answer_updates_next_context(engine, tmp_path):
    current = case(engine, tmp_path)
    edit = current.model.steps[0]

    def build(request):
        items = working_state(request)["checklist"]["items"]
        assert any(item["kind"] == "build" and item["status"] != "passed" for item in items)
        return _call("run_tests", {"command_id": "build"})

    def finish(request):
        state = working_state(request)
        assert not state["unmet_items"], state["checklist"]
        assert all(item["status"] == "passed" for item in state["checklist"]["items"])
        return _stop("Updated source.js; the fixed build and public tests passed on this tree.")

    current.model.steps = [
        edit,
        _call("run_tests", {"command_id": "tests"}),
        _stop("The public tests pass; compile verification is still required."),
        build,
        finish,
    ]
    service = current.service()
    result = service.create_task(current.request(service))
    candidate, _ = assert_candidate(current, result)
    assert candidate["model_calls"] == 5
    assert candidate["tool_calls"] == 4
    assert not current.model.steps


def _build_state(tmp_path):
    loop, workspace, _ = _loop(_seed(tmp_path, {"source.js": "function f() {}\n"}),
                               ScriptedModel([]))
    task = _task("feature").model_copy(update={"requirements": (
        VerificationRequirement(requirement_id="compile", kind="build", command_argv=BUILD),
    )})
    state = _RunState(
        task=task, grant=_grant()[0], plan=loop.plan(task),
        policy=loop._task_policy("feature"), token=CancellationToken(),
        baseline_files=dict(workspace.snapshot().files),
    )
    state.receipts.append(receipt(tree_hash=state.plan.checkpoint_tree))
    return loop, state


def test_passed_tool_refresh_cannot_replace_final_answer_verification(tmp_path):
    loop, state = _build_state(tmp_path)
    preview = loop._refresh_acceptance(state, [])
    assert not preview.failed
    assert preview.verification.status == "incomplete"
    result = loop._finish(state, "completed", "verified", "premature", preview)
    assert result.status == "blocked" and result.reason == "HARNESS_COMPLETION_STALE"
    assert result.checkpoint is not None


@pytest.mark.parametrize("change", ["tree", "command", "purpose", "failed"])
def test_receipt_refresh_does_not_reuse_stale_or_nonmatching_green_state(tmp_path, change):
    loop, state = _build_state(tmp_path)
    history = []
    assert not loop._refresh_acceptance(state, history).failed
    if change == "tree":
        loop.workspace.root.joinpath("source.js").write_text("function f() { return 3; }\n")
    elif change == "command":
        task = state.task
        other_command = ("node", "--check", "other.js")
        required = task.requirements[0].model_copy(update={"command_argv": other_command})
        state.task = task.model_copy(update={"requirements": (required,)})
        with pytest.raises(DomainError) as rejected:
            loop._refresh_acceptance(state, history)
        assert rejected.value.code == "ACCEPTANCE_CONTRACT_MISMATCH"
        state.task = task
        state.receipts = [receipt(tree_hash=state.plan.checkpoint_tree, argv=other_command)]
    elif change == "purpose":
        # A test-looking result must not satisfy the fixed build requirement.
        state.receipts = [state.receipts[0].model_copy(update={"purpose": "test"})]
    else:
        state.receipts.append(state.receipts[0].model_copy(update={
            "receipt_id": "d" * 64, "status": "failed", "exit_code": 1,
        }))
    refreshed = loop._refresh_acceptance(state, history)
    assert refreshed.failed and refreshed.verification.status != "verified"
    assert all(value[0] != "passed" for value in refreshed.item_status.values())
    assert all("All configured local checks pass" not in message.content for message in history)


def test_tool_feedback_defers_application_lifecycle_until_final_answer(tmp_path):
    application = FakeApplication()

    def finish(request):
        assert application.starts == application.stops == 0
        feedback = next(message.content for message in reversed(request.messages)
                        if message.name == "harness_acceptance_feedback")
        pending = json.loads(feedback.split(": ", 1)[1])
        assert pending["readiness"][0] == pending["cleanup"][0] == "pending"
        return _stop("The public suite passed; verify readiness and cleanup now.")

    model = ScriptedModel([_run("-m", "unittest", "-q"), finish])
    loop, _, _ = _loop(
        _seed(tmp_path, {"mathx.py": TOTAL_LOOP, "test_total.py": TEST_TOTAL}),
        model, write_paths=("mathx.py",), application=application,
    )
    task = _task("run_app")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"),
                     service_plan=ServicePlan(argv=("python3", "-m", "http.server"), port=8000))
    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)
    assert result.status == "completed"
    assert application.starts == application.stops == 1


def test_inspection_feedback_waits_for_a_cited_final_answer(tmp_path):
    from test_harness_v4_h10 import ScriptedModel, _runtime

    def finish(request):
        state = working_state(request)
        item = state["checklist"]["items"][0]
        assert item["kind"] == "evidence" and item["status"] == "pending"
        assert "final answer" in item["detail"]
        plan = json.loads(state["decisions"][0])
        assert plan["task_ref"] == "working_state"
        assert plan["checklist_ref"] == "working_state.checklist"
        for key in ("task_id", "objective", "acceptance_revision", "task_kind"):
            assert key not in plan and state[key] == getattr(request.task, key)
        return _stop("source.py contains the inspected source evidence.")

    model = ScriptedModel([_call("read_file", {"path": "source.py"}), finish])
    loop, task, grant, session = _runtime(tmp_path, model)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == "completed" and result.model_calls == 2
