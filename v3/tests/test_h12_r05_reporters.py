"""Fixed real Python/Node/Go reporters and incomplete-report counterexamples."""

import copy
import json
from pathlib import Path

import pytest

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.regression_types import DimensionReceipt, RegressionCommand, seal
from mgar_v3.harness.test_results import parse_independent_test
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_regression_scoring import load_report

ROOT = Path(__file__).parents[1]
LOCK = ROOT / ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json"
EXPRESS = Path(
    json.loads((ROOT / ".local/h12/public-dev-20261113/image-map.json").read_bytes())[
        "expressjs/express@8368dc178af16b91b576c4c1d135f701a0007e5d"
    ]
)


@pytest.mark.parametrize("language", ["unittest", "go", "node", "mocha", "ava"])
def test_actual_fixed_reporter_observes_every_test_and_failure(tmp_path, language):
    source = tmp_path / "source"
    source.mkdir()
    package = None
    if language == "unittest":
        files = {
            "test_public.py": "import unittest\nclass Public(unittest.TestCase):\n"
            " def test_green(self):self.assertEqual(1,1)\n"
            " def test_broken(self):self.assertEqual(1,2)\n"
        }
        argv = ("python3", "-B", "-m", "unittest", "-v", "test_public")
    elif language == "go":
        files = {
            "go.mod": "module example/r05\n\ngo 1.22\n",
            "calc.go": "package calc\n",
            "calc_test.go": 'package calc\nimport "testing"\n'
            'func TestGreen(t *testing.T){}\nfunc TestBroken(t *testing.T){t.Fatal("broken")}\n',
        }
        argv = ("go", "test", "-v", "-buildvcs=false", "./...")
    elif language == "node":
        files = {
            "test_public.cjs": "const test=require('node:test');"
            "const a=require('node:assert/strict');"
            "test('green',()=>a.equal(1,1));test('broken',()=>a.equal(1,2));\n"
        }
        argv = ("node", "--test", "test_public.cjs")
    elif language == "mocha":
        package = {
            "name": "r05-mocha",
            "version": "1.0.0",
            "scripts": {"test": "mocha test_public.js"},
        }
        files = {
            "package.json": json.dumps(package),
            "test_public.js": "var a=require('assert');"
            "describe('public',function(){it('green',function(){a.equal(1,1);});"
            "it('broken',function(){a.equal(1,2);});});\n",
        }
        argv = ("npm", "test")
    else:
        package = {
            "name": "r05-ava",
            "version": "1.0.0",
            "type": "module",
            "ava": {"cache": False},
            "scripts": {"test": "ava --serial test_public.mjs"},
        }
        files = {
            "package.json": json.dumps(package),
            "test_public.mjs": "import test from 'ava';\n"
            "test('green',t=>t.is(1,1));test('broken',t=>t.is(1,2));\n",
        }
        argv = ("npm", "test", "--", "--config=/opt/h12/ava-config.mjs")
    for name, body in files.items():
        (source / name).write_text(body)
    plan = regression_plan([argv], package=package)
    tree = _tree_hash(_scan(source)[0])
    store = ArtifactStore(tmp_path / "cas")
    runner = IndependentRegressionRunner(
        store,
        EXPRESS if language == "mocha" else LOCK,
        repository="example/reporters",
        task_id="r05-" + language,
        experiment_id="controlled",
        memory_mb=1024,
        nofile=1024,
    )
    ref, report = runner.execute(
        source, plan, tmp_path / "proof", phase="baseline", expected_tree=tree, frozen_tests={}
    )
    assert report.status == "FAIL", report.model_dump()
    _, receipts = load_report(
        store,
        ref,
        plan,
        task_id="r05-" + language,
        experiment_id="controlled",
        phase="baseline",
        tree=tree,
    )
    observed = receipts[0].result
    assert observed.parsed and observed.complete, observed.model_dump()
    assert observed.tests_run == 2 and observed.tests_failed == 1 and observed.tests_skipped == 0
    assert len(observed.failure_ids) == 1 and "broken" in observed.failure_ids[0].lower()
    assert _tree_hash(_scan(source)[0]) == tree


PYTEST = "collected 2 items\ntests/test_public.py::test_green PASSED [ 50%]\n"
PYTEST += "tests/test_public.py::test_broken FAILED [100%]\n1 failed, 1 passed in 0.01s\n"


@pytest.mark.parametrize(
    "variant",
    [
        "summary-only",
        "collected-more",
        "missing-identity",
        "duplicate-identity",
        "skipped",
        "fake-counter",
        "truncated",
        "wrong-exit",
        "undeclared-deselection",
    ],
)
def test_incomplete_or_excluded_pytest_cannot_be_complete_pass(variant):
    command = regression_plan([("python3", "-m", "pytest", "tests/")]).commands[0]
    output = PYTEST
    complete = True
    code = 1
    if variant == "summary-only":
        output = "2 passed in 0.01s\n"
        code = 0
    elif variant == "collected-more":
        output = output.replace("collected 2", "collected 3")
    elif variant == "missing-identity":
        output = output.replace("tests/test_public.py::test_green PASSED [ 50%]\n", "")
    elif variant == "duplicate-identity":
        output = output.replace("test_broken", "test_green")
    elif variant == "skipped":
        output = output.replace("FAILED", "SKIPPED").replace(
            "1 failed, 1 passed", "1 skipped, 1 passed"
        )
        code = 0
    elif variant == "fake-counter":
        output = output.replace("1 failed, 1 passed", "2 passed")
        code = 0
    elif variant == "truncated":
        complete = False
    elif variant == "wrong-exit":
        code = 2
    elif variant == "undeclared-deselection":
        output = output.replace(
            "collected 2 items", "collected 3 items / 1 deselected / 2 selected"
        )
    result = parse_independent_test(command, output, code, complete=complete)
    assert result.status != "PASS" and not result.complete


def test_public_scripts_are_frozen_and_new_script_can_be_added():
    package = {"scripts": {"test": "mocha tests/", "build": "node build.js"}}
    plan = regression_plan([("npm", "test")], package=package)
    assert plan.commands[0].framework == "mocha-json"
    assert plan.package_scripts == package["scripts"]
    assert plan.commands[0].public_argv == ("npm", "test")
    assert "--no-bail" in plan.commands[0].argv
    assert "--reporter" in plan.commands[0].argv


def test_unconfigured_script_reporter_stays_explicitly_unverified():
    command = regression_plan([("npm", "test")]).commands[0]
    assert command.framework == "unconfigured"
    result = parse_independent_test(command, "2 passing\n", 0, complete=True)
    assert result.status == "BLOCKED" and result.tests_failed is None


def test_explicit_go_repeat_count_is_not_silently_weakened():
    plan = regression_plan([("go", "test", "-count=2", "./...")])
    assert "-count=2" in plan.commands[0].argv and "-count=1" not in plan.commands[0].argv


@pytest.mark.parametrize("collection_prefix", ["", "collecting ... "])
def test_complete_pytest_failure_has_ids_not_only_counts(collection_prefix):
    command = RegressionCommand(
        suite_id="public",
        public_argv=("python3", "-m", "pytest", "tests/"),
        argv=("python3", "-m", "pytest", "tests/", "--verbosity=2"),
        framework="pytest",
    )
    result = parse_independent_test(command, collection_prefix + PYTEST, 1, complete=True)
    assert (
        result.status == "FAIL"
        and result.complete
        and result.failure_ids == ("tests/test_public.py::test_broken",)
    )


def test_ava_extra_runner_error_cannot_be_mistaken_for_finished_assertions():
    command = regression_plan([("ava", "tests/")]).commands[0]
    output = (
        "TAP version 13\nok 1 - green\nnot ok 2 - broken\n"
        "not ok 3 - Error: cannot create readonly runner cache\n"
        "1..2\n# tests 2\n# pass 1\n# fail 2\n"
    )
    result = parse_independent_test(command, output, 1, complete=True)
    assert not result.complete and not result.parsed and result.tests_failed is None


@pytest.mark.parametrize(
    "variant", ["valid", "dimensions-missing", "seal", "tree", "required", "count-missing", "null"]
)
def test_new_summary_requires_independent_dimensions_and_never_totals_unknown_as_zero(variant):
    from mgar_v3.quality_eval import h12

    tasks = h12.load_frozen_tasks("dev")
    task = tasks[0]
    dimensions = {}
    for kind in (
        "target_behavior",
        "public_regression",
        "independent_judge",
        "structural_constraints",
        "execution_integrity",
    ):
        required = kind != "structural_constraints"
        dimensions[kind] = seal(
            DimensionReceipt,
            dict(
                kind=kind,
                required=required,
                status="PASS" if required else "NOT_RUN",
                tree_hash="a" * 64,
                evidence_refs=("b" * 64,) if required else (),
                detail="toy",
            ),
        ).model_dump(mode="json")
    row = dict(
        task_id=task["task_id"],
        type=task["type"],
        split="dev",
        status="PASS",
        raw_runtime_status="completed",
        judge_status="PASS",
        final_tree="a" * 64,
        judge_tree="a" * 64,
        cost_micros=0,
        regression_evidence_known=True,
        new_regression_failures=0,
        scoring_protocol="harness-independent-regression-v1",
        independent_public_regression_status="PASS",
        dimensions=dimensions,
    )
    if variant == "dimensions-missing":
        row.pop("dimensions")
    elif variant in {"seal", "tree", "required"}:
        dim = row["dimensions"]["public_regression"]
        if variant == "seal":
            dim["receipt_id"] = "0" * 64
        else:
            fields = copy.deepcopy(dim)
            fields.pop("receipt_id")
            fields.update({"tree_hash": "f" * 64} if variant == "tree" else {"required": False})
            row["dimensions"]["public_regression"] = seal(DimensionReceipt, fields).model_dump(
                mode="json"
            )
    elif variant == "count-missing":
        row.pop("new_regression_failures")
    elif variant == "null":
        row.update(new_regression_failures=None, regression_evidence_known=False)
    original = copy.deepcopy(row)
    summary = h12.score_records([row], expected_tasks=tasks)
    assert summary["pass_count"] == (1 if variant == "valid" else 0)
    if variant in {"null", "count-missing"}:
        assert summary["new_regression_failures"] is None
        assert summary["verified_new_regression_failures_subtotal"] == 0
    assert row == original
