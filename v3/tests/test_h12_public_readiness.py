"""Development-only engineering readiness from real public sandbox executions."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_h12_public_plan import controller, fixture, sha
from test_h12_public_probe_service import BUGGY, FIXED, PROBE, REGRESSION, SMOKE

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval.h12_public_plan import PUBLIC_ROLES, load_public_plan
from mgar_v3.quality_eval.h12_public_readiness import (
    execute_public_original,
    validate_public_original_readiness,
)
from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4, EvaluationRunner, profile_for
from mgar_v3.quality_eval.harness_v4 import CatalogError

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/r03/sandbox-lock.json"


def toy(tmp_path, *, kind="bugfix", fixed=False, asset=PROBE):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "calc.py").write_text(FIXED if fixed else BUGGY)
    (workspace / "test_smoke.py").write_text(SMOKE)
    asset_root = tmp_path / "assets"
    asset_sha = sha(asset.encode())
    target = asset_root / asset_sha / "probe.py"
    target.parent.mkdir(parents=True)
    target.write_text(asset)
    task = {
        "task_id": "synthetic-public-original",
        "split": "dev",
        "type": kind,
        "language": "python",
        "repository": "example/add",
        "base_commit": "a" * 40,
        "goal": "Implement addition and preserve the existing import smoke test.",
        "scope": ["calc.py"],
        "resources": {
            "max_model_calls": 20,
            "max_tool_calls": 80,
            "max_cost_micros": 500000,
            "deadline_seconds": 60,
            "max_context_tokens": 32000,
            "max_repair_rounds": 6,
        },
        "environment": {"verify": ["python3 -B -m unittest -q test_smoke"]},
        "acceptance": {"regression": []},
    }
    document = {
        "schema": "h12-public-plan-v1",
        "status": "FROZEN",
        "task_id": task["task_id"],
        "repository": task["repository"],
        "base_commit": task["base_commit"],
        "public_goal_sha256": sha(task["goal"].encode()),
        "resources_sha256": canonical_hash(task["resources"]),
        "original_tree_sha256": _tree_hash(_scan(workspace)[0]),
        "roles": PUBLIC_ROLES,
        "regression_argv": list(REGRESSION),
        "public_basis": [{"path": "calc.py", "sha256": sha((workspace / "calc.py").read_bytes())}],
        "probe": {
            "plan_id": "toy-add-v1",
            "asset_sha256": asset_sha,
            "case_ids": ["positive", "negative"],
        },
    }
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(document))
    frozen = load_public_plan(
        manifest,
        sha(manifest.read_bytes()),
        task=task,
        asset_root=asset_root,
        regression_argv=REGRESSION,
    )
    profile = profile_for(
        task,
        "public-readiness-toy",
        "candidate",
        environment_revision=DEV_ENVIRONMENT_V4,
        public_probe=frozen.plan,
    )
    return task, frozen, profile, workspace


def test_real_public_red_and_regression_green_are_engineering_readiness_only(tmp_path):
    task, frozen, profile, workspace = toy(tmp_path)
    evidence = tmp_path / "proof"
    result = execute_public_original(frozen, workspace, profile, LOCK, evidence)
    assert result["status"] == "READY" and result["probe_outcome"] == "red"
    assert result["regression_status"] == "passed"
    assert result["zero_model"] is True and result["readiness_only"] is True
    assert result["paid_model_calls"] == result["cost_micros"] == 0
    assert result["quality_status"] == "NOT_RUN"
    assert result["behavior"] == "AWAITING_IMPLEMENTATION"
    assert result["tool_calls"] == 2
    assert result["original_tree_before"] == result["original_tree_after"]
    assert result["lease"]["write_paths"] == []
    assert not result["lease"]["external_write"]
    assert result["lease"]["max_calls"] == task["resources"]["max_tool_calls"]
    validate_public_original_readiness(
        result, evidence, frozen=frozen, profile=profile, image_lock=LOCK
    )


@pytest.mark.parametrize("kind,expected", [("bugfix", "NOT_RUN"), ("feature", "READY")])
def test_original_green_is_not_bug_reproduction_but_feature_may_start_green(
    tmp_path, kind, expected
):
    _, frozen, profile, workspace = toy(tmp_path, kind=kind, fixed=True)
    result = execute_public_original(frozen, workspace, profile, LOCK, tmp_path / "proof")
    assert result["status"] == expected and result["probe_outcome"] == "green"
    if expected == "NOT_RUN":
        assert "BUGFIX_PUBLIC_REPRODUCTION_NOT_RED" in result["blockers"]


def test_readiness_survives_experiment_registration_but_preserves_authority(tmp_path):
    _, frozen, profile, workspace = toy(tmp_path)
    evidence = tmp_path / "proof"
    result = execute_public_original(frozen, workspace, profile, LOCK, evidence)
    registered = profile.model_copy(update={"experiment_id": "later-registered-experiment"})
    assert result["schema"] == "h12-public-original-readiness-v2"
    validate_public_original_readiness(
        result, evidence, frozen=frozen, profile=registered, image_lock=LOCK
    )
    for changes in (
        {"objective": "different task"},
        {"resources": profile.resources.model_copy(update={"max_model_calls": 21})},
    ):
        with pytest.raises(CatalogError):
            validate_public_original_readiness(
                result, evidence, frozen=frozen,
                profile=registered.model_copy(update=changes), image_lock=LOCK,
            )
    legacy = {
        **result, "schema": "h12-public-original-readiness-v1",
        "profile_sha256": canonical_hash(profile.model_dump(mode="json")),
    }
    validate_public_original_readiness(
        legacy, evidence, frozen=frozen, profile=profile, image_lock=LOCK
    )
    with pytest.raises(CatalogError):
        validate_public_original_readiness(
            legacy, evidence, frozen=frozen, profile=registered, image_lock=LOCK
        )


def test_import_error_is_not_accepted_as_public_red(tmp_path):
    _, frozen, profile, workspace = toy(
        tmp_path,
        asset=PROBE.replace(
            'importlib.import_module("calc")',
            'importlib.import_module("missing_public_dependency")',
        ),
    )
    result = execute_public_original(frozen, workspace, profile, LOCK, tmp_path / "proof")
    assert result["status"] == "NOT_RUN" and result["probe_outcome"] == "invalid"
    assert result["regression_status"] == "passed"


@pytest.mark.parametrize("change", ["log", "argv", "tree", "lease", "image", "source"])
def test_readiness_rechecks_complete_log_receipts_source_and_readonly_authority(tmp_path, change):
    _, frozen, profile, workspace = toy(tmp_path)
    evidence = tmp_path / "proof"
    result = execute_public_original(frozen, workspace, profile, LOCK, evidence)
    assert result["status"] == "READY"
    changed_lock = LOCK
    if change == "log":
        (evidence / "probe.log").write_text("truncated")
    elif change in {"argv", "tree"}:
        raw = json.loads((evidence / "probe-receipt.json").read_text())
        raw["argv" if change == "argv" else "tree_hash"] = (
            ["python3", "other.py"] if change == "argv" else "0" * 64
        )
        (evidence / "probe-receipt.json").write_text(json.dumps(raw))
        result["commands"]["probe"]["receipt_sha256"] = sha(
            (evidence / "probe-receipt.json").read_bytes()
        )
    elif change == "lease":
        result["lease"]["write_paths"] = ["calc.py"]
    elif change == "source":
        result["original_tree_after"] = "0" * 64
    else:
        changed_lock = tmp_path / "changed-lock.json"
        changed_lock.write_text(LOCK.read_text() + "\n")
    with pytest.raises(CatalogError):
        validate_public_original_readiness(
            result, evidence, frozen=frozen, profile=profile, image_lock=changed_lock
        )


def test_explicit_subset_keeps_other_tasks_legacy_and_rejects_unknown_or_missing_refs(
    tmp_path, monkeypatch
):
    task, _, manifest, assets, workspace = fixture(tmp_path)
    runner = controller(tmp_path, task, manifest, assets, workspace, monkeypatch)
    unlisted = {**task, "task_id": "unlisted-node", "language": "javascript"}
    metadata = runner._public_plan_metadata([task, unlisted])
    assert set(metadata) == {task["task_id"]}
    assert runner._profile_for(unlisted, "candidate").public_probe is None
    assert runner._profile_for(unlisted, "candidate").verification_protocol == "legacy"
    with pytest.raises(CatalogError):
        runner._public_plan_metadata([unlisted])
    runner.public_plan_refs = {task["task_id"]: (tmp_path / "missing.json", "0" * 64)}
    with pytest.raises(CatalogError):
        runner._public_plan_metadata([task, unlisted])


def test_generic_runner_prepares_subset_without_model_or_private_judge_and_paid_stays_blocked(
    tmp_path, monkeypatch
):
    task, frozen, _, workspace = toy(tmp_path)
    runner = EvaluationRunner(
        SimpleNamespace(harness=SimpleNamespace()),
        experiment_id="toy-readiness",
        evidence=tmp_path / "controller-evidence",
        image_locks={task["repository"] + "@" + task["base_commit"]: str(LOCK)},
        api_key=None,
        environment_revision=DEV_ENVIRONMENT_V4,
        public_plan_refs={task["task_id"]: (frozen.manifest_path, frozen.manifest_sha256)},
        public_probe_root=frozen.asset_root,
    )
    monkeypatch.setattr(
        runner,
        "_tasks",
        lambda split: [task] if split == "dev" else pytest.fail("No holdout reads"),
    )
    monkeypatch.setattr(runner, "_source", lambda _: workspace)
    monkeypatch.setattr(
        runner,
        "_materialize_into",
        lambda task, source, target: Path(shutil.copytree(source, target)),
    )
    monkeypatch.setattr(runner, "_candidate_fingerprint", lambda: "c" * 64)
    monkeypatch.setattr(runner, "_environment_manifest_hash", lambda: "d" * 64)
    report = runner.prepare_public_readiness(tmp_path / "controller-proof")
    assert report["status"] == "PUBLIC_SUBSET_READY"
    assert report["quality_status"] == "NOT_RUN" and report["full_dev_status"] == "NOT_RUN"
    assert report["paid_dispatch_allowed"] is False
    assert report["zero_model"] is True and report["readiness_only"] is True
    assert report["paid_model_calls"] == 0
    assert report["task_ids"] == [task["task_id"]]
    runner.public_readiness_ref = (
        tmp_path / "controller-proof" / "report.json",
        sha((tmp_path / "controller-proof" / "report.json").read_bytes()),
    )
    runner.experiment_id = "frozen-for-registration"
    runner.validate_public_readiness()
    monkeypatch.setattr(runner, "_live_policy", lambda _: None)
    with pytest.raises(DomainError) as blocked:
        runner._require_dev_readiness()
    assert blocked.value.code == "H12_FULL_DEV_READINESS_REQUIRED"
    monkeypatch.setattr(runner, "_candidate_fingerprint", lambda: "e" * 64)
    with pytest.raises(CatalogError):
        runner.validate_public_readiness()


def test_empty_public_subset_is_rejected_and_legacy_constructor_is_unchanged(tmp_path):
    common = dict(
        engine=SimpleNamespace(harness=SimpleNamespace()),
        experiment_id="toy",
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key=None,
        environment_revision=DEV_ENVIRONMENT_V4,
    )
    legacy = EvaluationRunner(**common)
    assert legacy.public_plan_refs is None and legacy.public_readiness_ref is None
    with pytest.raises(CatalogError):
        EvaluationRunner(**common, public_plan_refs={}, public_probe_root=tmp_path)


def test_readiness_rejects_truncation_mount_epoch_and_replaced_public_asset(tmp_path):
    _, frozen, profile, workspace = toy(tmp_path)
    evidence = tmp_path / "proof"
    result = execute_public_original(frozen, workspace, profile, LOCK, evidence)
    assert result["status"] == "READY"
    for change in ("truncation", "mount", "epoch", "asset"):
        changed = copy.deepcopy(result)
        receipt_path = evidence / "probe-receipt.json"
        receipt_bytes = receipt_path.read_bytes()
        asset_path = frozen.asset_root / frozen.plan.asset_sha256 / "probe.py"
        asset_bytes = asset_path.read_bytes()
        if change == "truncation":
            changed["commands"]["probe"]["log_complete"] = False
        elif change == "mount":
            changed["mount"]["probe_read_only"] = False
        elif change == "epoch":
            raw = json.loads(receipt_bytes)
            raw["provenance"]["execution_epoch"] = 2
            receipt_path.write_text(json.dumps(raw))
            changed["commands"]["probe"]["receipt_sha256"] = sha(receipt_path.read_bytes())
        else:
            asset_path.write_text("# replaced public asset\n")
        try:
            with pytest.raises(CatalogError):
                validate_public_original_readiness(
                    changed,
                    evidence,
                    frozen=frozen,
                    profile=profile,
                    image_lock=LOCK,
                )
        finally:
            receipt_path.write_bytes(receipt_bytes)
            asset_path.write_bytes(asset_bytes)
