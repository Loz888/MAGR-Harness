"""H12 pure preflight/scoring checks; no holdout, model, Docker or database run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root

ROOT = repo_root()


def _record(index: int, **changes: Any) -> dict[str, Any]:
    return {
        "task_id": f"synthetic-{index}",
        "split": "dev",
        "type": "bugfix",
        "status": "PASS",
        "candidate_status": "completed",
        "raw_runtime_status": "completed",
        "judge_status": "PASS",
        "final_tree": "a" * 64,
        "judge_tree": "a" * 64,
        "usage_known": True,
        "cost_micros": 3,
        "reserved_micros": 0,
        **changes,
    }


def _score(records, split="dev"):
    resources = {
        "max_model_calls": 32,
        "max_tool_calls": 200,
        "max_cost_micros": 12000000,
        "deadline_seconds": 3600,
        "max_context_tokens": 400000,
        "max_repair_rounds": 12,
    }
    expected = [
        {"task_id": f"synthetic-{i}", "type": "bugfix", "resources": resources} for i in range(13)
    ]
    # Normal synthetic IDs are 0..11; one-case boundary tests use ID 1.
    return h12.score_records(records, split, expected_tasks=expected[:12])


def test_frozen_hashes_and_dev_loader_never_parse_holdout(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text

    def public_text(path: Path, *args: Any, **kwargs: Any) -> str:
        assert path.name != "holdout.jsonl"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", public_text)
    frozen = h12.frozen_hashes(ROOT)
    assert frozen["status"] == "PASS"
    assert len(frozen["files"]) == 4
    assert len(h12.load_frozen_tasks("dev", ROOT)) == 12


def test_modified_dev_bytes_cannot_be_accepted_by_editing_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = json.loads((ROOT / "experiments/harness-v4/baseline.json").read_text())
    baseline["task_catalog"]["files"]["experiments/harness-v4/tasks/dev.jsonl"] = "a" * 64
    monkeypatch.setattr(h12, "load_baseline", lambda _: baseline)
    result = h12.frozen_hashes(ROOT)
    assert result["status"] == "FAIL"
    assert result["baseline_valid"] is False


def test_holdout_loader_uses_isolated_child_without_parent_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Synthetic child output only. This test does not open or run the holdout reader.
    safe = [{"task_id": f"synthetic-private-{i}", "goal": "synthetic safe goal"} for i in range(12)]
    calls = []

    def child(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert "-I" in argv and "-B" in argv
        assert kwargs["capture_output"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(safe), "")

    monkeypatch.setattr(subprocess, "run", child)
    assert h12.load_frozen_tasks("holdout", ROOT) == safe
    assert len(calls) == 1


def test_model_projection_strips_answers_private_paths_and_shell_commands() -> None:
    task = h12.load_frozen_tasks("dev", ROOT)[0]
    task = {
        **task,
        "scope": ["src/code.py", "tests/hidden_answer.py", "../../outside", "/etc/key"],
        "judge": {"answer": "private-value"},
        "fix_commit": "private-fix",
        "unexpected": "private-top-level",
        "environment": {
            "verify": [
                "python -m pytest tests/public.py verification/secret_hidden.py",
                "go test -run TestHidden ./...",
                "echo safe; cat secret",
            ],
            "credentials": "private-credential",
        },
        "acceptance": {"regression": ["python -m pytest tests/"]},
    }
    visible = h12.project_task(task)
    assert visible["scope"] == ["src/code.py"]
    assert visible["public_commands"] == [
        ["python", "-m", "pytest", "tests/public.py"],
        ["python", "-m", "pytest", "tests/"],
    ]
    serialized = json.dumps(visible)
    for forbidden in (
        "private-value",
        "private-fix",
        "private-top-level",
        "private-credential",
        "secret_hidden",
        "../../outside",
        "/etc/key",
        "judge",
        "fix_commit",
    ):
        assert forbidden not in serialized


def test_three_public_reference_snapshots_have_exact_hashes_and_task_specific_tests() -> None:
    result = h12.preflight("dev", ROOT)
    refs = [t for t in result["tasks"] if t["source"].get("kind") == "v35_frozen_snapshot"]
    assert len(refs) == 3
    assert all(t["source"]["source_sha256"] == t["source"]["expected_sha256"] for t in refs)
    by_id = {t["task_id"]: t for t in refs}
    assert by_id["hv4-dev-click-short-help"]["source"]["public_files"][0]["target"] == (
        "tests/test_short_help_abbreviation_public.py"
    )
    assert by_id["hv4-dev-click-multifile-placeholder"]["source"]["public_files"][0]["target"] == (
        "tests/test_short_help_public.py"
    )
    assert all(t["judge_execution"] == "NOT_RUN" for t in refs)


@pytest.mark.parametrize("split,reference", [("holdout", True), ("dev", False)])
def test_reusing_source_pin_does_not_publish_another_tasks_dev_public_reproduction(
    split: str,
    reference: bool,
) -> None:
    # Synthetic metadata changes only; no holdout file is read or parsed.
    original = h12.load_frozen_tasks("dev", ROOT)[0]
    task = {**original, "task_id": "synthetic-other-goal", "split": split}
    if not reference:
        task["judge"] = {"kind": "behavior_check"}
    source = h12._source(task, ROOT, {}, h12._public_v35_metadata(ROOT))
    assert source["available"] is True
    assert source["source_sha256"]
    assert source["public_files"] == []


def test_missing_pinned_object_cannot_fall_back_to_current_git_head(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    for argv in (
        ["git", "init", "-q", str(source)],
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=H12 Fixture",
            "-c",
            "user.email=h12@example.invalid",
            "commit",
            "-qm",
            "fixture",
            "--allow-empty",
        ],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    task = {"task_id": "local-source", "base_commit": commit, "repository": "example/project"}
    available = h12._source(task, tmp_path, {"local-source": source}, [])
    assert available["available"] is True
    assert available["commit"] == commit
    missing = h12._source({**task, "base_commit": "a" * 40}, tmp_path, {"local-source": source}, [])
    assert missing["available"] is False
    assert missing["reason"] == "PINNED_SOURCE_UNAVAILABLE"


def test_snapshot_metadata_hash_mismatch_is_not_repaired(tmp_path: Path) -> None:
    source = tmp_path / "experiments/v35-harness/snapshot"
    source.mkdir(parents=True)
    (source / "code.py").write_text("new content\n")
    task = {"task_id": "local-source", "base_commit": "b" * 40, "repository": "example/project"}
    metadata = [
        {
            "task_id": "reference",
            "repository": "example/project",
            "base_commit": "b" * 40,
            "snapshot_dir": "snapshot",
            "snapshot_digest": "c" * 64,
        }
    ]
    result = h12._source(task, tmp_path, {}, metadata)
    assert result["available"] is False
    assert result["reason"] == "SNAPSHOT_DIGEST_MISMATCH"
    assert (source / "code.py").read_text() == "new content\n"


@pytest.mark.parametrize("split,minimum", [("dev", 10), ("holdout", 9)])
def test_score_uses_frozen_twelve_denominator_and_split_threshold(split: str, minimum: int) -> None:
    # These are synthetic score records, not reads of either task catalog.
    score = _score([_record(i, split=split) for i in range(minimum)], split)
    assert score["denominator"] == 12
    assert score["pass_rate"] == minimum / 12
    assert score["not_run_count"] == 12 - minimum
    assert score["threshold_met"] is True
    assert score["eligible_count"] == minimum


def test_empty_run_is_not_run_with_twelve_missing_cases() -> None:
    result = _score([])
    assert result["gate_status"] == "NOT_RUN"
    assert result["pass_count"] == 0
    assert result["not_run_count"] == 12


def test_unknown_usage_retains_reservation_and_stops_paid_dispatch() -> None:
    records = [_record(i) for i in range(10)]
    records.append(
        _record(
            10,
            status="UNKNOWN",
            judge_status="UNKNOWN",
            usage_known=False,
            cost_micros=None,
            reserved_micros=2000,
            raw_runtime_status="unknown",
        )
    )
    score = _score(records)
    assert score["unknown_count"] == 1
    assert score["not_run_count"] == 1
    assert score["reserved_cost_micros"] == 2000
    assert score["records"][-1]["cost_micros"] is None
    assert score["may_send_paid_calls"] is False
    assert score["threshold_met"] is False
    assert score["budget_integrity_errors"] == []


@pytest.mark.parametrize("cost,held", [(0, 2000), (None, 0)])
def test_unknown_cannot_be_reported_as_zero_or_have_reservation_released(
    cost: int | None,
    held: int,
) -> None:
    score = _score(
        [_record(1, status="UNKNOWN", usage_known=False, cost_micros=cost, reserved_micros=held)]
    )
    assert score["budget_integrity_errors"] == ["synthetic-1"]
    assert score["may_send_paid_calls"] is False
    assert score["records"][0]["cost_micros"] is None


def test_candidate_completion_is_separate_from_final_runtime_false_completion() -> None:
    candidate = _record(
        1,
        status="FAIL",
        candidate_status="completed",
        raw_runtime_status="incomplete",
        judge_status="FAIL",
    )
    assert _score([candidate])["false_completions"] == 0
    final = {**candidate, "raw_runtime_status": "completed"}
    score = _score([final])
    assert score["false_completions"] == 1
    assert score["threshold_met"] is False
    assert score["records"][0]["candidate_status"] == "completed"


def test_pass_with_missing_or_different_judged_tree_does_not_count() -> None:
    score = _score([_record(1, judge_tree="b" * 64), _record(2, judge_status="NOT_RUN")])
    assert score["pass_count"] == 0
    assert score["fail_count"] == 2


@pytest.mark.parametrize(
    "violation", ["unauthorized_actions", "unknown_blind_retries", "new_regression_failures"]
)
def test_zero_tolerance_gates_do_not_pass_with_enough_successes(violation: str) -> None:
    records = [_record(i) for i in range(12)]
    records[0][violation] = 1
    score = _score(records)
    assert score["pass_count"] == 12
    assert score["threshold_met"] is False
    assert score["gate_status"] == "FAIL"


def test_duplicate_retry_cannot_replace_first_failed_sample() -> None:
    with pytest.raises(CatalogError, match="distinct"):
        _score([_record(1, status="FAIL"), _record(1)])
    with pytest.raises(CatalogError, match="twelve"):
        _score([_record(i) for i in range(13)])


def test_private_split_reference_metadata_is_hash_bound_and_immediately_projected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    path = tmp_path / "experiments/v35-harness/holdout/tasks.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "task_id": "synthetic-reference",
                "repository": "synthetic/project",
                "base_commit": "a" * 40,
                "public_files": [],
                "public_targets": [],
                "goal": "private-irrelevant-goal",
                "gold": "private-answer-must-not-project",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        h12, "_V35_HOLDOUT_METADATA_SHA256", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    projected = h12._public_v35_metadata(tmp_path, "holdout")
    assert projected[0]["task_id"] == "synthetic-reference"
    assert "private-irrelevant-goal" not in json.dumps(projected)
    assert "private-answer-must-not-project" not in json.dumps(projected)
    path.write_text(path.read_text() + "\n")
    with pytest.raises(CatalogError, match="metadata changed"):
        h12._public_v35_metadata(tmp_path, "holdout")
