"""Real immutable source copies and fixed containers, with no model transport."""

import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.harness.regression_types import (
    IndependentRegressionReport,
    IndependentTestReceipt,
    seal,
)
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_regression_scoring import compare_reports, load_report

LOCK = (
    Path(__file__).parents[1]
    / ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json"
)
BASE = "def goal():return 2\ndef old_name():return 7\n"
FINAL = "def goal():return 2\n"
TESTS = """import api
def test_goal():
 assert api.goal()==2
def test_light_imports():
 from api import old_name
 assert old_name()==7
"""
SCOPE = dict(execution_task_id="r05-controlled-task", run_id=str(uuid4()), execution_epoch=1)


def tree(path):
    return _tree_hash(_scan(path)[0])


def create_pair(root, *, tests=TESTS, before=BASE, after=FINAL, commands=None, timeout=300):
    baseline = root / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "api.py").write_text(before)
    (baseline / "tests").mkdir()
    (baseline / "tests/test_public.py").write_text(tests)
    final = root / "final"
    shutil.copytree(baseline, final)
    (final / "api.py").write_text(after)
    plan = regression_plan(commands or [("python3", "-B", "-m", "pytest", "tests/")])
    plan = plan.model_copy(update={"timeout_seconds": timeout})
    artifacts = ArtifactStore(root / "cas")
    runner = IndependentRegressionRunner(
        artifacts, LOCK, repository="fixture/r05", task_id="toy-r05", experiment_id="toy-r05"
    )
    frozen = {p: h for p, h in _scan(baseline)[0].items() if p.startswith("tests/")}
    old, old_report = runner.execute(
        baseline,
        plan,
        root / "before-evidence",
        phase="baseline",
        expected_tree=tree(baseline),
        frozen_tests=frozen,
    )
    new, new_report = runner.execute(
        final,
        plan,
        root / "after-evidence",
        phase="final",
        expected_tree=tree(final),
        frozen_tests=frozen,
        sealed=True,
        **SCOPE,
    )
    return SimpleNamespace(
        root=root,
        baseline=baseline,
        final=final,
        plan=plan,
        artifacts=artifacts,
        runner=runner,
        frozen=frozen,
        old=old,
        new=new,
        old_report=old_report,
        new_report=new_report,
    )


@pytest.fixture(scope="module")
def public_pair(tmp_path_factory):
    return create_pair(tmp_path_factory.mktemp("r05-public-pair"))


def compare(pair, **changes):
    values = dict(
        baseline_ref=pair.old,
        final_ref=pair.new,
        baseline_tree=tree(pair.baseline),
        final_tree=tree(pair.final),
        task_id="toy-r05",
        experiment_id="toy-r05",
        **SCOPE,
    )
    values.update(changes)
    return compare_reports(pair.artifacts, pair.plan, **values)


def clone(pair, tmp_path):
    shutil.copytree(pair.artifacts.root, tmp_path / "cas")
    return SimpleNamespace(**(vars(pair) | {"artifacts": ArtifactStore(tmp_path / "cas")}))


def test_actual_target_green_cannot_hide_light_imports_full_regression(public_pair):
    pair = public_pair
    assert pair.old_report.status == "PASS", pair.old_report.detail
    assert pair.new_report.status == "FAIL", pair.new_report.detail
    value = compare(pair)
    assert value.comparable and value.new_regression_failures == 1, value.model_dump()
    assert value.final_regression_status == "FAIL" and value.status == "FAIL"
    assert any("test_light_imports" in name for name in value.final_failures)
    _, receipts = load_report(
        pair.artifacts,
        pair.new,
        pair.plan,
        task_id="toy-r05",
        experiment_id="toy-r05",
        phase="final",
        tree=tree(pair.final),
        **SCOPE,
    )
    assert {c.outcome for c in receipts[0].result.cases if "test_goal" in c.test_id} == {"passed"}
    assert {c.outcome for c in receipts[0].result.cases if "test_light_imports" in c.test_id} == {
        "failed"
    }
    assert pair.artifacts.read(receipts[0].log_hash)
    assert (
        tree(pair.baseline) == pair.old_report.tree_hash
        and tree(pair.final) == pair.new_report.tree_hash
    )


@pytest.mark.parametrize(
    "variant",
    [
        "missing-before",
        "missing-after",
        "source-tree",
        "run",
        "epoch",
        "log-missing",
        "log-tamper",
        "receipt-count",
        "scope-argv",
        "fake-count",
        "skipped",
        "manifest-tree",
        "foreign-producer",
    ],
)
def test_unverified_or_tampered_receipts_cannot_claim_zero(public_pair, tmp_path, variant):
    pair = clone(public_pair, tmp_path)
    changes = {}
    if variant == "missing-before":
        changes["baseline_ref"] = None
    elif variant == "missing-after":
        changes["final_ref"] = None
    elif variant == "source-tree":
        changes["final_tree"] = "f" * 64
    elif variant == "run":
        changes["run_id"] = str(uuid4())
    elif variant == "epoch":
        changes["execution_epoch"] = 0
    else:
        report = IndependentRegressionReport.model_validate_json(pair.artifacts.read(pair.new))
        receipt = IndependentTestReceipt.model_validate_json(
            pair.artifacts.read(report.receipt_refs[0])
        )
        if variant == "log-missing":
            pair.artifacts.path(receipt.log_hash).unlink()
        elif variant == "log-tamper":
            pair.artifacts.path(receipt.log_hash).write_text("2 passed\n")
        else:
            values = report.model_dump(exclude={"receipt_id"})
            if variant == "receipt-count":
                values["receipt_refs"] = ()
            elif variant == "manifest-tree":
                values["observed_tree_after"] = "f" * 64
            elif variant == "foreign-producer":
                values["experiment_id"] = "other"
            else:
                fields = receipt.model_dump(exclude={"receipt_id"})
                if variant == "scope-argv":
                    fields["argv"] = ("python3", "-m", "pytest", "tests/test_target.py")
                elif variant == "fake-count":
                    fields["result"]["tests_failed"] = 0
                elif variant == "skipped":
                    fields["result"]["tests_skipped"] = 1
                altered = seal(IndependentTestReceipt, fields)
                values["receipt_refs"] = (pair.artifacts.put(altered.model_dump_json().encode()),)
            altered_report = seal(IndependentRegressionReport, values)
            changes["final_ref"] = pair.artifacts.put(altered_report.model_dump_json().encode())
    result = compare(pair, **changes)
    assert result.new_regression_failures is None and not result.comparable
    assert result.status != "PASS"


def test_repeated_scoring_is_identical_and_does_not_mutate_evidence(public_pair):
    before = {str(p): p.read_bytes() for p in public_pair.artifacts.root.rglob("*") if p.is_file()}
    first = compare(public_pair)
    second = compare(public_pair)
    assert first == second
    assert before == {
        str(p): p.read_bytes() for p in public_pair.artifacts.root.rglob("*") if p.is_file()
    }


def test_known_independent_report_is_reused_without_relaunch(public_pair, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Known evidence must not launch again")

    monkeypatch.setattr(IsolatedExecutionBroker, "launch", denied)
    ref, report = public_pair.runner.execute(
        public_pair.final,
        public_pair.plan,
        public_pair.root / "after-evidence",
        phase="final",
        expected_tree=tree(public_pair.final),
        frozen_tests=public_pair.frozen,
        sealed=True,
        **SCOPE,
    )
    assert ref == public_pair.new and report == public_pair.new_report


def test_unconfirmed_dispatch_is_not_retried(public_pair, tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unknown dispatch must not be retried")

    monkeypatch.setattr(IsolatedExecutionBroker, "launch", denied)
    evidence = tmp_path / "unknown"
    evidence.mkdir()
    (evidence / "dispatch.json").write_text("{}")
    _, report = public_pair.runner.execute(
        public_pair.final,
        public_pair.plan,
        evidence,
        phase="final",
        expected_tree=tree(public_pair.final),
        frozen_tests=public_pair.frozen,
        sealed=True,
        **SCOPE,
    )
    assert report.status == "BLOCKED" and not report.receipt_refs


def test_baseline_failure_is_not_new_but_remaining_final_failure_still_fails(tmp_path):
    pair = create_pair(tmp_path, before=FINAL, after=FINAL)
    result = compare(pair)
    assert result.comparable and result.new_regression_failures == 0, result.model_dump()
    assert result.baseline_failures == result.final_failures and result.final_failures
    assert result.status == "FAIL" and result.final_regression_status == "FAIL"


def test_full_green_scopes_comparable_zero(tmp_path):
    pair = create_pair(tmp_path, after=BASE)
    result = compare(pair)
    assert result.status == "PASS" and result.comparable and result.new_regression_failures == 0


def test_partial_skip_does_not_prove_full_regression(tmp_path):
    tests = TESTS.replace(
        "def test_goal():",
        "import pytest\n@pytest.mark.skip(reason='not executed')\ndef test_goal():",
    )
    pair = create_pair(tmp_path, tests=tests, after=BASE)
    result = compare(pair)
    assert pair.new_report.status == "BLOCKED"
    assert not result.comparable and result.new_regression_failures is None


def test_every_frozen_command_runs_even_after_test_failure(tmp_path):
    pair = create_pair(
        tmp_path,
        before=FINAL,
        after=FINAL,
        commands=[
            ("python3", "-B", "-m", "pytest", "tests/"),
            ("python3", "-B", "-m", "pytest", "tests/", "-k", "test_goal"),
        ],
    )
    _, values = load_report(
        pair.artifacts,
        pair.new,
        pair.plan,
        task_id="toy-r05",
        experiment_id="toy-r05",
        phase="final",
        tree=tree(pair.final),
        **SCOPE,
    )
    assert (
        len(values) == 2 and values[0].result.status == "FAIL" and values[1].result.status == "PASS"
    )
    assert pair.new_report.status == "FAIL"


def test_no_seal_never_executes_or_claims_final_pass(public_pair, tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unsealed final source may not execute")

    monkeypatch.setattr(IsolatedExecutionBroker, "launch", denied)
    _, report = public_pair.runner.execute(
        public_pair.final,
        public_pair.plan,
        tmp_path / "not-sealed",
        phase="final",
        expected_tree=tree(public_pair.final),
        frozen_tests=public_pair.frozen,
        **SCOPE,
    )
    assert report.status == "NOT_RUN" and not report.receipt_refs


def test_actual_equal_failure_counts_compare_identities_not_subtraction(tmp_path):
    pair = create_pair(
        tmp_path,
        before="flag=False\n",
        after="flag=True\n",
        tests="import api\ndef test_old():assert api.flag\ndef test_new():assert not api.flag\n",
    )
    result = compare(pair)
    assert len(result.baseline_failures) == len(result.final_failures) == 1
    assert result.comparable and result.new_regression_failures == 1 and result.status == "FAIL"


def test_actual_dynamic_collection_change_makes_scopes_incomparable(tmp_path):
    pair = create_pair(
        tmp_path,
        before="cases=[1,2]\n",
        after="cases=[1,2,3]\n",
        tests="import api,pytest\n@pytest.mark.parametrize('value',api.cases)\n"
        "def test_positive(value):assert value>0\n",
    )
    assert pair.old_report.status == pair.new_report.status == "PASS"
    result = compare(pair)
    assert result.status == "BLOCKED" and result.new_regression_failures is None


def test_actual_timeout_keeps_other_frozen_suites_and_unknown_counts(tmp_path):
    pair = create_pair(
        tmp_path,
        after=BASE,
        tests=TESTS + "def test_hangs():\n import time\n time.sleep(20)\n",
        commands=[
            ("python3", "-B", "-m", "pytest", "tests/"),
            ("python3", "-B", "-m", "pytest", "tests/", "-k", "test_goal"),
        ],
        timeout=3,
    )
    assert pair.new_report.status == "BLOCKED" and len(pair.new_report.receipt_refs) == 2
    receipts = [
        IndependentTestReceipt.model_validate_json(pair.artifacts.read(h))
        for h in pair.new_report.receipt_refs
    ]
    assert receipts[0].state == "timed_out" and receipts[0].result.tests_failed is None
    assert receipts[1].result.status == "PASS"
    assert compare(pair).new_regression_failures is None
