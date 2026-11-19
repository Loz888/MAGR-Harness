"""Exercise the GA09 hand-off through the original durable CodingLoop."""

from __future__ import annotations

from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.general_agent.capabilities import CapabilityAccess
from mgar_v3.general_agent.coding import from_coding_request
from mgar_v3.general_agent.coding_executor import CodingExecutor, CodingOptions
from mgar_v3.general_agent.coding_executor_contract import CODING_CAPABILITY, INSPECT_CAPABILITY
from mgar_v3.general_agent.graph import deterministic_plan
from mgar_v3.harness.coding_loop import ServicePlan
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService


def _prepared(engine, tmp_path, kind="inspect", files=None, **options):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("Example project.\n", encoding="utf-8")
    for path, content in (files or {}).items():
        (source / path).write_text(content, encoding="utf-8")
    harness = _service(engine, tmp_path, **options)
    project = harness.register_project(str(source))["project_id"]
    request = CodingTaskRequest(project_id=project, objective="Check the project.", task_kind=kind)
    capability = INSPECT_CAPABILITY if kind == "inspect" else CODING_CAPABILITY
    proposal = from_coding_request(request).model_copy(
        update={"allowed_capabilities": (capability,)}
    )
    task = engine.general_tasks.create(project, None, uuid4(), proposal)
    graph = engine.general_graphs.install(task.task_id, 1, 1, deterministic_plan(task.spec, 1))
    descriptor = engine.general_graphs.registry.definition(capability)
    assert descriptor is not None
    access = CapabilityAccess(
        project_id=project,
        task_id=task.task_id,
        task_revision=1,
        capability_pins=(descriptor.identity(),),
        permissions=descriptor.permissions,
        max_risk="medium",
        workspace_write=kind != "inspect",
        resources=descriptor.upper_bound,
    )
    claim = engine.general_graphs.claim(
        task.task_id,
        1,
        graph.graph_revision,
        "work",
        access,
        context_tokens=8192,
    )
    assert claim is not None
    return CodingExecutor(engine, harness), claim, access, source


def _service(engine, tmp_path, **options):
    return HarnessCodingService(
        tmp_path / "harness",
        actor=engine.settings.local_actor,
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
        **options,
    )


def test_ga09_inspect_hands_off_and_publishes_via_independent_ga05(engine, tmp_path):
    adapter, claim, access, source = _prepared(engine, tmp_path)
    options = CodingOptions("README.md")
    child = adapter.start(claim, access, options)
    assert adapter.start(claim, access, options) == child
    with pytest.raises(DomainError, match="binding differs"):
        adapter.start(claim, access, CodingOptions("some-other-file"))
    native = adapter.harness.join_task(child, 10)
    assert native["status"] == "completed", native.get("reason")
    value = adapter.poll(claim, access)
    assert value["node_receipt_sha256"]
    assert adapter.poll(claim, access) == value
    with pytest.raises(DomainError):
        adapter.poll(claim, access.model_copy(update={"project_id": "another-project"}))
    rows = engine.harness.tasks()
    assert [row["task_id"] for row in rows] == [child]
    assert rows[0]["limits"]["max_cost_micros"] == 0
    assert rows[0]["limits"]["max_model_calls"] == 2
    assert rows[0]["grant_scope"]["write_paths"] == []
    assert (source / "README.md").read_text(encoding="utf-8") == "Example project.\n"
    assert engine.general_tasks.get(claim.task_id).state == "verifying"
    with pytest.raises(DomainError):
        adapter.verification(claim, access, verifier=engine.settings.local_actor)
    attestation = adapter.verification(claim, access, verifier="independent-verifier")
    assert attestation.outcome is not None
    assert engine.general_verification.publish(attestation.report_id).state == "completed"


@pytest.mark.parametrize("kind", ["bugfix", "feature", "refactor", "run_app"])
def test_ga09_does_not_grant_write_approval_by_mapping_a_coding_kind(engine, tmp_path, kind):
    adapter, claim, access, _ = _prepared(engine, tmp_path, kind=kind)
    with pytest.raises(DomainError, match="Invalid Coding executor binding"):
        adapter.start(claim, access, CodingOptions("README.md"))
    assert engine.harness.tasks() == []


def test_ga09_known_coding_failure_reaches_the_graph(engine, tmp_path):
    adapter, claim, access, _ = _prepared(engine, tmp_path)
    child = adapter.start(claim, access, CodingOptions("does-not-exist.py"))
    native = adapter.harness.join_task(child, 10)
    assert native["status"] in {"failed", "blocked", "budget_exhausted"}, native
    assert adapter.poll(claim, access)["node_receipt_sha256"]
    assert engine.general_graphs.get(claim.task_id).progress[claim.node_id].state == "failed"


def test_ga09_pause_and_resume_target_the_same_run(engine, tmp_path):
    entered, release = Event(), Event()

    def wait_once(_request):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)

    adapter, claim, access, _ = _prepared(engine, tmp_path, before_model_request=wait_once)
    child = adapter.start(claim, access, CodingOptions("README.md"))
    try:
        assert entered.wait(5)
        before = adapter.poll(claim, access)
        assert adapter.pause(claim, access)["status"] == "pausing"
    finally:
        release.set()
    deadline = monotonic() + 5
    while adapter.poll(claim, access)["status"] != "paused" and monotonic() < deadline:
        sleep(0.01)
    assert adapter.poll(claim, access)["status"] == "paused"
    adapter.resume(claim, access)
    assert adapter.harness.join_task(child, 10)["status"] == "completed"
    after = adapter.poll(claim, access)
    assert before["run_id"] == after["run_id"]
    assert after["node_receipt_sha256"]


def test_ga09_prelaunch_restart_reuses_the_persisted_task(engine, tmp_path, monkeypatch):
    adapter, claim, access, _ = _prepared(engine, tmp_path)
    options = CodingOptions("README.md")
    with monkeypatch.context() as fault:
        fault.setattr(adapter.harness, "_submit_runtime", lambda runtime: {"status": "queued"})
        child = adapter.start(claim, access, options)
    before = engine.harness.get(child)
    assert before["status"] == "queued"
    recovered = CodingExecutor(engine, _service(engine, tmp_path))
    assert recovered.start(claim, access, options) == child
    assert recovered.harness.join_task(child, 10)["status"] == "completed"
    after = engine.harness.get(child)
    assert before["run_id"] == after["run_id"]
    assert recovered.poll(claim, access)["node_receipt_sha256"]
    assert len(engine.harness.tasks()) == 1


def test_ga09_recovery_resumes_known_result_on_the_same_run(engine, tmp_path, monkeypatch):
    class Crash(BaseException):
        pass

    def interrupt(point, _payload):
        if point == "model_after_settlement":
            raise Crash

    adapter, claim, access, _ = _prepared(engine, tmp_path, fault_hook=interrupt)
    captured = {}

    def capture_runtime(runtime):
        captured["value"] = runtime
        return {"status": "queued"}

    with monkeypatch.context() as fault:
        fault.setattr(adapter.harness, "_submit_runtime", capture_runtime)
        child = adapter.start(claim, access, CodingOptions("README.md"))
    with pytest.raises(Crash):
        adapter.harness._run(captured["value"])
    before = engine.harness.get(child)
    assert before["status"] == "running"
    engine.harness.recover()
    recovered = CodingExecutor(engine, _service(engine, tmp_path))
    recovered.resume(claim, access)
    assert recovered.harness.join_task(child, 10)["status"] == "completed"
    after = engine.harness.get(child)
    assert before["run_id"] == after["run_id"]
    assert before["execution_epoch"] < after["execution_epoch"]
    with engine.transaction() as conn:
        usage = conn.execute(
            "SELECT count(*) AS total FROM app.usage_calls WHERE run_id=%s",
            (after["run_id"],),
        ).fetchone()
        assert usage["total"] == 2
    assert recovered.poll(claim, access)["node_receipt_sha256"]


def test_ga09_patch_and_test_receipt_are_accepted_by_ga05(engine, tmp_path):
    adapter, claim, access, source = _prepared(
        engine,
        tmp_path,
        kind="bugfix",
        files={
            "math_demo.py": "def add(a, b):\n    return a - b\n",
            "test_math_demo.py": (
                "import unittest\nfrom math_demo import add\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_sum(self):\n        self.assertEqual(add(2, 3), 5)\n"
            ),
        },
    )
    child = adapter.start(
        claim,
        access,
        CodingOptions(
            "math_demo.py",
            approved=True,
            old_text="a - b",
            new_text="a + b",
            test_argv=("python3", "-m", "unittest", "discover"),
        ),
    )
    native = adapter.harness.join_task(child, 25)
    assert native["status"] == "completed", (native.get("reason"), native.get("verification"))
    progress = adapter.poll(claim, access)
    assert progress["node_receipt_sha256"]
    report = adapter.verification(claim, access, verifier="independent-verifier")
    assert report.outcome is not None
    assert engine.general_verification.publish(report.report_id).state == "completed"
    assert (source / "math_demo.py").read_text(encoding="utf-8").endswith("a - b\n")


def test_ga09_service_receipt_is_accepted_by_ga05(engine, tmp_path):
    adapter, claim, access, _ = _prepared(
        engine,
        tmp_path,
        kind="run_app",
        files={
            "app.py": (
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.end_headers()\n"
                '        self.wfile.write(b"Hello from the fixture.")\n\n'
                'if __name__ == "__main__":\n'
                '    with HTTPServer(("127.0.0.1", 8088), Handler) as server:\n'
                '        print(f"Ready on port {server.server_port}", flush=True)\n'
                "        server.serve_forever()\n"
            )
        },
    )
    child = adapter.start(
        claim,
        access,
        CodingOptions(
            "app.py",
            approved=True,
            service_plan=ServicePlan(
                argv=("python3", "app.py"),
                mode="http",
                port=8088,
                timeout_seconds=10,
            ),
        ),
    )
    native = adapter.harness.join_task(child, 25)
    assert native["status"] == "completed", (native.get("reason"), native.get("verification"))
    assert adapter.poll(claim, access)["node_receipt_sha256"]
    report = adapter.verification(claim, access, verifier="independent-verifier")
    assert report.outcome is not None
    assert engine.general_verification.publish(report.report_id).state == "completed"
