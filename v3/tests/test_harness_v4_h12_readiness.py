"""The readiness gate uses actual public receipts and never pays to discover setup errors."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_readiness import (
    command_counts,
    declared_reference_assets,
    execution_readiness,
    reference_failures_reproduced,
    reproduce_public_reference_baseline,
)
from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V3, EvaluationRunner, profile_for
from mgar_v3.quality_eval.harness_v4 import CatalogError


def test_metadata_success_does_not_claim_that_a_single_command_executed() -> None:
    result = h12.preflight("dev")
    assert result["status"] in {"SOURCE_READY", "NOT_RUN"}
    assert result["execution_readiness"] == "NOT_CHECKED"
    assert all(row["judge_execution"] == "NOT_RUN" for row in result["tasks"])


def test_new_environment_limit_is_explicit_and_cannot_be_applied_to_exposed_holdout() -> None:
    task = h12.load_frozen_tasks("dev")[0]
    original = profile_for(task, "original", "candidate")
    assert original.sandbox_nofile == 128
    assert "sandbox_nofile" not in original.model_dump(mode="json")
    assert "sandbox_log_bytes" not in original.model_dump(mode="json")
    assert "sandbox_memory_mb" not in original.model_dump(mode="json")
    amended = profile_for(task, "new", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    assert amended.model_dump(mode="json")["sandbox_nofile"] == 1024
    assert amended.model_dump(mode="json")["sandbox_log_bytes"] == 1048576
    with pytest.raises(CatalogError, match="quarantined"):
        profile_for(
            {**task, "split": "holdout"},
            "new",
            "candidate",
            environment_revision=DEV_ENVIRONMENT_V3,
        )


def test_got_cache_and_memory_amendment_is_fixed_to_new_dev_environment() -> None:
    task = next(
        t for t in h12.load_frozen_tasks("dev") if t["task_id"] == "hv4-dev-got-local-roundtrip"
    )
    old = profile_for(task, "old", "candidate")
    new = profile_for(task, "new", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    assert old.public_test_argv == ("npm", "test") and old.sandbox_memory_mb == 256
    assert new.public_test_argv == ("npm", "test", "--", "--no-cache")
    assert new.sandbox_memory_mb == 1024
    assert h12.public_commands(task)[-1] == ["npm", "test"]
    other = {**task, "repository": "synthetic/other"}
    unchanged = profile_for(other, "new", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    assert unchanged.public_test_argv == ("npm", "test") and unchanged.sandbox_memory_mb == 256


def test_python_application_plans_require_public_unambiguous_commands() -> None:
    task = next(t for t in h12.load_frozen_tasks("dev") if t["type"] == "run_app")
    base = {
        **task,
        "language": "python",
        "scope": ["app.py"],
        "goal": "Start a server on default port 4321 serving GET /healthz.",
    }
    http = profile_for(base, "synthetic", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    assert http.service_argv == ("python3", "-B", "app.py")
    assert (http.service_port, http.service_path) == (4321, "/healthz")
    cli = {
        **base,
        "goal": "Run the CLI selfcheck; it exits 0 on success.",
        "environment": {"verify": ["python3 app.py --check"]},
    }
    one = profile_for(cli, "synthetic", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    assert one.service_mode == "oneshot" and one.service_argv == (
        "python3",
        "-B",
        "app.py",
        "--check",
    )
    module = {**cli, "environment": {"verify": ["python -m package.selfcheck"]}}
    assert profile_for(
        module, "synthetic", "candidate", environment_revision=DEV_ENVIRONMENT_V3
    ).service_argv == (
        "python3",
        "-B",
        "-m",
        "package.selfcheck",
    )
    ambiguous = profile_for(
        {**base, "scope": ["app.py", "other.py"]},
        "synthetic",
        "candidate",
        environment_revision=DEV_ENVIRONMENT_V3,
    )
    assert not ambiguous.service_argv


def test_complete_hashed_logs_are_required_for_public_counts(tmp_path: Path) -> None:
    log = tmp_path / "pytest.log"
    raw = b"1 failed, 7 passed, 2 skipped in 0.10s\n"
    log.write_bytes(raw)
    receipt = {
        "private_log": str(log),
        "log_sha256": hashlib.sha256(raw).hexdigest(),
        "exit_code": 1,
        "complete": True,
    }
    counted = command_counts({"receipts": [receipt]}, "python")[0]
    assert counted["executed"] >= 8 and counted["failed"] == 1
    log.write_text("0 failed, 1000 passed\n")
    with pytest.raises(CatalogError, match="identity"):
        command_counts({"receipts": [receipt]}, "python")


def test_missing_image_matrix_checks_all_twelve_before_any_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mgar_v3.quality_eval.h12_readiness.runtime_probe",
        lambda *a, **kw: pytest.fail("missing image must not launch a process"),
    )
    report = execution_readiness({}, tmp_path / "matrix")
    assert report["status"] == "NOT_RUN" and report["not_run_count"] == 12
    assert report["paid_model_calls"] == 0 and report["cost_micros"] == 0
    assert len(report["tasks"]) == 12
    assert all(
        "FROZEN_PROJECT_ENVIRONMENT_UNAVAILABLE" in row["blockers"] for row in report["tasks"]
    )
    assert len(list((tmp_path / "matrix").glob("*/readiness.json"))) == 12


def test_single_task_cannot_spend_before_the_full_readiness_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = EvaluationRunner(
        SimpleNamespace(harness=None),
        experiment_id="new-candidate",
        evidence=tmp_path,
        image_locks={},
        api_key="never-sent",
        execute_live=True,
        environment_revision=DEV_ENVIRONMENT_V3,
    )
    monkeypatch.setattr(
        "mgar_v3.quality_eval.h12_readiness.execution_readiness",
        lambda *a, **kw: {"status": "NOT_RUN", "ready_count": 11},
    )
    monkeypatch.setattr(
        runner, "_source", lambda *a: pytest.fail("dispatch must stop before task execution")
    )
    with pytest.raises(DomainError) as caught:
        runner.evaluate_task(h12.load_frozen_tasks("dev")[0], "candidate")
    assert caught.value.code == "H12_DEV_EXECUTION_NOT_READY"


def _public_reference(tmp_path: Path):
    workspace = tmp_path / "project"
    asset = workspace / "tests/test_reference_public.py"
    asset.parent.mkdir(parents=True)
    asset.write_text("# original public assertions for the missing placeholder parameter\n")
    task = {
        "task_id": "synthetic-reference",
        "split": "dev",
        "type": "feature",
        "language": "python",
        "judge": {"kind": "v35_reference", "reference_task": "original-reference"},
        "judge_sha256": "a" * 64,
        "acceptance": {"baseline": ["placeholder parameter does not exist"]},
    }
    source = {
        "public_files": [
            {
                "target": "tests/test_reference_public.py",
                "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
            }
        ]
    }
    log = tmp_path / "public-original.log"
    log.write_text(
        "FAILED tests/test_reference_public.py::test_placeholder - TypeError\n2 failed in 0.01s\n"
    )
    result = {
        "status": "PASS",
        "baseline_test_status": "FAIL",
        "tree_hash": h12_judge.tree_hash(workspace),
        "judge_sha256": task["judge_sha256"],
        "receipts": [
            {
                "private_log": str(log),
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "state": "failed",
                "exit_code": 1,
                "complete": True,
            }
        ],
    }
    return workspace, task, source, result


def test_expected_red_feature_requires_frozen_assets_and_independent_public_replay(
    tmp_path, monkeypatch
):
    workspace, task, source, observed = _public_reference(tmp_path)
    calls = []

    def replay(project, original, image, evidence, **kwargs):
        assert original is task and original["type"] == "feature"
        assert kwargs["phase"] == "environment"
        evidence.mkdir()
        calls.append(kwargs)
        return observed

    monkeypatch.setattr(h12_judge, "verify", replay)
    proof = reproduce_public_reference_baseline(
        workspace,
        task,
        source,
        tmp_path / "lock.json",
        tmp_path / "replay",
        memory_mb=256,
        observed=observed,
    )
    assert proof["status"] == "PASS" and len(calls) == 1
    assert proof["reason"] == "DECLARED_PUBLIC_REFERENCE_BASELINE_REPRODUCED"
    assert proof["paid_model_calls"] == 0


def test_reference_claim_does_not_allow_unrelated_failures(tmp_path, monkeypatch):
    workspace, task, source, observed = _public_reference(tmp_path)
    log = Path(observed["receipts"][0]["private_log"])
    log.write_text(
        "FAILED tests/test_unrelated.py::test_original - AssertionError\n2 failed in 0.01s\n"
    )
    observed["receipts"][0]["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
    monkeypatch.setattr(
        h12_judge, "verify", lambda *a, **kw: pytest.fail("unrelated red must stop before replay")
    )
    proof = reproduce_public_reference_baseline(
        workspace,
        task,
        source,
        tmp_path / "lock.json",
        tmp_path / "replay",
        memory_mb=256,
        observed=observed,
    )
    assert proof["status"] == "NOT_RUN" and proof["reason"] == "UNRELATED_PUBLIC_REGRESSION_FAILURE"


def test_reference_inventory_and_frozen_missing_declaration_are_both_required(tmp_path):
    workspace, task, source, _ = _public_reference(tmp_path)
    assert declared_reference_assets(workspace, task, source)
    assert not declared_reference_assets(
        workspace,
        {**task, "acceptance": {"baseline": ["existing behavior corpus is recorded"]}},
        source,
    )
    assert not declared_reference_assets(
        workspace, {**task, "judge": {"kind": "h12_spec_implementation"}}, source
    )
    (workspace / "tests/test_reference_public.py").write_text("# changed assertions\n")
    with pytest.raises(CatalogError, match="asset changed"):
        declared_reference_assets(workspace, task, source)


@pytest.mark.parametrize(
    "change", ["incomplete", "zero_exit", "missing_summary", "collection_error"]
)
def test_reference_baseline_requires_actual_complete_assertion_failure(tmp_path, change):
    workspace, task, source, observed = _public_reference(tmp_path)
    receipt = observed["receipts"][0]
    if change == "incomplete":
        receipt["complete"] = False
    elif change == "zero_exit":
        receipt["exit_code"], receipt["state"] = 0, "exited"
    else:
        log = Path(receipt["private_log"])
        log.write_text(
            "ERROR collecting tests/test_reference_public.py\n1 error in 0.01s\n"
            if change == "collection_error"
            else "process failed without any executed assertion\n"
        )
        receipt["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
    assert not reference_failures_reproduced(
        observed, declared_reference_assets(workspace, task, source)
    )
