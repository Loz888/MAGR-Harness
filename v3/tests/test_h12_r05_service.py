"""R05 real PG/CAS/Engine sealing with controlled free model and public toy Judge."""

import copy
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _stop

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CapabilityGrant,
    EvaluationProfile,
    EvaluationResources,
    sha256_text,
)
from mgar_v3.harness.lifecycle_types import LifecyclePolicy
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.regression_types import (
    IndependentEvaluationEvidence,
    IndependentRegressionReport,
)
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService
from mgar_v3.harness.workspace import _copy_tree_nofollow, _scan, _tree_hash
from mgar_v3.quality_eval import h12_judge
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_gate import evaluate_final
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_runner import EvaluationRunner

ROOT = Path(__file__).parents[1]
LOCK = ROOT / ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json"
BASE = "def goal():return 1\ndef old_name():return 7\n"
GOOD = "def goal():return 2\ndef old_name():return 7\n"
BROKEN = "def goal():return 2\n"
PROBE = """import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
state='passed'
try:
 import api
 assert api.goal()==2
except AssertionError: state='assertion_failed'
except Exception: state='error'
print('HARNESS_PUBLIC_PROBE_V1 '+json.dumps({'plan_id':'r05-public-goal',
 'asset_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
 'cases':[{'id':'goal','status':state}]}))
sys.exit(0 if state=='passed' else 1 if state=='assertion_failed' else 2)
"""


class ControlledModel(ScriptedModel):
    def complete(self, request, cancellation=None):
        if not self.steps:
            self.steps.append(_stop("Preserve api.py and the remaining blocker."))
        return super().complete(request, cancellation)


def toy_judge(source, task, output):
    """A public one-test oracle, deliberately narrower than full regression."""
    output.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="r05-public-toy-judge-", dir="/tmp"))
    files, _ = _scan(source)
    tree = _tree_hash(files)
    try:
        target = directory / "workspace"
        _copy_tree_nofollow(source, target, files)
        directory.chmod(0o755)
        for p in [target, *target.rglob("*")]:
            p.chmod(p.stat().st_mode | (0o555 if p.is_dir() else 0o444))
        lease = issue_lease(
            mode="investigate",
            actor="h12-verifier",
            workspace_id="toy-" + uuid4().hex,
            repository=task["repository"],
            execution_epoch=1,
            read_paths=("",),
            write_paths=(),
            allowed_commands=("python3",),
            timeout_seconds=30,
            max_output_bytes=1048576,
        )
        grant = CapabilityGrant(
            actor=lease.actor,
            workspace_id=lease.workspace_id,
            repository=lease.repository,
            read_paths=("",),
            write_paths=(),
            execute=True,
            allowed_commands=("python3",),
        )
        broker = IsolatedExecutionBroker(target, lease, lock_path=LOCK)
        session = CommandSession(broker)
        try:
            receipt, _ = h12_judge._run(
                session,
                grant,
                ["python3", "-B", "-m", "pytest", "-q", "tests/test_goal.py"],
                30,
                output,
                0,
                source_tree=tree,
                verifier_tree=tree,
                phase="final",
            )
        finally:
            session.close()
        result = {
            "receipt_protocol": "h12-independent-judge-v1",
            "task_id": task["task_id"],
            "phase": "final",
            "tree_hash": tree,
            "source_tree_hash": tree,
            "verifier_tree_hash": tree,
            "judge_sha256": task["judge_sha256"],
            "image_id": broker.image_id,
            "status": "PASS" if receipt["exit_code"] == 0 else "FAIL",
            "receipts": [receipt],
        }
        (output / "judge-result.json").write_text(json.dumps(result))
        return result
    finally:
        shutil.rmtree(directory)


def case(engine, settings, tmp_path, *, preserves):
    source = tmp_path / "project"
    source.mkdir()
    (source / "api.py").write_text(BASE)
    (source / "tests").mkdir()
    (source / "tests/test_goal.py").write_text("import api\ndef test_goal():assert api.goal()==2\n")
    (source / "tests/test_imports.py").write_text(
        "def test_light_imports():\n from api import old_name\n assert old_name()==7\n"
    )
    assets = tmp_path / "assets"
    assetsha = sha256_text(PROBE)
    asset = assets / assetsha / "probe.py"
    asset.parent.mkdir(parents=True)
    asset.write_text(PROBE)
    probe = PublicProbePlan(plan_id="r05-public-goal", asset_sha256=assetsha, case_ids=("goal",))
    plan = regression_plan([("python3", "-B", "-m", "pytest", "tests/")])
    profile = EvaluationProfile(
        experiment_id="r05-fixture-" + uuid4().hex[:10],
        task_id="r05-toy-dev-0",
        split="dev",
        arm="candidate",
        task_kind="bugfix",
        objective="Fix api.py goal to return 2 and preserve old_name.",
        resources=EvaluationResources(
            max_model_calls=10,
            max_tool_calls=40,
            max_cost_micros=1_000_000,
            deadline_seconds=180,
            max_context_tokens=32768,
            max_repair_rounds=1,
            max_output_tokens=2048,
        ),
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        write_paths=("api.py",),
        public_test_argv=("python3", "-B", "-m", "pytest", "-q", "tests/test_goal.py"),
        public_probe=probe,
        verification_protocol="public-probe-v1",
        independent_regression_plan=plan,
        lifecycle_policy=LifecyclePolicy(
            entry_timeout_seconds=3, shutdown_seconds=1, cleanup_seconds=2
        ),
        acceptance_protocol="contract-v1",
        acceptance_baseline_commit="0" * 40,
    )
    profiles = [
        profile.model_copy(update={"task_id": f"r05-toy-{split}-{n}", "split": split, "arm": arm})
        for split in ("dev", "holdout")
        for n in range(12)
        for arm in ("legacy-v35", "candidate")
    ]
    engine.harness.create_experiment(
        profile.experiment_id,
        {
            "schema": "harness-h12-experiment-v1",
            "profiles": [p.model_dump(mode="json") for p in profiles],
        },
        {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12_000_000},
    )
    task = {
        "task_id": profile.task_id,
        "split": "dev",
        "type": "bugfix",
        "language": "python",
        "repository": "local/project",
        "base_commit": "0" * 40,
        "judge_sha256": "b" * 64,
        "environment": {"verify": ["python3 -B -m pytest tests/test_goal.py"]},
        "acceptance": {"regression": ["python3 -B -m pytest tests/"]},
    }
    independent = IndependentRegressionRunner(
        engine.artifacts,
        LOCK,
        repository=task["repository"],
        task_id=task["task_id"],
        experiment_id=profile.experiment_id,
        public_probe_root=assets,
    )
    frozen = h12_judge.test_hashes(source)
    base_tree = h12_judge.tree_hash(source)
    baseline_ref, baseline = independent.execute(
        source,
        plan,
        tmp_path / "independent/baseline",
        phase="baseline",
        expected_tree=base_tree,
        frozen_tests=frozen,
    )
    assert baseline.status == "FAIL"

    def final_gate(workspace, candidate):
        row = engine.harness.get(workspace.manifest.task_id)
        return evaluate_final(
            independent,
            profile,
            workspace.root,
            tmp_path / "independent",
            baseline_ref=baseline_ref,
            baseline_tree=base_tree,
            final_tree=candidate["tree_after"],
            frozen_tests=frozen,
            execution_task_id=row["task_id"],
            run_id=str(row["run_id"]),
            execution_epoch=row["execution_epoch"],
            sealed=row["sealed_tree_hash"] == candidate["tree_after"],
            judge_sha256=task["judge_sha256"],
            judge_root=tmp_path / "judge-private",
            judge=lambda: toy_judge(workspace.root, task, tmp_path / "judge-private"),
        )

    def edit(request):
        return _call(
            "apply_edits",
            {
                "expected_tree": request.workspace_tree,
                "edits": [
                    {
                        "path": "api.py",
                        "expected_sha256": sha256_text(BASE),
                        "content": GOOD if preserves else BROKEN,
                    }
                ],
            },
        )

    model = ControlledModel(
        [
            _call("read_file", {"path": "api.py"}),
            edit,
            _call("run_tests", {"command_id": "public_probe"}),
            _call("run_tests", {"command_id": "tests"}),
            _stop("Fixed api.py and verified the public toy goal."),
        ]
    )
    service = HarnessCodingService(
        settings.harness_root,
        actor="owner",
        engine=engine,
        image_lock=LOCK,
        evaluation_profile=profile,
        public_probe_root=assets,
        allow_paid=True,
        live_api_key="controlled-unused",
        model_factory=lambda request: model,
        final_gate=final_gate,
    )
    registered = service.register_project(str(source))
    result = service.create_task(
        CodingTaskRequest(
            project_id=registered["project_id"],
            objective=profile.objective,
            task_kind="bugfix",
            mode="live",
            approved=True,
        )
    )
    scorer = EvaluationRunner(
        engine,
        experiment_id=profile.experiment_id,
        evidence=tmp_path / "history",
        image_locks={},
        api_key=None,
    )
    return SimpleNamespace(
        engine=engine,
        source=source,
        profile=profile,
        task=task,
        result=result,
        scorer=scorer,
        model=model,
        settings=settings,
        assets=assets,
        root=tmp_path,
    )


@pytest.mark.parametrize("preserves", [False, True])
def test_pg_sealed_target_and_judge_green_require_full_public_regression(
    engine,
    settings,
    tmp_path,
    preserves,
):
    value = case(engine, settings, tmp_path, preserves=preserves)
    result = value.result
    assert result["candidate_status"] == "completed", (result["reason"], result["summary"])
    assert (result["status"] == "completed") is preserves, result.get("independent_gate")
    assert result["tests"]["failed"] == 0 and result["tests"]["run"] == 1
    assert result["independent_gate"]["dimensions"]["target_behavior"]["status"] == "PASS"
    assert result["independent_gate"]["dimensions"]["independent_judge"]["status"] == "PASS"
    score = value.scorer._score_payload(value.task, value.profile, result)
    assert score["status"] == ("PASS" if preserves else "FAIL"), score
    assert score["new_regression_failures"] == (0 if preserves else 1), score
    assert score["regression_evidence_known"]
    assert score["dimensions"]["public_regression"]["status"] == ("PASS" if preserves else "FAIL")
    assert score["dimensions"]["structural_constraints"]["status"] == "NOT_RUN"
    row = engine.harness.get(result["task_id"])
    assert row["sealed_tree_hash"] == result["tree_after"] and row["result_hash"]
    assert result["reserved_micros"] == 0 and result["usage_known"]
    assert len(value.model.requests) == result["model_calls"]
    assert value.scorer._score_payload(value.task, value.profile, result) == score
    assert (value.source / "api.py").read_text() == BASE
    baseline_receipts = [
        r
        for r in result["receipts"]
        if r.get("tree_hash") == result["tree_before"] and r.get("purpose") == "test"
    ]
    assert any(r["tests_run"] == 1 and r["tests_failed"] == 1 for r in baseline_receipts)
    if preserves:
        _assert_scoring_boundaries(value)
        _assert_terminal_recovery(value)


def _assert_scoring_boundaries(value):
    def score(result=None, profile=None):
        return value.scorer._score_payload(
            value.task, profile or value.profile, result or value.result
        )

    for key, replacement in (
        ("tree_after", "f" * 64),
        ("run_id", str(uuid4())),
        ("execution_epoch", 99),
        ("candidate_status", "blocked"),
        ("task_id", "unrelated-task"),
    ):
        changed = copy.deepcopy(value.result)
        changed[key] = replacement
        measured = score(changed)
        assert measured["status"] != "PASS" and measured["new_regression_failures"] is None, key
    changed = copy.deepcopy(value.result)
    changed["independent_gate"]["independent_evidence_ref"] = "d" * 64
    assert score(changed)["new_regression_failures"] is None
    changed = copy.deepcopy(value.result)
    changed["independent_gate"]["dimensions"]["public_regression"]["required"] = False
    assert score(changed)["new_regression_failures"] is None
    altered_plan = regression_plan([("python3", "-m", "pytest", "tests/test_goal.py")])
    altered_profile = value.profile.model_copy(update={"independent_regression_plan": altered_plan})
    assert score(profile=altered_profile)["new_regression_failures"] is None

    artifacts = value.engine.artifacts
    root_ref = value.result["independent_gate"]["independent_evidence_ref"]
    root = IndependentEvaluationEvidence.model_validate_json(artifacts.read(root_ref))
    regression = IndependentRegressionReport.model_validate_json(artifacts.read(root.final_ref))
    for reference in (root_ref, root.judge_ref, root.target_ref, regression.receipt_refs[0]):
        path = artifacts.path(reference)
        raw = path.read_bytes()
        path.unlink()
        try:
            measured = score()
            assert measured["status"] != "PASS" and measured["new_regression_failures"] is None
        finally:
            path.write_bytes(raw)
    assert score()["status"] == "PASS"

    original = value.scorer.evidence / "candidate/dev/old-score.json"
    original.parent.mkdir(parents=True)
    old = json.dumps(
        {"task_id": value.task["task_id"], "status": "FAIL", "historical": True}
    ).encode()
    original.write_bytes(old)
    audited = value.scorer.append_regression_audit(
        value.task,
        value.profile,
        value.result,
        original_score=original,
        output=value.root / "additive-audit",
    )
    assert original.read_bytes() == old
    assert audited["original_status"] == "FAIL" and audited["recomputed"]["status"] == "PASS"
    assert audited["new_paid_calls"] == 0 and audited["resumed"] is False
    for output in (value.root / "additive-audit", value.scorer.evidence / "forbidden-audit"):
        with pytest.raises(ValueError):
            value.scorer.append_regression_audit(
                value.task,
                value.profile,
                value.result,
                original_score=original,
                output=output,
            )


def _assert_terminal_recovery(value):
    def denied(*args, **kwargs):
        raise AssertionError("Known final independent evidence must not dispatch again")

    task_id = value.result["task_id"]
    original = value.engine.harness.get(task_id)
    calls = len(value.model.requests)
    value.engine.close()
    replacement = Engine(value.settings, ArtifactStore(value.settings.artifact_root))
    try:
        replacement.recover()
        recovered = HarnessCodingService(
            value.settings.harness_root,
            actor="owner",
            engine=replacement,
            image_lock=LOCK,
            evaluation_profile=value.profile,
            public_probe_root=value.assets,
            allow_paid=True,
            live_api_key="controlled-unused",
            model_factory=denied,
            final_gate=denied,
        ).recover_task(task_id)
        assert recovered == value.result
        assert len(value.model.requests) == calls
        assert replacement.harness.get(task_id)["result_hash"] == original["result_hash"]
        scorer = EvaluationRunner(
            replacement,
            experiment_id=value.profile.experiment_id,
            evidence=value.root / "recovered-history",
            image_locks={},
            api_key=None,
        )
        measured = scorer._score_payload(value.task, value.profile, recovered)
        assert measured["status"] == "PASS" and measured["new_regression_failures"] == 0
    finally:
        replacement.close()
