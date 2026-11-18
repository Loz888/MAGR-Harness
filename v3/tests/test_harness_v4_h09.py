"""H09 acceptance tests: streaming, persistent cursors and mid-run interaction."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.harness.adapters import (
    HarnessToolRegistry,
    InMemoryBudgetLedger,
    InMemoryEventSink,
    LocalExecutionBroker,
    LocalVerifier,
    LocalWorkspace,
    SimpleContextManager,
)
from mgar_v3.harness.broker import CapabilityAuthority, issue_lease
from mgar_v3.harness.coding_loop import CodingLoop, SafePoint
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CapabilityGrant,
    EventDraft,
    HarnessToolCall,
    ModelResponse,
    ModelUsage,
    TaskBinding,
    VerificationRequirement,
)
from mgar_v3.harness.events import PersistentEventSink
from mgar_v3.harness.inbox import TaskInbox
from mgar_v3.harness.service import (
    CodingTaskRequest,
    HarnessCodingService,
)

ROOT = Path(__file__).resolve().parents[1]
IMAGE_LOCK = ROOT / ".local/r03/sandbox-lock.json"
PYTHON = Path(sys.executable).name
SUITE = VerificationRequirement(
    requirement_id="suite",
    kind="test",
    command_contains=("unittest",),
    min_tests=1,
)

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST_CALC = (
    "import unittest\n"
    "\n"
    "from calc import add\n"
    "\n"
    "\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
# The delay creates a deterministic window for mid-run HTTP calls while the
# real H06 container is executing the suite.
TEST_SLOW = (
    "import time\n"
    "import unittest\n"
    "\n"
    "from calc import add\n"
    "\n"
    "\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def setUp(self):\n"
    "        time.sleep(0.8)\n"
    "\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)


def _seed(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "workspace"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _task(
    kind: str,
    requirements: tuple[VerificationRequirement, ...] = (SUITE,),
    objective: str = "Make the change",
) -> TaskBinding:
    return TaskBinding(
        task_id=f"task-{uuid4().hex[:12]}",
        thread_id=uuid4(),
        run_id=uuid4(),
        actor="agent-1",
        repository="example/repo",
        base_commit="a" * 40,
        workspace_id="ws-1",
        acceptance_revision="acceptance-1",
        objective=objective,
        task_kind=kind,  # type: ignore[arg-type]
        requirements=requirements,
        execution_epoch=2,
    )


def _grant(
    *,
    write_paths: tuple[str, ...] = ("calc.py",),
    read_paths: tuple[str, ...] = ("",),
    execute: bool = True,
    mode: str = "modify_test",
):
    commands = (PYTHON,) if execute else ()
    grant = CapabilityGrant(
        actor="agent-1",
        workspace_id="ws-1",
        repository="example/repo",
        read_paths=read_paths,
        write_paths=write_paths,
        execute=execute,
        allowed_commands=commands,
        max_seconds=60,
    )
    lease = issue_lease(
        mode=mode,  # type: ignore[arg-type]
        actor="agent-1",
        workspace_id="ws-1",
        repository="example/repo",
        execution_epoch=2,
        read_paths=read_paths,
        write_paths=write_paths,
        allowed_commands=commands,
        approved_by="owner" if mode == "modify_test" else "",
        approval_ref="approval-1" if mode == "modify_test" else "",
    )
    authority = CapabilityAuthority(lease, budget_check=lambda: True)
    return grant, authority


def _python_argv(*args: str) -> tuple[str, ...]:
    return (sys.executable, "-B", *args)


def _tool(name: str, arguments: dict[str, object]):
    def step(request) -> ModelResponse:  # type: ignore[no-untyped-def]
        return ModelResponse(
            text="",
            finish_reason="tool_calls",
            tool_calls=(
                HarnessToolCall(
                    call_id=f"call-{uuid4().hex[:12]}",
                    name=name,
                    arguments=arguments,
                ),
            ),
            usage=ModelUsage(cost_micros=5),
        )

    return step


def _stop(text: str = "done"):
    def step(request) -> ModelResponse:  # type: ignore[no-untyped-def]
        return ModelResponse(
            text=text, finish_reason="stop", usage=ModelUsage(cost_micros=5)
        )

    return step


def _read(path: str):
    return _tool("read_file", {"path": path})


class ScriptedModel:
    def __init__(self, steps) -> None:  # type: ignore[no-untyped-def]
        self.steps = list(steps)
        self.calls = 0
        self.requests = []

    def complete(self, request, cancellation=None) -> ModelResponse:  # type: ignore[no-untyped-def]
        self.calls += 1
        self.requests.append(request)
        if not self.steps:
            return _stop()(request)
        step = self.steps.pop(0)
        if callable(step):
            return step(request)
        return step


def _loop(
    root: Path,
    model: ScriptedModel,
    *,
    interaction=None,  # type: ignore[no-untyped-def]
    write_paths: tuple[str, ...] = ("calc.py",),
    read_paths: tuple[str, ...] = ("",),
    execute: bool = True,
    mode: str = "modify_test",
) -> CodingLoop:
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    broker = LocalExecutionBroker(workspace)
    grant, authority = _grant(
        write_paths=write_paths,
        read_paths=read_paths,
        execute=execute,
        mode=mode,
    )
    loop = CodingLoop(
        model=model,
        tools=HarnessToolRegistry(workspace, broker, authority),
        workspace=workspace,
        broker=broker,
        context=SimpleContextManager(),
        verifier=LocalVerifier(),
        events=InMemoryEventSink(),
        budgets=InMemoryBudgetLedger(
            BudgetLimits(
                max_model_calls=16,
                max_tool_calls=64,
                max_cost_micros=500_000,
                max_seconds=300,
            )
        ),
        interaction=interaction,
        reservation_micros=100,
    )
    return loop


def _kind_names(loop: CodingLoop, task: TaskBinding) -> list[str]:
    events = loop.events  # type: ignore[attr-defined]
    return [item.kind for item in events.events(task.run_id)]


# -- safe points and stale responses ------------------------------------


def test_safe_point_applies_instruction_before_model(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel([_read("calc.py"), _stop("calc.py line 2 is wrong")])
    loop = _loop(
        root,
        model,
        write_paths=(),
        execute=False,
        mode="investigate",
    )
    task = _task("inspect")
    plan = loop.plan(task)
    stages: list[str] = []

    def checkpoint(stage: str, revision: int) -> SafePoint:
        stages.append(stage)
        if stage == "before_model" and revision == 0:
            return SafePoint(
                instructions=("Use addition, not subtraction.",),
                objective="Add the two numbers",
                revision=1,
            )
        return SafePoint()

    result = loop.run(
        task,
        _grant(write_paths=(), execute=False, mode="investigate")[0],
        plan,
        checkpoint=checkpoint,
    )

    assert result.status == "completed"
    request = model.requests[0]
    assert any(
        message.role == "user" and "Use addition" in message.content
        for message in request.messages
    )
    assert "Add the two numbers" in request.task.objective
    assert _kind_names(loop, task).count("replanned") == 1


def test_stale_model_result_cannot_override_a_new_instruction(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel(
        [_read("calc.py"), _read("calc.py"), _stop("calc.py line 2 is wrong")]
    )
    loop = _loop(
        root,
        model,
        write_paths=(),
        execute=False,
        mode="investigate",
    )
    task = _task("inspect")
    plan = loop.plan(task)
    delivered = False

    def checkpoint(stage: str, revision: int) -> SafePoint:
        nonlocal delivered
        if stage == "after_model" and not delivered:
            delivered = True
            return SafePoint(
                instructions=("Read calc.py again after the change.",),
                objective="Re-read after the change",
                revision=2,
            )
        return SafePoint()

    result = loop.run(
        task,
        _grant(write_paths=(), execute=False, mode="investigate")[0],
        plan,
        checkpoint=checkpoint,
    )

    kinds = _kind_names(loop, task)
    assert "model_discarded_stale" in kinds
    events = loop.events  # type: ignore[attr-defined]
    started = [
        item.payload.get("name")
        for item in events.events(task.run_id)
        if item.kind == "tool_started"
    ]
    # Two read steps were issued but the first response was discarded, so only
    # the second one reached the tool registry.
    assert started.count("read_file") == 1
    second = model.requests[1]
    assert any(
        message.role == "user" and "Read calc.py again" in message.content
        for message in second.messages
    )
    assert result.status == "completed"


class _AnsweringInteraction:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.questions: list[str] = []

    def ask(self, *, question: str, task: TaskBinding, revision: int) -> str:
        self.questions.append(question)
        return self.answer


def test_ask_user_waits_for_a_real_answer(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    interaction = _AnsweringInteraction("Use the existing helper.")
    model = ScriptedModel(
        [
            _tool("ask_user", {"question": "Which helper should I reuse?"}),
            _read("calc.py"),
            _stop("calc.py line 2 is wrong"),
        ]
    )
    loop = _loop(
        root,
        model,
        interaction=interaction,
        write_paths=(),
        execute=False,
        mode="investigate",
    )
    task = _task("inspect")
    plan = loop.plan(task)

    result = loop.run(
        task,
        _grant(write_paths=(), execute=False, mode="investigate")[0],
        plan,
    )

    assert result.status == "completed"
    assert interaction.questions == ["Which helper should I reuse?"]
    assert any(
        message.role == "tool" and "Use the existing helper." in message.content
        for message in model.requests[1].messages
    )


# -- persistent event log and private-reasoning boundary ----------------


def test_persistent_events_are_cursor_addressable_and_strip_reasoning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    sink = PersistentEventSink(path)
    first = sink.emit(
        EventDraft(
            run_id=uuid4(),
            operation_id="op-1",
            execution_epoch=1,
            revision=1,
            kind="model_completed",
            payload={
                "text": "visible",
                "reasoning": "secret",
                "nested": {"chain_of_thought": "hidden", "keep": 1},
                "items": [{"internal_reasoning": "hidden", "ok": 1}],
            },
        )
    )
    second = sink.emit(
        EventDraft(
            run_id=first.run_id,
            operation_id="op-2",
            execution_epoch=1,
            revision=2,
            kind="tool_completed",
            payload={"name": "read_file"},
        )
    )

    assert (first.cursor, second.cursor) == (1, 2)
    records, next_cursor, has_more = sink.page(after_cursor=0, limit=10)
    assert [record["cursor"] for record in records] == [1, 2]
    assert next_cursor == 2 and has_more is False
    assert records[0]["payload"] == {
        "text": "visible",
        "nested": {"keep": 1},
        "items": [{"ok": 1}],
    }
    assert sink.summary()["event_counts"]["model_completed"] == 1

    # A new sink over the same file must resume the persisted cursor.
    resumed = PersistentEventSink(path)
    third = resumed.emit(
        EventDraft(
            run_id=first.run_id,
            operation_id="op-3",
            execution_epoch=1,
            revision=3,
            kind="run_finished",
            payload={"status": "completed"},
        )
    )
    assert third.cursor == 3
    assert resumed.events(first.run_id, after_cursor=1)[0].cursor == 2


def test_inbox_is_idempotent_and_rejects_conflicting_ids(tmp_path: Path) -> None:
    inbox = TaskInbox(tmp_path / "inbox.json")
    record, duplicate = inbox.post(
        message_id="m-1", kind="instruction", text="Add a docstring."
    )
    assert duplicate is False
    again, duplicate_again = inbox.post(
        message_id="m-1", kind="instruction", text="Add a docstring."
    )
    assert duplicate_again is True
    assert again["message_id"] == record["message_id"]
    with pytest.raises(DomainError) as exc:
        inbox.post(message_id="m-1", kind="instruction", text="Different text")
    assert exc.value.code == "HARNESS_MESSAGE_CONFLICT"


# -- service and HTTP wiring --------------------------------------------


def _request(project_id: str) -> CodingTaskRequest:
    return CodingTaskRequest(
        project_id=project_id,
        objective="Fix add() and prove it with the suite",
        task_kind="bugfix",
        test_command="python3 -m unittest -v",
        target_path="calc.py",
        old_text="    return a - b\n",
        new_text="    return a + b\n",
        approved=True,
    )


def _wait_terminal(service: HarnessCodingService, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        page = service.event_page(task_id, after=0, limit=500)
        if page["terminal"]:
            return page
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not become terminal")


def _wait_status(
    service: HarnessCodingService,
    task_id: str,
    statuses: set[str],
    timeout: float = 30.0,
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = service.get_task(task_id)
        if result.get("status") in statuses:
            return result
        if result.get("status") in {
            "completed",
            "blocked",
            "denied",
            "failed",
            "cancelled",
            "incomplete",
            "budget_exhausted",
            "unknown",
        }:
            return result
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {statuses}")


def test_background_task_streams_persistent_cursor_events(tmp_path: Path) -> None:
    source = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    service = HarnessCodingService(
        tmp_path / "harness", actor="owner", image_lock=IMAGE_LOCK
    )
    project = service.register_project(str(source))

    submitted = service.submit_task(_request(project["project_id"]))
    assert submitted["status"] in {"queued", "running"}
    task_id = submitted["task_id"]

    page = _wait_terminal(service, task_id)
    kinds = [record["kind"] for record in page["events"]]
    for required in (
        "run_started",
        "model_requested",
        "model_completed",
        "patch_applied",
        "test_completed",
        "artifact_ready",
    ):
        assert required in kinds, (required, kinds)
    cursors = [record["cursor"] for record in page["events"]]
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == len(cursors)

    tail = service.event_page(task_id, after=cursors[0], limit=500)
    assert all(record["cursor"] > cursors[0] for record in tail["events"])
    assert tail["next_cursor"] == cursors[-1]

    result = service.join_task(task_id)
    assert result["status"] == "completed"
    assert result["background"] is True
    assert result["events_cursor"] == cursors[-1]
    assert Path(result["patch_path"]).read_text(encoding="utf-8") == result["diff"]


def test_mid_run_instruction_is_idempotent_and_applied_once(tmp_path: Path) -> None:
    source = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_SLOW})
    service = HarnessCodingService(
        tmp_path / "harness", actor="owner", image_lock=IMAGE_LOCK
    )
    project = service.register_project(str(source))
    submitted = service.submit_task(_request(project["project_id"]))
    task_id = submitted["task_id"]
    _wait_status(service, task_id, {"running"})

    first = service.post_message(
        task_id,
        kind="instruction",
        text="Also keep the public function name.",
        message_id="m-1",
    )
    assert first["duplicate"] is False
    again = service.post_message(
        task_id,
        kind="instruction",
        text="Also keep the public function name.",
        message_id="m-1",
    )
    assert again["duplicate"] is True
    with pytest.raises(DomainError) as exc:
        service.post_message(
            task_id,
            kind="instruction",
            text="Conflicting payload",
            message_id="m-1",
        )
    assert exc.value.code == "HARNESS_MESSAGE_CONFLICT"

    result = service.join_task(task_id, timeout=60.0)
    assert result["status"] == "completed"
    stored = [item for item in result["inbox"] if item["message_id"] == "m-1"]
    assert len(stored) == 1
    page = service.event_page(task_id, after=0, limit=500)
    applied = [
        record
        for record in page["events"]
        if record["kind"] == "instruction_applied"
        and record["payload"].get("message_id") == "m-1"
    ]
    assert len(applied) == 1


def test_cancel_receipt_confirms_delivery_not_refund(tmp_path: Path) -> None:
    source = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_SLOW})
    service = HarnessCodingService(
        tmp_path / "harness", actor="owner", image_lock=IMAGE_LOCK
    )
    project = service.register_project(str(source))
    submitted = service.submit_task(_request(project["project_id"]))
    task_id = submitted["task_id"]
    _wait_status(service, task_id, {"running"})

    receipt = service.cancel_task(task_id, "operator stopped the run")
    assert receipt["refund"] == "not_promised"
    result = service.join_task(task_id, timeout=60.0)
    assert result["status"] == "cancelled"
    assert result["cancel_receipt"]["status"] == "observed"
    assert result["cancel_receipt"]["requested_at"]
    assert result["cancel_receipt"]["refund"] == "not_promised"
    page = service.event_page(task_id, after=0, limit=500)
    kinds = [record["kind"] for record in page["events"]]
    assert "cancel_requested" in kinds


def test_pause_and_resume_keep_the_inbox_and_finish(tmp_path: Path) -> None:
    source = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_SLOW})
    service = HarnessCodingService(
        tmp_path / "harness", actor="owner", image_lock=IMAGE_LOCK
    )
    project = service.register_project(str(source))
    submitted = service.submit_task(_request(project["project_id"]))
    task_id = submitted["task_id"]
    _wait_status(service, task_id, {"running"})

    service.pause_task(task_id)
    paused = _wait_status(service, task_id, {"paused"})
    assert paused["status"] == "paused"
    service.resume_task(task_id)
    result = service.join_task(task_id, timeout=60.0)
    assert result["status"] == "completed"


def test_web_h09_stream_page_messages_and_task_id_validation(
    settings, engine, tmp_path: Path
) -> None:
    source = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    client = TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )
    with client:
        project_id = client.post(
            "/api/v3/harness/projects", json={"path": str(source)}
        ).json()["project_id"]
        created = client.post(
            "/api/v3/harness/tasks",
            json={
                "project_id": project_id,
                "objective": "Fix add()",
                "task_kind": "bugfix",
                "test_command": "python3 -m unittest -v",
                "target_path": "calc.py",
                "old_text": "    return a - b\n",
                "new_text": "    return a + b\n",
                "approved": True,
                "background": True,
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]

        page: dict = {}
        for _ in range(600):
            page = client.get(
                f"/api/v3/harness/tasks/{task_id}/events/page?after=0&limit=500"
            ).json()
            if page["terminal"]:
                break
            time.sleep(0.05)
        assert page["terminal"] is True

        stream = client.get(
            f"/api/v3/harness/tasks/{task_id}/events?follow=false"
        )
        assert stream.status_code == 200
        assert "event: update" in stream.text
        assert "event: done" in stream.text
        assert "id: " in stream.text

        messages = client.get(
            f"/api/v3/harness/tasks/{task_id}/messages"
        ).json()
        assert messages["task_id"] == task_id

        finished = client.post(
            f"/api/v3/harness/tasks/{task_id}/messages",
            json={"kind": "instruction", "text": "too late"},
        )
        assert finished.status_code == 409
        assert finished.json()["code"] == "HARNESS_TASK_FINISHED"

        assert client.get(
            "/api/v3/harness/tasks/not-a-task/events/page"
        ).status_code == 400
        assert client.get(
            "/api/v3/harness/tasks/h08-000000000000/events/page"
        ).status_code == 404
