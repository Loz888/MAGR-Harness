"""R07 real PG, original tools/containers and free controlled model feedback."""

import json
import time
from uuid import uuid4

import pytest
from test_h12_budget_context import working_state
from test_h12_r06_baseline import LOCK
from test_harness_v4_h10 import ScriptedModel, _call, _stop
from test_harness_v4_h11_service import _InjectedCrash

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import EvaluationProfile, EvaluationResources, sha256_text
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.public_feedback import PREFIX, PublicFeedbackPolicy, PublicTestFeedback
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.service import CodingTaskRequest, CodingToolRegistry, HarnessCodingService
from mgar_v3.harness.tool_errors import ToolErrorPolicy
from mgar_v3.harness.workspace import _tree_hash
from mgar_v3.quality_eval.h12_regression_plan import regression_plan

BASE = "def add(a,b):return a-b\ndef old_name():return 7\n"
GOOD = "def add(a,b):return a+b\ndef old_name():return 7\n"
WRONG = "def add(a,b):return a*b\ndef old_name():return 7\n"
MISSING_IMPORT = "def add(a,b):return a+b\n"
TARGET = "from calc import add\ndef test_add():assert add(2,3)==5\n"
IMPORT = "from calc import old_name\ndef test_light_imports():assert old_name()==7\n"
STATIC = (
    "python3",
    "-B",
    "-c",
    "import ast,pathlib;ast.parse(pathlib.Path('calc.py').read_text())",
)
PROBE = """import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
state='passed'
try:
 from calc import add
 assert add(2,3)==5
except AssertionError:state='assertion_failed'
except Exception:state='error'
print('HARNESS_PUBLIC_PROBE_V1 '+json.dumps({'plan_id':'r07-add',
 'asset_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
 'cases':[{'id':'addition','status':state,'detail':'public add(2,3) must equal 5'}]}))
sys.exit(0 if state=='passed' else 1 if state=='assertion_failed' else 2)
"""


class ControlledModel(ScriptedModel):
    def complete(self, request, cancellation=None):
        if not self.steps:
            self.steps.append(_stop("calc.py still has unresolved public checks."))
        return super().complete(request, cancellation)


def profile(*, tools=40):
    return EvaluationProfile(
        experiment_id="r07-free-" + uuid4().hex[:12],
        task_id="r07-dev-0",
        split="dev",
        arm="candidate",
        task_kind="bugfix",
        objective="Fix add and preserve old_name in calc.py.",
        resources=EvaluationResources(
            max_model_calls=10,
            max_tool_calls=tools,
            max_cost_micros=1000000,
            deadline_seconds=180,
            max_context_tokens=32768,
            max_repair_rounds=2,
            max_output_tokens=2048,
        ),
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        write_paths=("calc.py",),
        public_test_argv=("python3", "-B", "-m", "pytest", "tests/test_target.py"),
        public_build_argv=STATIC,
        acceptance_protocol="contract-v1",
        acceptance_revision="r07-public-feedback-fixture-v1",
        acceptance_baseline_commit="0" * 40,
        public_requirements=("Return the sum and preserve old_name import behavior.",),
        independent_regression_plan=regression_plan([("python3", "-m", "pytest", "tests/")]),
        public_feedback_policy=PublicFeedbackPolicy(),
        tool_error_policy=ToolErrorPolicy(),
        verification_protocol="public-probe-v1",
        public_probe=PublicProbePlan(
            plan_id="r07-add", asset_sha256=sha256_text(PROBE), case_ids=("addition",)
        ),
    )


def setup(engine, settings, tmp_path, model, *, tools=40, existing=None, gate=None):
    bound = existing or profile(tools=tools)
    if existing is None:
        profiles = [
            bound.model_copy(update={"task_id": f"r07-{split}-{i}", "split": split, "arm": arm})
            for split in ("dev", "holdout")
            for i in range(12)
            for arm in ("candidate", "legacy-v35")
        ]
        engine.harness.create_experiment(
            bound.experiment_id,
            {
                "schema": "harness-h12-experiment-v1",
                "profiles": [p.model_dump(mode="json") for p in profiles],
            },
            {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12000000},
        )
    if existing is None:
        (tmp_path / "public-assets" / sha256_text(PROBE)).mkdir(parents=True)
        (tmp_path / "public-assets" / sha256_text(PROBE) / "probe.py").write_text(PROBE)
    service = HarnessCodingService(
        settings.harness_root,
        actor="owner",
        engine=engine,
        image_lock=LOCK,
        evaluation_profile=bound,
        allow_paid=True,
        live_api_key="controlled-unused",
        model_factory=lambda request: model,
        final_gate=gate,
        public_probe_root=tmp_path / "public-assets",
    )
    if existing is not None:
        return service, bound, None
    project = tmp_path / "project"
    project.mkdir()
    (project / "tests").mkdir()
    for name, data in [
        ("calc.py", BASE),
        ("tests/test_target.py", TARGET),
        ("tests/test_light_imports.py", IMPORT),
    ]:
        (project / name).write_text(data)
    registration = service.register_project(str(project))
    request = CodingTaskRequest(
        project_id=registration["project_id"],
        objective=bound.objective,
        task_kind="bugfix",
        mode="live",
        approved=True,
    )
    return service, bound, request


def edit(content, expected):
    def apply(request):
        return _call(
            "apply_edits",
            {
                "expected_tree": request.workspace_tree,
                "edits": [
                    {
                        "path": "calc.py",
                        "expected_sha256": sha256_text(expected),
                        "content": content,
                    }
                ],
            },
        )

    return apply


def observations(request):
    values = []
    for message in request.messages:
        for line in message.content.splitlines():
            if line.startswith(PREFIX):
                values.append(PublicTestFeedback.model_validate_json(line[len(PREFIX) :]))
    return values


@pytest.mark.parametrize("first", [WRONG, MISSING_IMPORT], ids=["target-red", "import-regression"])
def test_pg_each_patch_runs_static_target_and_full_public_checks_before_next_model(
    engine,
    settings,
    tmp_path,
    first,
):
    trees = []

    def repair(request):
        current = [o for o in observations(request) if o.tree_hash == request.workspace_tree]
        assert {o.command_id for o in current} == {"build", "tests", "public_probe", "regression_1"}
        by_id = {o.command_id: o for o in current}
        assert by_id["build"].outcome == "passed"
        assert by_id["tests"].outcome == ("failed" if first == WRONG else "passed")
        assert by_id["regression_1"].outcome in {"failed", "unverified"}
        assert by_id["regression_1"].exit_code in {1, 2}
        assert by_id["regression_1"].log_complete
        assert any(o.failure_ids or o.assertion_excerpt for o in current)
        pinned = working_state(request)["public_test_feedback"]
        assert len(pinned) == 4 and all(o["tree_hash"] == request.workspace_tree for o in pinned)
        trees.append(request.workspace_tree)
        return edit(GOOD, first)(request)

    def finish(request):
        assert request.workspace_tree != trees[0]
        latest = [o for o in observations(request) if o.tree_hash == request.workspace_tree]
        assert len(latest) == 4 and all(o.outcome == "passed" for o in latest)
        assert all(o["outcome"] == "passed" for o in working_state(request)["public_test_feedback"])
        return _stop("calc.py now adds correctly and preserves old_name; all public checks passed.")

    model = ControlledModel(
        [_call("read_file", {"path": "calc.py"}), edit(first, BASE), repair, finish]
    )
    service, bound, request = setup(engine, settings, tmp_path, model)
    result = service.create_task(request)
    assert result["candidate_status"] == "completed", (result["reason"], result["summary"])
    assert result["status"] == "blocked" and result["independent_gate"]["status"] == "NOT_RUN"
    # Public feedback never fabricates an independent Judge/real Agent pass.
    feedback = [r["public_feedback"] for r in result["receipts"] if r.get("public_feedback")]
    assert len(feedback) == 10 and len({r["tree_hash"] for r in feedback}) == 3
    assert result["model_calls"] == 4 and result["tool_calls"] == 13
    assert result["usage_known"] and result["reserved_micros"] == 0
    row = engine.harness.tasks()[0]
    assert row["sealed_tree_hash"] == result["tree_after"]
    assert (
        row["evaluation_profile"]["public_feedback_policy"]
        == bound.public_feedback_policy.model_dump()
    )


def test_pg_automatic_checks_cannot_bypass_original_tool_budget(engine, settings, tmp_path):
    model = ControlledModel([_call("read_file", {"path": "calc.py"}), edit(GOOD, BASE)])
    service, _, request = setup(engine, settings, tmp_path, model, tools=4)
    result = service.create_task(request)
    assert result["reason"] == "budget_tool_calls", result
    assert result["tool_calls"] == 4 and result["model_calls"] == 2
    assert all(r["tree_hash"] == result["tree_before"] for r in result["receipts"])
    assert result["status"] != "completed"


def test_pg_stable_repeated_failures_stop_within_frozen_stagnation_bound(
    engine, settings, tmp_path
):
    model = ControlledModel(
        [
            _call("read_file", {"path": "calc.py"}),
            edit(WRONG, BASE),
            *[_call("run_tests", {"command_id": "tests"}) for _ in range(6)],
        ]
    )
    service, _, request = setup(engine, settings, tmp_path, model)
    result = service.create_task(request)
    assert result["reason"] == "no_progress", result["reason"]
    assert result["model_calls"] == 5 and result["tool_calls"] == 11
    failures = [
        r["public_feedback"]
        for r in result["receipts"]
        if r.get("public_feedback", {}).get("command_id") == "tests"
        and r["tree_hash"] != result["tree_before"]
    ]
    assert len(failures) == 4 and len({r["failure_signature"] for r in failures}) == 1
    assert all(r["outcome"] == "failed" and r["failure_ids"] for r in failures)


@pytest.mark.parametrize("crash_phase", ["outer-known", "process-known"])
def test_pg_known_automatic_check_survives_engine_restart_without_redispatch(
    engine,
    settings,
    tmp_path,
    monkeypatch,
    crash_phase,
):
    original = CodingToolRegistry._invoke
    calls = []
    crashed = False

    def observe(self, call, context, token):
        nonlocal crashed
        calls.append(context.operation_id)
        result = original(self, call, context, token)
        if (
            crash_phase == "process-known"
            and not crashed
            and ":check:regression_1:" in context.operation_id
        ):
            crashed = True
            raise _InjectedCrash("known native process before outer tool settlement")
        return result

    monkeypatch.setattr(CodingToolRegistry, "_invoke", observe)
    guarded = CodingToolRegistry.invoke_guarded

    def crash(self, call, context, token, check, transform, interaction=None):
        nonlocal crashed
        result = guarded(self, call, context, token, check, transform, interaction)
        if (
            crash_phase == "outer-known"
            and not crashed
            and ":check:regression_1:" in context.operation_id
        ):
            crashed = True
            raise _InjectedCrash("known automatic check before loop observation")
        return result

    monkeypatch.setattr(CodingToolRegistry, "invoke_guarded", crash)
    model = ControlledModel(
        [
            _call("read_file", {"path": "calc.py"}),
            edit(GOOD, BASE),
            _stop("calc.py passes the public addition and import checks."),
        ]
    )
    service, bound, request = setup(engine, settings, tmp_path, model)
    with pytest.raises(_InjectedCrash):
        service.create_task(request)
    row = engine.harness.tasks()[0]
    saved = [
        op
        for op in engine.harness.operations(row["task_id"])
        if ":check:" in op["operation_id"] and op["kind"] == "tool"
    ]
    assert len(saved) == 4
    assert sum(op["state"] == "known" for op in saved) == (4 if crash_phase == "outer-known" else 3)
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        resumed, _, _ = setup(replacement, settings, tmp_path, model, existing=bound)
        result = resumed.recover_task(row["task_id"])
        assert result["candidate_status"] == "completed", result["reason"]
        assert result["model_calls"] == 3 and result["tool_calls"] == 8
        assert all(
            r["public_feedback"]["execution_epoch"] == r["provenance"]["execution_epoch"]
            for r in result["receipts"]
            if r.get("public_feedback")
        )
        assert all(calls.count(op["operation_id"]) == 1 for op in saved)
        assert result["usage_known"] and result["reserved_micros"] == 0
    finally:
        replacement.close()


def test_pg_private_final_judge_never_enters_worker_feedback(engine, settings, tmp_path):
    secret = "PRIVATE_JUDGE_R07_SENTINEL_" + uuid4().hex
    private_hash = engine.artifacts.put(secret.encode())
    model = ControlledModel(
        [
            _call("read_file", {"path": "calc.py"}),
            edit(GOOD, BASE),
            _stop("calc.py passes the configured public checks."),
        ]
    )

    def final(workspace, candidate):
        assert engine.artifacts.read(private_hash).decode() == secret
        return {"status": "FAIL", "tree_hash": candidate["tree_after"], "reason": secret}

    service, _, request = setup(engine, settings, tmp_path, model, gate=final)
    result = service.create_task(request)
    assert result["candidate_status"] == "completed" and result["status"] == "blocked"
    messages = json.dumps([r.model_dump(mode="json") for r in model.requests])
    assert secret not in messages and private_hash not in messages
    assert len(model.requests) == 3 and result["independent_gate"]["status"] == "FAIL"


def test_pg_multiple_edits_in_one_model_batch_each_run_checks_without_breaking_tool_protocol(
    engine,
    settings,
    tmp_path,
):
    changed = _tree_hash(
        {
            "calc.py": sha256_text(WRONG),
            "tests/test_target.py": sha256_text(TARGET),
            "tests/test_light_imports.py": sha256_text(IMPORT),
        }
    )

    def batch(request):
        first = edit(WRONG, BASE)(request)
        second = _call(
            "apply_edits",
            {
                "expected_tree": changed,
                "edits": [
                    {"path": "calc.py", "expected_sha256": sha256_text(WRONG), "content": GOOD}
                ],
            },
        )
        return first.model_copy(update={"tool_calls": first.tool_calls + second.tool_calls})

    def finish(request):
        frames = observations(request)
        assert len([f for f in frames if f.tree_hash == changed]) == 4
        assert len([f for f in frames if f.tree_hash == request.workspace_tree]) == 4
        assert all(f["outcome"] == "passed" for f in working_state(request)["public_test_feedback"])
        return _stop("calc.py and the complete public checks are ready for final verification.")

    model = ControlledModel([_call("read_file", {"path": "calc.py"}), batch, finish])
    service, _, request = setup(engine, settings, tmp_path, model)
    result = service.create_task(request)
    assert result["candidate_status"] == "completed", result["reason"]
    assert result["tool_calls"] == 13 and result["model_calls"] == 3


def test_pg_public_clarification_waits_then_continues_same_run_and_grant(
    engine, settings, tmp_path
):
    model = ControlledModel(
        [
            _call("read_file", {"path": "calc.py"}),
            _call("ask_user", {"question": "Please confirm the declared old_name behavior."}),
            edit(GOOD, BASE),
            _stop("calc.py passes the confirmed public behavior checks."),
        ]
    )
    service, _, request = setup(engine, settings, tmp_path, model)
    started = service.submit_task(request)
    task_id = started["task_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = service.get_task(task_id)
        if current["status"] == "waiting_input":
            break
        time.sleep(0.05)
    assert current["status"] == "waiting_input", current
    before = engine.harness.get(task_id)
    assert before["run_id"] and len(model.requests) == 2
    service.post_message(
        task_id,
        kind="answer",
        text="Preserve old_name exactly as declared.",
        reply_to=current["question_id"],
    )
    while time.monotonic() < deadline:
        result = service.get_task(task_id)
        if result.get("candidate_status"):
            break
        time.sleep(0.05)
    assert result["candidate_status"] == "completed", result
    after = engine.harness.get(task_id)
    assert before["run_id"] == after["run_id"] and before["grant_scope"] == after["grant_scope"]
    assert before["evaluation_profile"] == after["evaluation_profile"]
    assert result["model_calls"] == 4 and result["tool_calls"] == 9
    assert result["reserved_micros"] == 0 and result["usage_known"]
    thread = service._runtimes[task_id].thread
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive()
