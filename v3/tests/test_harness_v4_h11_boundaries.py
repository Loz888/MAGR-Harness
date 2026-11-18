"""Independent negative boundaries over the real Engine/PG authority."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    CancellationToken,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TaskBinding,
    VerificationRequirement,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.ledger import PersistentBudgetLedger
from mgar_v3.harness.persistence import DurableModel, EngineEvents, EngineInbox
from mgar_v3.harness.recovery_io import RecoveryIO
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService


def task(engine, tmp_path):
    root = tmp_path / "boundary-workspace"
    root.mkdir()
    registration = {
        "workspace_id": "boundary-workspace",
        "repository": "local/boundary",
        "base_commit": "",
    }
    request = {
        "project_id": "boundary-workspace",
        "objective": "inspect",
        "mode": "deterministic",
        "workspace_root": str(root),
    }
    grant = {
        "actor": "owner",
        "workspace_id": "boundary-workspace",
        "repository": "local/boundary",
    }
    limits = {
        "max_model_calls": 12,
        "max_tool_calls": 48,
        "max_cost_micros": 100,
        "max_seconds": 600,
    }
    store = engine.harness
    store.create("boundary-task", registration, request, grant, limits)
    row = store.begin("boundary-task")
    store.record_tree("boundary-task", row["epoch"], "a" * 64)
    return store, row


@pytest.mark.parametrize("kind", ["instruction", "answer"])
def test_received_message_cannot_be_stranded_by_completed_result(engine, tmp_path, kind):
    store, row = task(engine, tmp_path)
    inbox = EngineInbox(store, "boundary-task")
    inbox.post(message_id="accepted-before-terminal", kind=kind, text="additional requirement")
    # The acceptance transaction wins the same row lock that finish acquires.
    # A result may not report success while that accepted work is unconsumed.
    with pytest.raises(DomainError):
        store.finish(
            "boundary-task", row["epoch"], "completed", engine.artifacts.put(b"success"), "a" * 64
        )
    assert store.get("boundary-task")["status"] == "running"
    assert inbox.messages()[0]["status"] == "received"


def test_terminal_winner_rejects_late_post(engine, tmp_path):
    store, row = task(engine, tmp_path)
    store.finish(
        "boundary-task", row["epoch"], "completed", engine.artifacts.put(b"success"), "a" * 64
    )
    with pytest.raises(DomainError, match="cannot accept"):
        EngineInbox(store, "boundary-task").post(
            message_id="too-late",
            kind="instruction",
            text="late work",
        )


def test_live_delivery_once_and_uncheckpointed_restart_replay(engine, tmp_path):
    source = tmp_path / "inbox-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
    )
    project = service.register_project(str(source))
    runtime = service._prepare(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective="Inspect source.py",
            task_kind="inspect",
            target_path="source.py",
        )
    )
    store = engine.harness
    row = store.begin(runtime.task_id)
    runtime.durable_row = row
    runtime.inbox = EngineInbox(store, runtime.task_id, epoch=row["epoch"])
    runtime.events = EngineEvents(store, runtime.task_id, row["epoch"])
    runtime.inbox.post(
        message_id="instruction-1", kind="instruction", text="exactly once per runtime"
    )
    checkpoint = service._checkpoint(runtime)
    first = checkpoint("after_model", 10)
    assert first.instructions == ("exactly once per runtime",)
    assert checkpoint("before_model", 11).instructions == ()
    # Rebuild the runtime, as after process loss before a selected checkpoint.
    restarted = replace(
        runtime,
        delivered_instructions=set(),
        inbox=EngineInbox(store, runtime.task_id, epoch=row["epoch"]),
    )
    replayed = service._checkpoint(restarted)("before_model", 10)
    assert replayed.instructions == ("exactly once per runtime",)
    assert service._checkpoint(restarted)("after_model", 11).instructions == ()
    store.save_checkpoint(
        runtime.task_id,
        row["epoch"],
        engine.artifacts.put(b"checkpoint"),
        1,
        tree_hash=runtime.workspace.snapshot().tree_hash,
        inbox_revision=replayed.revision,
    )
    assert not EngineInbox(store, runtime.task_id, epoch=row["epoch"]).pending_instructions()


def test_old_worker_cannot_mark_new_epoch_instruction_applied(engine, tmp_path):
    store, row = task(engine, tmp_path)
    old_worker = EngineInbox(store, "boundary-task", epoch=row["epoch"])
    store.pause("boundary-task")
    resumed = store.claim("boundary-task")
    current = EngineInbox(store, "boundary-task", epoch=resumed["epoch"])
    current.post(message_id="new-epoch", kind="instruction", text="new task instruction")
    with pytest.raises(DomainError):
        old_worker.mark_applied("new-epoch", 1)
    assert current.messages()[0]["status"] == "received"
    assert [message["message_id"] for message in current.pending_instructions()] == ["new-epoch"]


def test_old_worker_cannot_publish_success_after_takeover(engine, tmp_path):
    store, row = task(engine, tmp_path)
    store.pause("boundary-task")
    current = store.claim("boundary-task")
    with pytest.raises(DomainError):
        store.finish(
            "boundary-task",
            row["epoch"],
            "completed",
            engine.artifacts.put(b"stale success"),
            "a" * 64,
        )
    after = store.get("boundary-task")
    assert after["epoch"] == current["epoch"]
    assert after["status"] == "running" and after["result_hash"] is None


def test_known_model_result_cannot_be_reused_for_different_tree(engine, tmp_path):
    store, row = task(engine, tmp_path)
    binding = TaskBinding(
        task_id="boundary-task",
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        execution_epoch=row["epoch"],
        actor="owner",
        repository="local/boundary",
        workspace_id="boundary-workspace",
        base_commit="a" * 40,
        acceptance_revision="boundary-v1",
        objective="inspect",
        task_kind="inspect",
        requirements=(VerificationRequirement(requirement_id="boundary-test"),),
    )
    request = ModelRequest(
        operation_id="boundary-task:model:1",
        task=binding,
        messages=(ModelMessage(role="user", content="inspect original tree"),),
        tools=(),
        model_id="fixture",
        price_version="fixture-v1",
        max_output_tokens=16,
        reservation_micros=10,
        context_revision="context-1",
        workspace_tree="a" * 64,
    )
    calls = []

    class Model:
        def complete(self, request, cancellation):
            calls.append(request.workspace_tree)
            return ModelResponse(text="original", usage=ModelUsage(cost_micros=3))

    ledger = PersistentBudgetLedger(store, "boundary-task", row["epoch"])
    ledger.reserve("boundary-task", request.operation_id, "model", reserved_micros=10)
    adapter = DurableModel(Model(), store, "boundary-task", row["epoch"], lambda *args: None)
    assert adapter.complete(request, CancellationToken()).text == "original"
    changed = request.model_copy(update={"workspace_tree": "b" * 64})
    with pytest.raises(DomainError):
        adapter.complete(changed, CancellationToken())
    assert calls == ["a" * 64]


@pytest.mark.parametrize("late_change", ["tree", "instruction"])
def test_real_service_rechecks_terminal_boundary(engine, tmp_path, late_change):
    source = tmp_path / "terminal-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    invoked = []
    service = None

    def fault(point, payload):
        if point != "result_before_commit":
            return
        row = engine.harness.tasks()[0]
        invoked.append(point)
        if late_change == "tree":
            from pathlib import Path

            (Path(row["request"]["workspace_root"]) / "source.py").write_text("value = 99\n")
        else:
            service.post_message(
                row["task_id"],
                message_id="just-before-final",
                kind="instruction",
                text="This instruction must be handled before completion",
            )

    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
        fault_hook=fault,
    )
    project = service.register_project(str(source))
    result = None
    try:
        result = service.create_task(
            CodingTaskRequest(
                project_id=project["project_id"],
                objective="Inspect source.py",
                task_kind="inspect",
                target_path="source.py",
            )
        )
    except DomainError:
        pass
    assert invoked == ["result_before_commit"]
    assert result is None or result["status"] != "completed"
    assert engine.harness.tasks()[0]["status"] != "succeeded"


def test_selected_checkpoint_rejects_rollback_to_historical_known_edit_tree(engine, tmp_path):
    source = tmp_path / "historical-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
    )
    project = service.register_project(str(source))
    runtime = service._prepare(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective="Update source.py",
            task_kind="bugfix",
            approved=True,
            target_path="source.py",
            test_command="python3 -B -m unittest",
        )
    )
    store = engine.harness
    row = store.begin(runtime.task_id)
    io = RecoveryIO(
        store,
        runtime.task_id,
        row["epoch"],
        runtime.workspace,
        artifacts=engine.artifacts,
        run_id=str(row["run_id"]),
    )
    historical = io.edit(
        [
            WorkspaceEdit(
                path="source.py", expected_sha256=sha256_text("value = 1\n"), content="value = 2\n"
            )
        ],
        expected_tree=runtime.workspace.snapshot().tree_hash,
        operation_id=runtime.task_id + ":tool:1:1:edit",
    )
    # A later selected state is authoritative. The older known edit receipt
    # remains historical evidence and cannot authorize reverting that state.
    current = runtime.workspace.apply(
        [
            WorkspaceEdit(
                path="source.py", expected_sha256=sha256_text("value = 2\n"), content="value = 3\n"
            )
        ],
        expected_tree=historical.tree_hash,
    )
    checkpoint = {
        "task_id": runtime.task_id,
        "run_id": str(row["run_id"]),
        "continuation": {"request": {"operation_id": runtime.task_id + ":model:3"}},
    }
    store.save_checkpoint(
        runtime.task_id,
        row["epoch"],
        engine.artifacts.put(json.dumps(checkpoint).encode()),
        1,
        tree_hash=current.tree_hash,
        inbox_revision=0,
    )
    (runtime.workspace.root / "source.py").write_text("value = 2\n")
    store.pause(runtime.task_id)
    with pytest.raises(DomainError) as error:
        service.recover_task(runtime.task_id)
    assert error.value.code == "RECOVERY_TREE_CONFLICT"
    assert store.get(runtime.task_id)["tree_hash"] == current.tree_hash
    assert store.get(runtime.task_id)["status"] == "paused"
    assert (runtime.workspace.root / "source.py").read_text() == "value = 2\n"


def test_resigned_manifest_metadata_cannot_replace_pg_selected_identity(engine, tmp_path):
    source = tmp_path / "manifest-source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
    )
    project = service.register_project(str(source))
    runtime = service._prepare(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective="Inspect source.py",
            task_kind="inspect",
            target_path="source.py",
        )
    )
    store = engine.harness
    row = store.begin(runtime.task_id)
    tree = runtime.workspace.snapshot().tree_hash
    store.record_tree(runtime.task_id, row["epoch"], tree)
    store.pause(runtime.task_id)
    changed = runtime.workspace.manifest.model_copy(
        update={"created_at": "2000-01-01T00:00:00+00:00", "manifest_hash": ""},
    )
    changed = changed.model_copy(update={"manifest_hash": changed.computed_hash()})
    (runtime.workspace.root / ".mgar" / "manifest.json").write_text(changed.model_dump_json())
    # The local signature is self-consistent and the source tree is unchanged.
    assert (
        service.registry.open_task_workspace(runtime.registration, runtime.task_id).manifest
        == changed
    )
    with pytest.raises(DomainError) as error:
        service.recover_task(runtime.task_id)
    assert error.value.code == "RECOVERY_MANIFEST_CONFLICT"
    assert store.get(runtime.task_id)["tree_hash"] == tree
    assert store.get(runtime.task_id)["status"] == "paused"


def test_shared_engine_inbox_concurrent_reads_posts_and_acknowledgements(engine, tmp_path):
    store, row = task(engine, tmp_path)
    inbox = EngineInbox(store, "boundary-task", epoch=row["epoch"])

    def work(worker):
        for index in range(8):
            message_id = f"worker-{worker}-{index}"
            inbox.post(message_id=message_id, kind="note", text=message_id)
            inbox.messages()
            applied = inbox.mark_applied(message_id, 1 + worker * 8 + index)
            assert applied["status"] == "applied"
            inbox.messages()

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(work, index) for index in range(6)]
        for future in futures:
            future.result(timeout=15)
    messages = inbox.messages()
    assert len(messages) == 48
    assert all(message["status"] == "applied" for message in messages)
