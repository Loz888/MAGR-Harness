"""Seen zero-call cases may get one diagnostic; primary scores stay immutable."""
# ruff: noqa: F811 -- pytest fixtures imported from the controller fixture module.

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_h12_complete_matrix import complete_runner
from test_h12_quality_controller import quality  # noqa: F401
from test_h12_v2_public_development import bound  # noqa: F401

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_first_model_recovery import (
    SCHEMA,
    FirstModelRecoveryRunner,
    load_recovery_primary,
)
from mgar_v3.quality_eval.harness_v4 import CatalogError


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def reference(root, path):
    return {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def binding(root, tasks, *, original_id="original-complete", quality_path=None):
    records = []
    eligible = []
    for index, task in enumerate(tasks):
        unstarted = task["split"] == "holdout" and index >= 21
        score = {
            "experiment_id": original_id,
            "task_id": task["task_id"],
            "split": task["split"],
            "arm": "candidate",
            "usage_known": True,
            "reserved_micros": 0,
            "status": "NOT_RUN" if unstarted else "PASS",
            "run_id": None if unstarted else f"run-{index}",
            "model_calls": 0 if unstarted else 1,
            "cost_micros": 0 if unstarted else 1,
            "settled_cost_micros": 0 if unstarted else 1,
        }
        if unstarted:
            eligible.append(task["task_id"])
        path = put(
            root / f"primary/case-{index}.json",
            {
                "task": h12.project_task(task),
                "score": score,
                "judge_specification": {"sha256": task["judge_sha256"]},
            },
        )
        records.append(reference(root, path))
    path = put(
        root / "primary/report.json",
        {
            "schema": "h12-complete-thinking-matrix-report-v1",
            "matrix_complete": True,
            "ledger_matches_case_totals": True,
            "holdout": "QUARANTINED_AFTER_THIS_REPORT",
            "source_sha256": "c" * 64,
            "experiment_id": original_id,
            "case_records": records,
            "summaries": {
                "dev": {"pass_count": 12, "fail_count": 0, "not_run_count": 0, "unknown_count": 0}
            },
            "budget_after": {"unknown_calls": 0, "budget": {"reserved_micros": 0}},
        },
    )
    return {
        "schema": SCHEMA,
        "status": "USER_REQUESTED",
        "quality_status": "DIAGNOSTIC_ONLY",
        "allow_model_resample": False,
        "allow_score_replacement": False,
        "allow_unknown_retry": False,
        "allow_publication": False,
        "shared_budget_micros": 12000000,
        "shared_call_limit": 1536,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "primary_report": reference(root, path),
        "eligible_task_ids": sorted(eligible),
        "max_first_model_samples": len(eligible),
        "quality_freeze": reference(root, quality_path) if quality_path else {},
    }


def tasks():
    return [
        {
            "task_id": f"{split}-{i}",
            "split": split,
            "type": "feature",
            "repository": "fixture/project",
            "base_commit": "a" * 40,
            "language": "python",
            "goal": "Add a small API",
            "scope": ["source.py"],
            "resources": {"max_model_calls": 2},
            "judge_sha256": "b" * 64,
            "environment": {"verify": []},
            "acceptance": {"regression": []},
        }
        for split in ("dev", "holdout")
        for i in range(12)
    ]


def test_recovery_inventory_uses_all_original_hash_bound_records(tmp_path):
    inventory = tasks()
    document = binding(tmp_path, inventory)
    result = load_recovery_primary(tmp_path, document, inventory)
    assert result["eligible_task_ids"] == ["holdout-10", "holdout-11", "holdout-9"]
    assert len(result["records"]) == 24
    # A goal or resource change cannot be hidden behind the same task id.
    changed = copy.deepcopy(inventory)
    changed[-1]["scope"] = [""]
    with pytest.raises(CatalogError):
        load_recovery_primary(tmp_path, document, changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allow_model_resample", True),
        ("allow_score_replacement", True),
        ("allow_unknown_retry", True),
        ("allow_publication", True),
        ("shared_budget_micros", 24000000),
        ("shared_call_limit", 9999),
        ("quality_status", "PASS"),
        ("reasoning_effort", "high"),
        ("eligible_task_ids", ["holdout-1"]),
        ("max_first_model_samples", 12),
    ],
)
def test_recovery_rejects_expansion_or_quality_relabel(tmp_path, field, value):
    inventory = tasks()
    document = binding(tmp_path, inventory)
    document[field] = value
    with pytest.raises(CatalogError):
        load_recovery_primary(tmp_path, document, inventory)


@pytest.mark.parametrize("mutation", ["sent", "unknown", "reserved", "tampered"])
def test_a_relabelled_or_unaccounted_primary_cannot_grant_recovery(tmp_path, mutation):
    inventory = tasks()
    document = binding(tmp_path, inventory)
    report_path = tmp_path / document["primary_report"]["path"]
    report = json.loads(report_path.read_bytes())
    case_path = tmp_path / report["case_records"][-1]["path"]
    case = json.loads(case_path.read_bytes())
    if mutation == "sent":
        case["score"].update(model_calls=1, run_id="already-sent")
    elif mutation == "unknown":
        case["score"].update(usage_known=False)
    elif mutation == "reserved":
        case["score"]["reserved_micros"] = 100
    else:
        case["task"]["goal"] = "Different public objective"
    put(case_path, case)
    report["case_records"][-1] = reference(tmp_path, case_path)
    put(report_path, report)
    document["primary_report"] = reference(tmp_path, report_path)
    with pytest.raises(CatalogError):
        load_recovery_primary(tmp_path, document, inventory)


def test_same_case_claim_blocks_concurrent_worker_without_pg_transaction(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    def runner():
        value = object.__new__(FirstModelRecoveryRunner)
        value.engine = SimpleNamespace(artifacts=store)
        value.recovery_document = {"primary_report": {"sha256": "b" * 64}}
        return value

    first, second = runner(), runner()
    with first._first_model_claim("same-case"):
        with pytest.raises(DomainError) as caught:
            with second._first_model_claim("same-case"):
                pytest.fail("duplicate first sample entered")
        assert caught.value.code == "H12_RECOVERY_ALREADY_STARTED"
        with second._first_model_claim("another-case"):
            pass
    with second._first_model_claim("same-case"):
        pass  # Only the transient lock is released; PG still fences any actual Run.


def test_forged_file_facts_do_not_bypass_original_pg_or_mint_budget(
    quality,
    monkeypatch,
    engine,
):
    original, path, _ = complete_runner(quality)
    inventory = [*original._tasks("dev"), *original._tasks("holdout")]
    root = quality.bound.root
    document = binding(root, inventory, original_id=original.experiment_id, quality_path=path)
    recovery_path = put(root / "recovery/authority.json", document)
    value = FirstModelRecoveryRunner(
        engine,
        experiment_id="diagnostic-first-recovery",
        evidence=root / "recovery/evidence",
        recovery_path=recovery_path,
        recovery_sha256=reference(root, recovery_path)["sha256"],
        api_key=None,
        root=root,
    )
    original.engine, original.store = engine, engine.harness
    for item in (original, value):
        monkeypatch.setattr(item, "_source", lambda _: quality.bound.workspace)
        monkeypatch.setattr(
            item,
            "_materialize_into",
            lambda task, source, target: Path(shutil.copytree(source, target)),
        )
        monkeypatch.setattr(item, "_require_public_development_frozen", lambda: None)
        item.initialize()
    a = engine.harness.get_experiment(original.experiment_id)
    b = engine.harness.get_experiment(value.experiment_id)
    assert a["profile_hash"] == b["profile_hash"]
    assert b["used_calls"] == b["reserved_micros"] == b["settled_micros"] == 0
    assert b["config"]["first_model_recovery"]["quality_status"] == "DIAGNOSTIC_ONLY"
    with pytest.raises(DomainError) as caught:
        value._primary_pg_facts()
    assert caught.value.code == "H12_RECOVERY_PRIMARY_REQUIRED"
    assert engine.harness.get_experiment(value.experiment_id)["used_calls"] == 0


@pytest.mark.parametrize(
    "flags",
    [[], ["--execute-live"], ["--initialize-experiment", "--execute-live", "--task-id", "x"]],
)
def test_recovery_cli_needs_separate_explicit_selected_case_before_loading_authority(
    tmp_path,
    flags,
):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "not-created"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/h12_first_model_recovery.py"),
            "--recovery-freeze",
            str(tmp_path / "absent.json"),
            "--recovery-freeze-sha256",
            "a" * 64,
            "--experiment-id",
            "never-dispatched",
            "--out",
            str(output),
            *flags,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "error:" in result.stderr
    assert not output.exists()
