"""Exact twelve-task public preparation; no model, Judge or publication."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval.h12_full_dev_readiness import (
    _task_facts,
    command_plan,
    execute_task,
    task_binding,
    validate_full_report,
)
from mgar_v3.quality_eval.h12_public_plan import PUBLIC_ROLES, load_public_plan
from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4, EvaluationRunner
from mgar_v3.quality_eval.harness_v4 import CatalogError

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/r03/sandbox-lock.json"


class ToyRunner(EvaluationRunner):
    def _tasks(self, split):
        assert split == "dev", "Full preparation must never ask for another split"
        return self.tasks

    def _source(self, task):
        return self.source

    def _materialize_into(self, task, source, target):
        return Path(shutil.copytree(source, target))

    def _candidate_fingerprint(self):
        return "c" * 64


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("full-public-readiness")
    source = root / "source"
    source.mkdir()
    (source / "test_smoke.py").write_text(
        "import unittest\nclass Public(unittest.TestCase):\n"
        " def test_original(self): self.assertEqual(1+1,2)\n"
    )
    lock = json.loads(LOCK.read_text())
    runtime = "python-" + ".".join(lock["python_version"].split()[1].split(".")[:2])
    tasks = [
        dict(
            task_id=f"public-toy-{i}",
            split="dev",
            type="feature",
            language="python",
            repository="example/public",
            base_commit="a" * 40,
            goal=f"Add explicitly requested public feature {i}.",
            scope=["feature.py"],
            resources=dict(
                max_model_calls=20,
                max_tool_calls=80,
                max_cost_micros=500000,
                deadline_seconds=60,
                max_context_tokens=32000,
                max_repair_rounds=6,
            ),
            environment=dict(runtime=runtime, verify=["python3 -B -m unittest -q test_smoke"]),
            acceptance=dict(regression=["python3 -B -m unittest -q test_smoke"]),
        )
        for i in range(12)
    ]
    runner = ToyRunner(
        SimpleNamespace(harness=SimpleNamespace()),
        experiment_id="full-readiness-toy",
        evidence=root / "controller",
        image_locks={"example/public@" + "a" * 40: str(LOCK)},
        api_key=None,
        environment_revision=DEV_ENVIRONMENT_V4,
    )
    runner.tasks, runner.source = tasks, source
    report = runner.prepare_full_dev_readiness(root / "proof")
    path = root / "proof/report.json"
    runner.full_dev_readiness_ref = (path, hashlib.sha256(path.read_bytes()).hexdigest())
    return runner, report, root


def test_all_twelve_execute_public_commands_and_valid_full_gate_can_return(prepared, monkeypatch):
    runner, report, _ = prepared
    assert report["status"] == "FULL_DEV_READY" and report["ready_count"] == 12
    assert report["zero_model"] and report["readiness_only"]
    assert report["quality_status"] == "NOT_RUN"
    assert report["paid_model_calls"] == report["cost_micros"] == 0
    assert len(report["tasks"]) == 12
    assert (
        len(
            {
                entry["result"]["commands"]["runtime"]["operation_id"]
                for entry in report["tasks"].values()
            }
        )
        == 12
    )
    assert runner.validate_full_dev_readiness() == report
    monkeypatch.setattr(runner, "_live_policy", lambda split: None)
    monkeypatch.setattr(runner, "_validate_frozen_execution", lambda: {})
    runner._require_dev_readiness()


@pytest.mark.parametrize(
    "change",
    ["eleven", "extra", "source", "profile", "protocol", "image", "log", "tree", "argv", "unknown"],
)
def test_incomplete_stale_or_relabelled_full_report_is_rejected(prepared, tmp_path, change):
    runner, report, root = prepared
    document = copy.deepcopy(report)
    key = sorted(document["tasks"])[0]
    if change == "eleven":
        del document["tasks"][key]
    elif change == "extra":
        document["tasks"]["unfrozen-task"] = document["tasks"][key]
    elif change == "source":
        document["candidate_source_sha256_after"] = "d" * 64
    elif change in {"profile", "protocol", "image"}:
        document["bindings"][key][change] = "changed"
    else:
        document["tasks"][key]["result"]["commands"]["runtime"][change] = "changed"
    bindings = runner._full_dev_bindings()
    with pytest.raises(CatalogError):
        validate_full_report(document, root / "proof", bindings=bindings, candidate_source="c" * 64)


def test_missing_full_report_still_blocks_public_subset_before_paid(prepared, monkeypatch):
    runner, _, _ = prepared
    original = runner.full_dev_readiness_ref
    monkeypatch.setattr(runner, "_live_policy", lambda split: None)
    monkeypatch.setattr(runner, "public_plan_refs", {"a": (Path("missing"), "a" * 64)})
    monkeypatch.setattr(runner, "public_readiness_ref", None)
    runner.full_dev_readiness_ref = None
    try:
        with pytest.raises(DomainError) as blocked:
            runner._require_dev_readiness()
        assert blocked.value.code == "H12_PUBLIC_PLAN_READINESS_REQUIRED"
    finally:
        runner.full_dev_readiness_ref = original


def test_full_task_python_probe_on_go_source_preserves_go_regression_parser(tmp_path):
    from mgar_v3.quality_eval.h12_runner import profile_for

    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "go.mod").write_text("module example/public\n\ngo 1.22.0\n")
    (workspace / "add.go").write_text("package public\nfunc Add(a,b int) int { return a-b }\n")
    (workspace / "add_test.go").write_text(
        'package public\nimport "testing"\n'
        'func TestOriginal(t *testing.T) { if Add(1,0)!=1 {t.Fatal("existing contract")} }\n'
    )
    raw = """import hashlib,json,subprocess,tempfile
from pathlib import Path
asset=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
with tempfile.TemporaryDirectory(dir="/scratch") as directory:
    file=Path(directory)/"main.go"
    file.write_text('package main; import("fmt"; p "example/public"); '
                    'func main(){fmt.Println(p.Add(2,3))}')
    result=subprocess.run(["go","run",str(file)],capture_output=True,text=True,timeout=90)
    status=("error" if result.returncode else "passed"
            if result.stdout.strip()=="5" else "assertion_failed")
print("HARNESS_PUBLIC_PROBE_V1 "+json.dumps({"plan_id":"toy-go-add","asset_sha256":asset,
      "cases":[{"id":"addition","status":status}]}))
raise SystemExit(2 if status=="error" else 1 if status=="assertion_failed" else 0)
"""
    asset_sha = hashlib.sha256(raw.encode()).hexdigest()
    assets = tmp_path / "assets"
    asset = assets / asset_sha / "probe.py"
    asset.parent.mkdir(parents=True)
    asset.write_text(raw)
    task = dict(
        task_id="public-go-probe-toy",
        split="dev",
        type="bugfix",
        language="go",
        repository="example/public",
        base_commit="a" * 40,
        goal="Add two integers correctly.",
        scope=["add.go"],
        resources=dict(
            max_model_calls=20,
            max_tool_calls=80,
            max_cost_micros=500000,
            deadline_seconds=180,
            max_context_tokens=32000,
            max_repair_rounds=6,
        ),
        environment=dict(runtime="go-1.22", verify=["go test ./..."]),
        acceptance=dict(regression=["go test ./..."]),
    )
    legacy = profile_for(task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4)

    def sha(data):
        return hashlib.sha256(data).hexdigest()

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
        "regression_argv": list(legacy.public_test_argv),
        "public_basis": [{"path": "add.go", "sha256": sha((workspace / "add.go").read_bytes())}],
        "probe": {"plan_id": "toy-go-add", "asset_sha256": asset_sha, "case_ids": ["addition"]},
    }
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(document))
    public = load_public_plan(
        manifest,
        sha(manifest.read_bytes()),
        task=task,
        asset_root=assets,
        regression_argv=legacy.public_test_argv,
    )
    profile = profile_for(
        task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4, public_probe=public.plan
    ).model_copy(update={"sandbox_scratch_mb": 256})
    image_map = ROOT / ".local/h12/public-dev-20261113/image-map.json"
    go_lock = Path(
        json.loads(image_map.read_text())["spf13/pflag@f8a990678a6d44385313e9b19e40737f889f9b14"]
    )
    binding = task_binding(task, profile, workspace, go_lock, DEV_ENVIRONMENT_V4, public)
    result = execute_task(workspace, binding=binding, public=public, evidence=tmp_path / "proof")
    assert result["status"] == "READY", result
    assert result["command_facts"]["public_probe"]["outcome"] == "red"
    assert result["command_facts"]["public_01"]["tests_run"] == 1
    assert result["command_facts"]["public_01"]["status"] == "passed"
    assert result["command_facts"]["probe_runtime"]["status"] == "passed"
    assert (
        _task_facts(result, tmp_path / "proof", binding=binding, public=public)["status"] == "READY"
    )


def test_raw_environment_deadline_command_tree_and_truncation_are_rechecked(tmp_path):
    from mgar_v3.quality_eval.h12_runner import profile_for

    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "test_public.py").write_text(
        "import unittest\nclass Public(unittest.TestCase):\n"
        " def test_original(self): self.assertEqual(2,2)\n"
    )
    lock = json.loads(LOCK.read_text())
    runtime = "python-" + ".".join(lock["python_version"].split()[1].split(".")[:2])
    task = dict(
        task_id="public-evidence-toy",
        split="dev",
        type="feature",
        language="python",
        repository="example/public",
        base_commit="a" * 40,
        goal="Add a public feature.",
        scope=["feature.py"],
        resources=dict(
            max_model_calls=20,
            max_tool_calls=80,
            max_cost_micros=500000,
            deadline_seconds=60,
            max_context_tokens=32000,
            max_repair_rounds=6,
        ),
        environment=dict(runtime=runtime, verify=["python3 -B -m unittest -q test_public"]),
        acceptance=dict(regression=["python3 -B -m unittest -q test_public"]),
    )
    profile = profile_for(task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    binding = task_binding(task, profile, workspace, LOCK, DEV_ENVIRONMENT_V4, None)
    evidence = tmp_path / "proof"
    result = execute_task(workspace, binding=binding, public=None, evidence=evidence)
    assert result["status"] == "READY"
    for failure in (
        "environment_hash",
        "argv",
        "tree_hash",
        "duration_seconds",
        "deadline",
        "truncated",
    ):
        changed = copy.deepcopy(result)
        path = evidence / "runtime-receipt.json"
        original = path.read_bytes()
        if failure == "deadline":
            changed["elapsed_seconds"] = 61
        elif failure == "truncated":
            changed["commands"]["runtime"]["log_complete"] = False
        else:
            raw = json.loads(original)
            raw[failure] = (
                ["python3", "other.py"]
                if failure == "argv"
                else 61
                if failure == "duration_seconds"
                else "0" * 64
            )
            path.write_text(json.dumps(raw))
            changed["commands"]["runtime"]["receipt_sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        try:
            with pytest.raises(CatalogError):
                _task_facts(changed, evidence, binding=binding, public=None)
        finally:
            path.write_bytes(original)


def test_declared_failed_pytest_log_cannot_turn_exit137_into_original_red(tmp_path):
    from mgar_v3.quality_eval.h12_runner import profile_for

    workspace = tmp_path / "source"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests/test_public.py").write_text(
        "def test_public_defect():\n    assert 1 == 2\n"
    )
    image_map = ROOT / ".local/h12/public-dev-20261113/image-map.json"
    image_lock = Path(
        json.loads(image_map.read_text())["pallets/click@8c1a0a7abbc1c36f70d1f65f3604acc46c5ce6ab"]
    )
    version = json.loads(image_lock.read_text())["python_version"].split()[1]
    task = dict(
        task_id="public-declared-exit-toy",
        split="dev",
        type="bugfix",
        language="python",
        repository="example/public",
        base_commit="a" * 40,
        goal="Repair the declared public defect.",
        scope=["source.py"],
        resources=dict(
            max_model_calls=20,
            max_tool_calls=80,
            max_cost_micros=500000,
            deadline_seconds=60,
            max_context_tokens=32000,
            max_repair_rounds=6,
        ),
        environment=dict(
            runtime="python-" + ".".join(version.split(".")[:2]),
            verify=["python -m pytest tests/test_public.py"],
        ),
        acceptance=dict(regression=["python -m pytest tests/test_public.py"]),
    )
    profile = profile_for(task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    binding = task_binding(task, profile, workspace, image_lock, DEV_ENVIRONMENT_V4, None)
    evidence = tmp_path / "proof"
    result = execute_task(workspace, binding=binding, public=None, evidence=evidence)
    assert result["status"] == "READY", result
    assert result["command_facts"]["public_01"]["declared_public_red"] is True
    path = evidence / "public_01-receipt.json"
    raw = json.loads(path.read_text())
    assert raw["exit_code"] == 1 and raw["status"] == "failed"
    raw["exit_code"] = 137
    path.write_text(json.dumps(raw))
    result["commands"]["public_01"]["receipt_sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    facts = _task_facts(result, evidence, binding=binding, public=None)
    assert facts["status"] == "NOT_RUN"
    assert facts["command_facts"]["public_01"]["declared_public_red"] is False


def test_command_plan_uses_actual_primary_and_declared_regression_not_replaced_verify():
    from mgar_v3.quality_eval.h12_runner import profile_for

    task = dict(
        task_id="public-refactor-plan-toy",
        split="dev",
        type="refactor",
        language="python",
        repository="example/public",
        base_commit="a" * 40,
        goal="Extract a helper, preserve behavior.",
        scope=["source.py"],
        resources=dict(
            max_model_calls=20,
            max_tool_calls=80,
            max_cost_micros=500000,
            deadline_seconds=60,
            max_context_tokens=32000,
            max_repair_rounds=6,
        ),
        environment=dict(
            runtime="python-3.12", verify=["python -m pytest tests/no_longer_exists.py"]
        ),
        acceptance=dict(regression=["python -m pytest tests/"]),
    )
    base = profile_for(task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    actual = base.model_copy(
        update={"public_test_argv": ("python3", "-B", "-m", "pytest", "tests/")}
    )
    plan = command_plan(task, actual, DEV_ENVIRONMENT_V4)
    suites = [row for row in plan if row["kind"] == "tests"]
    assert len(suites) == 2
    assert tuple(suites[0]["argv"]) == actual.public_test_argv
    from mgar_v3.quality_eval.h12_judge import public_regression_commands

    assert suites[1]["argv"] == public_regression_commands(task, DEV_ENVIRONMENT_V4)[0]
    assert "no_longer_exists" not in json.dumps(plan)


def test_feature_declared_public_inventory_red_is_ready_and_changed_hash_is_rejected(
    tmp_path,
    monkeypatch,
):
    from mgar_v3.quality_eval import h12
    from mgar_v3.quality_eval.h12_runner import profile_for

    raw = (
        b"def test_assertion():\n    assert 1 == 2\n"
        b"def test_target_api():\n    raise TypeError('new argument absent')\n"
    )
    workspace = tmp_path / "source"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests/test_feature_public.py").write_bytes(raw)
    public_root = tmp_path / "catalog"
    reference = public_root / "experiments/v35-harness/dev/public/feature.py"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    task = dict(
        task_id="public-feature-inventory-toy",
        split="dev",
        type="feature",
        language="python",
        repository="example/public",
        base_commit="a" * 40,
        goal="Add the missing public API argument.",
        scope=["source.py"],
        resources=dict(
            max_model_calls=20,
            max_tool_calls=80,
            max_cost_micros=500000,
            deadline_seconds=60,
            max_context_tokens=32000,
            max_repair_rounds=6,
        ),
        environment=dict(runtime="python-3.12", verify=["python -m pytest tests/"]),
        acceptance=dict(regression=["python -m pytest tests/"]),
        judge=dict(kind="v35_reference", reference_task="synthetic-frozen-reference"),
    )
    inventory = [
        dict(
            task_id="synthetic-frozen-reference",
            repository=task["repository"],
            base_commit=task["base_commit"],
            public_targets=["tests/test_feature_public.py"],
            public_files=["dev/public/feature.py"],
        )
    ]

    def metadata(root, split):
        assert root == public_root and split == "dev"
        return inventory

    hashes = {"dev/public/feature.py": expected}
    monkeypatch.setattr(h12, "_public_v35_metadata", metadata)
    monkeypatch.setattr(h12, "_public_v35_hashes", lambda root, split: hashes)
    image_map = ROOT / ".local/h12/public-dev-20261113/image-map.json"
    lock = Path(
        json.loads(image_map.read_text())["pallets/click@8c1a0a7abbc1c36f70d1f65f3604acc46c5ce6ab"]
    )
    profile = profile_for(task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    binding = task_binding(
        task, profile, workspace, lock, DEV_ENVIRONMENT_V4, None, public_root=public_root
    )
    assert binding["public_test_assets"] == {"tests/test_feature_public.py": expected}
    result = execute_task(workspace, binding=binding, public=None, evidence=tmp_path / "proof")
    assert result["status"] == "READY", result
    assert result["command_facts"]["public_01"]["tests_failed"] == 2
    assert result["command_facts"]["public_01"]["declared_public_red"] is True
    hashes["dev/public/feature.py"] = "0" * 64
    with pytest.raises(CatalogError):
        task_binding(
            task, profile, workspace, lock, DEV_ENVIRONMENT_V4, None, public_root=public_root
        )
