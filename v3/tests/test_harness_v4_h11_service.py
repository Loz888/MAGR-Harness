"""Public service integration over real Engine/PG, using a free local model.

Process SIGKILL and dedicated PostgreSQL restart evidence lives in the separate
H11 fault probes.  These tests exercise service reconstruction and PG authority
without launching project commands or calling a remote provider.
"""

from __future__ import annotations

import json

import pytest

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.service import (
    CodingTaskRequest,
    DeterministicCodingModel,
    HarnessCodingService,
)


class _InjectedCrash(BaseException):
    pass


class _CountingModel:
    def __init__(self) -> None:
        self.requests = []
        self.inner = DeterministicCodingModel(
            task_kind="inspect", target_path="source.py", old_text="", new_text="", test_argv=(),
        )

    def complete(self, request, cancellation):
        self.requests.append(request.operation_id)
        return self.inner.complete(request, cancellation)


def _service(engine, settings, model, fault=None):
    return HarnessCodingService(
        settings.harness_root, actor="owner", image_lock=settings.harness_image_lock,
        engine=engine, model_factory=lambda request: model, fault_hook=fault,
    )


def _request(service, tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n", encoding="utf-8")
    project = service.register_project(str(source))
    return CodingTaskRequest(project_id=project["project_id"], objective="Inspect source.py",
                             task_kind="inspect", target_path="source.py")


def test_service_uses_real_run_identity_and_existing_usage_ledger(engine, settings, tmp_path):
    model = _CountingModel()
    service = _service(engine, settings, model)
    result = service.create_task(_request(service, tmp_path))
    task = engine.harness.get(result["task_id"])
    assert result["status"] == "completed"
    assert task["run_id"].int and task["thread_id"].int
    assert task["status"] == "succeeded" and task["result_hash"]
    assert json.loads(engine.artifacts.read(task["result_hash"]))["task_id"] == result["task_id"]
    with engine.transaction() as conn:
        calls = conn.execute(
            "SELECT * FROM app.usage_calls WHERE run_id=%s ORDER BY step_key", (task["run_id"],)
        ).fetchall()
    assert len(calls) == len(model.requests) == 2
    assert all(call["status"] == "known" and call["result"] is not None for call in calls)
    events = engine.harness.events(result["task_id"])
    assert events and all(event.run_id == task["run_id"] for event in events)
    assert result["commands"] == []


def test_service_reconstructs_same_run_and_reuses_known_model_result(engine, settings, tmp_path):
    model = _CountingModel()

    def crash(point, payload):
        if point == "model_after_settlement":
            raise _InjectedCrash("after known result commit")

    service = _service(engine, settings, model, crash)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    before = engine.harness.tasks()[0]
    first_operation = model.requests[0]
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        resumed = _service(replacement, settings, model).recover_task(before["task_id"])
        after = replacement.harness.get(before["task_id"])
        assert resumed["status"] == "completed"
        assert after["run_id"] == before["run_id"]
        assert after["execution_epoch"] > before["execution_epoch"]
        assert model.requests.count(first_operation) == 1
        assert len(model.requests) == 2
        assert after["tree_hash"] == resumed["tree_after"]
    finally:
        replacement.close()


def test_service_unknown_receipt_retains_budget_and_never_retries(engine, settings, tmp_path):
    model = _CountingModel()

    def crash(point, payload):
        if point == "model_before_settlement":
            raise _InjectedCrash("receipt lost before local settlement")

    service = _service(engine, settings, model, crash)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    before = engine.harness.tasks()[0]
    with engine.transaction() as conn:
        held = conn.execute("SELECT reserved_micros FROM app.budgets WHERE mandate_id=%s",
                            (before["mandate_id"],)).fetchone()["reserved_micros"]
    assert held > 0 and len(model.requests) == 1
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        with pytest.raises(DomainError, match="Unknown effects"):
            _service(replacement, settings, model).recover_task(before["task_id"])
        with replacement.transaction() as conn:
            call = conn.execute("SELECT status FROM app.usage_calls WHERE run_id=%s",
                                (before["run_id"],)).fetchone()
            budget = conn.execute("SELECT reserved_micros FROM app.budgets WHERE mandate_id=%s",
                                  (before["mandate_id"],)).fetchone()
        assert call["status"] == "unknown" and budget["reserved_micros"] == held
        assert len(model.requests) == 1
    finally:
        replacement.close()


def test_service_finished_result_survives_local_projection_loss(engine, settings, tmp_path):
    model = _CountingModel()
    service = _service(engine, settings, model)
    result = service.create_task(_request(service, tmp_path))
    (settings.harness_root / "runs" / result["task_id"] / "result.json").unlink()
    recovered = _service(engine, settings, model).recover_task(result["task_id"])
    assert recovered["status"] == "completed"
    assert recovered["tree_after"] == result["tree_after"]
    assert len(model.requests) == 2
