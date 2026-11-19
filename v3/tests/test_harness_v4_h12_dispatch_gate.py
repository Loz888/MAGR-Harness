"""Paid entrypoints cannot omit readiness or reopen the historical holdout."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_harness_v4_h12_budget import _freeze

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_live import MODEL_OBSERVATION
from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V3, EvaluationRunner, profile_for


def test_paid_constructor_requires_explicit_new_revision(tmp_path):
    with pytest.raises(DomainError) as caught:
        EvaluationRunner(
            SimpleNamespace(harness=None), experiment_id="synthetic", evidence=tmp_path,
            image_locks={}, api_key="never-sent", execute_live=True,
        )
    assert caught.value.code == "H12_ENVIRONMENT_REVISION_REQUIRED"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("split", ["dev", "holdout"])
def test_cli_rejects_missing_revision_before_database_or_provider_initialization(tmp_path, split):
    script = Path(__file__).resolve().parents[1] / "scripts/h12_eval.py"
    result = subprocess.run(
        [sys.executable, str(script), "--execute-live", "--split", split,
         "--settings", str(tmp_path / "absent-private-settings"), "--out", str(tmp_path / "out")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "new paid execution requires" in result.stderr
    assert not list(tmp_path.iterdir())


def test_direct_task_historical_holdout_is_blocked_before_readiness(tmp_path, monkeypatch):
    runner = EvaluationRunner(
        SimpleNamespace(harness=None), experiment_id="synthetic", evidence=tmp_path,
        image_locks={}, api_key="never-sent", execute_live=True,
        environment_revision=DEV_ENVIRONMENT_V3,
    )
    monkeypatch.setattr(
        runner, "_require_dev_readiness", lambda: pytest.fail("must quarantine first"),
    )
    task = {**h12.load_frozen_tasks("dev")[0], "split": "holdout"}
    with pytest.raises(DomainError) as caught:
        runner.evaluate_task(task, "candidate")
    assert caught.value.code == "H12_HISTORICAL_HOLDOUT_QUARANTINED"


def test_cached_ready_cannot_bypass_changed_frozen_environment(tmp_path, monkeypatch):
    lock = tmp_path / "environment-lock.json"
    lock.write_text("original")
    root = Path(__file__).resolve().parents[1]
    config = {
        "candidate_source": "a" * 64, "environment_revision": DEV_ENVIRONMENT_V3,
        "independent_registry_sha256": None,
        "model_observation_sha256": hashlib.sha256(
            (root / MODEL_OBSERVATION).read_bytes()
        ).hexdigest(),
        "image_locks": {"fixture@pin": {
            "path": str(lock), "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        }},
    }
    runner = EvaluationRunner(
        SimpleNamespace(harness=SimpleNamespace(get_experiment=lambda _: {"config": config})),
        experiment_id="synthetic", evidence=tmp_path / "evidence",
        image_locks={"fixture@pin": str(lock)}, api_key="never-sent", execute_live=True,
        environment_revision=DEV_ENVIRONMENT_V3,
    )
    monkeypatch.setattr(runner, "_candidate_fingerprint", lambda: "a" * 64)
    monkeypatch.setattr(h12, "frozen_hashes", lambda _: {"status": "PASS"})
    runner._dev_readiness_passed = True
    runner._require_dev_readiness()
    lock.write_text("changed after readiness")
    with pytest.raises(DomainError) as caught:
        runner.evaluate_task(h12.load_frozen_tasks("dev")[0], "candidate")
    assert caught.value.code == "H12_FROZEN_EXECUTION_CHANGED"


def test_internal_dispatch_requires_execution_authority_even_after_environment_check(
    tmp_path, monkeypatch,
):
    runner = EvaluationRunner(
        SimpleNamespace(harness=None), experiment_id="synthetic", evidence=tmp_path,
        image_locks={}, api_key="never-sent",
    )
    task = next(t for t in h12.load_frozen_tasks("dev") if t["type"] == "feature")
    source = tmp_path / "project"
    source.mkdir()
    monkeypatch.setattr(runner, "_materialize", lambda *args: source)
    monkeypatch.setattr(h12_judge, "verify", lambda *args, **kwargs: {
        "status": "PASS", "tree_hash": h12_judge.tree_hash(source),
        "judge_sha256": task["judge_sha256"],
    })
    monkeypatch.setattr(
        "mgar_v3.quality_eval.h12_runner.HarnessCodingService",
        lambda *a, **kw: pytest.fail("provider/service must not initialize"),
    )
    with pytest.raises(DomainError) as caught:
        runner._execute(
            task, profile_for(task, "synthetic", "candidate"),
            source, tmp_path / "lock", tmp_path / "case",
        )
    assert caught.value.code == "H12_LIVE_EXECUTION_REQUIRED"
    runner.execute_live = True
    with pytest.raises(DomainError) as caught:
        runner._live_policy("dev")
    assert caught.value.code == "H12_ENVIRONMENT_REVISION_REQUIRED"


@pytest.mark.parametrize("invalid", [None, "missing-task", "holdout", "missing-revision"])
def test_real_pg_dev_only_freeze_preserves_shared_budget_and_full_dev_denominator(
    engine, invalid,
):
    profiles, _, _, _ = _freeze(engine)
    name = "new-dev-only"
    dev = [
        p.model_copy(update={
            "experiment_id": name,
            "arm": "candidate" if p.arm == "new" else "legacy-v35",
        })
        for p in profiles if p.split == "dev"
    ]
    config = {
        "schema": "harness-h12-experiment-v1", "environment_revision": DEV_ENVIRONMENT_V3,
        "profiles": [p.model_dump(mode="json") for p in dev],
    }
    if invalid == "missing-task":
        config["profiles"] = config["profiles"][:-1]
    elif invalid == "holdout":
        config["profiles"].append(next(
            p.model_copy(update={"experiment_id": name}).model_dump(mode="json")
            for p in profiles if p.split == "holdout"
        ))
    elif invalid == "missing-revision":
        config.pop("environment_revision")
    approval = {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12000000}
    if invalid:
        with pytest.raises(DomainError) as caught:
            engine.harness.create_experiment(name, config, approval)
        assert caught.value.code == "EVALUATION_CONFIG_DENIED"
        return
    original = engine.harness.get_experiment(profiles[0].experiment_id)
    created = engine.harness.create_experiment(name, config, approval)
    assert created["profile_hash"] == original["profile_hash"]
    assert created["limit_micros"] == original["limit_micros"] == 12000000
    assert len(created["config"]["profiles"]) == 24
    assert all(p["split"] == "dev" for p in created["config"]["profiles"])
    assert created["used_calls"] == original["used_calls"] == 0
