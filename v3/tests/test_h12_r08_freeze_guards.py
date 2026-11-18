"""Paid admission and development/holdout barrier reject unverified local assertions."""

import json
import time

import pytest
from test_h12_r07_service import GOOD, ControlledModel, edit, setup
from test_harness_v4_h10 import _call, _stop

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval.h12_r08 import R08Runner, asset, document, reference
from mgar_v3.quality_eval.h12_r08_controls import validate_controls
from mgar_v3.quality_eval.h12_r08_engineering import validate_engineering
from mgar_v3.quality_eval.h12_r08_retest import repair_coverage


@pytest.mark.parametrize("fault", ["omit-new-check", "substitute-passed-for-failure"])
def test_targeted_repair_cannot_omit_or_substitute_required_checks(fault):
    old = {"old-pass": "passed", "old-fail": "failure"}
    current = set(old) | {"new-check"}
    good = {"old-fail": "passed", "new-check": "passed"}
    repair_coverage(old, current, good, set())
    bad = dict(good)
    if fault == "omit-new-check":
        del bad["new-check"]
    else:
        del bad["old-fail"]
        bad["old-pass"] = "passed"
    with pytest.raises(ValueError, match="exactly failed/new/changed"):
        repair_coverage(old, current, bad, set())


@pytest.mark.parametrize("fault", ["changed", "symlink", "traversal", "extra-field"])
def test_frozen_reference_cannot_switch_file_or_escape_root(tmp_path, fault):
    p = tmp_path / "value.json"
    p.write_text('{"status":"PASS"}')
    ref = reference(tmp_path, p)
    if fault == "changed":
        p.write_text('{"status":"FAIL"}')
    elif fault == "symlink":
        p.rename(tmp_path / "real.json")
        p.symlink_to(tmp_path / "real.json")
    elif fault == "traversal":
        ref["path"] = "../" + tmp_path.name + "/value.json"
    else:
        ref["trusted"] = "true"
    with pytest.raises(DomainError):
        asset(tmp_path, ref)


def test_duplicate_json_authority_is_not_interpreted_as_last_value(tmp_path):
    p = tmp_path / "candidate.json"
    p.write_text('{"phase":"PREPARATION","phase":"FROZEN"}')
    with pytest.raises(DomainError, match="Duplicate"):
        document(tmp_path, reference(tmp_path, p))


@pytest.mark.parametrize("cache", ["missing", "stale-question"])
def test_real_pg_pending_question_remains_answerable_without_status_cache(
    engine, settings, tmp_path, cache
):
    from test_h12_r07_service import BASE

    model = ControlledModel([
        _call("ask_user", {"question": "Confirm the declared public behavior."}),
        edit(GOOD, BASE), _stop("The declared public behavior is repaired."),
    ])
    service, _, request = setup(engine, settings, tmp_path, model)
    task_id = service.submit_task(request)["task_id"]
    runtime = service._runtimes[task_id]
    status_path = service.runs / task_id / "status.json"
    questions, cached, result = [], {}, {}
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = engine.harness.get(task_id)
            questions = [m for m in row["inbox"]["messages"]
                         if m["kind"] == "question" and m["status"] == "waiting"]
            if questions and status_path.is_file():
                cached = json.loads(status_path.read_bytes())
                if cached.get("question_id") == questions[0]["message_id"]:
                    break
            time.sleep(0.02)
        assert questions and cached.get("question_id") == questions[0]["message_id"]
        run_id = row["run_id"]
        if cache == "missing":
            status_path.unlink()
        else:
            status_path.write_text(json.dumps({
                "status": "waiting_input", "question_id": "q-stale-cache", "question": "old",
            }))
        current = service.get_task(task_id)
        assert current["status"] == "waiting_input"
        assert current["question_id"] == questions[0]["message_id"]
        assert current["question"] == questions[0]["text"]
        service.post_message(
            task_id, kind="answer", text="Preserve the declared behavior.",
            reply_to=current["question_id"],
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = service.get_task(task_id)
            if result.get("candidate_status"):
                break
            time.sleep(0.05)
        assert result.get("candidate_status") == "completed", result
        assert engine.harness.get(task_id)["run_id"] == run_id
        assert result["usage_known"] and result["reserved_micros"] == 0
    finally:
        if runtime.thread is not None:
            runtime.thread.join(timeout=1)
            if runtime.thread.is_alive():
                service.cancel_task(task_id)
                runtime.thread.join(timeout=10)
            assert not runtime.thread.is_alive()


def test_preparation_and_live_flag_do_not_create_paid_authority():
    runner = R08Runner.__new__(R08Runner)
    runner.execute_live = True
    runner.api_key = "controlled-not-used"
    runner.candidate = {"phase": "PREPARATION"}
    runner.bundles = {"dev": {}}
    with pytest.raises(DomainError) as caught:
        runner._live_policy("holdout")
    assert caught.value.code == "H12_R08_FULL_READINESS_REQUIRED"


def test_self_reported_twelve_controls_are_insufficient(tmp_path):
    p = tmp_path / "controls.json"
    p.write_text(json.dumps({"status": "PASS", "passed_tasks": 12, "tasks": {}}))
    tasks = [{"task_id": f"independent-{i}"} for i in range(12)]
    bundle = {"document": {"controls": reference(tmp_path, p)}, "tasks": tasks}
    with pytest.raises(DomainError) as caught:
        validate_controls(tmp_path, bundle)
    assert caught.value.code == "H12_R08_JUDGE_CONTROLS_REQUIRED"


def test_a_handwritten_engineering_pass_cannot_replace_registered_junit_and_raw_logs(tmp_path):
    directory = tmp_path / "evidence/attempt"
    directory.mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    (config / "verification.json").write_text(
        json.dumps({"HV4-H12-R08": {"tests": ["tests/test_public.py"]}})
    )
    source = "a" * 64
    commit = "b" * 40
    result = directory / "result.json"
    result.write_text(
        json.dumps(
            {
                "task": "HV4-H12-R08",
                "status": "PASS",
                "engineering_gate": "PASS",
                "command_exit_code": 0,
                "exit_code": 0,
                "source_sha256": source,
                "finished_source_sha256": source,
                "git_commit": commit,
                "counts": {"tests": 1, "failures": 0, "errors": 0, "skipped": 0},
            }
        )
    )
    candidate = {
        "engineering": reference(tmp_path, result),
        "source_sha256": source,
        "source_commit": commit,
        "engineering_evidence": {},
    }
    with pytest.raises(DomainError) as caught:
        validate_engineering(tmp_path, candidate)
    assert caught.value.code == "H12_R08_ENGINEERING_REQUIRED"


def test_real_pg_terminal_result_overrides_a_forged_development_pass(engine, settings, tmp_path):
    from test_h12_r07_service import BASE

    model = ControlledModel([edit(GOOD, BASE), _stop("The public fixture is repaired.")])
    service, profile, request = setup(engine, settings, tmp_path, model)
    result = service.create_task(request)
    assert result["status"] == "blocked" and result["candidate_status"] == "completed"
    task = {
        "task_id": profile.task_id,
        "split": "dev",
        "type": profile.task_kind,
        "repository": "toy/r08",
        "base_commit": "0" * 40,
        "scope": ["calc.py"],
        "goal": profile.objective,
        "resources": profile.resources.model_dump(),
        "judge_sha256": "f" * 64,
        "environment": {"verify": []},
        "acceptance": {"required": list(profile.public_requirements), "regression": []},
    }
    runner = R08Runner.__new__(R08Runner)
    runner.engine, runner.store = engine, engine.harness
    runner.experiment_id, runner.root = profile.experiment_id, tmp_path
    runner.evidence = tmp_path / "evaluation"
    runner.candidate_ref = {"path": "candidate.json", "sha256": "f" * 64}
    runner._tasks = lambda split: [task]
    runner._profile_for = lambda task, arm: profile
    original = runner._score_payload(task, profile, result)
    assert original["status"] == "FAIL"
    score = runner.evidence / "candidate/dev" / profile.task_id / "score.json"
    score.parent.mkdir(parents=True)
    score.write_text(json.dumps(original | {"status": "PASS"}))
    with pytest.raises(DomainError, match="stored first-sample score differs"):
        runner.audit_development()
    assert engine.harness.tasks()[0]["result_hash"]
    assert not (runner.evidence / "development-holdout-barrier.json").exists()


@pytest.mark.parametrize("between_calls", [False, True])
def test_real_pg_frozen_guard_stops_before_reservation_or_dispatch(
    engine, settings, tmp_path, between_calls
):
    manifest = tmp_path / "frozen-controller.json"
    manifest.write_text('{"source":"frozen"}')
    ref = reference(tmp_path, manifest)

    def operator_changes_inputs(request):
        # Controlled test operator mutation, outside the Worker tool registry.
        manifest.write_text('{"source":"changed"}')
        return _call("read_file", {"path": "calc.py"})

    steps = [operator_changes_inputs, _stop("unused")] if between_calls else [_stop("unused")]
    model = ControlledModel(steps)
    service, profile, request = setup(engine, settings, tmp_path, model)
    service.before_model_request = lambda request: asset(tmp_path, ref)
    if not between_calls:
        manifest.write_text('{"source":"changed"}')
    result = service.create_task(request)
    assert result["status"] == "blocked"
    assert result["reason"] == "H12_R08_FROZEN_INPUT_REQUIRED"
    assert len(model.requests) == int(between_calls)
    group = engine.harness.get_experiment(profile.experiment_id)
    assert group["usage"]["unknown_calls"] == 0 and group["reserved_micros"] == 0
    assert group["used_calls"] == int(between_calls)


@pytest.mark.parametrize("change", ["add-new-file", "omit-old-file", "hide-new-file"])
def test_targeted_collection_preserves_old_registered_files_and_exposes_new_checks(change):
    from mgar_v3.quality_eval.h12_r08_retest import collection_scope

    old = {"targets": ["tests/old.py"], "nodeids": ["tests/old.py::test_original"]}
    targets = ["tests/old.py", "tests/new.py"]
    if change == "omit-old-file":
        targets = ["tests/new.py"]
    if change == "hide-new-file":
        targets = ["tests/old.py"]
    current = {"targets": targets,
               "nodeids": ["tests/old.py::test_original", "tests/new.py::test_added"]}
    if change == "add-new-file":
        collection_scope(old, current, old["targets"], targets)
    else:
        with pytest.raises(ValueError, match="omit old scope or hide added"):
            collection_scope(old, current, old["targets"], targets)


@pytest.mark.parametrize("allowed", [True, False])
def test_targeted_dispatch_refuses_other_development_tasks_before_any_execution(
    monkeypatch, allowed,
):
    from mgar_v3.quality_eval.h12_runner import EvaluationRunner

    runner = R08Runner.__new__(R08Runner)
    runner.candidate = {"development_dispatch_allowlist": ["permitted"]}
    calls = []

    def execute(self, task, arm):
        calls.append(task["task_id"])
        return {"status": "controlled"}

    monkeypatch.setattr(EvaluationRunner, "evaluate_task", execute)
    task = {"split": "dev", "task_id": "permitted" if allowed else "not-selected"}
    if allowed:
        assert runner.evaluate_task(task, "candidate") == {"status": "controlled"}
        assert calls == ["permitted"]
    else:
        with pytest.raises(DomainError) as caught:
            runner.evaluate_task(task, "candidate")
        assert caught.value.code == "H12_R08_TASK_NOT_AUTHORIZED" and not calls
