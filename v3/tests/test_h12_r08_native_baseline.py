"""Actual language runners expose diagnostics separately from passing test names."""

import json

from test_h12_r05_reporters import EXPRESS
from test_h12_r06_baseline import LOCK, go_policy, go_project

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.regression_types import IndependentTestReceipt
from mgar_v3.quality_eval import h12_judge
from mgar_v3.quality_eval.h12_baseline_policy import execute_baseline
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_plan import regression_plan


def test_real_go_passing_error_named_tests_are_not_infrastructure_failures(tmp_path):
    source = go_project(tmp_path / "source", "green")
    p = source / "flag_test.go"
    p.write_text(p.read_text().replace("TestValue", "TestInvalidSyntaxError"))
    runner = IndependentRegressionRunner(
        ArtifactStore(tmp_path / "cas"),
        LOCK,
        repository="toy/go",
        task_id="r08-native",
        experiment_id="r08-native",
    )
    _, assessment = execute_baseline(
        runner,
        go_policy(),
        source,
        tmp_path / "proof",
        tree=h12_judge.tree_hash(source),
        frozen_tests=h12_judge.test_hashes(source),
    )
    assert assessment.status == "PASS" and assessment.target_observation == "GREEN"


def test_actual_go_compile_diagnostic_still_blocks_the_feature_baseline(tmp_path):
    source = go_project(tmp_path / "source", "syntax")
    runner = IndependentRegressionRunner(
        ArtifactStore(tmp_path / "cas"),
        LOCK,
        repository="toy/go",
        task_id="r08-compile",
        experiment_id="r08-compile",
    )
    _, assessment = execute_baseline(
        runner,
        go_policy(),
        source,
        tmp_path / "proof",
        tree=h12_judge.tree_hash(source),
        frozen_tests=h12_judge.test_hashes(source),
    )
    assert assessment.status == "BLOCKED"


def test_real_pytest_existing_exceptions_keep_non_pass_cases_in_cas(tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "test_cases.py").write_text(
        "import pytest\ndef test_active():assert 1==1\n"
        "@pytest.mark.skip(reason='the frozen different-platform case')\n"
        "def test_other_platform():assert False\n"
        "@pytest.mark.xfail(strict=True,reason='upstream known limitation')\n"
        "def test_known_limitation():assert 1==2\n"
    )
    plan = regression_plan([("python3", "-B", "-m", "pytest", "test_cases.py")])
    command = plan.commands[0].model_copy(
        update={
            "expected_exceptions": {
                "test_cases.py::test_other_platform": "skipped",
                "test_cases.py::test_known_limitation": "xfailed",
            }
        }
    )
    plan = plan.model_copy(update={"commands": (command,)})
    artifacts = ArtifactStore(tmp_path / "cas")
    runner = IndependentRegressionRunner(
        artifacts, LOCK, repository="toy/pytest", task_id="r08-nonpass", experiment_id="r08-nonpass"
    )
    _, report = runner.execute(
        source,
        plan,
        tmp_path / "proof",
        phase="baseline",
        expected_tree=h12_judge.tree_hash(source),
        frozen_tests=h12_judge.test_hashes(source),
    )
    assert report.status == "PASS"
    receipt = IndependentTestReceipt.model_validate_json(artifacts.read(report.receipt_refs[0]))
    assert receipt.result.tests_run == 3 and receipt.result.tests_skipped == 1
    assert receipt.result.tests_xfailed == 1
    assert [c.outcome for c in receipt.result.cases] == ["passed", "skipped", "xfailed"]
    assert receipt.log_complete and not receipt.log_truncated
    assert receipt.diagnostics is not None
    assert receipt.diagnostics.sample_count >= 1
    assert receipt.diagnostics.stop_reason == "natural_exit"
    assert receipt.diagnostics.last.container_id == receipt.diagnostics.first.container_id


def test_real_mocha_duplicate_titles_preserve_failure_identity_and_count(tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    package = {"name": "r08-mocha", "version": "1.0.0", "scripts": {"test": "mocha test_same.js"}}
    (source / "package.json").write_text(json.dumps(package))
    (source / "test_same.js").write_text(
        "var a=require('assert');describe('same',function(){"
        "it('title',function(){a.equal(1,1);});it('title',function(){a.equal(1,2);});});\n"
    )
    plan = regression_plan([("npm", "test")], package=package)
    plan = plan.model_copy(
        update={
            "commands": (
                plan.commands[0].model_copy(
                    update={"identity_policy": "mocha-file-title-occurrence-v1"}
                ),
            )
        }
    )
    artifacts = ArtifactStore(tmp_path / "cas")
    runner = IndependentRegressionRunner(
        artifacts,
        EXPRESS,
        repository="toy/mocha",
        task_id="r08-duplicate",
        experiment_id="r08-duplicate",
        memory_mb=1024,
        nofile=1024,
    )
    _, report = runner.execute(
        source,
        plan,
        tmp_path / "proof",
        phase="baseline",
        expected_tree=h12_judge.tree_hash(source),
        frozen_tests=h12_judge.test_hashes(source),
    )
    assert report.status == "FAIL"
    receipt = IndependentTestReceipt.model_validate_json(artifacts.read(report.receipt_refs[0]))
    assert receipt.result.complete and receipt.result.tests_run == 2
    assert receipt.result.tests_failed == 1
    assert len(receipt.result.failure_ids) == 1
    assert receipt.result.failure_ids[0].endswith("::occurrence-2")
