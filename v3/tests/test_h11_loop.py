"""Pure continuation regressions; process/PG evidence lives in the H11 suite."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_harness_v4_h07 import (
    BUGGY,
    FIXED,
    TEST_CALC,
    ScriptedModel,
    _grant,
    _loop,
    _python_argv,
    _run,
    _seed,
    _stop,
    _task,
)

from mgar_v3.domain import DomainError
from mgar_v3.harness.adapters import SimpleContextManager
from mgar_v3.harness.broker import CapabilityAuthority, issue_lease
from mgar_v3.harness.coding_loop import CodingLoop
from mgar_v3.harness.contracts import HarnessToolCall, ModelResponse, ModelUsage, sha256_text


class Crash(BaseException):
    """A non-cooperative interruption, without cleanup by the coding loop."""


class MemoryContinuation:
    def __init__(self, crash_before_baseline: bool = False) -> None:
        self.value: dict[str, Any] | None = None
        self.crash_before_baseline = crash_before_baseline

    def load(self) -> dict[str, Any] | None:
        return json.loads(json.dumps(self.value)) if self.value else None

    def save(self, payload: dict[str, Any]) -> None:
        self.value = json.loads(json.dumps(payload))
        if self.crash_before_baseline and payload["phase"] == "before_baseline":
            self.crash_before_baseline = False
            raise Crash()


class KnownModel:
    def __init__(self, inner: Any, *, crash: bool = False) -> None:
        self.inner, self.crash = inner, crash
        self.cache: dict[str, ModelResponse] = {}
        self.requests: list[Any] = []

    def complete(self, request: Any, cancellation: Any) -> ModelResponse:
        self.requests.append(request)
        if request.operation_id in self.cache:
            return self.cache[request.operation_id]
        response: ModelResponse = self.inner.complete(request, cancellation)
        self.cache[request.operation_id] = response
        if self.crash:
            self.crash = False
            raise Crash()
        return response


class KnownTools:
    def __init__(self, inner: Any, *, crash: int = 0) -> None:
        self.inner, self.crash = inner, crash
        self.cache: dict[str, Any] = {}
        self.effects: list[str] = []
        self.replays: list[str] = []

    def specs(self) -> Any:
        return self.inner.specs()

    def invoke(self, call: Any, context: Any, token: Any) -> Any:
        if context.operation_id in self.cache:
            self.replays.append(call.name)
            return self.cache[context.operation_id]
        result = self.inner.invoke(call, context, token)
        self.cache[context.operation_id] = result
        self.effects.append(call.name)
        if self.crash and self.crash == len(self.effects):
            self.crash = 0
            raise Crash()
        return result


class KnownBaseline:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.cache: dict[str, Any] = {}
        self.physical_calls = 0
        self.crash = True

    def execute(self, request: Any, grant: Any, token: Any) -> Any:
        if request.operation_id in self.cache:
            return self.cache[request.operation_id]
        self.physical_calls += 1
        receipt = self.inner.execute(request, grant, token)
        self.cache[request.operation_id] = receipt
        if self.crash:
            self.crash = False
            raise Crash()
        return receipt

    def is_recovered_receipt(self, receipt: Any) -> bool:
        return receipt.operation_id in self.cache


def edit_and_read(request: Any) -> ModelResponse:
    return ModelResponse(
        text="Apply the fix and inspect its result",
        finish_reason="tool_calls",
        tool_calls=(
            HarnessToolCall(
                call_id="fix-add",
                name="apply_edits",
                arguments={
                    "expected_tree": request.workspace_tree,
                    "edits": [
                        {"path": "calc.py", "expected_sha256": sha256_text(BUGGY), "content": FIXED}
                    ],
                },
            ),
            HarnessToolCall(call_id="read-fix", name="read_file", arguments={"path": "calc.py"}),
        ),
        usage=ModelUsage(cost_micros=5),
    )


def fresh_loop(old: CodingLoop, continuation: MemoryContinuation) -> CodingLoop:
    return CodingLoop(
        model=old.model,
        tools=old.tools,
        workspace=old.workspace,
        broker=old.broker,
        context=SimpleContextManager(),
        verifier=old.verifier,
        events=old.events,
        budgets=old.budgets,
        continuation=continuation,
        reservation_micros=100,
    )


@pytest.mark.parametrize(
    "cut", ["before_baseline", "baseline_known", "model_known", "patch_known", "read_known"]
)
def test_resume_preserves_request_history_baseline_and_partial_batch(
    tmp_path: Path,
    cut: str,
) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    physical = ScriptedModel([edit_and_read, _run("-m", "unittest", "-q"), _stop()])
    loop, workspace, _ = _loop(root, physical)
    continuation = MemoryContinuation(crash_before_baseline=cut == "before_baseline")
    model = KnownModel(physical, crash=cut == "model_known")
    tools = KnownTools(
        loop.tools, crash=1 if cut == "patch_known" else 2 if cut == "read_known" else 0
    )
    loop.model, loop.tools, loop.continuation = model, tools, continuation
    if cut == "baseline_known":
        loop.broker = KnownBaseline(loop.broker)
    task, grant = _task("bugfix"), _grant()[0]
    baseline = _python_argv("-m", "unittest", "-q")
    plan = loop.plan(task, baseline_command=baseline)
    with pytest.raises(Crash):
        loop.run(task, grant, plan)
    assert continuation.value is not None
    # Only the trusted reconciler may authorize the already applied patch.
    continuation.value["expected_tree"] = workspace.snapshot().tree_hash
    old_request = model.requests[0] if model.requests else None
    resumed_task = task.model_copy(update={"execution_epoch": task.execution_epoch + 1})
    tools.inner.authority = CapabilityAuthority(
        issue_lease(
            mode="modify_test",
            actor=task.actor,
            workspace_id=task.workspace_id,
            repository=task.repository,
            execution_epoch=resumed_task.execution_epoch,
            read_paths=grant.read_paths,
            write_paths=grant.write_paths,
            allowed_commands=grant.allowed_commands,
            approved_by="owner",
            approval_ref="approval-1",
        ),
        budget_check=lambda: True,
    )
    resumed = fresh_loop(loop, continuation)
    restored_plan = resumed.plan(resumed_task, baseline_command=baseline)
    result = resumed.run(resumed_task, grant, restored_plan)
    assert result.status == "completed", result.reason
    assert physical.calls == 3
    assert result.model_calls == 3
    assert tools.effects.count("apply_edits") == 1
    assert tools.effects.count("read_file") == 1
    assert result.tree_before == plan.checkpoint_tree
    assert result.tree_after == workspace.snapshot().tree_hash
    assert result.receipts[0].status == "failed"
    assert result.receipts[-1].status == "passed"
    if cut in {"patch_known", "read_known"}:
        assert tools.replays == (
            ["apply_edits"] if cut == "patch_known" else ["apply_edits", "read_file"]
        )
        assert old_request is not None
        assert model.requests[1].workspace_tree == old_request.workspace_tree
        assert model.requests[1].messages == old_request.messages
        assert model.requests[1].task.execution_epoch == resumed_task.execution_epoch
        assert not any(attempt.reason == "stale_tree" for attempt in result.attempts)
    if cut == "baseline_known":
        assert isinstance(loop.broker, KnownBaseline)
        assert loop.broker.physical_calls == 1


def test_resume_rejects_workspace_drift_before_replaying_known_output(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    physical = ScriptedModel([edit_and_read])
    loop, _, _ = _loop(root, physical)
    continuation = MemoryContinuation()
    loop.continuation = continuation
    loop.model = KnownModel(physical, crash=True)
    task, grant = _task("bugfix"), _grant()[0]
    plan = loop.plan(task)
    with pytest.raises(Crash):
        loop.run(task, grant, plan)
    (root / "calc.py").write_text("unreconciled manual write\n")
    resumed = fresh_loop(loop, continuation)
    with pytest.raises(DomainError) as denied:
        resumed.run(task, grant, resumed.plan(task))
    assert denied.value.code == "HARNESS_CONTINUATION_TREE"
    assert physical.calls == 1
    assert len(loop.model.requests) == 1


def test_ask_user_known_result_uses_the_same_durable_operation(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": FIXED})
    loop, _, _ = _loop(root, ScriptedModel([]), execute=False, write_paths=(), mode="investigate")
    calls: list[str] = []

    class Interaction:
        def ask(self, *, question: str, task: Any, revision: int) -> str:
            calls.append(question)
            return "confirmed"

    class Tools(KnownTools):
        def invoke_interaction(
            self,
            call: Any,
            context: Any,
            token: Any,
            callback: Callable[[], Any],
        ) -> Any:
            if context.operation_id in self.cache:
                return self.cache[context.operation_id]
            result = callback()
            self.cache[context.operation_id] = result
            raise Crash()

    tools = Tools(loop.tools)
    request = ModelResponse(
        text="",
        finish_reason="tool_calls",
        usage=ModelUsage(cost_micros=5),
        tool_calls=(
            HarnessToolCall(
                call_id="ask-one", name="ask_user", arguments={"question": "Continue?"}
            ),
        ),
    )
    read = ModelResponse(
        text="",
        finish_reason="tool_calls",
        usage=ModelUsage(cost_micros=5),
        tool_calls=(
            HarnessToolCall(
                call_id="read-after-answer", name="read_file", arguments={"path": "calc.py"}
            ),
        ),
    )
    physical = ScriptedModel([request, read, _stop("calc.py contains the implementation")])
    loop.model, loop.tools, loop.interaction = KnownModel(physical), tools, Interaction()
    continuation = MemoryContinuation()
    loop.continuation = continuation
    task, grant = _task("inspect"), _grant(execute=False, write_paths=(), mode="investigate")[0]
    plan = loop.plan(task)
    with pytest.raises(Crash):
        loop.run(task, grant, plan)
    resumed = fresh_loop(loop, continuation)
    resumed.interaction = loop.interaction
    result = resumed.run(task, grant, resumed.plan(task))
    assert result.status == "completed", result.reason
    assert calls == ["Continue?"]
    assert physical.calls == 3
    assert len(tools.cache) == 2
