"""H12 trusted profiles and absent-preimage edits; controlled providers only."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.adapters import InMemoryBudgetLedger
from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.coding_loop import ServicePlan
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CancellationToken,
    CapabilityGrant,
    EvaluationProfile,
    EvaluationResources,
    ModelUsage,
    TaskBinding,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.service import (
    CodingTaskRequest,
    DeterministicCodingModel,
    HarnessCodingService,
    SessionApplication,
    WorkspaceBoundBroker,
)
from mgar_v3.harness.workspace import WorkspaceRegistry


def resources():
    return EvaluationResources(
        max_model_calls=4,
        max_tool_calls=10,
        max_cost_micros=1_000_000,
        deadline_seconds=600,
        max_context_tokens=8192,
        max_repair_rounds=1,
        max_output_tokens=2048,
    )


def profile(**changes):
    return EvaluationProfile(
        experiment_id="h12-runtime-test",
        task_id="fixture-dev-0",
        split="dev",
        arm="new",
        task_kind="inspect",
        objective="Inspect source.py",
        resources=resources(),
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        **changes,
    )


def workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    return registry, registration, registry.create_task_workspace(registration, "task-h12")


def test_creation_requires_explicit_absent_preimage_protocol():
    with pytest.raises(ValidationError):
        WorkspaceEdit(path="new.py", content="new\n")
    with pytest.raises(ValidationError):
        WorkspaceEdit(path="new.py", content="new\n", create=True, expected_sha256="a" * 64)
    assert WorkspaceEdit(path="new.py", content="new\n", create=True).expected_sha256 is None


def test_new_file_is_visible_in_snapshot_and_reload(tmp_path):
    registry, registration, task = workspace(tmp_path)
    updated = task.apply(
        [WorkspaceEdit(path="package/new.py", create=True, content="answer = 42\n")],
        expected_tree=task.snapshot().tree_hash,
    )
    assert updated.files["package/new.py"] == "answer = 42\n"
    assert registry.open_task_workspace(registration, "task-h12").snapshot() == updated


@pytest.mark.parametrize("first", ["source.py", "package/new.py"])
def test_mixed_creation_and_modification_reconcile_without_duplicate_insert(
    tmp_path,
    monkeypatch,
    first,
):
    registry, registration, task = workspace(tmp_path)
    artifacts = ArtifactStore(tmp_path / "cas")
    changes = {
        "source.py": WorkspaceEdit(
            path="source.py", expected_sha256=sha256_text("value = 1\n"), content="value = 2\n"
        ),
        "package/new.py": WorkspaceEdit(path="package/new.py", create=True, content="new = 3\n"),
    }
    other = next(name for name in changes if name != first)
    intent = task.prepare_recovery_edit(
        [changes[first], changes[other]],
        expected_tree=task.snapshot().tree_hash,
        artifacts=artifacts,
    )

    def fault(point):
        if point == "edit.after_file_replace:" + first:
            raise SystemExit("crash after one atomic replacement")

    with pytest.raises(SystemExit):
        task.apply_recovery_edit(intent, artifacts=artifacts, fault_hook=fault)
    reopened = registry.open_task_workspace(registration, "task-h12", allow_unreconciled=True)
    result = reopened.apply_recovery_edit(intent, artifacts=artifacts)
    assert result.files == {"source.py": "value = 2\n", "package/new.py": "new = 3\n"}
    assert reopened.apply_recovery_edit(intent, artifacts=artifacts) == result


def test_create_collision_and_parent_symlink_preserve_existing_data(tmp_path):
    _, _, task = workspace(tmp_path)
    tree = task.snapshot().tree_hash
    with pytest.raises(DomainError):
        task.apply(
            [WorkspaceEdit(path="source.py", create=True, content="overwrite")], expected_tree=tree
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    (task.root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        task.apply(
            [WorkspaceEdit(path="escape/new.py", create=True, content="escape")], expected_tree=tree
        )
    assert (task.root / "source.py").read_text() == "value = 1\n"
    assert not (outside / "new.py").exists()


def test_profile_grant_has_frozen_scope_and_deadline(tmp_path, engine):
    _, registration, task = workspace(tmp_path)
    frozen = profile().model_copy(
        update={
            "task_kind": "feature",
            "write_paths": ("source.py", "package/new.py"),
            "public_test_argv": ("python3", "-m", "unittest"),
            "resources": resources().model_copy(update={"deadline_seconds": 2400}),
        }
    )
    service = HarnessCodingService(
        tmp_path / "service",
        actor="owner",
        image_lock=tmp_path / "unused-lock",
        engine=engine,
        evaluation_profile=frozen,
    )
    grant = service._grant(registration, "feature", frozen.public_test_argv)
    assert grant.write_paths == frozen.write_paths and grant.max_seconds == 2400
    assert not grant.can_write("tests/hidden.py")
    assert grant.can_write("package/new.py")
    assert (
        service._test_command(task, CodingTaskRequest("unused", frozen.objective), "feature")
        == frozen.public_test_argv
    )


def freeze(engine, *, deadline_seconds=600):
    first = profile()
    first = first.model_copy(update={
        "resources": first.resources.model_copy(update={"deadline_seconds": deadline_seconds}),
    })
    matrix = [
        first.model_copy(
            update={
                "task_id": f"fixture-{split}-{index}",
                "split": split,
                "arm": arm,
            }
        )
        for split in ("dev", "holdout")
        for index in range(12)
        for arm in ("baseline", "new")
    ]
    engine.harness.create_experiment(
        first.experiment_id,
        {
            "schema": "harness-h12-experiment-v1",
            "profiles": [entry.model_dump(mode="json") for entry in matrix],
        },
        {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12_000_000},
    )
    return next(
        entry for entry in matrix if entry.task_id == "fixture-dev-0" and entry.arm == "new"
    )


@pytest.mark.parametrize(
    "judge_status,judge_tree",
    [(status, "match") for status in ("PASS", "FAIL", "NOT_RUN", "UNKNOWN")]
    + [("PASS", "wrong"), ("PASS", None)],
)
def test_real_engine_final_gate_seals_before_judge_and_retains_candidate(
    engine,
    tmp_path,
    monkeypatch,
    judge_status,
    judge_tree,
):
    frozen = freeze(engine)
    source = tmp_path / "live-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    from mgar_v3.harness.commands import CommandSession

    closed = []
    close = CommandSession.close

    def record_close(session):
        closed.append(True)
        return close(session)

    monkeypatch.setattr(CommandSession, "close", record_close)

    def final_gate(task, candidate):
        row = engine.harness.tasks()[0]
        assert closed and row["sealed_tree_hash"] == task.snapshot().tree_hash
        assert candidate["status"] == "completed"
        with pytest.raises(DomainError):
            engine.harness.start_operation(
                row["task_id"], row["epoch"], "after-seal", "tool", "a" * 64
            )
        return {
            "status": judge_status,
            "tree_hash": candidate["tree_after"] if judge_tree == "match" else judge_tree,
            "evidence": "controlled independent verdict",
        }

    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
        allow_paid=True,
        live_api_key="fixture-do-not-send",
        evaluation_profile=frozen,
        final_gate=final_gate,
        model_factory=lambda request: DeterministicCodingModel(
            task_kind="inspect",
            target_path="source.py",
            old_text="",
            new_text="",
            test_argv=(),
        ),
    )
    project = service.register_project(str(source))
    # No target_path/old_text/new_text hints are required by the live port.
    result = service.create_task(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective=frozen.objective,
            task_kind="inspect",
            mode="live",
        )
    )
    assert result["candidate_status"] == "completed"
    expected_verdict = "FAIL" if judge_status == "PASS" and judge_tree != "match" else judge_status
    assert result["independent_gate"]["status"] == expected_verdict
    assert result["status"] == ("completed" if expected_verdict == "PASS" else "blocked")
    row = engine.harness.tasks()[0]
    assert row["sealed_tree_hash"] == result["tree_after"]
    with pytest.raises(DomainError):
        engine.harness.claim(row["task_id"])


def test_run_app_port_uses_owned_h06_lifecycle_and_does_not_cancel_parent():
    task = TaskBinding(
        task_id="task-app",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository="local/app",
        workspace_id="app",
        base_commit="a" * 40,
        acceptance_revision="frozen",
        objective="Run the local service",
        task_kind="run_app",
        requirements=(),
    )
    calls = []

    class Session:
        def start_service(self, request, grant, cancellation, probe):
            calls.append(("start", request.argv, probe.port, probe.path))
            self.token = cancellation
            return SimpleNamespace(
                address="127.0.0.1:8123", handle=SimpleNamespace(handle_id="owned")
            )

        def cancel(self, handle_id):
            calls.append(("stop", handle_id))
            self.token.cancel()
            return SimpleNamespace(log_sha256="a" * 64)

    parent = CancellationToken()
    budgets = InMemoryBudgetLedger(BudgetLimits())
    application = SessionApplication(Session(), task, budgets, None)
    grant = CapabilityGrant(
        actor="owner",
        workspace_id="app",
        repository="local/app",
        read_paths=("",),
        write_paths=("server.py",),
        execute=True,
        allowed_commands=("python3",),
    )
    started = application.start(
        ServicePlan(argv=("python3", "server.py"), port=8123, path="/health"),
        "b" * 64,
        grant,
        parent,
    )
    stopped = application.stop(started)
    assert started.ready and stopped.stopped and not parent.cancelled
    assert calls == [("start", ("python3", "server.py"), 8123, "/health"), ("stop", "owned")]
    assert budgets.snapshot(task.task_id).tool_calls == 2


@pytest.mark.parametrize("reported_input", [31, 8192])
def test_provider_token_window_is_recorded_and_enforced_after_settlement(
    engine, tmp_path, reported_input,
):
    frozen = freeze(engine)
    source = tmp_path / "token-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")

    class ObservedModel(DeterministicCodingModel):
        def complete(self, request, cancellation):
            response = super().complete(request, cancellation)
            return response.model_copy(update={
                "usage": ModelUsage(input_tokens=reported_input, output_tokens=7),
            })

    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
        allow_paid=True,
        live_api_key="fixture-do-not-send",
        evaluation_profile=frozen,
        final_gate=lambda workspace, candidate: {
            "status": "PASS", "tree_hash": candidate["tree_after"],
        },
        model_factory=lambda request: ObservedModel(
            task_kind="inspect", target_path="source.py", old_text="", new_text="", test_argv=(),
        ),
    )
    project = service.register_project(str(source))
    result = service.create_task(CodingTaskRequest(
        project_id=project["project_id"], objective=frozen.objective,
        task_kind="inspect", mode="live",
    ))
    assert result["input_tokens"] == reported_input * result["model_calls"]
    assert result["output_tokens"] == 7 * result["model_calls"]
    assert result["peak_context_tokens"] == reported_input + 7
    assert result["estimated_peak_input_tokens"] > 0
    assert result["repair_rounds"] == 0
    assert engine.harness.get_experiment(frozen.experiment_id)["usage"]["unknown_calls"] == 0
    if reported_input == 8192:
        assert result["status"] == "incomplete" and result["reason"] == "context_resource_exceeded"
        assert result["model_calls"] == 1
    else:
        assert result["status"] == "completed"


def test_feedback_repair_round_counts_once_across_tool_exchanges(engine, tmp_path):
    frozen = freeze(engine)
    source = tmp_path / "repair-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")

    class Model(DeterministicCodingModel):
        calls = 0

        def complete(self, request, cancellation):
            self.calls += 1
            if self.calls == 1:
                return self._text("I have not read any evidence yet.")
            return super().complete(request, cancellation)

    model = Model(
        task_kind="inspect", target_path="source.py", old_text="", new_text="", test_argv=(),
    )
    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
        allow_paid=True,
        live_api_key="fixture-do-not-send",
        evaluation_profile=frozen,
        final_gate=lambda workspace, candidate: {
            "status": "PASS", "tree_hash": candidate["tree_after"],
        },
        model_factory=lambda request: model,
    )
    project = service.register_project(str(source))
    result = service.create_task(CodingTaskRequest(
        project_id=project["project_id"], objective=frozen.objective,
        task_kind="inspect", mode="live",
    ))
    assert result["status"] == "completed"
    assert result["repair_rounds"] == 1 and result["model_calls"] == 3
    assert result["no_progress_rounds"] == 1


@pytest.mark.parametrize("exit_code,output_bytes", [(0, 20), (3, 20), (0, 2000)])
def test_oneshot_application_has_real_exit_logs_and_owned_cleanup(
    settings, tmp_path, exit_code, output_bytes,
):
    from mgar_v3.harness.commands import CommandSession

    _, registration, task_workspace = workspace(tmp_path)
    ancestor = task_workspace.root
    assert str(ancestor).startswith("/tmp/")
    while str(ancestor) != "/tmp":
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    task = TaskBinding(
        task_id="task-oneshot", thread_id=uuid4(), run_id=uuid4(), execution_epoch=1,
        actor="owner", repository=registration.repository, workspace_id=registration.workspace_id,
        base_commit=registration.base_commit, acceptance_revision="frozen", objective="Run app",
        task_kind="run_app", requirements=(),
    )
    grant = CapabilityGrant(
        actor="owner", workspace_id=registration.workspace_id, repository=registration.repository,
        read_paths=("",), write_paths=("source.py",), execute=True, allowed_commands=("python3",),
    )
    lease = issue_lease(
        mode="modify_test", actor="owner", workspace_id=registration.workspace_id,
        repository=registration.repository, execution_epoch=1, read_paths=("",),
        write_paths=("source.py",), allowed_commands=("python3",), approved_by="owner",
        approval_ref="oneshot-test", max_output_bytes=1024,
    )
    broker = WorkspaceBoundBroker(task_workspace, lease, lock_path=settings.harness_image_lock)
    session = CommandSession(broker)
    budgets = InMemoryBudgetLedger(BudgetLimits())
    parent = CancellationToken()
    application = SessionApplication(session, task, budgets, None)
    try:
        started = application.start(
            ServicePlan(
                argv=("python3", "-c", f"print('x'*{output_bytes});raise SystemExit({exit_code})"),
                mode="oneshot", timeout_seconds=10,
            ),
            task_workspace.snapshot().tree_hash, grant, parent,
        )
        handle = session.poll(started.command_handle_id)
        assert handle.exit_code == exit_code and handle.state != "running"
        assert handle.log_sha256 == started.log_sha256
        assert started.ready == (exit_code == 0 and output_bytes < 1024)
        assert started.started and started.stopped
        name = session._records[handle.handle_id].launched.name
        assert broker.inspect_command(name)[0] == "missing"
        stopped = application.stop(started)
        assert stopped.stopped and not parent.cancelled
        assert budgets.snapshot(task.task_id).tool_calls == 2
    finally:
        session.close()


@pytest.mark.parametrize("deadline_seconds", [1800, 2400])
def test_long_task_deadline_preserves_six_hundred_second_command_lease(
    engine, tmp_path, deadline_seconds,
):
    frozen = freeze(engine, deadline_seconds=deadline_seconds)
    source = tmp_path / "long-deadline-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    observed = []

    class Model(DeterministicCodingModel):
        def complete(self, request, cancellation):
            runtime = service._runtimes[request.task.task_id]
            assert runtime.lease.timeout_seconds == 600
            row = engine.harness.get(request.task.task_id)
            assert row["limits"]["max_seconds"] == deadline_seconds
            assert row["grant_scope"]["max_seconds"] == deadline_seconds
            remaining = (row["deadline_at"] - row["created_at"]).total_seconds()
            assert abs(remaining - deadline_seconds) < 1
            observed.append(request.operation_id)
            return super().complete(request, cancellation)

    judged = []

    def final_gate(workspace, candidate):
        judged.append(candidate["tree_after"])
        return {"status": "PASS", "tree_hash": candidate["tree_after"]}

    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner", image_lock=engine.settings.harness_image_lock, engine=engine,
        allow_paid=True, live_api_key="fixture-do-not-send", evaluation_profile=frozen,
        final_gate=final_gate,
        model_factory=lambda request: Model(
            task_kind="inspect", target_path="source.py", old_text="", new_text="", test_argv=(),
        ),
    )
    project = service.register_project(str(source))
    result = service.create_task(CodingTaskRequest(
        project_id=project["project_id"], objective=frozen.objective,
        task_kind="inspect", mode="live",
    ))
    assert result["status"] == "completed" and len(observed) == 2
    assert judged == [result["tree_after"]]
