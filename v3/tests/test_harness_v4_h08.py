"""H08 acceptance tests: incremental replacement edits on the model write path.

The V35-E02 evidence showed the model-facing ``apply_edits`` tool only accepted a
whole-file ``content`` payload, so a small change to a large file still had to be
emitted in full and hit the output-token ceiling (PATCH_TRUNCATED).  These tests
pin the two-protocol tool: the historical full-file form keeps working, and a
hash-pinned ``replacements`` form carries only the changed snippets with the H05
``PATCH_*`` semantics.  The output limit used by the large-file tests is a
synthetic model constraint, not a live provider call.
"""

from __future__ import annotations

import json
import sys
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
from mgar_v3.harness.coding_loop import CodingLoop
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    HarnessToolResult,
    ModelResponse,
    ModelUsage,
    TaskBinding,
    TextReplacement,
    ToolContext,
    VerificationRequirement,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.service import CodingTaskRequest, CodingWorkspace, HarnessCodingService
from mgar_v3.harness.workspace import (
    TaskWorkspace,
    WorkspaceLimits,
    WorkspaceRegistry,
    WorkspaceToolRegistry,
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

# A large module whose single bug sits at the end; echoing it costs ~100 KB.
_FILLER = "".join(
    f"def f{index}(value):\n    return value + {index}\n\n\n" for index in range(4000)
)
BUGGY_TAIL = "def target(x):\n    return x - 1\n"
FIXED_TAIL = "def target(x):\n    return x + 1\n"
BIG = _FILLER + BUGGY_TAIL
BIG_FIXED = _FILLER + FIXED_TAIL
TEST_TARGET = (
    "import unittest\n"
    "\n"
    "from big import target\n"
    "\n"
    "\n"
    "class TargetTest(unittest.TestCase):\n"
    "    def test_target(self):\n"
    "        self.assertEqual(target(2), 3)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
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
BUGGY_CALC = "def add(a, b):\n    return a - b\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n"


def _seed(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "workspace"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _task(kind: str = "bugfix") -> TaskBinding:
    return TaskBinding(
        task_id=f"task-{uuid4().hex[:12]}",
        thread_id=uuid4(),
        run_id=uuid4(),
        actor="agent-1",
        repository="example/repo",
        base_commit="a" * 40,
        workspace_id="ws-1",
        acceptance_revision="acceptance-1",
        objective="Fix the target function",
        task_kind=kind,  # type: ignore[arg-type]
        requirements=(SUITE,),
        execution_epoch=2,
    )


def _grant(*, write_paths: tuple[str, ...]) -> tuple[CapabilityGrant, CapabilityAuthority]:
    commands = (PYTHON,)
    grant = CapabilityGrant(
        actor="agent-1",
        workspace_id="ws-1",
        repository="example/repo",
        read_paths=("",),
        write_paths=write_paths,
        execute=True,
        allowed_commands=commands,
        max_seconds=60,
    )
    lease = issue_lease(
        mode="modify_test",
        actor="agent-1",
        workspace_id="ws-1",
        repository="example/repo",
        execution_epoch=2,
        read_paths=("",),
        write_paths=write_paths,
        allowed_commands=commands,
        approved_by="owner",
        approval_ref="approval-1",
    )
    return grant, CapabilityAuthority(lease, budget_check=lambda: True)


def _python_argv(*args: str) -> tuple[str, ...]:
    return (sys.executable, "-B", *args)


def _tool(name: str, arguments: dict[str, object]):
    def step(request):  # type: ignore[no-untyped-def]
        return ModelResponse(
            text="",
            finish_reason="tool_calls",
            tool_calls=(
                HarnessToolCall(
                    call_id=f"call-{uuid4().hex[:12]}", name=name, arguments=arguments
                ),
            ),
            usage=ModelUsage(cost_micros=5),
        )

    return step


def _stop(text: str = "done"):
    def step(request):  # type: ignore[no-untyped-def]
        return ModelResponse(text=text, finish_reason="stop", usage=ModelUsage(cost_micros=5))

    return step


def _run(*args: str):
    return _tool("run_tests", {"argv": list(_python_argv(*args))})


def _full_edit(path: str, before: str, after: str, *, tree: str):
    return _tool(
        "apply_edits",
        {
            "edits": [
                {"path": path, "expected_sha256": sha256_text(before), "content": after}
            ],
            "expected_tree": tree,
        },
    )


def _replace_edit(
    path: str,
    before: str,
    old: str,
    new: str,
    *,
    tree: str,
    occurrence: int | None = None,
):
    replacement: dict[str, object] = {
        "old_sha256": sha256_text(old),
        "old_text": old,
        "new_text": new,
    }
    if occurrence is not None:
        replacement["occurrence"] = occurrence
    return _tool(
        "apply_edits",
        {
            "edits": [
                {
                    "path": path,
                    "expected_sha256": sha256_text(before),
                    "replacements": [replacement],
                }
            ],
            "expected_tree": tree,
        },
    )


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


class ConstrainedModel(ScriptedModel):
    """A synthetic output ceiling: oversized tool arguments become ``length``."""

    def __init__(self, steps, *, limit: int) -> None:  # type: ignore[no-untyped-def]
        super().__init__(steps)
        self.limit = limit

    def complete(self, request, cancellation=None) -> ModelResponse:  # type: ignore[no-untyped-def]
        response = super().complete(request, cancellation)
        emitted = len(response.text.encode()) + sum(
            len(json.dumps(call.arguments, ensure_ascii=False).encode())
            for call in response.tool_calls
        )
        if emitted > self.limit:
            return ModelResponse(
                text="",
                finish_reason="length",
                usage=ModelUsage(cost_micros=5),
            )
        return response


class RecordingRegistry(HarnessToolRegistry):
    """Captures tool results so protocol/byte accounting is observable."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.results = []

    def invoke(self, call, context, cancellation):  # type: ignore[no-untyped-def]
        result = super().invoke(call, context, cancellation)
        self.results.append(result)
        return result


def _loop(
    root: Path,
    model,
    *,
    write_paths: tuple[str, ...],
) -> tuple[CodingLoop, LocalWorkspace, InMemoryEventSink, RecordingRegistry]:
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    broker = LocalExecutionBroker(workspace)
    grant, authority = _grant(write_paths=write_paths)
    registry = RecordingRegistry(workspace, broker, authority)
    loop = CodingLoop(
        model=model,
        tools=registry,
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
        reservation_micros=100,
    )
    return loop, workspace, loop.events, registry  # type: ignore[return-value]


def _context(workspace: LocalWorkspace, grant: CapabilityGrant, task: TaskBinding) -> ToolContext:
    return ToolContext(
        task=task,
        grant=grant,
        operation_id="op-1",
        workspace_tree=workspace.snapshot().tree_hash,
    )


def _call(arguments: dict[str, object]) -> HarnessToolCall:
    return HarnessToolCall(
        call_id=f"call-{uuid4().hex[:12]}", name="apply_edits", arguments=arguments
    )


# -- provider and error semantics ---------------------------------------


def test_incremental_edit_only_transmits_the_changed_snippet(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"big.py": BIG})
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    broker = LocalExecutionBroker(workspace)
    grant, authority = _grant(write_paths=("big.py",))
    registry = HarnessToolRegistry(workspace, broker, authority)
    task = _task()

    result = registry.invoke(
        _call(
            {
                "edits": [
                    {
                        "path": "big.py",
                        "expected_sha256": sha256_text(BIG),
                        "replacements": [
                            {
                                "old_sha256": sha256_text(BUGGY_TAIL),
                                "old_text": BUGGY_TAIL,
                                "new_text": FIXED_TAIL,
                            }
                        ],
                    }
                ],
                "expected_tree": workspace.snapshot().tree_hash,
            }
        ),
        _context(workspace, grant, task),
        CancellationToken(),
    )

    assert result.status == "ok", (result.code, result.content)
    assert result.data["protocol"] == "incremental"
    assert result.data["output_bytes"] < 200
    assert len(BIG.encode()) > 100_000
    assert workspace.read("big.py") == BIG_FIXED
    assert [change["path"] for change in result.data["changes"]] == ["big.py"]


def test_full_file_content_remains_supported(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"small.py": BUGGY_CALC})
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    broker = LocalExecutionBroker(workspace)
    grant, authority = _grant(write_paths=("small.py",))
    registry = HarnessToolRegistry(workspace, broker, authority)
    task = _task()

    result = registry.invoke(
        _call(
            {
                "edits": [
                    {
                        "path": "small.py",
                        "expected_sha256": sha256_text(BUGGY_CALC),
                        "content": FIXED_CALC,
                    }
                ],
                "expected_tree": workspace.snapshot().tree_hash,
            }
        ),
        _context(workspace, grant, task),
        CancellationToken(),
    )

    assert result.status == "ok", (result.code, result.content)
    assert result.data["protocol"] == "full"
    assert result.data["output_bytes"] == len(FIXED_CALC.encode())
    assert workspace.read("small.py") == FIXED_CALC


def test_replacement_requires_exactly_one_payload(tmp_path: Path) -> None:
    _seed(tmp_path, {"calc.py": BUGGY_CALC})
    payload = {
        "path": "calc.py",
        "expected_sha256": sha256_text(BUGGY_CALC),
        "content": FIXED_CALC,
        "replacements": [
            {
                "old_sha256": sha256_text("a - b"),
                "old_text": "a - b",
                "new_text": "a + b",
            }
        ],
    }
    with pytest.raises(ValueError):
        WorkspaceEdit.model_validate(payload)
    with pytest.raises(ValueError):
        WorkspaceEdit.model_validate(
            {"path": "calc.py", "expected_sha256": sha256_text(BUGGY_CALC)}
        )


def _invoke_replace(
    workspace: LocalWorkspace,
    edit: dict[str, object],
) -> HarnessToolResult:
    broker = LocalExecutionBroker(workspace)
    grant, authority = _grant(write_paths=("calc.py",))
    registry = HarnessToolRegistry(workspace, broker, authority)
    return registry.invoke(
        _call({"edits": [edit], "expected_tree": workspace.snapshot().tree_hash}),
        _context(workspace, grant, _task()),
        CancellationToken(),
    )


def test_incremental_stale_preimage_uses_patch_stale(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY_CALC})
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    result = _invoke_replace(
        workspace,
        {
            "path": "calc.py",
            "expected_sha256": "0" * 64,
            "replacements": [
                {
                    "old_sha256": sha256_text("a - b"),
                    "old_text": "a - b",
                    "new_text": "a + b",
                }
            ],
        },
    )
    assert result.status == "error"  # type: ignore[attr-defined]
    assert result.code == "PATCH_STALE"  # type: ignore[attr-defined]


def test_incremental_fragment_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY_CALC})
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    result = _invoke_replace(
        workspace,
        {
            "path": "calc.py",
            "expected_sha256": sha256_text(BUGGY_CALC),
            "replacements": [
                {
                    "old_sha256": "0" * 64,
                    "old_text": "a - b",
                    "new_text": "a + b",
                }
            ],
        },
    )
    assert result.status == "error"  # type: ignore[attr-defined]
    assert result.code == "PATCH_HASH_MISMATCH"  # type: ignore[attr-defined]


def test_incremental_ambiguous_context_is_rejected(tmp_path: Path) -> None:
    body = "x = 1\nx = 1\n"
    root = _seed(tmp_path, {"calc.py": body})
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    result = _invoke_replace(
        workspace,
        {
            "path": "calc.py",
            "expected_sha256": sha256_text(body),
            "replacements": [
                {
                    "old_sha256": sha256_text("x = 1\n"),
                    "old_text": "x = 1\n",
                    "new_text": "x = 2\n",
                }
            ],
        },
    )
    assert result.status == "error"  # type: ignore[attr-defined]
    assert result.code == "PATCH_AMBIGUOUS"  # type: ignore[attr-defined]


def test_occurrence_selects_the_requested_match(tmp_path: Path) -> None:
    body = "x = 1\nx = 1\n"
    root = _seed(tmp_path, {"dup.py": body})
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    snapshot = workspace.apply(
        [
            WorkspaceEdit(
                path="dup.py",
                expected_sha256=sha256_text(body),
                replacements=[
                    {
                        "old_sha256": sha256_text("x = 1\n"),
                        "old_text": "x = 1\n",
                        "new_text": "x = 2\n",
                        "occurrence": 2,
                    }
                ],
            )
        ],
        expected_tree=workspace.snapshot().tree_hash,
    )
    assert snapshot.files["dup.py"] == "x = 1\nx = 2\n"


def test_replacement_count_and_emitted_bytes_hit_patch_limit(tmp_path: Path) -> None:
    body = "x = 1\nx = 1\nx = 1\n"
    root = _seed(tmp_path, {"dup.py": body})
    workspace = LocalWorkspace(root, workspace_id="ws-1", max_output_bytes=8, max_replacements=1)
    replacement = {
        "old_sha256": sha256_text("x = 1\n"),
        "old_text": "x = 1\n",
        "new_text": "x = 2\n",
    }
    with pytest.raises(DomainError) as too_many:
        workspace.apply(
            [
                WorkspaceEdit(
                    path="dup.py",
                    expected_sha256=sha256_text(body),
                    replacements=[replacement, dict(replacement, occurrence=2)],
                )
            ],
            expected_tree=workspace.snapshot().tree_hash,
        )
    assert too_many.value.code == "PATCH_LIMIT"

    with pytest.raises(DomainError) as too_big:
        workspace.apply(
            [
                WorkspaceEdit(
                    path="dup.py",
                    expected_sha256=sha256_text(body),
                    replacements=[
                        {
                            "old_sha256": sha256_text("x = 1\n"),
                            "old_text": "x = 1\n",
                            "new_text": "x = 123456789\n",
                        }
                    ],
                )
            ],
            expected_tree=workspace.snapshot().tree_hash,
        )
    assert too_big.value.code == "PATCH_LIMIT"


def test_task_workspace_accepts_incremental_edits(tmp_path: Path) -> None:
    source = _seed(tmp_path, {"big.py": BIG})
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace: TaskWorkspace = registry.create_task_workspace(registration, "task-incremental")
    tools = WorkspaceToolRegistry(workspace)
    grant, _authority = _grant(write_paths=("big.py",))
    task = _task()
    tree = workspace.tree_hash()

    result = tools.invoke(
        _call(
            {
                "edits": [
                    {
                        "path": "big.py",
                        "expected_sha256": sha256_text(BIG),
                        "replacements": [
                            {
                                "old_sha256": sha256_text(BUGGY_TAIL),
                                "old_text": BUGGY_TAIL,
                                "new_text": FIXED_TAIL,
                            }
                        ],
                    }
                ],
                "expected_tree": tree,
            }
        ),
        ToolContext(task=task, grant=grant, operation_id="op-1", workspace_tree=tree),
        __import__("mgar_v3.harness.contracts", fromlist=["CancellationToken"]).CancellationToken(),
    )

    assert result.status == "ok", (result.code, result.content)
    assert result.data["protocol"] == "incremental"
    assert result.data["output_bytes"] < 200
    assert workspace.read("big.py") == BIG_FIXED
    assert workspace.limits.max_output_bytes == WorkspaceLimits().max_output_bytes


def test_structured_diff_reports_baseline_digests(tmp_path: Path) -> None:
    """``before_sha256`` must be the recorded baseline digest, never a hash of it."""

    source = _seed(
        tmp_path,
        {"calc.py": BUGGY_CALC, "keep.py": "keep = 1\n", "same.py": "same = 2\n"},
    )
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace = CodingWorkspace(registry.create_task_workspace(registration, "task-diff"))
    tree_before = workspace.snapshot().tree_hash

    workspace.apply(
        [
            WorkspaceEdit(
                path="calc.py",
                expected_sha256=sha256_text(BUGGY_CALC),
                replacements=[
                    TextReplacement(
                        old_text="    return a - b\n",
                        new_text="    return a + b\n",
                    )
                ],
            )
        ],
        expected_tree=tree_before,
    )
    # Artifacts of a command run and an external deletion both reach the diff.
    task_root = workspace.inner.root
    (task_root / "new.py").write_text("value = 1\n", encoding="utf-8")
    (task_root / "keep.py").unlink()

    changes = {change.path: change for change in workspace.diff(tree_before).changes}

    # Unchanged files stay out of the change list.
    assert "same.py" not in changes

    modified = changes["calc.py"]
    assert modified.change == "modified"
    assert modified.before_sha256 == sha256_text(BUGGY_CALC)
    # Regression guard: the old implementation reported sha256(baseline digest).
    assert modified.before_sha256 != sha256_text(sha256_text(BUGGY_CALC))
    assert modified.after_sha256 == sha256_text(FIXED_CALC)

    added = changes["new.py"]
    assert added.change == "added"
    assert added.before_sha256 is None
    assert added.after_sha256 == sha256_text("value = 1\n")

    deleted = changes["keep.py"]
    assert deleted.change == "modified"
    assert deleted.before_sha256 == sha256_text("keep = 1\n")
    assert deleted.before_sha256 != sha256_text(sha256_text("keep = 1\n"))
    assert deleted.after_sha256 == sha256_text("")

    patch = workspace.diff(tree_before).patch
    assert "-keep = 1" in patch and "+value = 1" in patch


# -- end-to-end: a constrained model can edit a large file -----------------


def test_large_file_full_rewrite_hits_the_model_output_limit(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"big.py": BIG, "test_target.py": TEST_TARGET})
    model = ConstrainedModel([], limit=2048)
    loop, workspace, _, _ = _loop(root, model, write_paths=("big.py",))
    task = _task()
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q", "test_target"))
    tree = workspace.snapshot().tree_hash
    model.steps = [
        _full_edit("big.py", BIG, BIG_FIXED, tree=tree),
        _run("-m", "unittest", "-q", "test_target"),
        _stop("fixed"),
    ]

    result = loop.run(task, _grant(write_paths=("big.py",))[0], plan)

    assert result.status == "incomplete"
    assert result.reason == "model_length"


def test_large_file_small_change_completes_under_the_same_limit(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"big.py": BIG, "test_target.py": TEST_TARGET})
    model = ConstrainedModel([], limit=2048)
    loop, workspace, _, registry = _loop(root, model, write_paths=("big.py",))
    task = _task()
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q", "test_target"))
    tree = workspace.snapshot().tree_hash
    model.steps = [
        _replace_edit("big.py", BIG, BUGGY_TAIL, FIXED_TAIL, tree=tree),
        _run("-m", "unittest", "-q", "test_target"),
        _stop("fixed"),
    ]

    result = loop.run(task, _grant(write_paths=("big.py",))[0], plan)

    assert result.status == "completed", (
        result.reason,
        [(item.item_id, item.status) for item in result.checklist.items],
        [(feedback.kind, feedback.message[:120]) for feedback in result.feedback],
    )
    assert result.verification is not None
    assert result.verification.status == "verified"
    assert result.diff.tree_after == result.tree_after
    applied = [item for item in registry.results if item.data.get("protocol") == "incremental"]
    assert len(applied) == 1
    assert applied[0].data["output_bytes"] < 200
    assert workspace.read("big.py") == BIG_FIXED
    assert len(BIG.encode()) > 100_000


def test_incremental_edit_cannot_weaken_tests(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"calc.py": BUGGY_CALC, "test_calc.py": TEST_CALC})
    model = ScriptedModel([])
    loop, workspace, _, _ = _loop(root, model, write_paths=("calc.py", "test_calc.py"))
    task = _task()
    plan = loop.plan(task, baseline_command=_python_argv("-m", "unittest", "-q"))
    tree = workspace.snapshot().tree_hash
    assertion = "        self.assertEqual(add(2, 3), 5)\n"
    model.steps = [
        _replace_edit("test_calc.py", TEST_CALC, assertion, "        add(2, 3)\n", tree=tree),
        _full_edit("calc.py", BUGGY_CALC, FIXED_CALC, tree="AFTER"),
        _run("-m", "unittest", "-q"),
        _stop("tests still pass"),
        _stop("tests still pass"),
        _stop("tests still pass"),
    ]

    def fix_calc(request):  # type: ignore[no-untyped-def]
        return _full_edit("calc.py", BUGGY_CALC, FIXED_CALC, tree=request.workspace_tree)(request)

    model.steps[1] = fix_calc
    result = loop.run(task, _grant(write_paths=("calc.py", "test_calc.py"))[0], plan)

    assert result.status != "completed"
    assert any(
        "weakened" in item.detail.lower() for item in result.checklist.items
    ) or any("weakened" in feedback.message.lower() for feedback in result.feedback)


def _web_project(tmp_path: Path) -> Path:
    root = _seed(tmp_path, {"calc.py": BUGGY_CALC, "test_calc.py": TEST_CALC})
    return root


def test_web_service_runs_a_bugfix_in_the_h06_container(tmp_path: Path) -> None:
    source = _web_project(tmp_path)
    service = HarnessCodingService(
        tmp_path / "harness",
        actor="owner",
        image_lock=IMAGE_LOCK,
    )
    project = service.register_project(str(source))
    result = service.create_task(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective="Fix add() and prove it with the suite",
            task_kind="bugfix",
            test_command="python3 -m unittest -v",
            target_path="calc.py",
            old_text="    return a - b\n",
            new_text="    return a + b\n",
            approved=True,
        )
    )

    assert result["status"] == "completed"
    assert result["live_model"] is False
    assert result["model_calls"] >= 3
    assert result["output_bytes"] < 4096
    assert "-    return a - b" in result["diff"]
    assert "+    return a + b" in result["diff"]
    assert result["tests"]["failed"] == 0 and result["tests"]["run"] >= 1
    assert [command["status"] for command in result["commands"]] == ["failed", "passed"]
    assert Path(result["patch_path"]).read_text(encoding="utf-8") == result["diff"]
    assert service.get_task(result["task_id"])["summary"] == result["summary"]

    changes = {change["path"]: change for change in result["changes"]}
    assert set(changes) == {"calc.py"}
    assert changes["calc.py"]["change"] == "modified"
    # The baseline is a path -> sha256 map; the reported hash must be that
    # digest itself, never a hash of the digest string.
    assert changes["calc.py"]["before_sha256"] == sha256_text(BUGGY_CALC)
    assert changes["calc.py"]["before_sha256"] != sha256_text(sha256_text(BUGGY_CALC))
    fixed = Path(result["workspace_path"]) / "calc.py"
    assert changes["calc.py"]["after_sha256"] == sha256_text(fixed.read_text(encoding="utf-8"))


def test_web_service_inspect_is_read_only(tmp_path: Path) -> None:
    source = _web_project(tmp_path)
    service = HarnessCodingService(
        tmp_path / "harness",
        actor="owner",
        image_lock=IMAGE_LOCK,
    )
    project = service.register_project(str(source))
    result = service.create_task(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective="Inspect the calculator",
            task_kind="inspect",
            target_path="calc.py",
        )
    )

    assert result["status"] == "completed"
    assert result["changes"] == []
    assert result["commands"] == []
    assert result["tool_calls"] >= 1
    evidence = {item["item_id"]: item for item in result["checklist"]["items"]}["evidence"]
    assert evidence["status"] == "passed"
    assert "calc.py" in evidence["detail"]


def test_web_api_registers_and_runs_a_task(settings, engine, tmp_path: Path) -> None:
    source = _web_project(tmp_path)
    client = TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )
    with client:
        created = client.post(
            "/api/v3/harness/projects", json={"path": str(source)}
        )
        assert created.status_code == 201
        project_id = created.json()["project_id"]

        response = client.post(
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
            },
        )
        assert response.status_code == 201
        result = response.json()
        assert result["status"] == "completed"
        task_id = result["task_id"]

        assert client.get(f"/api/v3/harness/tasks/{task_id}").status_code == 200
        assert "+    return a + b" in client.get(
            f"/api/v3/harness/tasks/{task_id}/diff"
        ).text
        patch = client.get(f"/api/v3/harness/tasks/{task_id}/patch")
        assert patch.status_code == 200
        assert "attachment" in patch.headers["content-disposition"]
        artifacts = client.get(f"/api/v3/harness/tasks/{task_id}/artifacts").json()
        assert artifacts["live_model"] is False
        assert artifacts["changes"][0]["path"] == "calc.py"
        assert client.get("/api/v3/repository-analyses").status_code == 200


def test_web_api_requires_approval_for_writes(settings, engine, tmp_path: Path) -> None:
    source = _web_project(tmp_path)
    client = TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )
    with client:
        project_id = client.post(
            "/api/v3/harness/projects", json={"path": str(source)}
        ).json()["project_id"]
        response = client.post(
            "/api/v3/harness/tasks",
            json={
                "project_id": project_id,
                "objective": "Fix add()",
                "task_kind": "bugfix",
                "target_path": "calc.py",
                "old_text": "    return a - b\n",
                "new_text": "    return a + b\n",
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "HARNESS_APPROVAL_REQUIRED"


def test_web_api_rejects_live_mode_without_approval(settings, engine, tmp_path: Path) -> None:
    source = _web_project(tmp_path)
    # Pin the offline configuration so a developer .env with paid credentials
    # cannot turn this rejection check into a real provider request.
    offline = settings.model_copy(update={"allow_paid": False, "deepseek_api_key": None})
    client = TestClient(
        create_app(offline, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )
    with client:
        project_id = client.post(
            "/api/v3/harness/projects", json={"path": str(source)}
        ).json()["project_id"]
        response = client.post(
            "/api/v3/harness/tasks",
            json={
                "project_id": project_id,
                "objective": "Fix add()",
                "task_kind": "bugfix",
                "target_path": "calc.py",
                "old_text": "    return a - b\n",
                "new_text": "    return a + b\n",
                "approved": True,
                "mode": "live",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "HARNESS_LIVE_MODEL_UNAVAILABLE"


def test_web_api_rejects_malformed_task_ids(settings, engine) -> None:
    client = TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )
    malformed = [
        "%2e%2e",  # ``..`` after URL decoding
        "%2E%2E",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "h08",
        "h08-",
        "h08-" + "a" * 11,
        "h08-" + "a" * 13,
        "h08-" + "g" * 12,
        "h08-" + "A" * 12,
        "a" * 200,
        "%00",
    ]
    with client:
        for task_id in malformed:
            response = client.get(f"/api/v3/harness/tasks/{task_id}")
            assert response.status_code == 404, (task_id, response.status_code, response.text)
            assert "task_id" not in response.text
            patch = client.get(f"/api/v3/harness/tasks/{task_id}/patch")
            assert patch.status_code == 404
        # A syntactically valid but unknown id still resolves to "not found".
        assert client.get("/api/v3/harness/tasks/h08-000000000000").status_code == 404
