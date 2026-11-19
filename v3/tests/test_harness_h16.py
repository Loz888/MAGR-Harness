"""Managed Harness acceptance over PostgreSQL, Docker, browser auth and HTTP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import HarnessToolCall, ModelResponse, ModelUsage
from mgar_v3.harness.gateway import create_app as create_front
from mgar_v3.harness.scheduler import HarnessScheduler

ORIGIN = "http://127.0.0.1:18080"
ADMIN = {"X-MGAR-Token": "h16-fixture-operator"}
IMAGE_LOCK = Path(__file__).resolve().parents[1] / ".local/r03/sandbox-lock.json"
PASSWORD = "fixture-member-password"
TESTS = """import unittest
class Existing(unittest.TestCase):
    def test_example(self): self.assertEqual(2 + 3, 5)
"""


def expect(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def wait_for(callback: Callable, *, seconds: float = 35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.08)
    pytest.fail("The expected durable state did not appear before the deadline")


def member(client, user):
    info = expect(
        client.post(
            "/api/v3/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": user, "password": PASSWORD},
        )
    )
    assert not info["is_owner"]
    return {"Origin": ORIGIN, "X-CSRF-Token": info["csrf_token"]}


def setup_tenants(env, *, max_calls=16):
    client = env.operator
    env.users = {}
    for user in ("alice", "bob", "carol"):
        env.users[user] = expect(
            client.post(
                "/api/v3/agent/accounts",
                headers=ADMIN,
                json={"username": user, "password": PASSWORD},
            ),
            201,
        )["user_id"]
    env.projects = {}
    for label, members in (
        ("alpha", {"alice": "contributor", "carol": "reader"}),
        ("beta", {"bob": "contributor"}),
    ):
        source = env.tmp_path / label
        source.mkdir()
        (source / "README.md").write_text(f"Scope marker for {label}.\n")
        (source / "test_example.py").write_text(TESTS)
        project = env.projects[label] = expect(
            client.post(
                "/api/v3/harness/projects",
                headers=ADMIN,
                json={"path": str(source)},
            ),
            201,
        )["project_id"]
        route = f"/api/v3/agent/projects/{project}"
        expect(
            client.put(route + "/quota", headers=ADMIN, json={"max_parallel": 1, "max_queued": 1})
        )
        expect(
            client.put(
                route + "/credentials",
                headers=ADMIN,
                json={"deepseek_api_key": f"fixture-project-key-{label}"},
            )
        )
        expect(
            client.post(
                route + "/authorization",
                headers=ADMIN,
                json={"allow_write": True, "allow_paid": True, "max_model_calls": max_calls},
            )
        )
        for username, role in members.items():
            expect(
                client.put(
                    route + "/members/" + env.users[username],
                    headers=ADMIN,
                    json={"role": role},
                )
            )


class ControlledModel:
    def __init__(self, env, key, request):
        self.env, self.key = env, key
        self.ordinal = 0
        self.marker = request.objective

    def complete(self, request, cancellation):
        self.ordinal += 1
        self.env.requests.append(
            (request.task.task_id, request.task.workspace_id, request.operation_id, self.key)
        )
        if self.env.hook is not None:
            self.env.hook(self, request, cancellation)
        cancellation.ensure_active()
        call = None
        if (
            request.task.task_kind == "inspect"
            and self.ordinal == 1
            and not any(message.role == "tool" for message in request.messages)
        ):
            call = HarnessToolCall(
                call_id="read", name="read_file", arguments={"path": "README.md"}
            )
        if request.task.task_kind != "inspect" and self.ordinal == 1:
            call = HarnessToolCall(
                call_id="edit",
                name="apply_edits",
                arguments={
                    "expected_tree": request.workspace_tree,
                    "edits": [
                        {
                            "path": "answer.txt",
                            "expected_sha256": None,
                            "create": True,
                            "content": "Verified fixture answer.\n",
                        }
                    ],
                },
            )
        if request.task.task_kind != "inspect" and self.ordinal == 2:
            call = HarnessToolCall(
                call_id="tests",
                name="run_command",
                arguments={"command_id": "tests"},
            )
        usage = ModelUsage(input_tokens=110, output_tokens=22)
        if call is not None:
            return ModelResponse(tool_calls=(call,), finish_reason="tool_calls", usage=usage)
        return ModelResponse(
            text="Inspected README.md and verified the recorded project evidence.",
            usage=usage,
        )


def attach_model(env, app):
    app.state.harness.product_adapter_factory = lambda request, key, caps: ControlledModel(
        env,
        key,
        request,
    )


@pytest.fixture
def tenants(engine, settings, tmp_path, request):
    settings.local_token = SecretStr(ADMIN["X-MGAR-Token"])
    settings.harness_image_lock = IMAGE_LOCK
    settings.deepseek_api_key = SecretStr("fixture-global-key-denied-for-managed-projects")
    settings.allow_paid = True
    settings.max_concurrent_runs = 2
    app = create_app(settings, engine, run_background=getattr(request, "param", True))
    with TestClient(app, base_url=ORIGIN) as operator:
        env = SimpleNamespace(
            operator=operator,
            app=app,
            engine=engine,
            settings=settings,
            tmp_path=tmp_path,
            requests=[],
            hook=None,
        )
        attach_model(env, app)
        yield env


def client(env):
    # TestClient maintains its cookie jar per browser. The operator owns lifespan.
    return TestClient(env.app, base_url=ORIGIN)


def submit(browser, headers, project_id, text, client_id, *, status=200):
    return expect(
        browser.post(
            "/api/v3/agent/sessions",
            headers=headers,
            json={"project_id": project_id, "text": text, "client_id": client_id},
        ),
        status,
    )


def task_path(task_id):
    return "/api/v3/harness/tasks/" + task_id


def finish(browser, task_id):
    def outcome():
        task = expect(browser.get(task_path(task_id)))
        return (
            task
            if task["status"]
            in {
                "completed",
                "blocked",
                "failed",
                "incomplete",
                "budget_exhausted",
                "unknown",
            }
            else None
        )

    return wait_for(outcome)


def test_two_projects_isolate_sessions_tasks_artifacts_and_credentials(tenants):
    env = tenants
    setup_tenants(env)
    alice, bob, carol = [client(env) for _ in range(3)]
    a, b, c = (
        member(browser, user)
        for browser, user in ((alice, "alice"), (bob, "bob"), (carol, "carol"))
    )
    alpha, beta = env.projects["alpha"], env.projects["beta"]
    projects = expect(alice.get("/api/v3/harness/projects"))
    assert {item["project_id"] for item in projects} == {alpha}
    assert all("path" not in item for item in projects)
    assert expect(alice.get("/api/v3/agent/profile"))["model"] == {"scope": "project"}
    assert alice.get(f"/api/v3/agent/projects/{beta}/readiness").status_code == 404
    assert alice.get("/api/v3/settings/analysis").status_code == 403
    assert alice.get("/api/v3/runs").status_code == 403
    assert alice.get("/api/v3/agent/accounts").status_code == 403
    assert alice.get("/api/v3/runner/devices").status_code == 403
    assert (
        alice.post(
            "/api/v3/harness/projects", headers=a, json={"path": str(env.tmp_path)}
        ).status_code
        == 403
    )
    assert submit(carol, c, alpha, "新增一个文件", "write-denied", status=403)["code"] == (
        "HARNESS_MEMBER_READONLY"
    )
    submit(alice, a, beta, "解释项目", "foreign", status=404)
    first = submit(alice, a, alpha, "新增一个答案文件", "same-client-id")
    second = submit(bob, b, beta, "解释项目", "same-client-id")
    assert first["session_id"] != second["session_id"]
    assert submit(alice, a, alpha, "新增一个答案文件", "same-client-id")["duplicate"]
    task_a, task_b = first["task_id"], second["task_id"]
    assert finish(alice, task_a)["status"] == "completed"
    assert finish(bob, task_b)["status"] == "completed"
    assert not (env.tmp_path / "alpha/answer.txt").exists()
    assert {key for tid, _, _, key in env.requests if tid == task_a} == {
        "fixture-project-key-alpha"
    }
    assert {key for tid, _, _, key in env.requests if tid == task_b} == {"fixture-project-key-beta"}
    log = expect(alice.get(task_path(task_a) + "/artifacts"))["logs"][-1]
    assert b"test_example" in alice.get(log["url"]).content
    assert bob.get(log["url"]).status_code == 404
    assert carol.get(log["url"]).status_code == 404
    assert bob.get(task_path(task_b) + "/logs/" + log["sha256"]).status_code == 404
    assert bob.get("/api/v3/artifacts/" + log["sha256"]).status_code == 403
    assert bob.get(f"/api/v3/agent/sessions/{first['session_id']}/export").status_code == 404
    for suffix in ("", "/diff", "/patch", "/artifacts", "/events/page", "/events", "/messages"):
        assert bob.get(task_path(task_a) + suffix).status_code == 404
    for control in ("cancel", "pause", "resume"):
        assert carol.post(task_path(task_a) + "/" + control, headers=c).status_code == 404
    assert (
        alice.post(
            f"/api/v3/agent/sessions/{first['session_id']}/apply",
            headers=a,
            json={"task_id": task_a, "expected_tree": "a" * 64},
        ).status_code
        == 403
    )
    assert {row["task_id"] for row in expect(bob.get("/api/v3/harness/tasks"))} == {task_b}
    assert alice.get(task_path(task_a) + "/events?follow=false").status_code == 200

    route = f"/api/v3/agent/projects/{alpha}/members/{env.users['alice']}"
    expect(env.operator.delete(route, headers=ADMIN), 204)
    assert alice.get(log["url"]).status_code == 404
    assert alice.get(task_path(task_a) + "/events?follow=false").status_code == 404
    assert expect(alice.get("/api/v3/harness/projects")) == []
    expect(env.operator.put(route, headers=ADMIN, json={"role": "contributor"}))
    assert alice.get(task_path(task_a)).status_code == 404
    assert not expect(alice.get("/api/v3/agent/sessions"))
    with env.engine.transaction() as conn:
        stored = conn.execute(
            "SELECT encrypted_payload FROM app.harness_project_credentials"
        ).fetchall()
    assert len(stored) == 2
    assert all("fixture-project-key" not in row["encrypted_payload"] for row in stored)


def test_capacity_bounds_queue_and_prevents_cross_project_head_of_line_blocking(tenants):
    env = tenants
    setup_tenants(env)
    alice, bob = client(env), client(env)
    a, b = member(alice, "alice"), member(bob, "bob")
    alpha, beta = env.projects["alpha"], env.projects["beta"]
    entered, release = threading.Event(), threading.Event()

    def hook(model, request, cancellation):
        if "慢任务" in model.marker and model.ordinal == 1:
            entered.set()
            assert release.wait(25)

    env.hook = hook
    try:
        running = submit(alice, a, alpha, "解释慢任务", "slow")
        assert entered.wait(8)
        queued = submit(alice, a, alpha, "解释排队任务", "queue")
        assert expect(alice.get(task_path(queued["task_id"])))["status"] == "queued"
        rejected = submit(alice, a, alpha, "解释另一个任务", "over-quota", status=429)
        assert rejected["code"] == "HARNESS_QUEUE_FULL"
        other = submit(bob, b, beta, "解释这个项目", "other")
        assert finish(bob, other["task_id"])["status"] == "completed"
        assert expect(alice.get(task_path(queued["task_id"])))["status"] == "queued"
        assert sum(tid == queued["task_id"] for tid, *_ in env.requests) == 0
    finally:
        release.set()
    assert finish(alice, running["task_id"])["status"] == "completed"
    assert finish(alice, queued["task_id"])["status"] == "completed"
    with env.engine.transaction() as conn:
        timing = conn.execute(
            "SELECT safe_payload->>'queued_ms' AS ms FROM app.run_events "
            "JOIN app.harness_tasks USING(run_id) WHERE task_id=%s "
            "AND kind='harness_dispatch_started'",
            (queued["task_id"],),
        ).fetchone()
    assert timing is not None and int(timing["ms"]) > 0


def test_project_ledgers_and_credentials_cannot_fall_back_to_another_project(tenants):
    env = tenants
    setup_tenants(env, max_calls=2)
    alice, bob = client(env), client(env)
    a, b = member(alice, "alice"), member(bob, "bob")
    alpha, beta = env.projects["alpha"], env.projects["beta"]
    first = submit(alice, a, alpha, "解释最小项目", "first")
    assert finish(alice, first["task_id"])["status"] == "completed"
    past_limit = submit(alice, a, alpha, "解释另一个模块", "limit")
    answer = finish(alice, past_limit["task_id"])
    assert answer["status"] == "budget_exhausted" and "budget" in answer["reason"]
    assert all(tid != past_limit["task_id"] for tid, *_ in env.requests)
    other = submit(bob, b, beta, "解释项目", "other")
    assert finish(bob, other["task_id"])["status"] == "completed"
    expect(
        env.operator.put(
            f"/api/v3/agent/projects/{beta}/credentials",
            headers=ADMIN,
            json={"deepseek_api_key": ""},
        )
    )
    denial = submit(bob, b, beta, "解释其余部分", "cleared", status=409)
    assert denial["code"] == "HARNESS_LIVE_MODEL_UNAVAILABLE"


@pytest.mark.parametrize("tenants", [False], indirect=True)
@pytest.mark.parametrize("resume_requested", [False, True])
def test_replacement_controller_rebuilds_a_never_started_job_with_same_run(
    tenants,
    resume_requested,
):
    env = tenants
    setup_tenants(env)
    alice = client(env)
    headers = member(alice, "alice")
    job = submit(alice, headers, env.projects["alpha"], "解释项目", "restart")
    original = env.engine.harness.get(job["task_id"])
    assert original["status"] == "queued" and not env.requests
    if resume_requested:
        expect(alice.post(task_path(job["task_id"]) + "/pause", headers=headers), 202)
        resume = expect(alice.post(task_path(job["task_id"]) + "/resume", headers=headers), 202)
        assert resume["status"] == "queued"
    with pytest.raises(DomainError) as conflict:
        Engine(env.settings, env.engine.artifacts)
    assert conflict.value.code == "ENGINE_ALREADY_RUNNING"

    env.engine.close()
    replacement = Engine(env.settings, env.engine.artifacts)
    try:
        replacement.recover()
        app = create_app(env.settings, replacement, run_background=False)
        with TestClient(app, base_url=ORIGIN):
            attach_model(env, app)
            scheduler = HarnessScheduler(app.state.harness, 1)
            try:
                scheduler.tick()
                done = wait_for(
                    lambda: (
                        row
                        if (row := replacement.harness.get(job["task_id"]))["status"]
                        in {"succeeded", "failed"}
                        else None
                    )
                )
            finally:
                scheduler.close()
        assert done["status"] == "succeeded", done.get("reason_code")
        assert done["run_id"] == original["run_id"]
        if resume_requested:
            assert done["execution_epoch"] > original["execution_epoch"]
        assert len(env.requests) == 2
    finally:
        replacement.close()


@contextmanager
def private_http(app, path):
    server = uvicorn.Server(
        uvicorn.Config(app, uds=str(path), log_level="error", forwarded_allow_ips="*")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, seconds=6)
        yield
    finally:
        server.should_exit = True
        thread.join(8)
        assert not thread.is_alive()


@contextmanager
def public_http(socket_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mgar_v3.harness.gateway:create_app",
            "--factory",
            "--fd",
            str(listener.fileno()),
            "--log-level",
            "error",
            "--no-proxy-headers",
        ],
        pass_fds=(listener.fileno(),),
        # This process gets only its Unix socket, no database or model config.
        env={"PATH": os.environ["PATH"], "MGAR_H16_CONTROLLER_SOCKET": str(socket_path)},
    )
    listener.close()
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as front:
        try:

            def ready():
                assert process.poll() is None
                try:
                    return front.get("/api/v3/auth/session", timeout=0.5).status_code == 200
                except httpx.TransportError:
                    return False

            wait_for(ready, seconds=10)
            yield front
        finally:
            process.terminate()
            process.wait(timeout=8)


@pytest.mark.parametrize("tenants", [False], indirect=True)
def test_front_proxy_keeps_cookie_jars_and_forwarded_identity_isolated(tenants):
    env = tenants
    setup_tenants(env)
    path = env.tmp_path / "private/controller.sock"
    path.parent.mkdir(mode=0o700)
    backend = create_app(env.settings, env.engine, run_background=False)
    with private_http(backend, path), public_http(path) as front:
        a = member(front, "alice")
        projects = expect(front.get("/api/v3/harness/projects"))
        assert {item["project_id"] for item in projects} == {env.projects["alpha"]}
        queued = submit(front, a, env.projects["alpha"], "解释项目", "stream")
        route = f"/api/v3/agent/projects/{env.projects['alpha']}/members/{env.users['alice']}"
        with front.stream("GET", task_path(queued["task_id"]) + "/events") as stream:
            assert stream.status_code == 200
            expect(env.operator.delete(route, headers=ADMIN), 204)
            assert any(line == "event: auth_required" for line in stream.iter_lines())
        front.cookies.clear()
        assert front.get("/api/v3/harness/projects").status_code == 401
        member(front, "bob")
        projects = expect(
            front.get(
                "/api/v3/harness/projects",
                headers={
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Forwarded-Proto": "https",
                },
            )
        )
        assert {item["project_id"] for item in projects} == {env.projects["beta"]}
    with TestClient(create_front(path), base_url=ORIGIN) as front:
        assert front.get("/api/v3/harness/projects").status_code == 503


def test_revocation_at_model_dispatch_releases_the_unspent_reservation(tenants):
    env = tenants
    setup_tenants(env)
    alice = client(env)
    headers = member(alice, "alice")
    waiting, proceed = threading.Event(), threading.Event()

    def fault(point, payload):
        if point == "model_before_dispatch":
            waiting.set()
            assert proceed.wait(20)

    env.app.state.harness.fault_hook = fault
    project_id = env.projects["alpha"]
    try:
        job = submit(alice, headers, project_id, "解释项目", "revoked-before-dispatch")
        assert waiting.wait(7)
        expect(
            env.operator.delete(
                f"/api/v3/agent/projects/{project_id}/members/{env.users['alice']}",
                headers=ADMIN,
            ),
            204,
        )
        assert alice.get(task_path(job["task_id"]) + "/events?follow=false").status_code == 404
    finally:
        proceed.set()

    def finished():
        response = expect(env.operator.get(task_path(job["task_id"]), headers=ADMIN))
        return (
            response if response["status"] in {"denied", "failed", "blocked", "unknown"} else None
        )

    assert finished() or wait_for(finished)
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT used_calls,reserved_micros,settled_micros FROM app.authorization_budgets "
            "JOIN app.harness_tasks USING (profile_hash) WHERE task_id=%s",
            (job["task_id"],),
        ).fetchone()
    assert row == {"used_calls": 0, "reserved_micros": 0, "settled_micros": 0}
    assert not env.requests


def test_bounded_dispatch_burst_records_wait_times_across_projects(tenants, record_property):
    env = tenants
    setup_tenants(env, max_calls=32)
    alice, bob = client(env), client(env)
    a, b = member(alice, "alice"), member(bob, "bob")
    projects = (env.projects["alpha"], env.projects["beta"])
    for project_id in projects:
        expect(
            env.operator.put(
                f"/api/v3/agent/projects/{project_id}/quota",
                headers=ADMIN,
                json={"max_parallel": 1, "max_queued": 8},
            )
        )

    entered, release = threading.Barrier(3), threading.Event()
    first = set()
    lock = threading.Lock()

    def hold_initial_slots(model, request, cancellation):
        if model.ordinal != 1:
            return
        workspace = request.task.workspace_id
        with lock:
            initial = workspace not in first
            first.add(workspace)
        if initial:
            entered.wait(20)
            assert release.wait(45)

    env.hook = hold_initial_slots
    start = time.monotonic()
    jobs = []
    try:
        for index in range(6):
            browser, headers = (alice, a) if index % 2 == 0 else (bob, b)
            jobs.append(
                (
                    browser,
                    submit(
                        browser,
                        headers,
                        projects[index % 2],
                        "解释项目",
                        f"burst-{index}",
                    )["task_id"],
                )
            )
            if index == 1:
                entered.wait(20)
        assert (
            sum(
                expect(browser.get(task_path(task)))["status"] == "queued" for browser, task in jobs
            )
            == 4
        )
    finally:
        release.set()
    for browser, task in jobs:
        assert finish(browser, task)["status"] == "completed"
    with env.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT (safe_payload->>'queued_ms')::int AS delay FROM app.run_events "
            "WHERE kind='harness_dispatch_started' ORDER BY delay"
        ).fetchall()
    waits = [row["delay"] for row in rows]
    assert len(waits) == 6 and min(waits) >= 0 and max(waits) > 0
    record_property("managed_dispatch_wait_ms", ",".join(map(str, waits)))
    record_property("managed_burst_wall_ms", round((time.monotonic() - start) * 1000))


class ControllerLoss(BaseException):
    pass


@pytest.mark.parametrize("tenants", [False], indirect=True)
def test_recovered_managed_worker_reuses_known_model_response(tenants):
    env = tenants
    setup_tenants(env)
    alice = client(env)
    headers = member(alice, "alice")
    job = submit(alice, headers, env.projects["alpha"], "解释项目", "known-first")
    task_id = job["task_id"]
    before = env.engine.harness.get(task_id)

    def crash(point, payload):
        if point == "model_after_settlement":
            raise ControllerLoss()

    env.app.state.harness.fault_hook = crash
    old = HarnessScheduler(env.app.state.harness, 1)
    try:
        old.tick()
        wait_for(lambda: task_id in old._active and old._active[task_id][1].done())
        assert isinstance(old._active[task_id][1].exception(), ControllerLoss)
    finally:
        old.close()

    known = next(op for op in env.engine.harness.operations(task_id) if op["kind"] == "model")
    assert known["state"] == "known" and len(env.requests) == 1
    env.engine.close()
    replacement = Engine(env.settings, env.engine.artifacts)
    try:
        replacement.recover()
        paused = replacement.harness.get(task_id)
        assert paused["status"] == "paused"
        assert paused["execution_epoch"] > before["execution_epoch"]
        app = create_app(env.settings, replacement, run_background=True)
        with TestClient(app, base_url=ORIGIN) as browser:
            attach_model(env, app)
            headers = member(browser, "alice")
            expect(browser.post(task_path(task_id) + "/resume", headers=headers), 202)
            answer = finish(browser, task_id)
            assert answer["status"] == "completed", answer
        assert len(env.requests) == 2
        assert sum(operation == known["operation_id"] for _, _, operation, _ in env.requests) == 1
        done = replacement.harness.get(task_id)
        assert (
            done["run_id"] == before["run_id"]
            and done["execution_epoch"] > paused["execution_epoch"]
        )
    finally:
        replacement.close()


def test_forward_migration_preserves_legacy_owner_session_and_migration_hashes(settings, tmp_path):
    import hashlib
    import shutil
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from mgar_v3.evidence.artifacts import ArtifactStore
    from mgar_v3.store.migrate import migrate
    from mgar_v3.web_access import LoginInput, WebAccounts, password_hash

    base_dsn = settings.database_url.get_secret_value()
    database = "mgar_h16_upgrade_" + uuid4().hex
    old = tmp_path / "previous-migrations"
    old.mkdir()
    for migration in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
        if migration.name < "0057":
            shutil.copy(migration, old / migration.name)
    with psycopg.connect(base_dsn, autocommit=True) as management:
        management.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        dsn = make_conninfo(base_dsn, dbname=database)
        try:
            migrate(dsn, old)
            uid = uuid4()
            with psycopg.connect(dsn) as previous:
                previous.execute(
                    "INSERT INTO app.web_accounts(user_id,username,password_hash) VALUES(%s,%s,%s)",
                    (uid, "legacy-owner", password_hash(PASSWORD)),
                )
                previous.execute(
                    "INSERT INTO app.web_sessions(token_hash,user_id,csrf_token,expires_at) "
                    "VALUES (%s,%s,'csrf-before-upgrade',now()+interval '1 hour')",
                    (hashlib.sha256(b"fixture-legacy-session").hexdigest(), uid),
                )
                previous.execute(
                    "INSERT INTO app.harness_sessions "
                    "(session_id,project_id,actor,title,initial_client_id) "
                    "VALUES ('hs-000000000000000000000001','legacy',%s,'history','old-id')",
                    (settings.local_actor,),
                )
                hashes = previous.execute(
                    "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
                ).fetchall()
            migrations = Path(__file__).resolve().parents[1] / "migrations"
            expected = sorted(p.name for p in migrations.glob("*.sql") if p.name >= "0057")
            assert expected[0] == "0057_harness_tenants.sql"
            assert migrate(dsn) == expected
            assert not migrate(dsn)
            with psycopg.connect(dsn) as migrated:
                assert (
                    migrated.execute(
                        "SELECT name,sha256 FROM app.schema_migrations "
                        "WHERE name<'0057' ORDER BY name"
                    ).fetchall()
                    == hashes
                )
                assert migrated.execute(
                    "SELECT title,membership_id FROM app.harness_sessions"
                ).fetchone() == ("history", None)
            authority = Engine(
                settings.model_copy(update={"database_url": SecretStr(dsn)}),
                ArtifactStore(tmp_path / "upgrade-artifacts"),
            )
            try:
                accounts = WebAccounts(authority)
                credentials = LoginInput(username="legacy-owner", password=SecretStr(PASSWORD))
                assert accounts.login(credentials, "fixture-client") == uid
                legacy = accounts.session("fixture-legacy-session")
                assert legacy is not None and legacy.is_owner and legacy.user_id == uid
                assert accounts.session(accounts.issue_session(uid)).is_owner
                member_id = accounts.create_member(
                    LoginInput(username="added-user", password=SecretStr(PASSWORD))
                )
                assert accounts.session(accounts.issue_session(member_id)).is_owner is False
                with pytest.raises(DomainError) as second_owner:
                    accounts.create_owner(
                        LoginInput(username="other-owner", password=SecretStr(PASSWORD))
                    )
                assert second_owner.value.code == "ACCOUNT_ALREADY_EXISTS"
            finally:
                authority.close()
        finally:
            management.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


def test_overlapping_registered_roots_and_runtime_state_cannot_be_shared(tenants):
    env = tenants
    setup_tenants(env)
    parent = env.tmp_path / "alpha"
    child = parent / "nested-project"
    child.mkdir()
    (child / "README.md").write_text("Private nested project fixture.\n")
    project = expect(
        env.operator.post(
            "/api/v3/harness/projects",
            headers=ADMIN,
            json={"path": str(child)},
        ),
        201,
    )["project_id"]
    quota = {"max_parallel": 1, "max_queued": 2}
    denied = expect(
        env.operator.put(
            f"/api/v3/agent/projects/{project}/quota",
            headers=ADMIN,
            json=quota,
        ),
        409,
    )
    assert denied["code"] == "HARNESS_WORKSPACE_OVERLAP"

    alice = client(env)
    headers = member(alice, "alice")
    failed = submit(alice, headers, env.projects["alpha"], "解释项目", "overlap", status=409)
    assert failed["code"] == "HARNESS_WORKSPACE_OVERLAP"
    assert not env.requests

    state_path = env.settings.harness_root / "runtime-control-files"
    state_path.mkdir()
    protected = expect(
        env.operator.post(
            "/api/v3/harness/projects",
            headers=ADMIN,
            json={"path": str(state_path)},
        ),
        201,
    )["project_id"]
    result = expect(
        env.operator.put(
            f"/api/v3/agent/projects/{protected}/quota",
            headers=ADMIN,
            json=quota,
        ),
        409,
    )
    assert result["code"] == "HARNESS_WORKSPACE_PROTECTED"
