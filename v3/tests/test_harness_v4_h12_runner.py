"""Evaluator regressions use public/synthetic projects and no paid transport."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_runner import EvaluationRunner, profile_for
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _runner(tmp_path: Path) -> EvaluationRunner:
    engine = SimpleNamespace(harness=SimpleNamespace(tasks=lambda: []))
    return EvaluationRunner(
        engine,
        experiment_id="synthetic",
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key=None,
    )


def test_materialization_uses_fixed_object_despite_dirty_and_unrelated_head(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    (source / "code.py").write_text("original\n")
    _git(source, "add", "code.py")
    _git(
        source,
        "-c",
        "user.name=H12",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "original",
    )
    fixed = _git(source, "rev-parse", "HEAD")
    (source / "code.py").write_text("later commit\n")
    _git(source, "add", "code.py")
    _git(
        source,
        "-c",
        "user.name=H12",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "later",
    )
    (source / "code.py").write_text("uncommitted\n")
    (source / "untracked-secret").write_text("must not copy\n")
    runner = _runner(tmp_path)
    target = runner._materialize({"base_commit": fixed, "repository": "synthetic/project"}, source)
    assert (target / "code.py").read_text() == "original\n"
    assert not (target / "untracked-secret").exists()
    assert _git(target, "rev-parse", "HEAD") == fixed
    assert (source / "code.py").read_text() == "uncommitted\n"


def test_source_uses_exact_reference_snapshot_path_instead_of_unrelated_git_pin(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    task = h12.load_frozen_tasks("dev")[0]
    source = runner._source(task)
    assert source is not None
    assert source.is_relative_to(repo_root() / "experiments")
    assert not source.is_relative_to(repo_root() / ".local/h12/sources")
    assert h12._snapshot_digest(source)


def test_legacy_not_run_does_not_invoke_a_provider_or_create_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_source", lambda _: None)
    task = h12.load_frozen_tasks("dev")[0]
    result = runner.evaluate_task(task, "legacy-v35")
    assert result["status"] == "NOT_RUN"
    assert result["reason"] == "LEGACY_SAME_MODEL_BUDGET_ADAPTER_UNAVAILABLE"
    assert result["cost_micros"] == 0
    assert list(runner.evidence.rglob("score.json"))


@pytest.mark.parametrize("change", ["source", "image", "registry", "model"])
def test_experiment_freeze_rejects_changed_execution_before_loading_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import hashlib

    runner = _runner(tmp_path)
    config = {
        "candidate_source": "original",
        "image_locks": {},
        "independent_registry_sha256": None,
        "model_observation_sha256": hashlib.sha256(
            (repo_root() / "experiments/harness-v4/h12-model-v2.json").read_bytes()
        ).hexdigest(),
    }
    runner.store.get_experiment = lambda _: {"config": config}
    monkeypatch.setattr(runner, "_candidate_fingerprint", lambda: "original")
    if change == "source":
        config["candidate_source"] = "changed"
    elif change == "image":
        config["image_locks"] = {"other": {}}
    elif change == "registry":
        config["independent_registry_sha256"] = "changed"
    else:
        config["model_observation_sha256"] = "changed"
    with pytest.raises(DomainError) as error:
        runner.evaluate(split="dev", arm="candidate")
    assert error.value.code == "H12_FROZEN_EXECUTION_CHANGED"


def test_arbitrary_task_ids_cannot_invent_a_passing_frozen_score() -> None:
    with pytest.raises(CatalogError, match="frozen task IDs"):
        h12.score_records([{"task_id": "invented", "status": "PASS"}])


def test_completion_and_resource_limits_are_part_of_independent_score() -> None:
    tasks = h12.load_frozen_tasks("dev")
    task = tasks[0]
    record = {
        "task_id": task["task_id"],
        "type": task["type"],
        "split": "dev",
        "status": "PASS",
        "judge_status": "PASS",
        "raw_runtime_status": "incomplete",
        "cost_micros": 0,
        "final_tree": "a" * 64,
        "judge_tree": "a" * 64,
    }
    assert h12.score_records([record])["pass_count"] == 0
    record.update(
        raw_runtime_status="completed", model_calls=task["resources"]["max_model_calls"] + 1
    )
    result = h12.score_records([record])
    assert result["pass_count"] == 0
    assert result["resource_violations"] == [task["task_id"]]
    assert result["records"][0]["resource_errors"] == ["model_calls"]


@pytest.mark.parametrize("name", ["pytest.ini", "conftest.py", "pyproject.toml"])
def test_adding_test_configuration_after_model_start_invalidates_the_tree(
    tmp_path: Path,
    name: str,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    frozen = h12_judge.test_hashes(workspace)
    (workspace / name).write_text("# a new test-disabling config\n")
    result = h12_judge.verify(
        workspace,
        h12.load_frozen_tasks("dev")[0],
        tmp_path / "lock",
        tmp_path / "judge",
        sealed=True,
        frozen_tests=frozen,
    )
    assert result["reason"] == "FROZEN_TEST_MODIFIED"


@pytest.mark.parametrize("output", ["", "exit status 1", "process failed", "1 error"])
def test_failed_process_does_not_prove_a_bugfix_baseline(output: str) -> None:
    assert not h12_judge._baseline_failure("python", ["python3", "probe.py"], output)


def test_missing_environment_returns_before_service_or_paid_model_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    source = tmp_path / "project"
    source.mkdir()
    task = next(t for t in h12.load_frozen_tasks("dev") if t["type"] == "feature")
    monkeypatch.setattr(runner, "_materialize", lambda *args: source)
    monkeypatch.setattr(
        h12_judge,
        "verify",
        lambda *a, **k: {
            "status": "NOT_RUN",
            "reason": "DEPENDENCY_OR_LOG_UNAVAILABLE",
        },
    )
    monkeypatch.setattr(
        "mgar_v3.quality_eval.h12_runner.HarnessCodingService",
        lambda *a, **k: pytest.fail("must not initialize a paid task"),
    )
    case = tmp_path / "case"
    result = runner._execute(
        task,
        profile_for(task, "synthetic", "candidate"),
        source,
        tmp_path / "lock",
        case,
    )
    assert result["status"] == "NOT_RUN"
    assert result["cost_micros"] == 0
    assert result["environment_reason"] == "DEPENDENCY_OR_LOG_UNAVAILABLE"


def test_paid_model_failure_without_final_judge_remains_fail(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.store.get_experiment = lambda _: {"usage": {"unknown_calls": 0}, "reserved_micros": 0}
    task = h12.load_frozen_tasks("dev")[0]
    result = runner._score_payload(
        task,
        profile_for(task, "synthetic", "candidate"),
        {"status": "incomplete", "reason": "model_bad_patch", "model_calls": 1, "cost_micros": 5},
    )
    assert result["status"] == "FAIL"
    assert result["judge_status"] == "NOT_RUN"
    assert result["cost_micros"] == 5


def test_public_one_shot_application_goal_keeps_regression_and_selfcheck_distinct() -> None:
    task = next(
        t
        for t in h12.load_frozen_tasks("dev")
        if t["type"] == "run_app" and t["language"] == "typescript"
    )
    profile = profile_for(task, "synthetic", "candidate")
    assert profile.service_mode == "oneshot"
    assert profile.service_argv == ("npm", "run", "selfcheck")
    assert profile.public_test_argv == ("npm", "test")


def test_http_application_plan_uses_only_explicit_public_endpoint_and_port() -> None:
    task = next(
        t
        for t in h12.load_frozen_tasks("dev")
        if t["type"] == "run_app" and t["language"] == "javascript"
    )
    profile = profile_for(task, "synthetic", "candidate")
    assert profile.service_mode == "http"
    assert profile.service_port == 3000
    assert profile.service_path == "/"
    assert profile.public_test_argv[1] == "/node_modules/mocha/bin/mocha.js"
    changed = {**task, "goal": "Start a persistent app on default port 4321, serving GET /ready."}
    profile = profile_for(changed, "synthetic", "candidate")
    assert profile.service_port == 4321 and profile.service_path == "/ready"
    unknown = profile_for(
        {**task, "goal": "Start a persistent app and await instructions."}, "synthetic", "candidate"
    )
    assert unknown.service_argv == ()


@pytest.mark.parametrize("same_pin", [True, False])
def test_reference_public_fixture_uses_its_split_and_exact_repository_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_pin: bool,
) -> None:
    import hashlib

    runner = _runner(tmp_path)
    runner.root = tmp_path
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("value = 1\n")
    public = tmp_path / "experiments/v35-harness/holdout/public/synthetic.py"
    public.parent.mkdir(parents=True)
    public.write_text("# synthetic public fixture\n")
    task = {
        "repository": "synthetic/project",
        "base_commit": "a" * 40,
        "split": "holdout",
        "judge": {"reference_task": "synthetic-reference"},
    }

    def metadata(root, split):
        assert split == "holdout"
        return [
            {
                "task_id": "synthetic-reference",
                "repository": "synthetic/project",
                "base_commit": ("a" if same_pin else "b") * 40,
                "public_files": ["holdout/public/synthetic.py"],
                "public_targets": ["tests/test_public.py"],
            }
        ]

    monkeypatch.setattr(h12, "_public_v35_metadata", metadata)
    monkeypatch.setattr(
        h12,
        "_public_v35_hashes",
        lambda *args: {
            "holdout/public/synthetic.py": hashlib.sha256(public.read_bytes()).hexdigest(),
        },
    )
    if same_pin:
        materialized = runner._materialize(task, source)
        assert (materialized / "tests/test_public.py").read_bytes() == public.read_bytes()
    else:
        with pytest.raises(DomainError) as error:
            runner._materialize(task, source)
        assert error.value.code == "H12_PUBLIC_TEST_SOURCE_BINDING_CONFLICT"
