"""H11 authority/recovery tests against the real PostgreSQL Engine fixture."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import EventDraft, ModelResponse, ModelUsage
from mgar_v3.harness.ledger import PersistentBudgetLedger


def _task(engine, tmp_path, *, model_calls=12, tool_calls=48, cost=100):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    registration = {"workspace_id": "workspace-1", "repository": "local/test", "base_commit": ""}
    request = {"project_id": "workspace-1", "objective": "inspect", "mode": "deterministic",
               "workspace_root": str(root)}
    grant = {"actor": "owner", "workspace_id": "workspace-1", "repository": "local/test"}
    limits = {"max_model_calls": model_calls, "max_tool_calls": tool_calls,
              "max_cost_micros": cost, "max_seconds": 600}
    engine.harness.create("task-1", registration, request, grant, limits)
    row = engine.harness.begin("task-1")
    return engine.harness, row, PersistentBudgetLedger(engine.harness, "task-1", row["epoch"])


def test_create_real_identity_is_immutable_and_idempotent(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    assert isinstance(task["run_id"], UUID) and task["run_id"].int
    assert isinstance(task["thread_id"], UUID) and task["thread_id"].int
    assert task["run_id"] != task["thread_id"] and task["epoch"] == 1
    same = store.create("task-1", task["registration"], task["request"], task["grant_scope"],
                        task["limits"])
    assert same["run_id"] == task["run_id"]
    with pytest.raises(DomainError, match="another scope"):
        store.create("task-1", task["registration"], {**task["request"], "objective": "changed"},
                     task["grant_scope"], task["limits"])


def test_budget_failure_rolls_back_both_existing_money_ledgers(engine, tmp_path):
    _, task, ledger = _task(engine, tmp_path, cost=50)
    first = ledger.reserve("task-1", "model-1", "model", reserved_micros=40)
    with pytest.raises(DomainError, match="budget exhausted"):
        ledger.reserve("task-1", "model-2", "model", reserved_micros=20)
    assert ledger.snapshot("task-1").cost_micros == 40
    assert ledger.reserve("task-1", "model-1", "model", reserved_micros=40) == first
    with engine.transaction() as conn:
        authorization = conn.execute(
            "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s",
            (task["profile_hash"],),
        ).fetchone()
        calls = conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()
    assert authorization["reserved_micros"] == 40 and authorization["used_calls"] == 1
    assert calls["n"] == 1


def test_zero_cost_fixture_uses_existing_usage_calls(engine, tmp_path):
    store, task, ledger = _task(engine, tmp_path, cost=0)
    reservation = ledger.reserve("task-1", "free", "model", reserved_micros=0)
    response = ModelResponse(text="free", usage=ModelUsage(cost_micros=0))
    store.save_model_result("task-1", task["epoch"], "free", response)
    ledger.settle(reservation.reservation_id, actual_micros=0)
    assert ledger.snapshot("task-1").model_calls == 1
    assert ledger.snapshot("task-1").cost_micros == 0


def test_model_result_and_settlement_rollback_together(engine, tmp_path, monkeypatch):
    store, task, ledger = _task(engine, tmp_path)
    ledger.reserve("task-1", "model-1", "model", reserved_micros=40)
    response = ModelResponse(text="known", usage=ModelUsage(cost_micros=3))
    original = engine._event

    def fail_settlement(conn, run_id, kind, **payload):
        if kind == "model_settled":
            raise RuntimeError("injected before commit")
        return original(conn, run_id, kind, **payload)

    monkeypatch.setattr(engine, "_event", fail_settlement)
    with pytest.raises(RuntimeError, match="injected"):
        store.save_model_result("task-1", task["epoch"], "model-1", response)
    assert store.model_result("task-1", task["epoch"], "model-1") is None
    assert ledger.snapshot("task-1").cost_micros == 40
    monkeypatch.setattr(engine, "_event", original)
    store.save_model_result("task-1", task["epoch"], "model-1", response)
    store.save_model_result("task-1", task["epoch"], "model-1", response)
    assert store.model_result("task-1", task["epoch"], "model-1") == response
    assert ledger.snapshot("task-1").cost_micros == 3


def test_unknown_model_retains_reservation_and_blocks_same_run_resume(engine, tmp_path):
    store, _, ledger = _task(engine, tmp_path)
    reservation = ledger.reserve("task-1", "lost", "model", reserved_micros=40)
    ledger.mark_unknown(reservation.reservation_id, "receipt lost")
    store.pause("task-1")
    assert ledger.snapshot("task-1").cost_micros == 40
    with pytest.raises(DomainError, match="Unknown effects"):
        store.claim("task-1")


def test_prepared_model_survives_recovery_without_double_reservation(engine, tmp_path):
    store, task, ledger = _task(engine, tmp_path)
    reservation = ledger.reserve("task-1", "not-sent", "model", reserved_micros=40)
    engine.recover()
    resumed = store.claim("task-1")
    assert resumed["run_id"] == task["run_id"] and resumed["epoch"] > task["epoch"]
    new_ledger = PersistentBudgetLedger(store, "task-1", resumed["epoch"])
    assert new_ledger.reserve("task-1", "not-sent", "model", reserved_micros=40) == reservation
    assert new_ledger.snapshot("task-1").model_calls == 1


def test_dispatched_model_becomes_unknown_and_fences_old_worker(engine, tmp_path):
    store, task, ledger = _task(engine, tmp_path)
    ledger.reserve("task-1", "sent", "model", reserved_micros=40)
    store.start_operation("task-1", task["epoch"], "sent", "model", "a" * 64)
    store.dispatch_operation("task-1", task["epoch"], "sent")
    with pytest.raises(DomainError, match="cannot be resent"):
        store.model_result("task-1", task["epoch"], "sent")
    engine.recover()
    assert store.operation("task-1", "sent")["state"] == "unknown"
    assert ledger.snapshot("task-1").cost_micros == 40
    with pytest.raises(DomainError, match="no longer authorized"):
        store.guard("task-1", task["epoch"])
    with pytest.raises(DomainError, match="Unknown effects"):
        store.claim("task-1")


def test_write_unknown_requires_receipt_reconciliation(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    store.start_operation("task-1", task["epoch"], "edit-1", "edit", "a" * 64)
    store.dispatch_operation("task-1", task["epoch"], "edit-1")
    engine.recover()
    with pytest.raises(DomainError, match="Unknown effects"):
        store.claim("task-1")
    content_hash = engine.artifacts.put(b'{"tree":"known"}')
    store.reconcile_operation("task-1", "edit-1", result_hash=content_hash,
                              receipt={"tree": "known"})
    resumed = store.claim("task-1")
    assert resumed["run_id"] == task["run_id"]
    op = store.start_operation("task-1", resumed["epoch"], "edit-1", "edit", "a" * 64)
    assert op["state"] == "known" and op["result_hash"] == content_hash


def test_read_only_operation_can_replay_after_restart(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    store.start_operation("task-1", task["epoch"], "read-1", "read", "a" * 64, read_only=True)
    store.dispatch_operation("task-1", task["epoch"], "read-1")
    engine.recover()
    resumed = store.claim("task-1")
    replay = store.start_operation("task-1", resumed["epoch"], "read-1", "read", "a" * 64,
                                   read_only=True)
    assert replay["state"] == "prepared" and replay["execution_epoch"] == resumed["epoch"]


def test_checkpoint_cas_and_epoch_fence_are_authoritative(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    content_hash = engine.artifacts.put(b"checkpoint")
    store.save_checkpoint("task-1", task["epoch"], content_hash, 1, tree_hash="a" * 64)
    store.pause("task-1")
    with pytest.raises(DomainError, match="no longer authorized"):
        store.save_checkpoint("task-1", task["epoch"], engine.artifacts.put(b"stale"), 2)
    assert store.get("task-1")["checkpoint_hash"] == content_hash


def test_final_tree_must_match_authoritative_record(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    store.record_tree("task-1", task["epoch"], "a" * 64)
    content_hash = engine.artifacts.put(b"result")
    with pytest.raises(DomainError, match="Final tree"):
        store.finish("task-1", task["epoch"], "completed", content_hash, "b" * 64)
    assert store.get("task-1")["status"] == "running"
    assert store.finish("task-1", task["epoch"], "completed", content_hash,
                        "a" * 64)["status"] == "succeeded"


def test_events_are_pg_authoritative_and_filter_private_reasoning(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    envelope = store.emit("task-1", task["epoch"], EventDraft(
        run_id=task["run_id"], execution_epoch=task["epoch"], revision=1,
        operation_id="model-1", kind="model_completed",
        payload={"text": "public", "reasoning": "private"},
    ))
    assert envelope.cursor > 0
    assert store.events("task-1")[0].payload == {"text": "public"}
    assert not store.events("task-1", envelope.cursor)


def test_concurrent_budget_reservations_cannot_exceed_cap(engine, tmp_path):
    _, _, ledger = _task(engine, tmp_path, model_calls=1)

    def reserve(operation_id):
        try:
            ledger.reserve("task-1", operation_id, "model", reserved_micros=10)
            return "reserved"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("one", "two")))
    assert sorted(outcomes) == ["BUDGET_EXCEEDED", "reserved"]
    assert ledger.snapshot("task-1").model_calls == 1


def test_completion_refuses_unfinished_tool_reservation(engine, tmp_path):
    store, task, ledger = _task(engine, tmp_path)
    ledger.reserve("task-1", "unfinished", "tool")
    store.record_tree("task-1", task["epoch"], "a" * 64)
    with pytest.raises(DomainError, match="Unknown effects"):
        store.finish("task-1", task["epoch"], "completed", engine.artifacts.put(b"result"),
                     "a" * 64)
    assert store.get("task-1")["status"] == "running"


def test_cancel_fences_inflight_models_and_retains_reserved_money(engine, tmp_path):
    store, task, ledger = _task(engine, tmp_path)
    ledger.reserve("task-1", "sent", "model", reserved_micros=40)
    store.start_operation("task-1", task["epoch"], "sent", "model", "a" * 64)
    store.dispatch_operation("task-1", task["epoch"], "sent")
    cancelled = store.cancel("task-1")
    assert cancelled["status"] == "cancelled" and cancelled["epoch"] > task["epoch"]
    assert store.operation("task-1", "sent")["state"] == "unknown"
    with engine.transaction() as conn:
        call = conn.execute("SELECT status FROM app.usage_calls WHERE run_id=%s",
                            (task["run_id"],)).fetchone()
    assert call["status"] == "unknown" and ledger.snapshot("task-1").cost_micros == 40
    with pytest.raises(DomainError, match="no longer authorized"):
        store.guard("task-1", task["epoch"])


def test_claim_clears_prior_result_projection_and_preserves_checkpoint(engine, tmp_path):
    store, task, _ = _task(engine, tmp_path)
    checkpoint = engine.artifacts.put(b"checkpoint")
    result = engine.artifacts.put(b"incomplete result")
    store.save_checkpoint("task-1", task["epoch"], checkpoint, 1, tree_hash="a" * 64)
    store.finish("task-1", task["epoch"], "incomplete", result, "a" * 64)
    claimed = store.claim("task-1")
    assert claimed["run_id"] == task["run_id"] and claimed["epoch"] > task["epoch"]
    assert claimed["status"] == "running" and claimed["public_status"] == "running"
    assert claimed["result_hash"] is None and claimed["report_hash"] is None
    assert claimed["outcome"] is None and claimed["checkpoint_hash"] == checkpoint
    assert engine.artifacts.read(result) == b"incomplete result"
