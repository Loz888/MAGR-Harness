"""R04 actual PG/service loop, using free scripted adapters and scoped containers."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_h12_r04_runtime import PYTHON_HTTP
from test_harness_v4_h10 import ScriptedModel, _call, _stop
from test_harness_v4_h11_service import _InjectedCrash

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import EvaluationProfile, EvaluationResources
from mgar_v3.harness.entrypoints import EntryPointChecker
from mgar_v3.harness.lifecycle_types import HTTPExpectation, LifecyclePolicy
from mgar_v3.harness.lifecycle_validation import validate_lifecycle_result
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService, SessionApplication

NODE_LOCK = Path(__file__).parents[1] / (
    ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json"
)


class BoundedModel(ScriptedModel):
    def complete(self, request, cancellation=None):
        if not self.steps:
            self.steps.append(
                _stop("Preserve source evidence in marker.py and the remaining blocker.")
            )
        return super().complete(request, cancellation)


def service(engine, settings, model, *, node=False, profile=None, fault=None):
    return HarnessCodingService(
        settings.harness_root,
        actor="owner",
        engine=engine,
        image_lock=NODE_LOCK if node else settings.harness_image_lock,
        model_factory=lambda request: model,
        allow_paid=profile is not None,
        live_api_key="controlled-unused" if profile else "",
        evaluation_profile=profile,
        fault_hook=fault,
    )


def feature_request(value, tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "marker.py").write_text("VALUE=7\n")
    (source / "package.json").write_text(
        json.dumps({"name": "r04-entry-fixture", "version": "1.0.0"})
    )
    (source / "test_smoke.py").write_text(
        "import unittest\nclass Smoke(unittest.TestCase):\n"
        " def test_old_suite(self):\n  import marker\n  self.assertEqual(marker.VALUE,7)\n"
    )
    project = value.register_project(str(source))
    return CodingTaskRequest(
        project_id=project["project_id"],
        objective="Add examples/entry.cjs that can be loaded independently; preserve the suite.",
        task_kind="feature",
        target_path="examples/entry.cjs",
        approved=True,
        old_text="controlled fixture hint",
        new_text="controlled fixture hint",
        test_command="python3 -B -m unittest -v",
    )


def feature_model(content):
    def edit(request):
        return _call(
            "apply_edits",
            {
                "expected_tree": request.workspace_tree,
                "edits": [
                    {
                        "path": "examples/entry.cjs",
                        "create": True,
                        "expected_sha256": None,
                        "content": content,
                    }
                ],
            },
        )

    return BoundedModel(
        [
            _call("read_file", {"path": "marker.py"}),
            edit,
            _call("run_tests", {"command_id": "tests"}),
            _stop("Added examples/entry.cjs; marker.py and the public suite are preserved."),
        ]
    )


@pytest.mark.parametrize("valid", [False, True])
def test_pg_old_suite_cannot_replace_new_module_load(engine, settings, tmp_path, valid):
    model = feature_model(
        "module.exports={value:7}\n" if valid else "module.exports=require('../../')\n"
    )
    value = service(engine, settings, model, node=True)
    result = value.create_task(feature_request(value, tmp_path))
    if valid:
        assert result["status"] == "completed", (result["reason"], result["summary"])
    else:
        assert result["status"] != "completed"
    assert result["tests"]["failed"] == 0 and result["tests"]["run"] > 0
    batches = result["entrypoint_batches"]
    assert len(batches) == 1 and len(batches[0]["receipts"]) == 1
    receipt = batches[0]["receipts"][0]
    assert receipt["status"] == ("PASS" if valid else "FAIL")
    assert receipt["tree_hash"] == result["tree_after"]
    assert receipt["acceptance_hash"] == result["acceptance_contract"]["acceptance_hash"]
    assert receipt["cleanup"]["container_removed"] and receipt["cleanup"]["port_released"]
    ops = engine.harness.operations(result["task_id"])
    actual = [op for op in ops if op["operation_id"] == receipt["operation_id"]]
    assert len(actual) == 1 and actual[0]["state"] == "known" and actual[0]["result_hash"]
    assert engine.artifacts.read(receipt["log_hash"])


def app_profile(engine):
    profile = EvaluationProfile(
        experiment_id="r04-toy-" + uuid4().hex[:12],
        task_id="r04-toy-dev-0",
        split="dev",
        arm="new",
        task_kind="run_app",
        objective='Run app.py; verify Ready with actual port, GET / returning "ok", '
        "and SIGTERM cleanup.",
        resources=EvaluationResources(
            max_model_calls=8,
            max_tool_calls=24,
            max_cost_micros=1_000_000,
            deadline_seconds=120,
            max_context_tokens=32768,
            max_repair_rounds=1,
            max_output_tokens=2048,
        ),
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        write_paths=("app.py",),
        public_test_argv=("python3", "-B", "-m", "unittest", "-v"),
        service_argv=("python3", "-B", "app.py"),
        service_port=0,
        service_timeout_seconds=4,
        lifecycle_policy=LifecyclePolicy(
            entry_timeout_seconds=3, shutdown_seconds=1, cleanup_seconds=2
        ),
        lifecycle_routes=(HTTPExpectation(path="/", body="ok", content_type="text/plain"),),
        acceptance_protocol="contract-v1",
    )
    # Disposable synthetic matrix: no benchmark assets and no provider calls.
    profiles = [
        profile.model_copy(
            update={"task_id": f"r04-toy-{split}-{index}", "split": split, "arm": arm}
        )
        for split in ("dev", "holdout")
        for index in range(12)
        for arm in ("baseline", "new")
    ]
    engine.harness.create_experiment(
        profile.experiment_id,
        {
            "schema": "harness-h12-experiment-v1",
            "profiles": [item.model_dump(mode="json") for item in profiles],
        },
        {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12_000_000},
    )
    return profile


def app_request(value, tmp_path, source):
    project = tmp_path / "project"
    project.mkdir()
    # Importable module; the independently started entry is the exact same final file.
    (project / "app.py").write_text(
        'if __name__ == "__main__":\n' + "".join(" " + line + "\n" for line in source.splitlines())
    )
    (project / "test_smoke.py").write_text(
        "import unittest\nclass Smoke(unittest.TestCase):\n"
        " def test_old_suite(self):self.assertTrue(True)\n"
    )
    registration = value.register_project(str(project))
    return CodingTaskRequest(
        project_id=registration["project_id"],
        objective="Run app.py; verify "
        'Ready with actual port, GET / returning "ok", and SIGTERM cleanup.',
        task_kind="run_app",
        mode="live",
        target_path="app.py",
        approved=True,
        test_command="python3 -B -m unittest -v",
    )


@pytest.mark.parametrize("ignores_term", [False, True])
def test_pg_ready_and_cleanup_are_independent_final_tree_requirements(
    engine,
    settings,
    tmp_path,
    ignores_term,
):
    source = PYTHON_HTTP
    if ignores_term:
        source = source.replace(
            "signal.signal(signal.SIGTERM,stop)", "signal.signal(signal.SIGTERM,signal.SIG_IGN)"
        )
    model = BoundedModel(
        [
            _call("read_file", {"path": "app.py"}),
            _call("run_tests", {"command_id": "tests"}),
            _stop("app.py supplies the entry evidence."),
        ]
    )
    profile = app_profile(engine)
    value = service(engine, settings, model, profile=profile)
    result = value.create_task(app_request(value, tmp_path, source))
    assert (result["candidate_status"] == "completed") is (not ignores_term), (
        result["reason"],
        result["summary"],
    )
    assert result["status"] != "completed"  # Independent quality is never asserted here.
    evidence = result["receipts"]
    assert any(r["tests_run"] == 1 for r in evidence)
    process = result["context_state"]
    assert process is not None
    lifecycle_ops = [
        op
        for op in engine.harness.operations(result["task_id"])
        if op["kind"] == "application" and ":app:" in op["operation_id"]
    ]
    start = next(op for op in lifecycle_ops if op["operation_id"].endswith(":start"))["receipt"]
    stop = next(op for op in lifecycle_ops if op["operation_id"].endswith(":stop"))["receipt"]
    assert start["ready"] and start["lifecycle"]["ready"]["port"]
    assert start["lifecycle"]["ready"]["port"] == start["lifecycle"]["ready"]["log_port"]
    cleanup = stop["lifecycle"]["cleanup"]
    assert cleanup["graceful"] is (not ignores_term)
    assert cleanup["processes_exited"] and cleanup["port_released"] and cleanup["container_removed"]
    assert cleanup["tree_hash"] == result["tree_after"]
    assert stop["log_sha256"] and engine.artifacts.read(stop["log_sha256"])
    assert result["entrypoint_batches"][-1]["status"] == "PASS"
    if ignores_term:
        with pytest.raises(DomainError, match="entry/Ready/cleanup"):
            validate_lifecycle_result(result, profile)
    else:
        validate_lifecycle_result(result, profile)
        # Tampered final scope or discarded facts cannot pass the scorer.
        for field in ("entrypoint_batches", "service_receipts", "tree_after"):
            invalid = deepcopy(result)
            invalid[field] = "0" * 64 if field == "tree_after" else []
            with pytest.raises(DomainError):
                validate_lifecycle_result(invalid, profile)


def test_pg_known_entry_probe_survives_restart_without_duplicate_execution(
    engine,
    settings,
    tmp_path,
    monkeypatch,
):
    original = EntryPointChecker.check
    crashed = False

    def interrupt(self, *args, **kwargs):
        nonlocal crashed
        result = original(self, *args, **kwargs)
        if not crashed:
            crashed = True
            raise _InjectedCrash("known entry result before loop checkpoint")
        return result

    monkeypatch.setattr(EntryPointChecker, "check", interrupt)
    model = feature_model("module.exports={value:7}\n")
    value = service(engine, settings, model, node=True)
    with pytest.raises(_InjectedCrash):
        value.create_task(feature_request(value, tmp_path))
    row = engine.harness.tasks()[0]
    before = [
        op for op in engine.harness.operations(row["task_id"]) if ":entry:" in op["operation_id"]
    ]
    assert before and all(op["state"] == "known" for op in before)
    calls = len(model.requests)
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        result = service(replacement, settings, model, node=True).recover_task(row["task_id"])
        assert result["status"] == "completed", (result["reason"], result["summary"])
        assert len(model.requests) == calls and result["model_calls"] == calls
        after = [
            op
            for op in replacement.harness.operations(row["task_id"])
            if ":entry:" in op["operation_id"]
        ]
        assert [op["result_hash"] for op in after] == [op["result_hash"] for op in before]
        assert result["reserved_micros"] == 0 and result["usage_known"]
    finally:
        replacement.close()


def test_pg_known_lifecycle_cleanup_survives_restart_without_duplicate_start(
    engine,
    settings,
    tmp_path,
    monkeypatch,
):
    original = SessionApplication.start
    crashed = False

    def interrupt(self, *args, **kwargs):
        nonlocal crashed
        receipt = original(self, *args, **kwargs)
        if not crashed:
            crashed = True
            assert receipt.lifecycle.cleanup.container_removed
            raise _InjectedCrash("known lifecycle result before loop checkpoint")
        return receipt

    monkeypatch.setattr(SessionApplication, "start", interrupt)
    model = BoundedModel(
        [
            _call("read_file", {"path": "app.py"}),
            _call("run_tests", {"command_id": "tests"}),
            _stop("app.py provides the verified entry and routes."),
        ]
    )
    profile = app_profile(engine)
    value = service(engine, settings, model, profile=profile)
    with pytest.raises(_InjectedCrash):
        value.create_task(app_request(value, tmp_path, PYTHON_HTTP))
    row = engine.harness.tasks()[0]
    before = [op for op in engine.harness.operations(row["task_id"]) if op["kind"] == "command"]
    start = next(
        op
        for op in engine.harness.operations(row["task_id"])
        if op["operation_id"].endswith(":start")
    )
    assert start["state"] == "known"
    calls = len(model.requests)
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        result = service(replacement, settings, model, profile=profile).recover_task(row["task_id"])
        assert result["candidate_status"] == "completed", (result["reason"], result["summary"])
        assert result["status"] == "blocked" and result["independent_gate"]["status"] == "NOT_RUN"
        assert len(model.requests) == calls and result["model_calls"] == calls
        after = [
            op for op in replacement.harness.operations(row["task_id"]) if op["kind"] == "command"
        ]
        assert [(op["operation_id"], op["result_hash"]) for op in after] == [
            (op["operation_id"], op["result_hash"]) for op in before
        ]
        assert result["reserved_micros"] == 0 and result["usage_known"]
        validate_lifecycle_result(result, profile)
    finally:
        replacement.close()


def test_pg_unknown_lifecycle_is_reconciled_without_retry_or_graceful_claim(
    engine,
    settings,
    tmp_path,
):
    def interrupt(point, payload):
        if point == "command.after_start":
            operations = engine.harness.operations(payload["task_id"])
            if any(
                op["kind"] == "command"
                and op["state"] == "dispatched"
                and op["intent"].get("request", {}).get("runtime_control", {}).get("mode") == "http"
                for op in operations
            ):
                raise _InjectedCrash("owned HTTP command started before handle registration")

    model = BoundedModel(
        [
            _call("read_file", {"path": "app.py"}),
            _call("run_tests", {"command_id": "tests"}),
            _stop("app.py supplies the evidence."),
        ]
    )
    profile = app_profile(engine)
    value = service(engine, settings, model, profile=profile, fault=interrupt)
    with pytest.raises(_InjectedCrash):
        value.create_task(app_request(value, tmp_path, PYTHON_HTTP))
    row = engine.harness.tasks()[0]
    before = engine.harness.operations(row["task_id"])
    command = next(op for op in before if op["kind"] == "command" and ":app:" in op["operation_id"])
    assert (
        command["intent"]["launched"]["runtime_control"]["assets_hash"]
        == profile.lifecycle_policy.control_assets_hash
    )
    calls = len(model.requests)
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        with pytest.raises(DomainError):
            service(replacement, settings, model, profile=profile).recover_task(row["task_id"])
        after = replacement.harness.operations(row["task_id"])
        assert len(model.requests) == calls
        assert [op["operation_id"] for op in after if op["kind"] == "command"] == [
            op["operation_id"] for op in before if op["kind"] == "command"
        ]
        app = next(op for op in after if op["operation_id"].endswith(":start"))
        assert app["state"] == "unknown" and not app["receipt"]
        # The original command reconciler owns the exact old labels, removes
        # that container and retains its raw output, never a Ready assertion.
        process = next(op for op in after if op["operation_id"] == command["operation_id"])
        assert process["state"] == "known" and process["receipt"]["raw_log_hash"]
    finally:
        replacement.close()
