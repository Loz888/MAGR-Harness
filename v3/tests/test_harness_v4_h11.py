"""H11 real SIGKILL and dedicated PostgreSQL crash/restart fault matrix."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest
from h11_helpers import IsolatedPostgres, Worker, _docker
from psycopg.rows import dict_row


@pytest.fixture(scope="module")
def h11_postgres():
    postgres = IsolatedPostgres()
    try:
        yield postgres.start()
    finally:
        postgres.close()


def _case(postgres):
    directory = Path(tempfile.mkdtemp(prefix="magr-h11-case-", dir="/tmp"))
    return directory, postgres.create_database()


def _calls(directory):
    path = directory / "shared" / "provider_calls.jsonl"
    return [json.loads(line)["operation_id"] for line in path.read_text().splitlines()]


def _cas(directory, content_hash):
    assert len(content_hash) == 64 and all(character in "0123456789abcdef"
                                          for character in content_hash)
    content = (directory / "shared" / "artifacts" / content_hash[:2] / content_hash).read_bytes()
    assert sha256(content).hexdigest() == content_hash
    return content


def _audit(database_url, task_id):
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        task = conn.execute(
            "SELECT h.*,r.status,r.execution_epoch,t.mandate_id FROM app.harness_tasks h "
            "JOIN app.runs r USING(run_id) JOIN app.tickets t USING(ticket_id) "
            "WHERE task_id=%s", (task_id,),
        ).fetchone()
        calls = conn.execute("SELECT * FROM app.usage_calls WHERE run_id=%s", (task["run_id"],)
                             ).fetchall()
        budget = conn.execute("SELECT * FROM app.budgets WHERE mandate_id=%s",
                              (task["mandate_id"],)).fetchone()
    return task, calls, budget


@pytest.mark.parametrize("point,known", [
    ("model_before_dispatch", False),
    ("model_before_settlement", False),
    ("model_after_settlement", True),
])
def test_model_process_sigkill_recovery_never_duplicates_provider_request(
    h11_postgres, point, known,
):
    directory, database_url = _case(h11_postgres)
    payload = {"database_url": database_url, "directory": str(directory / "shared"),
               "mode": "start", "scenario": "model", "fault": point}
    worker = Worker.launch(directory / "first", payload)
    try:
        marker = worker.wait_marker()
        worker.kill()
        before, _, before_budget = _audit(database_url, marker["task_id"])
        resumed = Worker.launch(directory / "resumed", {
            **payload, "mode": "recover", "task_id": marker["task_id"], "fault": "",
        })
        try:
            result = resumed.finish()
        finally:
            resumed.close()
        after, calls, budget = _audit(database_url, marker["task_id"])
        assert str(after["run_id"]) == marker["run_id"]
        assert after["execution_epoch"] > marker["epoch"]
        if point == "model_before_settlement":
            assert result["error"] == "RECOVERY_BLOCKED"
            assert len(_calls(directory)) == 1
            assert calls[0]["status"] == "unknown" and calls[0]["result"] is None
            assert budget["reserved_micros"] == before_budget["reserved_micros"] > 0
        else:
            assert result["status"] == "completed"
            assert result["tree_after"] == marker["tree_hash"]
            assert all(count == 1 for count in Counter(_calls(directory)).values())
            assert len(_calls(directory)) == 2
            assert all(call["status"] == "known" and call["result"] is not None for call in calls)
            assert budget["reserved_micros"] == 0
            assert bool(before["checkpoint_hash"])
        if known:
            assert before["checkpoint_hash"] and before_budget["settled_micros"] == 0
    finally:
        worker.close()


def test_dedicated_postgres_abrupt_restart_fences_old_engine_and_recovers_same_run(h11_postgres):
    directory, database_url = _case(h11_postgres)
    payload = {"database_url": database_url, "directory": str(directory / "shared"),
               "mode": "start", "scenario": "model", "fault": "model_after_settlement"}
    worker = Worker.launch(directory / "first", payload)
    try:
        marker = worker.wait_marker()
        h11_postgres.stop(abrupt=True)
        h11_postgres.restart()
        (worker.directory / "release").touch()
        stale = worker.finish()
        assert stale["error"] == "POSTGRES_CONNECTION_LOST"
        resumed = Worker.launch(directory / "resumed", {
            **payload, "mode": "recover", "task_id": marker["task_id"], "fault": "",
        })
        try:
            result = resumed.finish()
        finally:
            resumed.close()
        task, calls, budget = _audit(database_url, marker["task_id"])
        assert result["status"] == "completed"
        assert str(task["run_id"]) == marker["run_id"]
        assert task["execution_epoch"] > marker["epoch"]
        assert result["tree_after"] == marker["tree_hash"] == task["tree_hash"]
        assert len(_calls(directory)) == len(set(_calls(directory))) == 2
        assert all(call["status"] == "known" for call in calls)
        assert budget["reserved_micros"] == 0
    finally:
        worker.close()


@pytest.mark.parametrize("point", [
    "edit.before_apply", "edit.after_file_replace:a.py", "edit.after_apply",
])
def test_patch_sigkill_uses_preimages_and_postimages_without_double_apply(h11_postgres, point):
    directory, database_url = _case(h11_postgres)
    payload = {"database_url": database_url, "directory": str(directory / "shared"),
               "mode": "start", "scenario": "edit", "fault": point}
    worker = Worker.launch(directory / "first", payload)
    try:
        marker = worker.wait_marker()
        worker.kill()
        resumed = Worker.launch(directory / "resumed", {
            **payload, "mode": "recover", "task_id": marker["task_id"], "fault": "",
        })
        try:
            result = resumed.finish()
        finally:
            resumed.close()
        assert result["run_id"] == marker["run_id"]
        assert result["epoch"] > marker["epoch"]
        assert result["files"] == {"a.py": "value = 2\n", "b.py": "other = 2\n"}
        assert result["operations"][0]["state"] == "known"
        assert result["operations"][0]["result_hash"]
        receipt = json.loads(_cas(directory, result["operations"][0]["result_hash"]))
        assert receipt["snapshot"]["tree_hash"] == result["tree_hash"]
        task, _, _ = _audit(database_url, marker["task_id"])
        assert task["tree_hash"] == result["tree_hash"]
        assert len(result["manifest_hash"]) == 64
    finally:
        worker.close()


@pytest.mark.parametrize("point", ["command.after_start", "command.before_log_cas"])
def test_command_sigkill_reconciles_owned_container_and_output_without_relaunch(
    h11_postgres, point,
):
    directory, database_url = _case(h11_postgres)
    payload = {"database_url": database_url, "directory": str(directory / "shared"),
               "mode": "start", "scenario": "command", "fault": point}
    worker = Worker.launch(directory / "first", payload)
    try:
        marker = worker.wait_marker()
        worker.kill()
        resumed = Worker.launch(directory / "resumed", {
            **payload, "mode": "recover", "task_id": marker["task_id"], "fault": "",
        })
        try:
            result = resumed.finish()
        finally:
            resumed.close()
        assert result["run_id"] == marker["run_id"]
        assert result["epoch"] > marker["epoch"]
        assert len(result["operations"]) == len(result["receipts"]) == 1
        assert result["operations"][0]["state"] == "known"
        receipt = result["receipts"][0]
        assert receipt["raw_log_hash"] and receipt["launched"]["provenance"]
        assert json.loads(_cas(directory, result["operations"][0]["result_hash"])) == receipt
        logs = _cas(directory, receipt["raw_log_hash"])
        assert len(logs) <= receipt["total_bytes"]
        if point == "command.before_log_cas":
            assert logs == b"h11-command-output\n"
        assert receipt["launched"]["identity"]["execution_epoch"] == marker["epoch"]
        remaining = _docker("ps", "--all", "--quiet", "--filter",
                            "label=mgar.v4.h11.task=" + marker["task_id"]).stdout.strip()
        assert remaining == ""
    finally:
        worker.close()


@pytest.mark.parametrize("scenario,point", [
    ("command", "command.after_start"), ("model", "model_before_settlement"),
])
def test_cancel_after_worker_sigkill_cleans_owned_process_and_keeps_unknown_money(
    h11_postgres, scenario, point,
):
    directory, database_url = _case(h11_postgres)
    payload = {"database_url": database_url, "directory": str(directory / "shared"),
               "mode": "start", "scenario": scenario, "fault": point}
    worker = Worker.launch(directory / "first", payload)
    try:
        marker = worker.wait_marker()
        worker.kill()
        _, _, before_budget = _audit(database_url, marker["task_id"])
        cancelling = Worker.launch(directory / "cancel", {
            **payload, "mode": "cancel", "task_id": marker["task_id"], "fault": "",
        })
        try:
            result = cancelling.finish()
        finally:
            cancelling.close()
        task, calls, budget = _audit(database_url, marker["task_id"])
        assert result["status"] == task["status"] == "cancelled"
        assert result["run_id"] == marker["run_id"]
        assert result["epoch"] > marker["epoch"]
        assert result["reconciliation_errors"] == []
        if scenario == "command":
            assert len(result["receipts"]) == len(result["operations"]) == 1
            receipt = result["receipts"][0]
            assert receipt["launched"]["identity"]["execution_epoch"] == marker["epoch"]
            assert json.loads(_cas(directory, result["operations"][0]["result_hash"])) == receipt
            assert len(_cas(directory, receipt["raw_log_hash"])) <= receipt["total_bytes"]
            assert _docker("ps", "--all", "--quiet", "--filter",
                           "label=mgar.v4.h11.task=" + marker["task_id"]).stdout.strip() == ""
        else:
            assert len(_calls(directory)) == len(calls) == 1
            assert calls[0]["status"] == "unknown"
            assert budget["reserved_micros"] == before_budget["reserved_micros"] > 0
    finally:
        worker.close()
