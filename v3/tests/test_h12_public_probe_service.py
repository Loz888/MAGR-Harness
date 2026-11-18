"""Synthetic public-probe service integration: real PG/Docker, controlled model only."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _stop

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    EvaluationProfile,
    EvaluationResources,
    ExecutionRequest,
    sha256_text,
)
from mgar_v3.harness.ledger import PersistentBudgetLedger
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.service import (
    CodingTaskRequest,
    HarnessCodingService,
    WorkspaceBoundBroker,
)

ROOT = Path(__file__).resolve().parents[1]
IMAGE_LOCK = ROOT / ".local/r03/sandbox-lock.json"
DEFAULT_ASSET_ROOT = object()
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
REGRESSION = ("python3", "-B", "-m", "unittest", "-q", "test_smoke")
SMOKE = (
    "import unittest\n"
    "class Smoke(unittest.TestCase):\n"
    "    def test_import(self):\n"
    "        import calc\n"
    "        self.assertTrue(callable(calc.add))\n"
)
# This public toy asset is deliberately unrelated to every benchmark task.
PROBE = """import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys

asset_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
sys.path.insert(0, str(Path.cwd()))
cases = []
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        calc = importlib.import_module("calc")
except Exception:
    calc = None
for case_id, a, b, expected in [
    ("positive", 2, 3, 5),
    ("negative", -2, 3, 1),
]:
    if calc is None:
        cases.append({"id": case_id, "status": "error"})
        continue
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            actual = calc.add(a, b)
    except Exception:
        status = "error"
    else:
        try:
            assert actual == expected
        except AssertionError:
            status = "assertion_failed"
        else:
            status = "passed"
    cases.append({"id": case_id, "status": status})
print("HARNESS_PUBLIC_PROBE_V1 " + json.dumps({
    "plan_id": "toy-add-v1", "asset_sha256": asset_sha256, "cases": cases,
}))
sys.exit(2 if any(c["status"] == "error" for c in cases) else
         1 if any(c["status"] == "assertion_failed" for c in cases) else 0)
"""


class BoundedScriptedModel(ScriptedModel):
    """Keep failed acceptance under the real loop's finite progress/call limits."""

    def complete(self, request, cancellation=None):
        if not self.steps:
            self.steps.append(_stop("Current changes and available verification are preserved."))
        return super().complete(request, cancellation)


@dataclass
class ProbeCase:
    engine: Any
    root: Path
    source: Path
    asset_root: Path
    plan: PublicProbePlan
    profile: EvaluationProfile
    model: BoundedScriptedModel
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def final_gate(self, workspace, candidate):
        del workspace
        self.candidates.append(dict(candidate))
        # This test exercises candidate engineering, never independent quality.
        return {"status": "NOT_RUN", "tree_hash": candidate["tree_after"]}

    def service(self, *, engine=None, asset_root=DEFAULT_ASSET_ROOT, **kwargs):
        return HarnessCodingService(
            self.root,
            actor="owner",
            image_lock=IMAGE_LOCK,
            engine=engine or self.engine,
            allow_paid=True,
            live_api_key="controlled-do-not-send",
            evaluation_profile=self.profile,
            public_probe_root=self.asset_root if asset_root is DEFAULT_ASSET_ROOT else asset_root,
            model_factory=lambda request: self.model,
            final_gate=self.final_gate,
            **kwargs,
        )

    def request(self, service):
        project = service.register_project(str(self.source))
        return CodingTaskRequest(
            project_id=project["project_id"],
            objective=self.profile.objective,
            task_kind=self.profile.task_kind,
            mode="live",
            approved=True,
        )


def _case(
    engine,
    tmp_path,
    *,
    kind="bugfix",
    protocol="public-probe-v1",
    with_plan=True,
    regression=REGRESSION,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text(BUGGY)
    (source / "test_smoke.py").write_text(SMOKE)
    asset_root = tmp_path / "public-assets"
    asset_sha = hashlib.sha256(PROBE.encode()).hexdigest()
    asset = asset_root / asset_sha / "probe.py"
    asset.parent.mkdir(parents=True)
    asset.write_text(PROBE)
    plan = PublicProbePlan(
        plan_id="toy-add-v1", asset_sha256=asset_sha, case_ids=("positive", "negative")
    )
    resources = EvaluationResources(
        max_model_calls=20,
        max_tool_calls=120,
        max_cost_micros=6_000_000,
        deadline_seconds=600,
        max_context_tokens=32768,
        max_repair_rounds=8,
        max_output_tokens=2048,
    )
    from mgar_v3.harness.model import DEEPSEEK_CHAT

    experiment_id = "public-probe-service-" + uuid4().hex[:12]
    profile = EvaluationProfile(
        experiment_id=experiment_id,
        task_id="synthetic-dev-0",
        split="dev",
        arm="new",
        task_kind=kind,
        objective="Implement the toy add contract and preserve the import smoke test.",
        resources=resources,
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        write_paths=("calc.py",),
        public_test_argv=regression,
        verification_protocol=protocol,
        public_probe=plan if with_plan else None,
    )
    # Bind the exact new plan before Engine freezes it. The rest of this matrix
    # is synthetic catalog data, not a read of any benchmark holdout.
    matrix = [
        profile.model_copy(
            update={"task_id": f"synthetic-{split}-{index}", "split": split, "arm": arm}
        )
        for split in ("dev", "holdout")
        for index in range(12)
        for arm in ("baseline", "new")
    ]
    engine.harness.create_experiment(
        experiment_id,
        {
            "schema": "harness-h12-experiment-v1",
            "profiles": [p.model_dump(mode="json") for p in matrix],
        },
        {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12_000_000},
    )
    model = BoundedScriptedModel([])
    return ProbeCase(engine, engine.settings.harness_root, source, asset_root, plan, profile, model)


def _edit(request, before=BUGGY, after=FIXED):
    return _call(
        "apply_edits",
        {
            "expected_tree": request.workspace_tree,
            "edits": [
                {
                    "path": "calc.py",
                    "expected_sha256": sha256_text(before),
                    "replacements": [{"old_text": before, "new_text": after}],
                }
            ],
        },
    )


def _successful_steps():
    return [
        _call("read_public_probe", {}),
        _edit,
        _call("run_tests", {"command_id": "public_probe"}),
        _call("run_tests", {"command_id": "tests"}),
        _stop("Implemented add and verified the public behavior and regression commands."),
    ]


def _candidate(case: ProbeCase, result):
    assert result.get("independent_gate", {}).get("status") == "NOT_RUN"
    assert case.candidates, "The candidate never reached the engineering final gate"
    return case.candidates[-1]


def test_bugfix_public_red_green_and_green_regression_complete_with_real_receipts(engine, tmp_path):
    case = _case(engine, tmp_path)
    case.model.steps = _successful_steps()
    service = case.service()
    result = service.create_task(case.request(service))
    candidate = _candidate(case, result)
    assert candidate["status"] == "completed"
    receipts = candidate["receipts"]
    probes = [r for r in receipts if tuple(r["argv"]) == case.plan.argv]
    suites = [r for r in receipts if tuple(r["argv"]) == REGRESSION]
    assert [r["public_probe"]["outcome"] for r in probes] == ["red", "green"]
    assert len(suites) == 2 and all(r["status"] == "passed" for r in suites)
    assert probes[0]["tree_hash"] == candidate["tree_before"]
    assert probes[1]["tree_hash"] == candidate["tree_after"]
    assert all(r["public_probe"]["plan_hash"] == case.plan.plan_hash for r in probes)
    reproductions = [
        item for item in candidate["checklist"]["items"] if item["kind"] == "reproduction"
    ]
    assert len(reproductions) == 1 and reproductions[0]["status"] == "passed"
    assert len(case.model.requests) == candidate["model_calls"] == 5
    assert candidate["tool_calls"] == 6  # two baseline commands plus four requested tools
    projected = service.store.get(result["task_id"])
    assert projected["limits"]["max_model_calls"] == 20
    assert projected["limits"]["max_tool_calls"] == 120
    usage = PersistentBudgetLedger(service.store, result["task_id"], projected["epoch"]).snapshot(
        result["task_id"]
    )
    assert usage.tool_calls == 6 and usage.model_calls == 5
    operations = engine.harness.operations(result["task_id"])
    assert all(o["state"] == "known" for o in operations if o["budget_kind"] == "tool")
    public_read = next(
        m
        for request in case.model.requests
        for m in request.messages
        if m.role == "tool" and m.name == "read_public_probe"
    )
    assert "toy-add-v1" in public_read.content
    descriptions = "\n".join(
        spec.description for request in case.model.requests for spec in request.tools
    )
    assert case.plan.asset_sha256 in descriptions and case.plan.plan_hash in descriptions


def test_feature_green_old_suite_cannot_substitute_for_behavior_on_same_interpreter(
    engine, tmp_path
):
    case = _case(engine, tmp_path, kind="feature")
    case.model.steps = [
        lambda request: _edit(request, after=BUGGY + "\n# investigated\n"),
        _call("run_tests", {"command_id": "tests"}),
        _stop("The smoke suite is green."),
    ]
    service = case.service()
    candidate = _candidate(case, service.create_task(case.request(service)))
    assert candidate["status"] != "completed"
    assert any(
        item["kind"] == "behavior" and item["status"] != "passed"
        for item in candidate["checklist"]["items"]
    )
    assert any(
        tuple(r["argv"]) == REGRESSION
        and r["status"] == "passed"
        and r["tree_hash"] == candidate["tree_after"]
        for r in candidate["receipts"]
    )


@pytest.mark.parametrize(
    "problem", ["missing-plan", "missing-asset", "changed-asset", "missing-root"]
)
def test_public_probe_capability_is_validated_before_any_model_call(engine, tmp_path, problem):
    case = _case(engine, tmp_path, with_plan=problem != "missing-plan")
    if problem == "missing-asset":
        (case.asset_root / case.plan.asset_sha256 / "probe.py").unlink()
    if problem == "changed-asset":
        (case.asset_root / case.plan.asset_sha256 / "probe.py").write_text(PROBE + "# changed\n")
    with pytest.raises(DomainError) as exc:
        case.service(asset_root=None if problem == "missing-root" else case.asset_root)
    assert exc.value.code.startswith("PUBLIC_PROBE_")
    assert not case.model.requests


def test_legacy_profile_cannot_silently_add_a_public_probe(engine, tmp_path):
    case = _case(engine, tmp_path, protocol="legacy")
    with pytest.raises(DomainError) as exc:
        case.service()
    assert exc.value.code.startswith("PUBLIC_PROBE_")
    assert not case.model.requests


def test_missing_execute_authority_does_not_consume_model_budget(engine, tmp_path, monkeypatch):
    case = _case(engine, tmp_path)
    service = case.service()
    grant = service._grant
    monkeypatch.setattr(
        service,
        "_grant",
        lambda *args, **kwargs: grant(*args, **kwargs).model_copy(
            update={"execute": False, "allowed_commands": ()},
        ),
    )
    try:
        result = service.create_task(case.request(service))
    except DomainError as exc:
        assert exc.code in {
            "EVALUATION_SCOPE_DENIED",
            "EVALUATION_PROFILE_DENIED",
            "HARNESS_SCOPE_DENIED",
        } or any(part in exc.code for part in ("EXECUTE", "COMMAND", "PUBLIC_PROBE"))
    else:
        candidate = case.candidates[-1] if case.candidates else result
        assert candidate["status"] != "completed"
        assert any(part in candidate["reason"] for part in ("EXECUTE", "COMMAND", "PUBLIC_PROBE"))
    assert not case.model.requests


def test_known_probe_and_model_receipts_recover_without_relaunch(engine, tmp_path, monkeypatch):
    case = _case(engine, tmp_path)
    case.model.steps = _successful_steps()
    launches = []
    launch = WorkspaceBoundBroker.launch

    def count_launch(broker, request, *args, **kwargs):
        if tuple(request.argv) == case.plan.argv:
            launches.append(request.operation_id)
        return launch(broker, request, *args, **kwargs)

    monkeypatch.setattr(WorkspaceBoundBroker, "launch", count_launch)
    fired = []

    def crash(point, payload):
        del payload
        if point == "model_after_settlement" and not fired:
            fired.append(point)
            raise SystemExit("controlled crash after known receipt")

    service = case.service(fault_hook=crash)
    with pytest.raises(SystemExit, match="controlled crash"):
        service.create_task(case.request(service))
    row = engine.harness.tasks()[0]
    before_epoch, run_id = row["epoch"], row["run_id"]
    assert len(launches) == 1 and len(case.model.requests) == 1
    engine.close()
    replacement = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        replacement.recover()
        resumed = case.service(engine=replacement)
        result = resumed.recover_task(row["task_id"])
        candidate = _candidate(case, result)
        assert candidate["status"] == "completed"
        after = replacement.harness.get(row["task_id"])
        assert after["run_id"] == run_id and after["epoch"] > before_epoch
        assert len(launches) == 2 and all(count == 1 for count in Counter(launches).values())
        request_ids = [request.operation_id for request in case.model.requests]
        assert len(request_ids) == len(set(request_ids)) == 5
        probes = [r for r in candidate["receipts"] if tuple(r["argv"]) == case.plan.argv]
        assert probes[0]["public_probe"]["execution_epoch"] == before_epoch
        assert probes[-1]["public_probe"]["execution_epoch"] == after["epoch"]
    finally:
        replacement.close()


def test_distinct_regression_interpreter_binds_only_two_frozen_programs(
    engine,
    tmp_path,
    record_property,
):
    case = _case(engine, tmp_path, regression=("python", *REGRESSION[1:]))
    case.model.steps = _successful_steps()
    service = case.service()
    runtime = service._prepare(case.request(service))
    row = engine.harness.get(runtime.task_id)
    assert tuple(row["grant_scope"]["allowed_commands"]) == ("python", "python3")
    assert tuple(row["request"]["trusted_test_argv"])[0] == "python"
    assert row["limits"]["max_model_calls"] == 20

    # This fixed image may provide only python3. Check its actual command table
    # in an independently bounded, read-only fixture before choosing coverage.
    lease = issue_lease(
        mode="investigate",
        actor="owner",
        workspace_id="alias-availability",
        repository="example/alias-availability",
        execution_epoch=1,
        read_paths=("",),
        write_paths=(),
        allowed_commands=("python3",),
        timeout_seconds=10,
    )
    broker = IsolatedExecutionBroker(runtime.workspace.root, lease, lock_path=IMAGE_LOCK)
    available = broker.execute(
        ExecutionRequest(
            operation_id="alias-availability",
            purpose="command",
            argv=(
                "python3",
                "-B",
                "-c",
                "import shutil,sys;sys.exit(0 if shutil.which('python') else 1)",
            ),
            execution_epoch=1,
        ),
        CapabilityGrant(
            actor="owner",
            workspace_id="alias-availability",
            repository="example/alias-availability",
            read_paths=("",),
            write_paths=(),
            execute=True,
            allowed_commands=("python3",),
        ),
        CancellationToken(),
    )
    assert available.exit_code in {0, 1}
    record_property("regression_python_available", str(available.exit_code == 0))
    if available.exit_code == 0:
        result = service._run(runtime)
        candidate = _candidate(case, result)
        assert candidate["status"] == "completed"
        prefix = "Public verification baseline facts: "
        initial = next(
            message.content
            for message in case.model.requests[0].messages
            if message.content.startswith(prefix)
        )
        facts, _ = json.JSONDecoder().raw_decode(initial[len(prefix) :])
        assert len(facts) == 2
        assert {fact["public_outcome"] for fact in facts} == {None, "red"}
        assert all(fact["tree_hash"] == candidate["tree_before"] for fact in facts)
        assert any(fact["status"] == "passed" and fact["tests_run"] > 0 for fact in facts)
        assert "command_id=tests" in initial and "command_id=public_probe" in initial
        assert any(tuple(r["argv"]) == case.profile.public_test_argv for r in candidate["receipts"])
        record_property("verification_scope", "real-docker-baseline-and-candidate")
    else:
        stopped = service.cancel_task(runtime.task_id, "Pinned image has no python alias")
        assert stopped["status"] == "cancelled"
        assert not case.model.requests
        assert not engine.harness.operations(runtime.task_id)
        record_property("verification_scope", "prepared-compatibility-only; no python alias")


def test_known_raw_probe_recovers_before_outer_tool_receipt_without_relaunch(
    engine,
    tmp_path,
    monkeypatch,
):
    case = _case(engine, tmp_path)
    case.model.steps = _successful_steps()
    launches = []
    launch = WorkspaceBoundBroker.launch

    def count_launch(broker, request, *args, **kwargs):
        launches.append((request.operation_id, tuple(request.argv)))
        return launch(broker, request, *args, **kwargs)

    monkeypatch.setattr(WorkspaceBoundBroker, "launch", count_launch)
    # poll() is a supported command-progress driver. Keep this fault on the
    # calling thread, so SystemExit cannot merely terminate a monitor thread.
    monkeypatch.setattr(CommandSession, "_monitor", lambda *args: None)
    close = CommandSession.close
    interrupted = []
    monkeypatch.setattr(CommandSession, "close", lambda self: 0 if interrupted else close(self))
    recorded = []

    def crash(point, payload):
        del payload
        if point == "command.after_receipt":
            recorded.append(point)
            if len(recorded) == 2:
                interrupted.append(True)
                raise SystemExit("controlled crash after raw public probe receipt")

    service = case.service(fault_hook=crash)
    with pytest.raises(SystemExit, match="after raw public probe receipt"):
        service.create_task(case.request(service))
    row = engine.harness.tasks()[0]
    before_epoch, run_id = row["epoch"], row["run_id"]
    operations = engine.harness.operations(row["task_id"])
    probe_tool = next(o for o in operations if o["operation_id"].endswith(":baseline:public_probe"))
    probe_process = next(
        o for o in operations if o["operation_id"].endswith(":baseline:public_probe:process")
    )
    assert probe_tool["state"] == "unknown" and probe_process["state"] == "known"
    assert len(launches) == 2 and not case.model.requests
    interrupted.clear()
    engine.close()
    replacement = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        replacement.recover()
        result = case.service(engine=replacement).recover_task(row["task_id"])
        candidate = _candidate(case, result)
        assert candidate["status"] == "completed"
        after = replacement.harness.get(row["task_id"])
        assert after["run_id"] == run_id and after["epoch"] > before_epoch
        counts = Counter(argv for _, argv in launches)
        assert counts == {REGRESSION: 2, case.plan.argv: 2}
        assert len({operation for operation, _ in launches}) == len(launches) == 4
        assert len(case.model.requests) == candidate["model_calls"] == 5
        assert candidate["tool_calls"] == 6
        probes = [r for r in candidate["receipts"] if tuple(r["argv"]) == case.plan.argv]
        assert [r["public_probe"]["outcome"] for r in probes] == ["red", "green"]
        assert probes[0]["public_probe"]["execution_epoch"] == before_epoch
        assert probes[-1]["public_probe"]["execution_epoch"] == after["epoch"]
    finally:
        replacement.close()


def test_import_assertion_is_invalid_probe_evidence_and_never_a_target_reproduction(
    engine,
    tmp_path,
):
    case = _case(engine, tmp_path)
    (case.source / "calc.py").write_text("raise AssertionError('import is not target behavior')\n")
    service = case.service()
    candidate = _candidate(case, service.create_task(case.request(service)))
    assert candidate["status"] == "blocked"
    assert candidate["reason"] == "PUBLIC_PROBE_BASELINE_INVALID"
    probes = [r for r in candidate["receipts"] if tuple(r["argv"]) == case.plan.argv]
    assert len(probes) == 1 and probes[0]["public_probe"]["outcome"] == "invalid"
    assert {item["status"] for item in probes[0]["public_probe"]["cases"]} == {"error"}
    assert probes[0]["status"] == "error" and probes[0]["exit_code"] == 2
    assert not case.model.requests
