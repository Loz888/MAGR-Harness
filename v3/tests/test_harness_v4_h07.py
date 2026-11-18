"""H07 acceptance tests: one unified coding loop with task-kind evidence rules."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.adapters import (
    HarnessToolRegistry,
    HarnessToolResult,
    InMemoryBudgetLedger,
    InMemoryEventSink,
    LocalExecutionBroker,
    LocalVerifier,
    LocalWorkspace,
    SimpleContextManager,
)
from mgar_v3.harness.broker import CapabilityAuthority, issue_lease
from mgar_v3.harness.coding_loop import (
    AcceptanceProposal,
    ApplicationPort,
    CodingLoop,
    ServicePlan,
    ServiceReceipt,
)
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    ModelResponse,
    ModelUsage,
    TaskBinding,
    ToolSpec,
    VerificationRequirement,
    sha256_text,
)

PYTHON = Path(sys.executable).name
SUITE = VerificationRequirement(
    requirement_id="suite",
    kind="test",
    command_contains=("unittest",),
    min_tests=1,
)
NEW_TESTS = VerificationRequirement(
    requirement_id="new-tests",
    kind="test",
    command_contains=("unittest", "test_mul"),
    min_tests=1,
)

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
WRONG_MUL = "def add(a, b):\n    return a * b\n"
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
MATHX = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def mul(a, b):\n"
    "    raise NotImplementedError\n"
)
MATHX_FULL = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def mul(a, b):\n"
    "    return a * b\n"
)
MATHX_COMMENT = "def add(a, b):\n    # tuned later\n    return a + b\n"
TEST_MATHX = (
    "import unittest\n"
    "\n"
    "from mathx import add, mul\n"
    "\n"
    "\n"
    "class MathTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
    "\n"
    "    def test_mul(self):\n"
    "        self.skipTest('not implemented')\n"
    "        self.assertEqual(mul(2, 3), 6)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
TEST_MATHX_ENABLED = TEST_MATHX.replace("        self.skipTest('not implemented')\n", "")
TOTAL_LOOP = (
    "def total(values):\n"
    "    result = 0\n"
    "    for value in values:\n"
    "        result += value\n"
    "    return result\n"
)
TOTAL_BUILTIN = "def total(values):\n    return sum(values)\n"
TOTAL_SIGNATURE = (
    "def total(values, floor=0):\n"
    "    return max(floor, sum(values))\n"
)
TEST_TOTAL = (
    "import unittest\n"
    "\n"
    "from mathx import total\n"
    "\n"
    "\n"
    "class TotalTest(unittest.TestCase):\n"
    "    def test_total(self):\n"
    "        self.assertEqual(total([1, 2, 3]), 6)\n"
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
    # -B keeps the local broker from reusing same-size, same-second
    # __pycache__ entries after an edit (mtime+size invalidation is coarse).
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
        return ModelResponse(text=text, finish_reason="stop", usage=ModelUsage(cost_micros=5))

    return step


def _edit(path: str, old: str, new: str, *, tree: str):
    return _tool(
        "apply_edits",
        {
            "edits": [
                {
                    "path": path,
                    "expected_sha256": sha256_text(old),
                    "content": new,
                }
            ],
            "expected_tree": tree,
        },
    )


def _run(*args: str):
    return _tool("run_tests", {"argv": list(_python_argv(*args))})


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


class EmptySearchRegistry(HarnessToolRegistry):
    """Adds one real read tool whose result is always empty for this test."""

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(super().specs()) + (
            ToolSpec(
                name="search_files",
                description="Search the workspace",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                capability="read",
            ),
        )

    def invoke(self, call, context, cancellation):  # type: ignore[no-untyped-def]
        if call.name != "search_files":
            return super().invoke(call, context, cancellation)
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content="",
            data={"path": ".", "matches": []},
        )


class FakeApplication(ApplicationPort):
    def __init__(self, *, ready: bool = True, stopped: bool = True) -> None:
        self.ready = ready
        self.stopped = stopped
        self.starts = 0
        self.stops = 0

    def start(self, plan, tree, grant, cancellation):  # type: ignore[no-untyped-def]
        self.starts += 1
        return ServiceReceipt(
            receipt_id=digest({"tree": tree, "start": self.starts, "ready": self.ready}),
            argv=plan.argv,
            tree_hash=tree,
            started=True,
            ready=self.ready,
            stopped=False,
            address=f"127.0.0.1:{plan.port}" if self.ready else "",
            process_id=4242 if self.ready else None,
            probe_detail="GET / -> 200" if self.ready else "connection refused",
            log_sha256="a" * 64,
        )

    def stop(self, receipt):  # type: ignore[no-untyped-def]
        self.stops += 1
        return receipt.model_copy(
            update={
                "receipt_id": digest({"stop": receipt.receipt_id, "ok": self.stopped}),
                "stopped": self.stopped,
                "cleanup_detail": "process exited" if self.stopped else "process still running",
            }
        )


def _loop(
    root: Path,
    model: ScriptedModel,
    *,
    write_paths: tuple[str, ...] = ("calc.py",),
    read_paths: tuple[str, ...] = ("",),
    execute: bool = True,
    mode: str = "modify_test",
    application: ApplicationPort | None = None,
    registry: type[HarnessToolRegistry] = HarnessToolRegistry,
    limits: BudgetLimits | None = None,
) -> tuple[CodingLoop, LocalWorkspace, InMemoryEventSink]:
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    broker = LocalExecutionBroker(workspace)
    grant, authority = _grant(
        write_paths=write_paths,
        read_paths=read_paths,
        execute=execute,
        mode=mode,
    )
    caps = limits or BudgetLimits(
        max_model_calls=16,
        max_tool_calls=64,
        max_cost_micros=500_000,
        max_seconds=300,
    )
    loop = CodingLoop(
        model=model,
        tools=registry(workspace, broker, authority),
        workspace=workspace,
        broker=broker,
        context=SimpleContextManager(),
        verifier=LocalVerifier(),
        events=InMemoryEventSink(),
        budgets=InMemoryBudgetLedger(caps),
        application=application,
        reservation_micros=100,
    )
    return loop, workspace, loop.events  # type: ignore[return-value]


def _item(result, kind: str):  # type: ignore[no-untyped-def]
    matches = [item for item in result.checklist.items if item.kind == kind]
    assert len(matches) == 1, [item.item_id for item in result.checklist.items]
    return matches[0]


# -- planning and checklist ---------------------------------------------


def test_plan_binds_kind_objective_and_worktree(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel([])
    loop, workspace, _ = _loop(root, model)
    task = _task("bugfix", objective="Fix add")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    assert plan.task_id == task.task_id
    assert plan.objective == task.objective
    assert plan.task_kind == "bugfix"
    assert plan.checkpoint_tree == workspace.snapshot().tree_hash
    kinds = [item.kind for item in plan.checklist.required_items()]
    assert kinds == ["reproduction", "regression"]
    assert all(item.requirement_id == "suite" for item in plan.checklist.items)


def test_model_proposals_are_additive_and_bound_to_trusted_commands(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    loop, _, _ = _loop(root, ScriptedModel([]))
    task = _task("bugfix")
    plan = loop.plan(task)
    required_before = {item.item_id for item in plan.checklist.required_items()}

    updated = loop.accept_proposal(
        task,
        plan,
        [
            AcceptanceProposal(
                item_id="extra.suite",
                description="Re-run the suite once more",
                kind="regression",
                requirement_id="suite",
                required=False,
            )
        ],
    )
    assert updated.checklist.revision == plan.checklist.revision + 1
    assert {item.item_id for item in updated.checklist.required_items()} == required_before
    assert updated.checklist.items[-1].source == "model"


def test_proposal_cannot_replace_required_items_or_invent_commands(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    loop, _, _ = _loop(root, ScriptedModel([]))
    task = _task("bugfix")
    plan = loop.plan(task)
    existing = plan.checklist.items[0].item_id

    with pytest.raises(DomainError) as replaced:
        loop.accept_proposal(
            task,
            plan,
            [AcceptanceProposal(item_id=existing, description="skip it", kind="evidence")],
        )
    assert replaced.value.code == "HARNESS_ACCEPTANCE_REJECTED"

    with pytest.raises(DomainError) as invented:
        loop.accept_proposal(
            task,
            plan,
            [
                AcceptanceProposal(
                    item_id="extra.fake",
                    description="use a command nobody trusted",
                    kind="behavior",
                    requirement_id="invented",
                    required=True,
                )
            ],
        )
    assert invented.value.code == "HARNESS_ACCEPTANCE_REJECTED"


# -- bugfix -------------------------------------------------------------


def test_bugfix_reproduces_then_fixes_and_completes(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, FIXED, tree="TREE"),
            _run("-m", "unittest", "-q"),
            _stop("fixed"),
        ]
    )
    loop, workspace, _ = _loop(root, model)
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))
    # The scripted edit needs the tree produced by the baseline snapshot.
    tree = workspace.snapshot().tree_hash
    model.steps[0] = _edit("calc.py", BUGGY, FIXED, tree=tree)

    result = loop.run(task, _grant()[0], plan)
    assert result.status == "completed", (
        result.reason,
        [
            (item.item_id, item.status, item.detail[:120])
            for item in result.checklist.items
        ],
        [(feedback.kind, feedback.message[:120]) for feedback in result.feedback],
        [
            (receipt.tree_hash[:8], receipt.status, receipt.exit_code, receipt.tests_run)
            for receipt in result.receipts
        ],
        result.model_calls,
        result.tool_calls,
    )
    assert result.tree_before == tree
    assert result.tree_after == workspace.snapshot().tree_hash
    assert result.verification is not None
    assert result.verification.status == "verified"
    assert result.verification.tree_hash == result.tree_after
    assert result.diff.tree_after == result.tree_after
    assert [change.path for change in result.diff.changes] == ["calc.py"]
    assert _item(result, "reproduction").status == "passed"
    baseline = [receipt for receipt in result.receipts if receipt.tree_hash == tree]
    assert any(receipt.status == "failed" for receipt in baseline)
    assert result.receipts[-1].status == "passed"
    assert result.model_calls == 3
    assert result.graph.nodes[0].kind == "plan"
    assert result.graph.nodes[-1].kind in {"verification", "feedback", "checkpoint"}
    assert result.events_cursor > 0


def test_bugfix_cannot_skip_reproduction_even_when_the_fix_passes(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, FIXED, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("fixed"),
            _stop("still fixed"),
            _stop("done"),
        ]
    )
    loop, _, _ = _loop(root, model)
    task = _task("bugfix")
    plan = loop.plan(task)  # no baseline command: the model never saw a failure

    result = loop.run(task, _grant()[0], plan)

    assert result.status != "completed"
    assert result.status == "blocked"
    assert _item(result, "reproduction").status == "failed"
    assert _item(result, "regression").status == "passed"
    assert result.checkpoint is not None
    assert any("reproduce" in item for item in result.checkpoint.unmet_items)


def test_verification_failure_keeps_the_task_incomplete(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, WRONG_MUL, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("still wrong"),
            _stop("still wrong"),
            _stop("still wrong"),
        ]
    )
    loop, workspace, _ = _loop(root, model)
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant()[0], plan)

    assert result.status != "completed"
    assert _item(result, "regression").status == "failed"
    assert result.verification is not None
    assert result.verification.status != "verified"
    assert result.checkpoint is not None
    assert _item(result, "regression").item_id in result.checkpoint.unmet_items


# -- feature ------------------------------------------------------------


def test_feature_requires_new_behavior_and_regression(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": MATHX, "test_mathx.py": TEST_MATHX})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("mathx.py", MATHX, MATHX_FULL, tree=tree),
            _edit("test_mathx.py", TEST_MATHX, TEST_MATHX_ENABLED, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _run("-m", "unittest", "test_mathx.MathTest.test_mul"),
            _stop("mul is implemented"),
        ]
    )
    loop, _, _ = _loop(
        root,
        model,
        write_paths=("mathx.py", "test_mathx.py"),
    )
    task = _task("feature", requirements=(SUITE, NEW_TESTS))
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    # The second edit runs after the first one, so patch its expected tree lazily.
    def edit_test(request):  # type: ignore[no-untyped-def]
        return _edit(
            "test_mathx.py", TEST_MATHX, TEST_MATHX_ENABLED, tree=request.workspace_tree
        )(request)

    model.steps[1] = edit_test

    result = loop.run(task, _grant(write_paths=("mathx.py", "test_mathx.py"))[0], plan)

    assert result.status == "completed", (result.status, result.reason, result.feedback)
    assert len([item for item in result.checklist.items if item.kind == "behavior"]) == 2
    assert all(
        item.status == "passed"
        for item in result.checklist.items
        if item.kind == "behavior"
    )


def test_feature_without_the_new_behavior_is_not_complete(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": MATHX, "test_mathx.py": TEST_MATHX})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("mathx.py", MATHX, MATHX_COMMENT, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("looks good"),
            _stop("looks good"),
            _stop("looks good"),
        ]
    )
    loop, _, _ = _loop(root, model, write_paths=("mathx.py",))
    task = _task("feature", requirements=(SUITE, NEW_TESTS))
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)

    assert result.status != "completed"
    new_items = [
        item for item in result.checklist.items if item.requirement_id == "new-tests"
    ]
    assert any(item.status == "failed" for item in new_items)


def test_deleting_or_weakening_tests_cannot_complete_a_task(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    weakened = TEST_CALC.replace(
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        "    def test_add(self):\n        self.skipTest('removed')\n",
    )
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, FIXED, tree=tree),
            _edit("test_calc.py", TEST_CALC, weakened, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _stop("tests still pass"),
            _stop("tests still pass"),
            _stop("tests still pass"),
        ]
    )
    loop, _, _ = _loop(root, model, write_paths=("calc.py", "test_calc.py"))

    def edit_test(request):  # type: ignore[no-untyped-def]
        return _edit("test_calc.py", TEST_CALC, weakened, tree=request.workspace_tree)(request)

    model.steps[1] = edit_test
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(
        task, _grant(write_paths=("calc.py", "test_calc.py"))[0], plan
    )

    assert result.status != "completed"
    assert any(
        "tests_run" in item.detail or "tests removed" in item.detail
        for item in result.checklist.items
    )


TEST_BARE = (
    "import unittest\n"
    "\n"
    "from calc import add\n"
    "\n"
    "\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
TEST_BARE_WEAK = (
    "import unittest\n"
    "\n"
    "from calc import add\n"
    "\n"
    "\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        add(2, 3)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
TEST_HELPER = (
    "import unittest\n"
    "\n"
    "from calc import add\n"
    "\n"
    "\n"
    "def _check_equal(left, right):\n"
    "    assert left == right\n"
    "\n"
    "\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        _check_equal(add(2, 3), 5)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)


def test_deleting_a_bare_assert_cannot_complete(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_BARE})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, FIXED, tree=tree),
            _edit("test_calc.py", TEST_BARE, TEST_BARE_WEAK, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _stop("bare assertion removed"),
            _stop("bare assertion removed"),
            _stop("bare assertion removed"),
        ]
    )
    loop, _, _ = _loop(root, model, write_paths=("calc.py", "test_calc.py"))

    def edit_test(request):  # type: ignore[no-untyped-def]
        return _edit("test_calc.py", TEST_BARE, TEST_BARE_WEAK, tree=request.workspace_tree)(
            request
        )

    model.steps[1] = edit_test
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant(write_paths=("calc.py", "test_calc.py"))[0], plan)

    assert result.status != "completed"
    assert any("tests weakened" in item.detail for item in result.checklist.items)


def test_extracting_an_assertion_into_a_helper_is_not_weakening(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, FIXED, tree=tree),
            _edit("test_calc.py", TEST_CALC, TEST_HELPER, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _stop("assertion moved into a helper"),
        ]
    )
    loop, _, _ = _loop(root, model, write_paths=("calc.py", "test_calc.py"))

    def edit_test(request):  # type: ignore[no-untyped-def]
        return _edit("test_calc.py", TEST_CALC, TEST_HELPER, tree=request.workspace_tree)(
            request
        )

    model.steps[1] = edit_test
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant(write_paths=("calc.py", "test_calc.py"))[0], plan)

    assert result.status == "completed", (result.status, result.reason)
    assert not any("tests weakened" in item.detail for item in result.checklist.items)


# -- refactor -----------------------------------------------------------


def test_refactor_keeps_behavior_and_public_signatures(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": TOTAL_LOOP, "test_total.py": TEST_TOTAL})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("mathx.py", TOTAL_LOOP, TOTAL_BUILTIN, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("refactored"),
        ]
    )
    loop, _, _ = _loop(root, model, write_paths=("mathx.py",))
    task = _task("refactor")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)

    assert result.status == "completed", (result.status, result.reason)
    assert _item(result, "signature").status == "passed"


def test_refactor_rejects_a_changed_public_signature(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": TOTAL_LOOP, "test_total.py": TEST_TOTAL})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("mathx.py", TOTAL_LOOP, TOTAL_SIGNATURE, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("refactored"),
            _stop("refactored"),
            _stop("refactored"),
        ]
    )
    loop, _, _ = _loop(root, model, write_paths=("mathx.py",))
    task = _task("refactor")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)

    assert result.status != "completed"
    assert _item(result, "signature").status == "failed"
    assert "public signatures changed" in _item(result, "signature").detail


# -- run_app ------------------------------------------------------------


def _app_loop(root: Path, model: ScriptedModel, application: FakeApplication):
    return _loop(root, model, write_paths=("mathx.py",), application=application)


def test_run_app_requires_readiness_and_cleanup(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": TOTAL_LOOP, "test_total.py": TEST_TOTAL})
    application = FakeApplication()
    model = ScriptedModel(
        [_run("-m", "unittest", "-q"), _stop("app is up")]
    )
    loop, _, _ = _app_loop(root, model, application)
    task = _task("run_app")
    plan = loop.plan(
        task,
        baseline_command=_python_argv("-m", "unittest", "-q"),
        service_plan=ServicePlan(argv=("python3", "-m", "http.server"), port=8000),
    )

    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)

    assert result.status == "completed", (result.status, result.reason)
    assert _item(result, "readiness").status == "passed"
    assert _item(result, "cleanup").status == "passed"
    assert application.starts == 1
    assert application.stops == 1


def test_run_app_without_readiness_is_not_complete(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": TOTAL_LOOP, "test_total.py": TEST_TOTAL})
    application = FakeApplication(ready=False)
    model = ScriptedModel(
        [
            _run("-m", "unittest", "-q"),
            _stop("started"),
            _stop("started"),
            _stop("started"),
        ]
    )
    loop, _, _ = _app_loop(root, model, application)
    task = _task("run_app")
    plan = loop.plan(
        task,
        baseline_command=_python_argv("-m", "unittest", "-q"),
        service_plan=ServicePlan(argv=("python3", "-m", "http.server"), port=8000),
    )

    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)

    assert result.status != "completed"
    assert _item(result, "readiness").status == "failed"
    assert result.checkpoint is not None
    assert "readiness" in result.checkpoint.unmet_items


def test_run_app_without_cleanup_is_not_complete(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"mathx.py": TOTAL_LOOP, "test_total.py": TEST_TOTAL})
    application = FakeApplication(stopped=False)
    model = ScriptedModel(
        [
            _run("-m", "unittest", "-q"),
            _stop("started"),
            _stop("started"),
            _stop("started"),
        ]
    )
    loop, _, _ = _app_loop(root, model, application)
    task = _task("run_app")
    plan = loop.plan(
        task,
        baseline_command=_python_argv("-m", "unittest", "-q"),
        service_plan=ServicePlan(argv=("python3", "-m", "http.server"), port=8000),
    )

    result = loop.run(task, _grant(write_paths=("mathx.py",))[0], plan)

    assert result.status != "completed"
    assert _item(result, "cleanup").status == "failed"


# -- inspect ------------------------------------------------------------


def test_inspect_is_read_only_and_requires_citations(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel(
        [_read("calc.py"), _stop("calc.py line 2 subtracts instead of adding")]
    )
    loop, _, _ = _loop(
        root,
        model,
        write_paths=(),
        mode="investigate",
        execute=False,
    )
    task = _task("inspect", requirements=(SUITE,))
    plan = loop.plan(task)

    result = loop.run(task, _grant(write_paths=(), mode="investigate", execute=False)[0], plan)

    assert result.status == "completed", (result.status, result.reason)
    assert _item(result, "evidence").status == "passed"
    assert result.diff.changes == ()


def test_inspect_denies_writes_before_any_change(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel([_edit("calc.py", BUGGY, FIXED, tree=tree)])
    loop, workspace, _ = _loop(
        root,
        model,
        write_paths=(),
        mode="investigate",
        execute=False,
    )
    task = _task("inspect", requirements=(SUITE,))
    plan = loop.plan(task)

    result = loop.run(
        task,
        _grant(write_paths=(), mode="investigate", execute=False)[0],
        plan,
    )

    assert result.status == "denied"
    assert result.reason == "HARNESS_WRITE_DENIED"
    assert (root / "calc.py").read_text(encoding="utf-8") == BUGGY
    assert workspace.snapshot().tree_hash == tree


def test_inspect_denies_command_execution(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel([_run("-m", "unittest", "-q")])
    loop, _, _ = _loop(
        root,
        model,
        write_paths=(),
        mode="investigate",
        execute=False,
    )
    task = _task("inspect", requirements=(SUITE,))
    plan = loop.plan(task)

    result = loop.run(
        task,
        _grant(write_paths=(), mode="investigate", execute=False)[0],
        plan,
    )

    assert result.status == "denied"
    assert result.reason == "HARNESS_COMMAND_DENIED"


# -- progress policy ----------------------------------------------------


def test_repeated_failure_without_new_evidence_blocks_with_a_checkpoint(
    tmp_path: Path,
) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, WRONG_MUL, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("wrong"),
            _stop("wrong"),
            _stop("wrong"),
        ]
    )
    loop, _, _ = _loop(root, model)
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant()[0], plan)

    assert result.status == "blocked"
    assert result.reason == "no_progress"
    assert result.checkpoint is not None
    assert result.checkpoint.unmet_items
    assert any(feedback.kind == "no_progress" for feedback in result.feedback)
    assert result.model_calls < 8


def test_patch_round_trip_is_detected(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, WRONG_MUL, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("wrong"),
            _edit("calc.py", WRONG_MUL, BUGGY, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _stop("back"),
            _edit("calc.py", BUGGY, WRONG_MUL, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _stop("wrong again"),
        ]
    )
    loop, _, _ = _loop(root, model)
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    def edit(index: int, old: str, new: str):  # type: ignore[no-untyped-def]
        def step(request):  # type: ignore[no-untyped-def]
            return _edit("calc.py", old, new, tree=request.workspace_tree)(request)

        model.steps[index] = step

    edit(3, WRONG_MUL, BUGGY)
    edit(6, BUGGY, WRONG_MUL)

    result = loop.run(task, _grant()[0], plan)

    assert result.status == "blocked"
    assert any(feedback.kind == "patch_round_trip" for feedback in result.feedback)


def test_empty_search_is_reported_and_blocks(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    model = ScriptedModel(
        [
            _tool("search_files", {"query": "answer"}),
            _stop("nothing"),
            _tool("search_files", {"query": "answer"}),
            _stop("nothing"),
            _tool("search_files", {"query": "answer"}),
            _stop("nothing"),
        ]
    )
    loop, _, _ = _loop(
        root,
        model,
        write_paths=(),
        mode="investigate",
        execute=False,
        registry=EmptySearchRegistry,
    )
    task = _task("inspect", requirements=(SUITE,))
    plan = loop.plan(task)

    result = loop.run(
        task,
        _grant(write_paths=(), mode="investigate", execute=False)[0],
        plan,
    )

    assert result.status == "blocked", (
        result.status,
        result.reason,
        [(f.attempt, f.kind, f.message[:120]) for f in result.feedback],
        [(a.attempt, a.reason, a.progress) for a in result.attempts],
        result.model_calls,
    )
    assert any(feedback.kind == "empty_search" for feedback in result.feedback)
    assert any("repeated" in feedback.message for feedback in result.feedback)


def test_progress_resets_the_no_progress_guard(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, WRONG_MUL, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("wrong"),
            _stop("wrong"),
            _edit("calc.py", BUGGY, FIXED, tree="AFTER"),
            _run("-m", "unittest", "-q"),
            _stop("fixed"),
        ]
    )
    loop, _, _ = _loop(root, model)
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    def edit(index: int, old: str, new: str):  # type: ignore[no-untyped-def]
        def step(request):  # type: ignore[no-untyped-def]
            return _edit("calc.py", old, new, tree=request.workspace_tree)(request)

        model.steps[index] = step

    edit(4, WRONG_MUL, FIXED)

    result = loop.run(task, _grant()[0], plan)

    assert result.status == "completed", (result.status, result.reason, result.feedback)
    assert any(feedback.kind == "no_progress" for feedback in result.feedback)
    assert any(feedback.kind == "progress" for feedback in result.feedback)


def test_legacy_cap_stops_while_resource_policy_can_continue(tmp_path: Path) -> None:
    steps = [
        None,  # edit to a wrong implementation
        _run("-m", "unittest", "-q"),
        _stop("wrong"),
        _run("-m", "unittest", "-v"),
        _stop("wrong"),
        _run("-m", "unittest"),
        _stop("wrong"),
        _run("-m", "unittest", "-v"),
        _stop("wrong"),
        None,  # edit to the real fix
        _run("-m", "unittest", "-q"),
        _stop("fixed"),
    ]

    def build() -> ScriptedModel:
        model = ScriptedModel(list(steps))

        def edit_wrong(request):  # type: ignore[no-untyped-def]
            return _edit("calc.py", BUGGY, WRONG_MUL, tree=request.workspace_tree)(request)

        def edit_fixed(request):  # type: ignore[no-untyped-def]
            return _edit("calc.py", WRONG_MUL, FIXED, tree=request.workspace_tree)(request)

        model.steps[0] = edit_wrong
        model.steps[9] = edit_fixed
        return model

    legacy_root = _seed(tmp_path / "legacy", {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    legacy_model = build()
    legacy_loop, _, _ = _loop(legacy_root, legacy_model)
    task = _task("bugfix")
    legacy_plan = legacy_loop.plan(
        task,
        baseline_command=_python_argv("-m", "unittest", "-q"),
        attempt_policy="legacy",
    )
    legacy = legacy_loop.run(task, _grant()[0], legacy_plan)

    assert legacy.status == "incomplete"
    assert legacy.reason == "repair_limit"
    assert legacy.model_calls == 9

    resource_root = _seed(
        tmp_path / "resource", {"calc.py": BUGGY, "test_calc.py": TEST_CALC}
    )
    resource_model = build()
    resource_loop, _, _ = _loop(resource_root, resource_model)
    resource_plan = resource_loop.plan(
        task,
        baseline_command=_python_argv("-m", "unittest", "-q"),
        attempt_policy="resource",
    )
    resource = resource_loop.run(task, _grant()[0], resource_plan)

    assert resource.status == "completed", (resource.status, resource.reason)
    assert resource.model_calls == 12


# -- budget, unknown and denied calls -----------------------------------


def test_budget_exhaustion_never_completes(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    tree = LocalWorkspace(root, workspace_id="ws-1").snapshot().tree_hash
    model = ScriptedModel(
        [
            _edit("calc.py", BUGGY, FIXED, tree=tree),
            _run("-m", "unittest", "-q"),
            _stop("fixed"),
        ]
    )
    loop, _, _ = _loop(
        root,
        model,
        limits=BudgetLimits(
            max_model_calls=2,
            max_tool_calls=8,
            max_cost_micros=100_000,
            max_seconds=300,
        ),
    )
    task = _task("bugfix")
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))

    result = loop.run(task, _grant()[0], plan)

    assert result.status == "budget_exhausted"
    assert result.checkpoint is not None
    assert result.verification is None


def test_unknown_model_usage_never_completes(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})

    def unknown(request):  # type: ignore[no-untyped-def]
        return ModelResponse(
            text="",
            finish_reason="stop",
            usage=ModelUsage(known=False),
        )

    loop, _, _ = _loop(root, ScriptedModel([unknown]))
    task = _task("bugfix")
    plan = loop.plan(task)

    result = loop.run(task, _grant()[0], plan)

    assert result.status == "unknown"
    assert result.checkpoint is not None


def test_unknown_tool_and_out_of_grant_command_are_denied(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    loop, _, _ = _loop(root, ScriptedModel([_tool("explode", {})]))
    task = _task("bugfix")
    plan = loop.plan(task)

    result = loop.run(task, _grant()[0], plan)
    assert result.status == "denied"
    assert result.reason == "HARNESS_TOOL_UNDECLARED"

    root2 = _seed(tmp_path / "other", {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    loop2, _, _ = _loop(
        root2,
        ScriptedModel([_tool("run_tests", {"argv": ["/bin/sh", "-c", "echo owned"]})]),
    )
    plan2 = loop2.plan(task)
    result2 = loop2.run(task, _grant()[0], plan2)
    assert result2.status == "denied"
    assert result2.reason == "HARNESS_COMMAND_DENIED"


def test_stale_plan_is_rejected(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    loop, workspace, _ = _loop(root, ScriptedModel([]))
    task = _task("bugfix")
    plan = loop.plan(task)
    (root / "calc.py").write_text(FIXED, encoding="utf-8")

    with pytest.raises(DomainError) as stale:
        loop.run(task, _grant()[0], plan)
    assert stale.value.code == "HARNESS_PLAN_STALE"
    assert workspace.snapshot().tree_hash != plan.checkpoint_tree


def test_cancellation_never_completes(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC})
    token = CancellationToken()

    def cancel(request):  # type: ignore[no-untyped-def]
        token.cancel()
        return ModelResponse(text="stop", finish_reason="stop", usage=ModelUsage(cost_micros=1))

    loop, _, _ = _loop(root, ScriptedModel([cancel]))
    task = _task("bugfix")
    plan = loop.plan(task)

    result = loop.run(task, _grant()[0], plan, cancellation=token)

    assert result.status == "cancelled"
