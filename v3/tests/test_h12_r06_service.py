"""Real PG/CAS sealing, reviewed private Judge and public structural proof."""

import hashlib
import json
from uuid import uuid4

import pytest
from test_h12_r06_baseline import LOCK
from test_h12_r06_contracts import (
    BASE,
    BEHAVIOR,
    JUDGE,
    STRUCTURE,
    helper_source,
    manifest_for,
    refactor_policy,
)
from test_harness_v4_h10 import ScriptedModel, _call, _stop

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import EvaluationProfile, EvaluationResources, sha256_text
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.regression_types import IndependentEvaluationEvidence
from mgar_v3.harness.semantic_types import SemanticRevision
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService
from mgar_v3.quality_eval import h12_judge
from mgar_v3.quality_eval.h12_baseline_policy import execute_baseline
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_gate import evaluate_final
from mgar_v3.quality_eval.h12_runner import EvaluationRunner
from mgar_v3.quality_eval.h12_structural_probe import freeze_structural_probe
from mgar_v3.quality_eval.harness_v4 import judge_sha256

BEHAVIOR_TESTS = """from box import Box
import pytest
def test_case_and_overwrite():
 b=Box();b['AA']=1;b['aa']=2
 assert b['AA']==2 and len(b.data)==1
def test_delete_and_missing():
 b=Box();b['AA']=1;del b['aa']
 with pytest.raises(KeyError):b['AA']
def test_distinct_unicode_keys():
 b=Box();b['straße']=1;b['STRASSE']=2
 assert b['straße']==1 and b['strasse']==2
def test_iteration_payload():
 b=Box();b['Mixed']=7
 assert list(b.data.values())==[('Mixed',7)]
"""


class ControlledModel(ScriptedModel):
    def complete(self, request, cancellation=None):
        if not self.steps:
            self.steps.append(_stop("Preserve box.py and unresolved requirements."))
        return super().complete(request, cancellation)


def private_files(root, policy, task, *, bad_name=False):
    root.mkdir()
    data = JUDGE
    if bad_name:
        data += b"\ndef test_arbitrary_name():\n assert hasattr(Box,'_canonical_key')\n"
    filename = "tests/hidden/r06_reviewed_judge.py"
    source = root / "private_test.py"
    source.write_bytes(data)
    registry = {
        "schema": "mgar-h12-judge-implementation-v1",
        "tasks": [
            {
                "task_id": task["task_id"],
                "original_judge_sha256": task["judge_sha256"],
                "files": [
                    {
                        "source": source.name,
                        "target": filename,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
                "command": ["python3", "-m", "pytest", "-q", filename],
                "baseline_oracle": False,
            }
        ],
    }
    registry_path = root / "registry.json"
    registry_path.write_text(json.dumps(registry))
    manifest = manifest_for(
        policy,
        {filename: data},
        task_id=task["task_id"],
        commands=[h12_judge._normalize(registry["tasks"][0]["command"].copy())],
    )
    trace = root / "traceability.json"
    trace.write_text(manifest.model_dump_json())
    return (
        registry_path,
        hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        trace,
        hashlib.sha256(trace.read_bytes()).hexdigest(),
    )


def profile_for_case(policy, probe, trace_sha, experiment, registry_sha="c" * 64):
    return EvaluationProfile(
        experiment_id=experiment,
        task_id="r06-refactor-dev-0",
        split="dev",
        arm="candidate",
        task_kind="refactor",
        objective="Refactor box.py to use one shared canonicalizer, retaining behavior.",
        resources=EvaluationResources(
            max_model_calls=8,
            max_tool_calls=40,
            max_cost_micros=1_000_000,
            deadline_seconds=180,
            max_context_tokens=32768,
            max_repair_rounds=1,
            max_output_tokens=2048,
        ),
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        write_paths=("box.py",),
        public_test_argv=policy.behavior_plan.commands[0].public_argv,
        acceptance_protocol="contract-v1",
        acceptance_revision=policy.benchmark_revision,
        acceptance_baseline_commit="0" * 40,
        public_requirements=(BEHAVIOR, STRUCTURE),
        independent_regression_plan=policy.behavior_plan,
        baseline_policy=policy,
        structural_probe=probe,
        judge_traceability_sha256=trace_sha,
        judge_registry_sha256=registry_sha,
    )


@pytest.mark.parametrize("phase", ["execute", "freeze"])
def test_pg_unpublished_judge_constraint_is_blocked_before_model_or_tool_dispatch(
    engine,
    settings,
    tmp_path,
    monkeypatch,
    phase,
):
    policy = refactor_policy()
    assets = tmp_path / "assets"
    probe = freeze_structural_probe(policy.shared_helper, assets)
    task = {
        "task_id": "r06-refactor-dev-0",
        "split": "dev",
        "type": "refactor",
        "language": "python",
        "repository": "toy/private-review",
        "base_commit": "0" * 40,
        "judge_status": "pending",
        "judge": {
            "kind": "r06-reviewed-python",
            "version": "r06-toy-v2",
            "pass_rule": "Public requirements only",
        },
        "environment": {"verify": []},
        "acceptance": {"regression": ["python3 -B -m pytest tests/"]},
    }
    task["judge_sha256"] = judge_sha256(task)
    registry, registry_sha, trace, trace_sha = private_files(
        tmp_path / "private", policy, task, bad_name=True
    )
    profile = profile_for_case(policy, probe, trace_sha, "r06-denied-review", registry_sha)
    project = tmp_path / "project"
    project.mkdir()
    (project / "box.py").write_text(BASE)
    evaluator = EvaluationRunner(
        engine,
        experiment_id=profile.experiment_id,
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key=None,
        registry_path=registry,
        registry_sha256=registry_sha,
        public_probe_root=assets,
        semantic_revisions={
            task["task_id"]: SemanticRevision(
                policy=policy,
                traceability_path=trace,
                traceability_sha256=trace_sha,
                registry_path=registry,
                registry_sha256=registry_sha,
                structural_probe=probe,
            )
        },
    )
    monkeypatch.setattr(evaluator, "_materialize", lambda *args: project)

    def denied(*args, **kwargs):
        raise AssertionError("Unreviewed constraint must not dispatch paid IO")

    monkeypatch.setattr("mgar_v3.quality_eval.h12_runner.RecordedFlashModel", denied)
    with pytest.raises(DomainError, match="symbol name absent"):
        if phase == "execute":
            evaluator._execute(task, profile, project, LOCK, tmp_path / "evaluation")
        else:
            monkeypatch.setattr(evaluator, "_tasks", lambda split: [task])
            monkeypatch.setattr(evaluator, "_profile_for", lambda task, arm: profile)
            evaluator.initialize()
    assert engine.harness.tasks() == []
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


@pytest.mark.parametrize("variant", ["_lowerkey", "unused", "identity-inline"])
def test_pg_refactor_requires_equivalence_and_shared_return_on_same_sealed_tree(
    engine,
    settings,
    tmp_path,
    variant,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "box.py").write_text(BASE)
    (project / "tests").mkdir()
    (project / "tests/test_behavior.py").write_text(BEHAVIOR_TESTS)
    policy = refactor_policy()
    assets = tmp_path / "public-assets"
    probe = freeze_structural_probe(policy.shared_helper, assets)
    task = {
        "task_id": "r06-refactor-dev-0",
        "split": "dev",
        "type": "refactor",
        "language": "python",
        "repository": "local/r06",
        "base_commit": "0" * 40,
        "judge_status": "pending",
        "goal": "Refactor box.py to use one shared canonicalizer, retaining behavior.",
        "judge": {
            "kind": "r06-reviewed-python",
            "version": "r06-fixture-v2",
            "pass_rule": "Public behavior and shared-helper requirements",
        },
        "environment": {"verify": []},
        "acceptance": {
            "regression": ["python3 -m pytest tests/"],
            "required": [BEHAVIOR, STRUCTURE],
        },
    }
    task["judge_sha256"] = judge_sha256(task)
    registry, registry_sha, trace, trace_sha = private_files(tmp_path / "private", policy, task)
    profile = profile_for_case(
        policy, probe, trace_sha, "r06-controlled-" + uuid4().hex[:10], registry_sha
    )
    profiles = [
        profile.model_copy(
            update={"task_id": f"r06-refactor-{split}-{n}", "split": split, "arm": arm}
        )
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
    runner = IndependentRegressionRunner(
        engine.artifacts,
        LOCK,
        repository=task["repository"],
        task_id=task["task_id"],
        experiment_id=profile.experiment_id,
        public_probe_root=assets,
    )
    old_tree = h12_judge.tree_hash(project)
    tests = h12_judge.test_hashes(project)
    assessment_ref, assessment = execute_baseline(
        runner,
        policy,
        project,
        tmp_path / "independent-baseline",
        tree=old_tree,
        frozen_tests=tests,
    )
    assert assessment.status == "PASS" and assessment.target_ref is None
    assert not (tmp_path / "independent-baseline/structure").exists()

    def gate(workspace, candidate):
        row = engine.harness.get(workspace.manifest.task_id)
        return evaluate_final(
            runner,
            profile,
            workspace.root,
            tmp_path / "independent-final",
            baseline_ref=assessment.behavior_ref,
            baseline_tree=old_tree,
            final_tree=candidate["tree_after"],
            frozen_tests=tests,
            execution_task_id=row["task_id"],
            run_id=str(row["run_id"]),
            execution_epoch=row["execution_epoch"],
            sealed=row["sealed_tree_hash"] == candidate["tree_after"],
            judge_sha256=task["judge_sha256"],
            judge_root=tmp_path / "private-final",
            baseline_assessment_ref=assessment_ref,
            judge=lambda: h12_judge.verify(
                workspace.root,
                task,
                LOCK,
                tmp_path / "private-final",
                expected_tree=candidate["tree_after"],
                sealed=True,
                frozen_tests=tests,
                registry_path=registry,
                registry_sha256=registry_sha,
                baseline_policy=policy,
                traceability_ref=(trace, trace_sha),
            ),
        )

    final = (
        helper_source(variant)
        if variant == "_lowerkey"
        else BASE + " def _fold(self,key):return key.lower()\n"
    )
    if variant == "identity-inline":
        final = (
            BASE.replace("key.lower()", "self._fold(key.lower())")
            + " def _fold(self,key):return key\n"
        )

    def edit(request):
        return _call(
            "apply_edits",
            {
                "expected_tree": request.workspace_tree,
                "edits": [
                    {"path": "box.py", "expected_sha256": sha256_text(BASE), "content": final}
                ],
            },
        )

    model = ControlledModel(
        [
            _call("read_file", {"path": "box.py"}),
            edit,
            _call("run_tests", {"command_id": "tests"}),
            _stop("Refactored box.py and checked behavior."),
        ]
    )
    service = HarnessCodingService(
        settings.harness_root,
        actor="owner",
        engine=engine,
        image_lock=LOCK,
        evaluation_profile=profile,
        allow_paid=True,
        live_api_key="controlled-unused",
        model_factory=lambda request: model,
        final_gate=gate,
    )
    registration = service.register_project(str(project))
    result = service.create_task(
        CodingTaskRequest(
            project_id=registration["project_id"],
            objective=profile.objective,
            task_kind="refactor",
            mode="live",
            approved=True,
        )
    )
    assert result["candidate_status"] == "completed", (result["reason"], result["summary"])
    assert result["tests"]["run"] == 4 and result["tests"]["failed"] == 0
    good = variant == "_lowerkey"
    dimensions = result["independent_gate"]["dimensions"]
    assert dimensions["public_regression"]["status"] == "PASS", result["independent_gate"]
    assert dimensions["structural_constraints"]["status"] == ("PASS" if good else "FAIL")
    assert dimensions["independent_judge"]["status"] == "PASS", result["independent_gate"]
    scorer = EvaluationRunner(
        engine,
        experiment_id=profile.experiment_id,
        evidence=tmp_path / "scores",
        image_locks={},
        api_key=None,
    )
    measured = scorer._score_payload(task, profile, result)
    assert measured["status"] == ("PASS" if good else "FAIL"), measured
    assert measured["baseline_policy_status"] == "PASS" and measured["new_regression_failures"] == 0
    assert (
        len(model.requests) == result["model_calls"]
        and result["usage_known"]
        and result["reserved_micros"] == 0
    )
    assert scorer._score_payload(task, profile, result) == measured
    assert (project / "box.py").read_text() == BASE
    for request in model.requests:
        assert "r06_reviewed_judge.py" not in str(request.messages)
        assert "test_public_structure" not in str(request.messages)
    if good:
        root_ref = result["independent_gate"]["independent_evidence_ref"]
        proof = IndependentEvaluationEvidence.model_validate_json(engine.artifacts.read(root_ref))
        for reference in [proof.structural_ref, proof.baseline_assessment_ref, trace_sha]:
            path = engine.artifacts.path(reference)
            raw = path.read_bytes()
            path.unlink()
            try:
                unverified = scorer._score_payload(task, profile, result)
                assert (
                    unverified["status"] != "PASS" and unverified["new_regression_failures"] is None
                )
            finally:
                path.write_bytes(raw)
        engine.close()
        replacement = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            replacement.recover()

            def denied(*args, **kwargs):
                raise AssertionError("Known result must never dispatch again")

            restored = HarnessCodingService(
                settings.harness_root,
                actor="owner",
                engine=replacement,
                image_lock=LOCK,
                evaluation_profile=profile,
                allow_paid=True,
                live_api_key="controlled-unused",
                model_factory=denied,
                final_gate=denied,
            ).recover_task(result["task_id"])
            assert restored == result
        finally:
            replacement.close()
