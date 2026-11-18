"""Real frozen Python/Go old-behavior and target baselines; no private Judge needed."""

from pathlib import Path

import pytest
from test_h12_r06_contracts import BASE, helper_source, refactor_policy, requirement

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.regression_types import (
    IndependentRegressionReport,
    IndependentTestReceipt,
    RegressionCommand,
    RegressionPlan,
)
from mgar_v3.harness.semantic_types import MissingTargetSymbol, TaskBaselinePolicy, public_id
from mgar_v3.quality_eval import h12_judge
from mgar_v3.quality_eval.h12_baseline_policy import declared_missing_target, execute_baseline
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_structural_probe import freeze_structural_probe

ROOT = Path(__file__).parents[1]
LOCK = ROOT / ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json"
GO_BEHAVIOR = "Preserve FlagSet.Value existing behavior."
GO_TARGET = "Add FlagSet.Names returning the registered names without changing Value."
GO_BASE = "package flags\ntype FlagSet struct{}\nfunc(f *FlagSet) Value()int{return 7}\n"
GO_GOOD = GO_BASE + 'func(f *FlagSet) Names()[]string{return []string{"a"}}\n'


def go_policy():
    return TaskBaselinePolicy(
        benchmark_revision="r06-go-public-revision-2",
        task_kind="feature",
        requirements=(requirement(GO_BEHAVIOR, "behavior"), requirement(GO_TARGET, "target")),
        behavior_plan=regression_plan(
            [("go", "test", "-buildvcs=false", "flag.go", "flag_test.go")]
        ),
        target_plan=regression_plan(
            [("go", "test", "-buildvcs=false", "flag.go", "feature_test.go")]
        ),
        missing_targets=(
            MissingTargetSymbol(
                acceptance_requirement_id=public_id(GO_TARGET),
                name="Names",
                receiver="FlagSet",
                reference_paths=("feature_test.go",),
            ),
        ),
    )


def go_project(path, variant="missing"):
    path.mkdir()
    (path / "go.mod").write_text("module example/r06\n\ngo 1.22\n")
    (path / "flag.go").write_text(GO_GOOD if variant == "green" else GO_BASE)
    (path / "flag_test.go").write_text(
        'package flags\nimport "testing"\n'
        'func TestValue(t *testing.T){if (&FlagSet{}).Value()!=7{t.Fatal("old behavior")}}\n'
    )
    target = 'package flags\nimport "testing"\nfunc TestNames(t *testing.T){\n'
    target += 'f:=&FlagSet{};if len(f.Names())!=1{t.Fatal("target")}\n}\n'
    if variant == "other-symbol":
        target = target.replace("f.Names()", "f.Unrelated()")
    elif variant == "syntax":
        target = target.replace("f:=&FlagSet{}", "f:=&&")
    elif variant == "dependency":
        target = target.replace(
            'import "testing"', 'import ("testing";_ "missing.invalid/dependency")'
        )
    elif variant == "mixed":
        target = target.replace("}\n}\n", "};_ = f.Unrelated()\n}\n")
    (path / "feature_test.go").write_text(target)
    return path


@pytest.mark.parametrize(
    "variant", ["missing", "green", "other-symbol", "syntax", "dependency", "mixed"]
)
def test_real_go_feature_distinguishes_declared_api_absence_from_environment_failure(
    tmp_path, variant
):
    project = go_project(tmp_path / "source", variant)
    artifacts = ArtifactStore(tmp_path / "cas")
    runner = IndependentRegressionRunner(
        artifacts, LOCK, repository="toy/go", task_id="r06-go", experiment_id="r06-go"
    )
    reference, assessment = execute_baseline(
        runner,
        go_policy(),
        project,
        tmp_path / "baseline",
        tree=h12_judge.tree_hash(project),
        frozen_tests=h12_judge.test_hashes(project),
    )
    assert assessment.status == ("PASS" if variant in {"missing", "green"} else "BLOCKED"), (
        assessment.model_dump()
    )
    if variant == "missing":
        assert assessment.target_observation == "DECLARED_TARGET_MISSING"
        report = IndependentRegressionReport.model_validate_json(
            artifacts.read(assessment.target_ref)
        )
        receipt = IndependentTestReceipt.model_validate_json(artifacts.read(report.receipt_refs[0]))
        assert receipt.exit_code == 1 and receipt.log_complete
        assert receipt.result.tests_run is None and receipt.result.tests_failed is None
    assert artifacts.read(reference) and not (project / "tests/hidden").exists()


@pytest.mark.parametrize("kind", ["refactor", "run_app"])
def test_original_behavior_baseline_never_requires_new_structure_or_service(
    tmp_path, monkeypatch, kind
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "box.py").write_text(BASE)
    (source / "tests").mkdir()
    (source / "tests/test_behavior.py").write_text(
        "from box import Box\ndef test_old():\n b=Box();b['AA']=7\n assert b['aa']==7\n"
    )
    values = refactor_policy().model_dump(mode="json")
    if kind == "run_app":
        values.update(task_kind="run_app", shared_helper=None)
        values["requirements"] = [
            requirement("New service must start and stop at final only.", "lifecycle").model_dump(
                mode="json"
            )
        ]
    policy = TaskBaselinePolicy.model_validate(values)

    def denied(*args, **kwargs):
        raise AssertionError("Baseline must not read or execute final private Judge")

    monkeypatch.setattr(h12_judge, "prepare_judge", denied)
    task = {
        "task_id": "r06-original",
        "repository": "toy/public",
        "type": kind,
        "judge_sha256": "b" * 64,
    }
    result = h12_judge.verify(
        source, task, LOCK, tmp_path / "proof", phase="baseline", baseline_policy=policy
    )
    assert result["status"] == "PASS", result
    assert result["baseline_assessment"]["target_observation"] == "NOT_RUN"
    assert "_fold" not in (source / "box.py").read_text()
    assert not (tmp_path / "proof/private").exists()


@pytest.mark.parametrize("red", [True, False])
def test_bugfix_original_target_must_actually_fail_assertion(tmp_path, red):
    source = tmp_path / "source"
    source.mkdir()
    (source / "api.py").write_text(f"value={1 if red else 2}\n")
    (source / "test_old.py").write_text("def test_old():assert True\n")
    (source / "test_goal.py").write_text("import api\ndef test_goal():assert api.value==2\n")
    policy = TaskBaselinePolicy(
        benchmark_revision="r06-bug-v2",
        task_kind="bugfix",
        requirements=(
            requirement("Fix api.value to equal 2 while old behavior stays valid.", "target"),
        ),
        behavior_plan=regression_plan([("python3", "-B", "-m", "pytest", "test_old.py")]),
        target_plan=regression_plan([("python3", "-B", "-m", "pytest", "test_goal.py")]),
    )
    runner = IndependentRegressionRunner(
        ArtifactStore(tmp_path / "cas"),
        LOCK,
        repository="toy/bug",
        task_id="r06-bug",
        experiment_id="r06-bug",
    )
    _, observed = execute_baseline(
        runner,
        policy,
        source,
        tmp_path / "proof",
        tree=h12_judge.tree_hash(source),
        frozen_tests=h12_judge.test_hashes(source),
    )
    assert (observed.status == "PASS") is red, observed.model_dump()
    assert observed.target_observation == ("ASSERTION_RED" if red else "GREEN")


def test_exact_public_target_parser_rejects_wrong_type_path_or_additional_error():
    error = (
        "./feature_test.go:4:8: f.Names undefined "
        "(type *FlagSet has no field or method Names)\nFAIL\n"
    )
    policy = go_policy()
    assert declared_missing_target(error, policy)
    for output in [
        error.replace("Names", "Unrelated"),
        error.replace("FlagSet", "OtherType"),
        error.replace("feature_test.go", "dependency_test.go"),
        error + "./flag.go:2:9: undefined: BrokenDependency\n",
        error + "go: downloading missing.invalid/pkg v1.0.0\n",
    ]:
        assert not declared_missing_target(output, policy)


def test_go_refactor_original_behavior_does_not_require_final_shared_writer(tmp_path, monkeypatch):
    project = go_project(tmp_path / "project")
    feature = go_policy()
    policy = TaskBaselinePolicy(
        benchmark_revision="r06-go-refactor-v2",
        task_kind="refactor",
        requirements=(feature.requirements[0],),
        behavior_plan=feature.behavior_plan,
    )

    def denied(*args, **kwargs):
        raise AssertionError("Final structure is not a baseline precondition")

    monkeypatch.setattr(h12_judge, "prepare_judge", denied)
    result = h12_judge.verify(
        project,
        {
            "task_id": "r06-go-refactor",
            "type": "refactor",
            "repository": "toy/go",
            "judge_sha256": "b" * 64,
        },
        LOCK,
        tmp_path / "proof",
        phase="baseline",
        baseline_policy=policy,
    )
    assert result["status"] == "PASS", result
    assert result["baseline_assessment"]["target_observation"] == "NOT_RUN"


def test_real_refactor_shared_helper_cannot_hide_a_changed_unicode_behavior(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "box.py").write_text(helper_source().replace("key.lower()", "key.casefold()"))
    (source / "tests").mkdir()
    (source / "tests/test_unicode.py").write_text(
        "from box import Box\ndef test_unicode_equivalence():\n"
        " b=Box();b['straße']=1;b['STRASSE']=2\n assert b['straße']==1 and b['strasse']==2\n"
    )
    policy = refactor_policy()
    assets = tmp_path / "assets"
    probe = freeze_structural_probe(policy.shared_helper, assets)
    runner = IndependentRegressionRunner(
        ArtifactStore(tmp_path / "cas"),
        LOCK,
        repository="toy/equivalence",
        task_id="r06-equivalence",
        experiment_id="r06-equivalence",
        public_probe_root=assets,
    )
    tree = h12_judge.tree_hash(source)
    frozen = h12_judge.test_hashes(source)
    _, behavior = runner.execute(
        source,
        policy.behavior_plan,
        tmp_path / "behavior",
        phase="baseline",
        expected_tree=tree,
        frozen_tests=frozen,
    )
    structure = RegressionPlan(
        commands=(
            RegressionCommand(
                suite_id="structure",
                public_argv=probe.argv,
                argv=probe.argv,
                framework="public-probe",
                public_probe=probe,
            ),
        )
    )
    _, actual_structure = runner.execute(
        source,
        structure,
        tmp_path / "structure",
        phase="final",
        expected_tree=tree,
        frozen_tests=frozen,
        sealed=True,
        role="structural_constraints",
        execution_task_id="r06-owned",
        run_id="r06-owned-run",
        execution_epoch=1,
    )
    assert actual_structure.status == "PASS" and behavior.status == "FAIL"
