"""Synthetic opt-in public plans never import hidden evaluation assets."""

from __future__ import annotations

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.quality_eval.h12_public_plan import (
    PUBLIC_ROLES,
    load_public_plan,
    public_plan_readiness,
    validate_public_original,
)
from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4, EvaluationRunner, profile_for
from mgar_v3.quality_eval.harness_v4 import CatalogError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture(tmp_path):
    from mgar_v3.harness.workspace import _scan, _tree_hash

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("value = 'original'\n")
    raw = b"# A public synthetic check, independently authored for the declared goal.\n"
    asset_hash = sha(raw)
    asset_root = tmp_path / "public-assets"
    asset = asset_root / asset_hash / "probe.py"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(raw)
    task = {
        "task_id": "synthetic-public-plan",
        "split": "dev",
        "type": "bugfix",
        "repository": "example/project",
        "base_commit": "a" * 40,
        "language": "python",
        "goal": "Preserve an explicitly stated synthetic behavior.",
        "scope": ["source.py"],
        "resources": {
            "max_model_calls": 20,
            "max_tool_calls": 80,
            "max_cost_micros": 500000,
            "deadline_seconds": 300,
            "max_context_tokens": 32000,
            "max_repair_rounds": 6,
        },
        "environment": {"verify": ["python3 -m pytest tests/test_public.py"]},
        "acceptance": {"regression": []},
    }
    profile = profile_for(task, "synthetic", "candidate")
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
        "regression_argv": list(profile.public_test_argv),
        "public_basis": [
            {"path": "source.py", "sha256": sha((workspace / "source.py").read_bytes())}
        ],
        "probe": {
            "schema_version": "harness-public-probe-plan-v1",
            "plan_id": "synthetic-probe",
            "asset_sha256": asset_hash,
            "runner": "python-json-v1",
            "case_ids": ["behavior"],
        },
    }
    manifest = tmp_path / "public-plan.json"
    manifest.write_text(json.dumps(document))
    return task, document, manifest, asset_root, workspace


def load(task, manifest, asset_root):
    profile = profile_for(task, "synthetic", "candidate")
    return load_public_plan(
        manifest,
        sha(manifest.read_bytes()),
        task=task,
        asset_root=asset_root,
        regression_argv=profile.public_test_argv,
    )


def test_plan_binds_public_goal_resources_assets_and_original_tree_without_claiming_execution(
    tmp_path,
):
    task, document, manifest, asset_root, workspace = fixture(tmp_path)
    frozen = load(task, manifest, asset_root)
    validate_public_original(frozen, workspace)
    ready = public_plan_readiness(frozen, workspace)
    assert ready["status"] == "NOT_RUN" and ready["asset_status"] == "BOUND"
    assert ready["reproduction"] == ready["regression"] == "NOT_RUN"
    assert ready["behavior"] == "AWAITING_IMPLEMENTATION"
    assert ready["plan_hash"] == frozen.plan.plan_hash
    legacy = profile_for(task, "synthetic", "candidate")
    opted = profile_for(task, "synthetic", "candidate", public_probe=frozen.plan)
    assert "public_probe" not in legacy.model_dump(mode="json")
    assert "verification_protocol" not in legacy.model_dump(mode="json")
    assert opted.public_probe == frozen.plan
    assert opted.verification_protocol == "public-probe-v1"
    assert opted.resources == legacy.resources and opted.write_paths == legacy.write_paths
    assert opted.public_test_argv == legacy.public_test_argv
    assert frozen.freeze_metadata()["manifest_sha256"] == sha(manifest.read_bytes())


@pytest.mark.parametrize("field", ["task_id", "repository", "base_commit", "goal", "resources"])
def test_task_binding_cannot_be_reused_for_another_goal_or_larger_budget(tmp_path, field):
    task, _, manifest, asset_root, _ = fixture(tmp_path)
    changed = dict(task)
    changed[field] = (
        {**task[field], "max_model_calls": 21}
        if field == "resources"
        else "b" * 40
        if field == "base_commit"
        else "different"
    )
    with pytest.raises(CatalogError):
        load(changed, manifest, asset_root)


@pytest.mark.parametrize(
    "failure",
    [
        "manifest_hash",
        "asset_missing",
        "asset_changed",
        "symlink",
        "private_basis",
        "unknown_key",
        "shell_argv",
        "draft",
    ],
)
def test_changed_missing_or_private_inputs_are_rejected_before_execution(tmp_path, failure):
    task, document, manifest, asset_root, workspace = fixture(tmp_path)
    asset = asset_root / document["probe"]["asset_sha256"] / "probe.py"
    if failure == "manifest_hash":
        with pytest.raises(CatalogError):
            load_public_plan(
                manifest,
                "0" * 64,
                task=task,
                asset_root=asset_root,
                regression_argv=profile_for(task, "synthetic", "candidate").public_test_argv,
            )
        return
    if failure == "asset_missing":
        asset.unlink()
    elif failure == "asset_changed":
        asset.write_text("changed")
    elif failure == "symlink":
        asset.unlink()
        asset.symlink_to(workspace / "source.py")
    elif failure == "private_basis":
        document["public_basis"][0]["path"] = "verification/private.py"
    elif failure == "unknown_key":
        document["private_answer"] = "never projected"
    elif failure == "shell_argv":
        document["regression_argv"] += [";", "echo", "unsafe"]
    elif failure == "draft":
        document["status"] = "DRAFT"
    manifest.write_text(json.dumps(document))
    with pytest.raises(CatalogError):
        load(task, manifest, asset_root)


def test_only_named_asset_is_exposed_and_changed_original_or_asset_is_rejected(tmp_path):
    task, _, manifest, asset_root, workspace = fixture(tmp_path)
    (asset_root / "unrelated-private-sentinel.txt").write_text("must not enter a plan")
    frozen = load(task, manifest, asset_root)
    metadata = json.dumps(frozen.freeze_metadata())
    assert "sentinel" not in metadata and "must not enter" not in metadata
    (workspace / "source.py").write_text("changed original")
    with pytest.raises(CatalogError):
        validate_public_original(frozen, workspace)


def test_plan_loader_refuses_non_dev_input_without_reading_other_assets(tmp_path):
    task, _, manifest, asset_root, _ = fixture(tmp_path)
    task["split"] = "holdout"
    with pytest.raises(CatalogError):
        load(task, manifest, asset_root)


def controller(tmp_path, task, manifest, asset_root, workspace, monkeypatch):
    runner = EvaluationRunner(
        SimpleNamespace(harness=SimpleNamespace()),
        experiment_id="synthetic-controller",
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key=None,
        environment_revision=DEV_ENVIRONMENT_V4,
        public_plan_refs={task["task_id"]: (manifest, sha(manifest.read_bytes()))},
        public_probe_root=asset_root,
    )
    monkeypatch.setattr(
        runner,
        "_tasks",
        lambda split: (
            [task]
            if split == "dev"
            else pytest.fail("This controller must not read any other split")
        ),
    )
    monkeypatch.setattr(runner, "_source", lambda _: workspace)

    def materialize(task, source, target):
        shutil.copytree(source, target)
        return target

    monkeypatch.setattr(runner, "_materialize_into", materialize)
    return runner


def test_controller_binds_real_original_before_freezing_and_refuses_missing_plans(
    tmp_path, monkeypatch
):
    task, _, manifest, asset_root, workspace = fixture(tmp_path)
    runner = controller(tmp_path, task, manifest, asset_root, workspace, monkeypatch)
    bound = runner._public_plan_metadata([task])
    assert (
        bound[task["task_id"]]["original_tree_sha256"]
        == load(task, manifest, asset_root).original_tree_sha256
    )
    profile = runner._profile_for(task, "candidate")
    assert profile.public_probe is not None and profile.verification_protocol == "public-probe-v1"
    # Plans are an explicit subset: unlisted tasks remain frozen legacy tasks.
    unlisted = {**task, "task_id": "unlisted-plan"}
    assert runner._public_plan_metadata([task, unlisted]) == bound
    assert runner._profile_for(unlisted, "candidate").public_probe is None
    reference = runner.public_plan_refs[task["task_id"]]
    runner.public_plan_refs[task["task_id"]] = (tmp_path / "missing.json", "0" * 64)
    with pytest.raises(CatalogError):
        runner._public_plan_metadata([task, unlisted])
    runner.public_plan_refs[task["task_id"]] = reference
    (workspace / "source.py").write_text("unreviewed source")
    with pytest.raises(CatalogError):
        runner._public_plan_metadata([task])


def test_controller_never_treats_bound_assets_as_executed_model_readiness(tmp_path, monkeypatch):
    from mgar_v3.domain import DomainError

    task, _, manifest, asset_root, workspace = fixture(tmp_path)
    runner = controller(tmp_path, task, manifest, asset_root, workspace, monkeypatch)
    monkeypatch.setattr(runner, "_live_policy", lambda _: None)
    runner._dev_readiness_passed = True
    with pytest.raises(DomainError) as blocked:
        runner._require_dev_readiness()
    assert blocked.value.code == "H12_PUBLIC_PLAN_READINESS_REQUIRED"
    assert runner.execute_live is False and runner.api_key is None


def test_frozen_controller_rechecks_asset_bytes_and_cannot_drop_opt_in(tmp_path, monkeypatch):
    from mgar_v3.domain import DomainError
    from mgar_v3.quality_eval import h12
    from mgar_v3.quality_eval.h12_live import MODEL_OBSERVATION

    task, document, manifest, asset_root, workspace = fixture(tmp_path)
    runner = controller(tmp_path, task, manifest, asset_root, workspace, monkeypatch)
    model = runner.root / MODEL_OBSERVATION
    config = {
        "candidate_source": "synthetic-source",
        "image_locks": {},
        "environment_revision": DEV_ENVIRONMENT_V4,
        "environment_manifest_sha256": "env",
        "independent_registry_sha256": None,
        "model_observation_sha256": sha(model.read_bytes()),
        "public_plans": runner._public_plan_metadata([task]),
        "public_probe_root": str(asset_root.resolve()),
    }
    runner.store.get_experiment = lambda _: {"config": config}
    monkeypatch.setattr(runner, "_candidate_fingerprint", lambda: "synthetic-source")
    monkeypatch.setattr(runner, "_environment_manifest_hash", lambda: "env")
    monkeypatch.setattr(h12, "frozen_hashes", lambda _: {"status": "PASS"})
    assert runner._validate_frozen_execution() == config
    refs = runner.public_plan_refs
    runner.public_plan_refs = None
    with pytest.raises(DomainError) as downgrade:
        runner._validate_frozen_execution()
    assert downgrade.value.code == "H12_PUBLIC_PLAN_CHANGED"
    runner.public_plan_refs = refs
    (asset_root / document["probe"]["asset_sha256"] / "probe.py").write_text("changed script")
    with pytest.raises(CatalogError):
        runner._validate_frozen_execution()


def test_public_basis_cannot_read_a_protected_file_and_duplicate_json_is_rejected(tmp_path):
    task, document, manifest, asset_root, _ = fixture(tmp_path)
    document["public_basis"][0]["path"] = ".env"
    manifest.write_text(json.dumps(document))
    with pytest.raises(CatalogError):
        load(task, manifest, asset_root)
    manifest.write_text('{"schema":"h12-public-plan-v1","schema":"other"}')
    with pytest.raises(CatalogError):
        load(task, manifest, asset_root)
