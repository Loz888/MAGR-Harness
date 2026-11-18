"""H10 F01: completion is fenced across terminal reads and persistence.

These tests use real temporary workspaces and CAS state with a controlled,
zero-cost model. No command, Docker container or database is started.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _runtime, _stop

from mgar_v3.domain import DomainError
from mgar_v3.harness.adapters import InMemoryEventSink
from mgar_v3.harness.contracts import EventDraft
from mgar_v3.harness.long_context import LongContextManager


class _TerminalBoundary:
    def __init__(self) -> None:
        self.verified = False
        self.finishing = False
        self.injected = False
        self.verified_tree = ""
        self.original_verification: Any = None


@pytest.mark.parametrize("simple", [False, True], ids=["bounded", "legacy"])
@pytest.mark.parametrize(
    "boundary",
    ["verification_event", "snapshot_return", "diff_return", "finish_event"],
)
def test_terminal_tree_drift_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, simple: bool, boundary: str
) -> None:
    _exercise_terminal_drift(tmp_path, monkeypatch, simple=simple, boundary=boundary)


def test_tree_drift_during_context_persistence_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exercise_terminal_drift(tmp_path, monkeypatch, simple=False, boundary="context_persist")


def _exercise_terminal_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, simple: bool, boundary: str
) -> None:
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, simple=simple)
    trigger = _TerminalBoundary()
    before = loop.workspace.snapshot().tree_hash

    def mutate() -> None:
        if not trigger.injected:
            loop.workspace.inner.root.joinpath("source.py").write_text(
                "value = 99\n", encoding="utf-8"
            )
            trigger.injected = True

    class BoundaryEvents(InMemoryEventSink):
        def emit(self, draft: EventDraft) -> Any:
            event = super().emit(draft)
            if draft.kind == "verification_result" and draft.payload.get("status") == "verified":
                trigger.verified = True
                trigger.verified_tree = str(draft.payload["tree"])
                if boundary == "verification_event":
                    mutate()
            elif draft.kind == "run_finished" and boundary == "finish_event":
                mutate()
            return event

    loop.events = BoundaryEvents()
    finish = loop._finish

    def enter_finish(*args: Any, **kwargs: Any) -> Any:
        trigger.finishing = True
        evaluation = args[4] if len(args) > 4 else kwargs.get("evaluation")
        if evaluation is not None:
            trigger.original_verification = evaluation.verification.model_copy(deep=True)
        return finish(*args, **kwargs)

    monkeypatch.setattr(loop, "_finish", enter_finish)
    snapshot = loop.workspace.snapshot

    def snapshot_then_change() -> Any:
        view = snapshot()
        if trigger.finishing and boundary == "snapshot_return":
            mutate()
        return view

    monkeypatch.setattr(loop.workspace, "snapshot", snapshot_then_change)
    diff = loop.workspace.diff

    def diff_then_change(*args: Any, **kwargs: Any) -> Any:
        view = diff(*args, **kwargs)
        if trigger.finishing and boundary == "diff_return":
            mutate()
        return view

    monkeypatch.setattr(loop.workspace, "diff", diff_then_change)
    if isinstance(loop.context, LongContextManager):
        persist = loop.context.persist_state

        def persist_then_change(*args: Any, **kwargs: Any) -> Any:
            reference = persist(*args, **kwargs)
            if trigger.finishing and boundary == "context_persist":
                mutate()
            return reference

        monkeypatch.setattr(loop.context, "persist_state", persist_then_change)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()

    assert trigger.verified and trigger.injected, "The requested terminal boundary was not reached"
    assert trigger.verified_tree == before
    assert result.status != "completed", (boundary, result.model_dump(mode="json"))
    assert result.checkpoint is not None
    assert result.tree_after == snapshot().tree_hash
    assert result.tree_after != before
    assert result.diff.tree_after == result.tree_after
    assert result.checkpoint.tree_hash == result.tree_after
    assert result.checklist.unmet_ids(), "Old passing statuses cannot certify the changed tree"
    assert result.checkpoint.unmet_items == result.checklist.unmet_ids()
    assert result.verification == trigger.original_verification, (
        "Do not re-sign the old verification"
    )
    assert result.verification.tree_hash == before
    assert result.model_calls == 2 and result.tool_calls == 1
    assert result.cost_micros == 0
    assert loop.workspace.read("source.py") == "value = 99\n"
    if isinstance(loop.context, LongContextManager):
        assert result.context_state is not None
        assert result.context_state.tree == result.tree_after
        assert result.context_state.unmet_items == result.checklist.unmet_ids()
        assert result.checkpoint.context_state == result.context_state
        assert result.checkpoint.context_ref == result.context_ref
        assert result.context_ref is not None
        assert loop.context.state_path is not None
        record = loop.context.load(loop.context.state_path, result.context_state)
        assert record.working_state == result.context_state
        assert record.working_state.tree != result.verification.tree_hash
    finished = [event for event in loop.events.events(task.run_id) if event.kind == "run_finished"]
    assert finished and finished[-1].payload["status"] == result.status


@pytest.mark.parametrize("simple", [False, True], ids=["bounded", "legacy"])
def test_normal_completion_binds_all_terminal_artifacts_to_one_tree(
    tmp_path: Path, simple: bool
) -> None:
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, simple=simple)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    current = loop.workspace.snapshot().tree_hash
    assert result.status == "completed"
    assert result.verification is not None and result.verification.status == "verified"
    assert result.tree_after == result.diff.tree_after == result.verification.tree_hash == current
    assert result.checklist.unmet_ids() == ()
    assert all(item.status == "passed" for item in result.checklist.required_items())
    assert result.checkpoint is None
    assert result.model_calls == 2 and result.tool_calls == 1 and result.cost_micros == 0
    if isinstance(loop.context, LongContextManager):
        assert result.context_state is not None and result.context_ref is not None
        assert result.context_state.tree == current
        assert result.context_state.unmet_items == ()
        assert result.context_state.checklist == result.checklist.model_dump(mode="json")
        assert loop.context.state_path is not None
        record = loop.context.load(loop.context.state_path, result.context_state)
        assert record.working_state == result.context_state


@pytest.mark.parametrize("simple", [False, True], ids=["bounded", "legacy"])
def test_pending_required_item_cannot_complete_even_with_verified_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, simple: bool
) -> None:
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, simple=simple)
    evaluate = loop._evaluate

    def inconsistent_checklist(*args: Any, **kwargs: Any) -> Any:
        evaluation = evaluate(*args, **kwargs)
        assert evaluation.verification.status == "verified"
        assert not evaluation.failed
        evaluation.item_status["evidence"] = ("pending", "missing item certification", ())
        return evaluation

    monkeypatch.setattr(loop, "_evaluate", inconsistent_checklist)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status != "completed"
    assert result.checklist.unmet_ids() == ("evidence",)
    assert result.checkpoint is not None
    assert result.checkpoint.unmet_items == ("evidence",)
    assert result.model_calls == 2 and result.tool_calls == 1


def test_context_persistence_failure_blocks_completion_without_a_new_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model)
    assert isinstance(loop.context, LongContextManager)
    attempts = []

    def fail_persistence(*args: Any, **kwargs: Any) -> Any:
        attempts.append(True)
        raise DomainError("CONTEXT_CHECKPOINT_WRITE_FAILED", "Controlled storage failure", 409)

    monkeypatch.setattr(loop.context, "persist_state", fail_persistence)
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert attempts == [True]
    assert result.status == "blocked"
    assert result.reason == "CONTEXT_CHECKPOINT_WRITE_FAILED"
    assert result.context_ref is None
    assert result.checkpoint is not None and result.checkpoint.context_ref is None
    assert result.context_state is not None
    assert result.context_state.tree == result.tree_after == loop.workspace.snapshot().tree_hash
    assert result.checkpoint.tree_hash == result.tree_after
    assert result.verification is not None and result.verification.tree_hash == result.tree_after
    assert result.model_calls == 2 and result.tool_calls == 1 and result.cost_micros == 0
    finished = [event for event in loop.events.events(task.run_id) if event.kind == "run_finished"]
    assert finished and finished[-1].payload["status"] == "blocked"


@pytest.mark.parametrize("simple", [False, True], ids=["bounded", "legacy"])
def test_continuously_changing_terminal_tree_fails_within_four_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, simple: bool
) -> None:
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), _stop()])
    loop, task, grant, session = _runtime(tmp_path, model, simple=simple)
    finishing = False
    changes = []
    finish = loop._finish
    diff = loop.workspace.diff

    def enter_finish(*args: Any, **kwargs: Any) -> Any:
        nonlocal finishing
        finishing = True
        return finish(*args, **kwargs)

    def diff_then_change(*args: Any, **kwargs: Any) -> Any:
        view = diff(*args, **kwargs)
        if finishing:
            changes.append(True)
            loop.workspace.inner.root.joinpath("source.py").write_text(
                f"value = {99 + len(changes)}\n", encoding="utf-8"
            )
        return view

    monkeypatch.setattr(loop, "_finish", enter_finish)
    monkeypatch.setattr(loop.workspace, "diff", diff_then_change)
    try:
        with pytest.raises(DomainError) as caught:
            loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert caught.value.code == "HARNESS_TERMINAL_TREE_UNSTABLE"
    assert 1 <= len(changes) <= 4, "Unstable terminal reads must have a finite retry bound"
    events = loop.events.events(task.run_id)
    assert any(event.kind == "terminal_invalidated" for event in events)
    assert not any(
        event.kind == "run_finished" and event.payload.get("status") == "completed"
        for event in events
    )
    assert len(model.requests) == 2
