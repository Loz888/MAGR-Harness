"""Real Engine accounting survives a controlled post-model sample failure."""

from __future__ import annotations

import hashlib
import json

import pytest
from test_harness_v4_h12_budget import _freeze, _known, _task

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import ModelResponse, ModelUsage
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_runner import (
    DEV_ENVIRONMENT_V3,
    EvaluationRunner,
    private_json,
    profile_for,
)


@pytest.mark.parametrize("unknown", [False, True])
def test_paid_usage_then_artifact_failure_is_not_free_not_run(
    engine,
    tmp_path,
    monkeypatch,
    unknown,
):
    profiles, _, _, _ = _freeze(engine)
    profile = profiles[0]
    other, other_ledger = _task(engine, tmp_path, profiles[-1], "unrelated-run")
    other_ledger.reserve("unrelated-run", "other-call", "model", reserved_micros=1000)
    _known(engine, other, "other-call", 1000, input_tokens=900, output_tokens=100)
    source = tmp_path / "project"
    source.mkdir()
    (source / "code.py").write_text("value = 1\n")
    task = {
        "task_id": profile.task_id,
        "split": "dev",
        "type": "inspect",
        "language": "python",
        "judge_sha256": "a" * 64,
        "environment": {"verify": []},
        "acceptance": {"regression": []},
    }

    class ControlledService:
        def __init__(self, *args, **kwargs):
            pass

        def register_project(self, path):
            return {"project_id": "controlled"}

        def create_task(self, request):
            row, ledger = _task(engine, tmp_path, profile, "failed-sample")
            ledger.reserve("failed-sample", "known-call", "model", reserved_micros=100)
            _known(engine, row, "known-call", 7, input_tokens=17, output_tokens=3)
            if unknown:
                ledger.reserve("failed-sample", "unknown-call", "model", reserved_micros=11)
                engine.harness.save_model_result(
                    "failed-sample",
                    row["epoch"],
                    "unknown-call",
                    ModelResponse(usage=ModelUsage(known=False, input_tokens=11, output_tokens=2)),
                )
            raise DomainError("ARTIFACT_TOO_LARGE", "Controlled post-settlement failure", 413)

    monkeypatch.setattr("mgar_v3.quality_eval.h12_runner.HarnessCodingService", ControlledService)
    runner = EvaluationRunner(
        engine,
        experiment_id=profile.experiment_id,
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key="controlled-unused",
        execute_live=True,
        environment_revision=DEV_ENVIRONMENT_V3,
    )
    # This test isolates accounting after a controlled readiness gate. The
    # actual zero-model dispatch gate has separate negative integration tests.
    monkeypatch.setattr(runner, "_require_dev_readiness", lambda: None)
    monkeypatch.setattr(runner, "_materialize", lambda *args: source)
    monkeypatch.setattr(
        h12_judge,
        "verify",
        lambda *args, **kwargs: {
            "status": "PASS",
            "tree_hash": h12_judge.tree_hash(source),
            "judge_sha256": task["judge_sha256"],
        },
    )
    case = tmp_path / "case"
    case.mkdir()
    score = runner._execute(task, profile, source, tmp_path / "lock", case)
    selected = engine.harness.get("failed-sample")
    assert score["status"] == ("UNKNOWN" if unknown else "FAIL")
    assert score["cost_micros"] == (None if unknown else 7)
    assert score["settled_cost_micros"] == 7
    assert score["reserved_micros"] == (11 if unknown else 0)
    assert score["model_calls"] == (2 if unknown else 1)
    assert score["input_tokens"] == (28 if unknown else 17)
    assert score["output_tokens"] == (5 if unknown else 3)
    assert score["run_id"] == str(selected["run_id"])
    assert selected["status"] == "paused" and selected["epoch"] == 2
    assert engine.harness.get_experiment(profile.experiment_id)["settled_micros"] == 1007
    assert engine.harness.get("unrelated-run")["status"] == "running"
    receipt = json.loads(engine.artifacts.read(score["exception_receipt_hash"]))
    assert receipt["reason"] == "ARTIFACT_TOO_LARGE"
    assert receipt["usage"]["settled_cost_micros"] == 7


def test_before_model_setup_failure_archives_only_that_not_run_case(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from mgar_v3.quality_eval.harness_v4 import load_tasks

    runner = EvaluationRunner(
        SimpleNamespace(
            harness=SimpleNamespace(
                tasks=lambda: [],
                get_experiment=lambda _: {"usage": {"unknown_calls": 0}},
            )
        ),
        experiment_id="synthetic",
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key="controlled-unused",
        execute_live=True,
        environment_revision=DEV_ENVIRONMENT_V3,
    )
    monkeypatch.setattr(runner, "_require_dev_readiness", lambda: None)
    task = load_tasks("dev")[0]
    lock = tmp_path / "lock"
    lock.write_text("controlled")
    runner.image_locks[task["repository"] + "@" + task["base_commit"]] = str(lock)
    monkeypatch.setattr(runner, "_source", lambda _: tmp_path)

    def invalid(*args):
        raise DomainError("H12_PUBLIC_TEST_UNAVAILABLE", "Controlled missing fixture", 409)

    monkeypatch.setattr(runner, "_materialize", invalid)
    record = runner.evaluate_task(task, "candidate")
    assert record["status"] == "NOT_RUN" and record["cost_micros"] == 0
    assert record["reason"] == "H12_PUBLIC_TEST_UNAVAILABLE"
    assert next((tmp_path / "evidence").rglob("score.json")).is_file()


def test_accounting_correction_is_append_only_and_fences_only_explicit_known_run(
    engine,
    tmp_path,
    monkeypatch,
):
    resources = {
        "max_model_calls": 8,
        "max_tool_calls": 20,
        "max_cost_micros": 10000,
        "deadline_seconds": 600,
        "max_context_tokens": 8192,
        "max_repair_rounds": 4,
    }
    tasks = [
        {
            "task_id": f"synthetic-audit-{i}",
            "split": "dev",
            "type": "inspect",
            "language": "python",
            "goal": "Inspect the synthetic fixture and summarize findings",
            "resources": resources,
            "scope": [],
            "environment": {"verify": []},
            "acceptance": {"regression": []},
        }
        for i in range(12)
    ]
    experiment_id = "h12-correction-synthetic"
    profiles = [
        profile_for(
            {
                **task,
                "split": split,
                "task_id": task["task_id"] if split == "dev" else "synthetic-private-" + str(i),
            },
            experiment_id,
            arm,
        )
        for split in ("dev", "holdout")
        for i, task in enumerate(tasks)
        for arm in ("candidate", "legacy-v35")
    ]
    config = {
        "schema": "harness-h12-experiment-v1",
        "profiles": [p.model_dump(mode="json") for p in profiles],
        "frozen_catalog": h12.frozen_hashes(),
        "candidate_source": "a" * 64,
    }
    engine.harness.create_experiment(
        experiment_id,
        config,
        {
            "approved_by": "owner",
            "allow_paid": True,
            "max_cost_micros": 12000000,
        },
    )
    row, ledger = _task(engine, tmp_path, profiles[0], "audit-failed-sample")
    ledger.reserve("audit-failed-sample", "paid", "model", reserved_micros=100)
    _known(engine, row, "paid", 7, input_tokens=17, output_tokens=3)
    runner = EvaluationRunner(
        engine,
        experiment_id=experiment_id,
        evidence=tmp_path / "originals",
        image_locks={},
        api_key=None,
    )
    originals = []
    for task in tasks:
        path = runner.evidence / "candidate/dev" / task["task_id"] / "score.json"
        private_json(path, runner._not_run(task, "candidate", "ARTIFACT_TOO_LARGE"))
        originals.append((path, hashlib.sha256(path.read_bytes()).hexdigest()))
    monkeypatch.setattr("mgar_v3.quality_eval.h12_runner.load_tasks", lambda *args: tasks)
    monkeypatch.setattr(runner, "_candidate_fingerprint", lambda: "b" * 64)
    output = tmp_path / "corrections"
    report = runner.correct_archived_scores(
        split="dev",
        arm="candidate",
        output=output,
        fence_run_ids=[str(row["run_id"])],
    )
    assert report["new_paid_calls"] == 0
    assert report["executed_source_sha256"] == "a" * 64
    assert report["scoring_source_sha256"] == "b" * 64
    assert report["summary"]["fail_count"] == 1
    assert report["summary"]["not_run_count"] == 11
    assert report["summary"]["settled_cost_micros"] == 7
    assert engine.harness.get("audit-failed-sample")["status"] == "paused"
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals
    )
    assert (output / "summary-correction.json").is_file()
    assert engine.harness.get_experiment(experiment_id)["used_calls"] == 1
