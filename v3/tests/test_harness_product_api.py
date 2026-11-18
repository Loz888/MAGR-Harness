"""Independent product API acceptance with real PostgreSQL and Docker.

All model responses are local scripted fixtures. Browser authentication, public
request shapes, preview isolation and restart cleanup use production routes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mgar_v3.api import create_app
from mgar_v3.harness import broker as broker_module
from mgar_v3.harness.broker import CommandIdentity, IsolatedExecutionBroker
from mgar_v3.harness.client import HarnessAPIError, HarnessClient
from mgar_v3.harness.contracts import ModelResponse, ModelUsage
from mgar_v3.harness.product import HarnessProductService
from mgar_v3.harness.service import DeterministicCodingModel, HarnessCodingService

ROOT = Path(__file__).resolve().parents[1]
IMAGE_LOCK = ROOT / ".local/r03/sandbox-lock.json"
ORIGIN = "http://127.0.0.1:18080"
OPERATOR_TOKEN = "fixture-product-operator-token"
MODEL_KEY = "fixture-product-model-key"

APP = '''import json
from http.server import HTTPServer,BaseHTTPRequestHandler
PORT = 8123
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/redirect'):
            self.send_response(302)
            self.send_header('Location','/api/v3/agent/profile')
            self.end_headers()
            return
        if self.path.startswith('/image.png'):
            body=b'\\x89PNG\\r\\n\\x1a\\nfixture'
            content_type='image/png'
        elif self.path == '/':
            body=b'<h1>isolated project preview</h1>'
            content_type='text/html; charset=utf-8'
        else:
            body=json.dumps({'surface':'project-preview','path':self.path,
                             'headers':dict(self.headers)}).encode()
            content_type='application/json'
        self.send_response(200)
        self.send_header('Content-Type',content_type)
        self.send_header('Set-Cookie','untrusted-project-cookie=must-not-reach-browser')
        self.end_headers()
        self.wfile.write(body)
if __name__ == '__main__':
    HTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
'''


@pytest.fixture
def api_env(settings, engine, tmp_path, monkeypatch):
    settings.deepseek_api_key = None
    settings.allow_paid = False
    settings.local_token = SecretStr(OPERATOR_TOKEN)
    settings.harness_image_lock = IMAGE_LOCK
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("A controlled API acceptance project.\n")
    (source / "app.py").write_text(APP)
    (source / "calc.py").write_text("def add(a,b):\n    return a - b\n")
    (source / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass CalcTest(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2,3),5)\n"
    )
    captured = []
    app = create_app(settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                    client=("127.0.0.1", 54321)) as client:
        setup = client.post("/api/v3/auth/setup", json={
            "username": "api-owner", "password": "fixture-product-password",
        })
        assert setup.status_code == 201, setup.text
        client.headers["X-CSRF-Token"] = setup.json()["csrf_token"]

        class FixtureModel:
            def __init__(self, request):
                captured.append(request)
                again = "再次" in request.objective
                self.inner = DeterministicCodingModel(
                    task_kind=request.task_kind,
                    target_path="README.md" if request.task_kind == "inspect" else "calc.py",
                    old_text="return a + b" if again else "return a - b",
                    new_text="return (a + b)" if again else "return a + b",
                    test_argv=tuple(
                        request.product_plan.get("commands", {}).get("tests", {}).get("argv", ())
                    ),
                )

            def complete(self, request, cancellation):
                return self.inner.complete(request, cancellation).model_copy(
                    update={"usage": ModelUsage(cost_micros=3)}
                )

        harness = app.state.harness
        harness.model_factory = FixtureModel
        monkeypatch.setattr(harness, "submit_task", harness.create_task)
        registered = client.post("/api/v3/harness/projects", json={"path": str(source)})
        assert registered.status_code == 201, registered.text
        project_id = registered.json()["project_id"]

        def configure():
            saved = client.put("/api/v3/settings/analysis", json={
                "deepseek_api_key": MODEL_KEY, "allow_paid": True, "expected_revision": 0,
                "model_id": "deepseek-flash",
            })
            assert saved.status_code == 200, saved.text
            authorized = client.post(
                f"/api/v3/agent/projects/{project_id}/authorization", json={
                    "allow_write": True, "allow_paid": True,
                    "max_cost_micros": 250000, "max_model_calls": 64,
                },
            )
            assert authorized.status_code == 200, authorized.text

        yield SimpleNamespace(
            app=app, client=client, source=source, project_id=project_id, harness=harness,
            engine=engine, settings=settings, configure=configure, captured=captured,
        )


def _completed(env, *, client_id="api-first"):
    env.configure()
    created = env.client.post("/api/v3/agent/sessions", json={
        "project_id": env.project_id, "text": "修复加法函数的错误", "client_id": client_id,
    })
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "completed", created.json()
    return created.json()


def _preview(env, session):
    response = env.client.post(
        f"/api/v3/agent/sessions/{session['session_id']}/preview",
        json={"task_id": session["task_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    return response.json()


def test_product_cookie_csrf_origin_operator_token_and_no_store(api_env):
    env = api_env
    profile = env.client.get("/api/v3/agent/profile")
    assert profile.status_code == 200
    assert profile.headers["cache-control"] == "no-store"
    assert MODEL_KEY not in profile.text and OPERATOR_TOKEN not in profile.text
    authorization = f"/api/v3/agent/projects/{env.project_id}/authorization"
    payload = {"allow_write": True, "allow_paid": False}
    csrf = env.client.headers.pop("X-CSRF-Token")
    assert env.client.post(authorization, json=payload).status_code == 403
    env.client.headers["X-CSRF-Token"] = csrf
    assert env.client.post(authorization, json=payload,
                           headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert env.client.post(authorization, json=payload,
                           headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    env.client.cookies.clear()
    assert env.client.get("/api/v3/agent/profile").status_code == 401
    env.client.headers.pop("Origin")
    env.client.headers.pop("X-CSRF-Token")
    token_response = env.client.post(authorization, json=payload,
                                     headers={"X-MGAR-Token": OPERATOR_TOKEN})
    assert token_response.status_code == 200, token_response.text
    assert token_response.headers["cache-control"] == "no-store"
    assert not env.captured


@pytest.mark.parametrize("extra", [
    {"mode": "deterministic"}, {"task_kind": "feature"}, {"task_type": "bugfix"},
    {"approved": True},
    {"test_command": "python3 -c 'print(1)'"}, {"argv": ["sh", "-c", "echo unsafe"]},
    {"target_path": "calc.py", "old_text": "old", "new_text": "new"},
])
def test_natural_entry_rejects_fixture_task_type_and_raw_execution_inputs(api_env, extra):
    env = api_env
    response = env.client.post("/api/v3/agent/sessions", json={
        "project_id": env.project_id, "text": "修复错误", **extra,
    })
    assert response.status_code == 422, response.text
    assert not env.captured
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.harness_sessions").fetchone()["n"] == 0


def test_public_natural_session_continues_after_completion_and_applies_verified_diff(api_env):
    env = api_env
    session = _completed(env)
    assert session["turns"][0]["text"] == "修复加法函数的错误"
    assert env.captured[0].old_text == "" and env.captured[0].new_text == ""
    assert env.captured[0].mode == "live"
    first_task = session["task_id"]
    again = env.client.post(
        f"/api/v3/agent/sessions/{session['session_id']}/messages", json={
            "text": "再次调整加法函数写法", "client_id": "api-again",
        },
    )
    assert again.status_code == 200, again.text
    updated = again.json()
    assert updated["status"] == "completed" and updated["task_id"] != first_task
    assert len(updated["turns"]) == 2
    task = env.harness.get_task(updated["task_id"])
    applied = env.client.post(
        f"/api/v3/agent/sessions/{session['session_id']}/apply", json={
            "task_id": updated["task_id"], "expected_tree": task["tree_after"],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert "return (a + b)" in (env.source / "calc.py").read_text()
    listed = env.client.get("/api/v3/agent/sessions")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"


def test_public_known_blocked_turn_can_continue_without_reviving_its_old_run(api_env):
    env = api_env
    env.configure()
    normal_factory = env.harness.model_factory

    class KnownBlockedModel:
        def complete(self, request, cancellation):
            cancellation.ensure_active()
            return ModelResponse(text="No implementation produced", finish_reason="stop",
                                 usage=ModelUsage(cost_micros=3))

    env.harness.model_factory = lambda request: KnownBlockedModel()
    blocked = env.client.post("/api/v3/agent/sessions", json={
        "project_id": env.project_id, "text": "新增一个功能", "client_id": "known-blocked",
    })
    assert blocked.status_code == 200, blocked.text
    first = blocked.json()
    assert first["status"] in {"blocked", "incomplete"}, first
    old = env.harness.store.get(first["task_id"])
    env.harness.model_factory = normal_factory
    continued = env.client.post(
        f"/api/v3/agent/sessions/{first['session_id']}/messages", json={
            "text": "修复加法函数的错误", "client_id": "after-known-blocked",
        },
    )
    assert continued.status_code == 200, continued.text
    latest = continued.json()
    assert latest["status"] == "completed" and latest["task_id"] != first["task_id"]
    new = env.harness.store.get(latest["task_id"])
    assert new["thread_id"] == old["thread_id"] and new["run_id"] != old["run_id"]
    with env.engine.transaction() as conn:
        before = conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"]
    stale = env.client.post(f"/api/v3/harness/tasks/{first['task_id']}/resume")
    assert stale.status_code in {403, 409}, stale.text
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == before


@pytest.fixture
def headless_gateway(api_env):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        server = uvicorn.Server(uvicorn.Config(
            api_env.app, lifespan="off", access_log=False, log_level="error", ws="none"
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def _headless(api, *argv, payload=None, token=OPERATOR_TOKEN):
    environ = dict(os.environ, MGAR_V3_LOCAL_TOKEN=token)
    environ.pop("MGAR_V3_DATABASE_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "mgar_v3.cli", "--api", api, "harness", *argv],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environ,
    )


def test_h13_web_sdk_and_headless_share_the_authoritative_result(
    api_env, headless_gateway, tmp_path
):
    env, api = api_env, headless_gateway
    env.configure()
    payload = {"project_id": env.project_id, "text": "修复加法函数的错误", "client_id": "h13-first"}
    cli = _headless(api, "submit", "--input", "-", payload=payload)
    assert cli.returncode == 0, cli.stderr
    submitted = json.loads(cli.stdout)
    session_id, task_id = submitted["session_id"], submitted["task_id"]
    with HarnessClient(OPERATOR_TOKEN, api=api) as sdk:
        duplicate = sdk.submit(**payload)
        assert duplicate["duplicate"] and duplicate["task_id"] == task_id
        assert len(env.captured) == 1
        session_path = f"/api/v3/agent/sessions/{session_id}"
        assert sdk.session(session_id) == env.client.get(session_path).json()
        task_path = f"/api/v3/harness/tasks/{task_id}"
        web_task = env.client.get(task_path).json()
        cli_task = _headless(api, "task", task_id)
        assert cli_task.returncode == 0
        assert sdk.task(task_id) == json.loads(cli_task.stdout) == web_task
        assert web_task["tree_before"] != web_task["tree_after"]
        assert web_task["settled_cost_micros"] > 0 and web_task["usage_known"]
        assert sdk.diff(task_id) == web_task["diff"]
        assert sdk.artifacts(task_id) == env.client.get(task_path + "/artifacts").json()
        output = tmp_path / "headless.patch"
        patch = _headless(api, "patch", task_id, "--output", str(output))
        assert patch.returncode == 0, patch.stderr
        assert output.read_bytes() == sdk.patch(task_id)
        assert sha256(output.read_bytes()).hexdigest() == web_task["patch_sha256"]

        web_export = env.client.get(session_path + "/export")
        assert web_export.headers["cache-control"] == "no-store"
        assert sdk.export_session(session_id) == web_export.json()
        assert web_export.json()["schema_version"] == "mgar.harness.session.v1"
        assert web_export.json()["latest_task"]["tree_after"] == web_task["tree_after"]
        assert "workspace_path" not in web_export.json()["latest_task"]
        assert MODEL_KEY not in web_export.text and OPERATOR_TOKEN not in web_export.text

        with closing(sdk.events(task_id, follow=True)) as stream:
            first = next(stream)
        replay = _headless(api, "events", task_id, "--after", str(first["cursor"]))
        assert replay.returncode == 0, replay.stderr
        resumed = [json.loads(row) for row in replay.stdout.splitlines()]
        page = sdk.event_page(task_id, after=first["cursor"])
        assert resumed[-1]["event"] == "done"
        assert [item["data"] for item in resumed[:-1]] == page["events"]
        assert resumed[-1]["cursor"] == page["next_cursor"]

        continued = sdk.send(session_id, "再次调整加法函数写法", client_id="h13-followup")
        assert continued["status"] == "completed" and len(continued["turns"]) == 2
        assert continued["task_id"] != task_id
        exported = _headless(api, "export", session_id)
        assert exported.returncode == 0
        assert json.loads(exported.stdout) == sdk.export_session(session_id)


def test_h13_authentication_and_dependency_exit_codes(api_env, headless_gateway, monkeypatch):
    env, api = api_env, headless_gateway
    unauthorized = _headless(api, "sessions", token="unauthorized")
    assert unauthorized.returncode == 1
    assert json.loads(unauthorized.stderr)["code"] == "AUTH_REQUIRED"
    with HarnessClient("unauthorized", api=api) as sdk:
        with pytest.raises(HarnessAPIError, match="登录") as error:
            list(sdk.events("h08-000000000000"))
        assert error.value.status_code == 401
    env.configure()
    monkeypatch.setattr(env.harness, "image_lock", env.source / "missing-lock.json")
    readiness = _headless(api, "readiness", env.project_id)
    assert readiness.returncode == 3, readiness.stderr
    assert json.loads(readiness.stdout)["environment"]["status"] == "waiting_environment"
    blocked = _headless(api, "submit", payload={
        "project_id": env.project_id, "text": "修复加法", "client_id": "h13-blocked",
    })
    assert blocked.returncode == 3
    assert json.loads(blocked.stderr)["code"] == "HARNESS_ENVIRONMENT_REQUIRED"
    assert not env.captured


def test_h13_pause_resume_cancel_and_incomplete_exit_use_the_same_run(
    api_env, headless_gateway, monkeypatch
):
    env, api = api_env, headless_gateway
    env.configure()
    fixture_model = env.harness.model_factory
    arrived = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    class GatedModel:
        def __init__(self, request):
            self.model = fixture_model(request)
            self.calls = 0

        def complete(self, request, cancellation):
            response = self.model.complete(request, cancellation)
            index = self.calls
            self.calls += 1
            if index < len(arrived):
                arrived[index].set()
                assert release[index].wait(30)
            return response

    monkeypatch.setattr(env.harness, "model_factory", GatedModel)
    monkeypatch.setattr(
        env.harness, "submit_task", HarnessCodingService.submit_task.__get__(env.harness)
    )
    with HarnessClient(OPERATOR_TOKEN, api=api) as sdk:
        task = sdk.submit(env.project_id, "修复加法错误", client_id="h13-control")["task_id"]

        def await_pause():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and sdk.task(task)["status"] == "running":
                time.sleep(0.02)
            assert sdk.task(task)["status"] == "paused"

        try:
            assert arrived[0].wait(20)
            running = _headless(api, "task", task)
            assert running.returncode == 2, running.stderr
            run_id = json.loads(running.stdout)["run_id"]
            assert sdk.pause(task)["task_id"] == task
            release[0].set()
            await_pause()
            assert env.harness.store.get(task)["public_status"] == "paused"
            resumed = _headless(api, "resume", task)
            assert resumed.returncode == 0, resumed.stderr
            assert arrived[1].wait(20)
            assert sdk.task(task)["status"] == "running"
            sdk.pause(task)
            release[1].set()
            await_pause()
            cancelled = _headless(api, "cancel", task, "--reason", "H13 fixture cancellation")
            assert cancelled.returncode == 0, cancelled.stderr
            assert json.loads(cancelled.stdout)["status"] == "cancelled"
        finally:
            for gate in release:
                gate.set()
            result = env.harness.join_task(task, timeout=30)
        assert result["status"] == "cancelled" and result["run_id"] == run_id
        assert env.harness.store.get(task)["public_status"] == "cancelled"
        terminal = _headless(api, "task", task)
        assert terminal.returncode == 1
        assert json.loads(terminal.stdout)["status"] == "cancelled"
        assert sdk.task(task) == env.client.get(f"/api/v3/harness/tasks/{task}").json()


def test_real_preview_mime_sandbox_no_cookie_forwarding_and_control_api_boundary(api_env):
    env = api_env
    session = _completed(env)
    preview = _preview(env, session)
    url = preview["preview_url"]
    env.client.headers["Authorization"] = "Bearer fixture-must-not-be-forwarded"
    env.client.headers["X-MGAR-Token"] = OPERATOR_TOKEN
    response = env.client.get(url)
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert "isolated project preview" in response.text
    csp = response.headers["content-security-policy"]
    assert "sandbox" in csp and "allow-same-origin" not in csp
    assert "form-action 'none'" in csp and "base-uri 'none'" in csp
    assert "script-src " + ORIGIN + url in csp
    assert "connect-src " + ORIGIN + url in csp
    assert "default-src 'none'" in csp
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers
    image = env.client.get(url + "image.png")
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    echo = env.client.get(url + "echo?next=http%3A%2F%2Fexample.invalid%2Fsecret").json()
    assert echo["surface"] == "project-preview"
    assert echo["path"] == "/echo?next=http%3A%2F%2Fexample.invalid%2Fsecret"
    forwarded = {name.lower(): value for name, value in echo["headers"].items()}
    for name in ("cookie", "authorization", "x-mgar-token", "x-csrf-token"):
        assert name not in forwarded
    root_api = env.client.get(url + "api/v3/agent/profile")
    assert root_api.json()["surface"] == "project-preview"
    redirect = env.client.get(url + "redirect", follow_redirects=False)
    assert redirect.status_code == 302 and "location" not in redirect.headers
    invalid = env.client.get(url + "%0Asecret")
    assert invalid.status_code in {400, 404, 422}
    stopped = env.client.delete(preview["preview_control_url"])
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    assert env.client.get(url).status_code == 404


def test_real_preview_authorization_revocation_stops_process_and_retains_owner_receipt(api_env):
    env = api_env
    session = _completed(env)
    preview = _preview(env, session)
    with env.engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.harness_previews").fetchone()
    identity = CommandIdentity(**row["identity"])
    assert row["state"] == "running" and identity.task_id == session["task_id"]
    handle = env.app.state.product._previews[session["session_id"]]
    broker = handle.session.broker
    assert broker.owned_command_state(identity)[0] == "running"
    revoked = env.client.delete(f"/api/v3/agent/projects/{env.project_id}/authorization")
    assert revoked.status_code == 200
    assert env.client.get(preview["preview_url"]).status_code in {403, 404}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and broker.owned_command_state(identity)[0] != "missing":
        time.sleep(.03)
    assert broker.owned_command_state(identity)[0] == "missing"
    assert env.client.delete(preview["preview_control_url"]).status_code == 200
    with env.engine.transaction() as conn:
        state = conn.execute("SELECT state FROM app.harness_previews").fetchone()["state"]
        assert state == "closed"


def test_real_preview_restart_cleans_only_the_journalled_owned_container(api_env):
    env = api_env
    session = _completed(env)
    started = _preview(env, session)
    with env.engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.harness_previews").fetchone()
    identity = CommandIdentity(**row["identity"])
    broker = env.app.state.product._previews[session["session_id"]].session.broker
    assert broker.owned_command_state(identity)[0] == "running"
    restarted = HarnessProductService(env.harness, env.app.state.repo_service.web_settings)
    try:
        assert broker.owned_command_state(identity)[0] == "missing"
        with env.engine.transaction() as conn:
            assert conn.execute("SELECT state FROM app.harness_previews").fetchone()["state"] == (
                "closed"
            )
        restored = restarted.get(session["session_id"])
        assert restored["task_id"] == session["task_id"]
        old_token = started["preview_url"].rstrip("/").rsplit("/", 1)[-1]
        from mgar_v3.domain import DomainError

        with pytest.raises(DomainError) as rejected:
            restarted.fetch_preview_token(old_token, "")
        assert rejected.value.code == "HARNESS_PREVIEW_UNKNOWN"
    finally:
        restarted.close()


def test_new_turn_replaces_old_preview_with_a_new_owned_process(api_env):
    env = api_env
    session = _completed(env)
    _preview(env, session)
    with env.engine.transaction() as conn:
        old = conn.execute("SELECT * FROM app.harness_previews").fetchone()
    old_identity = CommandIdentity(**old["identity"])
    old_broker = env.app.state.product._previews[session["session_id"]].session.broker
    next_turn = env.client.post(
        f"/api/v3/agent/sessions/{session['session_id']}/messages", json={
            "text": "再次调整加法函数写法", "client_id": "preview-next-turn",
        },
    )
    assert next_turn.status_code == 200, next_turn.text
    latest = next_turn.json()
    assert latest["status"] == "completed" and latest["task_id"] != session["task_id"]
    restarted = _preview(env, latest)
    with env.engine.transaction() as conn:
        new = conn.execute("SELECT * FROM app.harness_previews").fetchone()
    new_identity = CommandIdentity(**new["identity"])
    assert new_identity.task_id == latest["task_id"]
    assert new_identity.name != old_identity.name
    assert old_broker.owned_command_state(old_identity)[0] == "missing"
    assert env.client.get(restarted["preview_url"]).status_code == 200


def test_preview_read_capability_is_anonymous_scoped_and_cannot_mutate_control_plane(api_env):
    env = api_env
    session = _completed(env)
    preview = _preview(env, session)
    token = preview["preview_url"].rstrip("/").rsplit("/", 1)[-1]
    assert len(token) == 43
    # No second lifespan: this client exercises the already-running app as an
    # anonymous network peer, without replacing its Engine or service objects.
    anonymous = TestClient(env.app, base_url=ORIGIN)
    try:
        response = anonymous.get(preview["preview_url"])
        assert response.status_code == 200 and "isolated project preview" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        echo = anonymous.get(preview["preview_url"] + "echo").json()
        assert echo["surface"] == "project-preview"
        assert "cookie" not in {name.lower() for name in echo["headers"]}
        assert anonymous.get("/api/v3/agent/sessions").status_code == 401
        assert anonymous.get("/api/v3/agent/profile", headers={
            "X-MGAR-Token": token,
        }).status_code == 401
        assert anonymous.post("/api/v3/agent/sessions", json={
            "project_id": env.project_id, "text": "must not start a task",
        }).status_code == 401
        assert anonymous.delete(preview["preview_control_url"]).status_code == 401
        assert anonymous.post(preview["preview_url"], json={"text": "mutate"}).status_code == 405
        assert anonymous.get("/api/v3/agent/preview/" + "A" * 43 + "/").status_code == 404
        stopped = env.client.delete(preview["preview_control_url"])
        assert stopped.status_code == 200
        assert anonymous.get(preview["preview_url"]).status_code == 404
    finally:
        anonymous.close()


def test_preview_capability_expires_and_cleans_the_owned_process(api_env, monkeypatch):
    env = api_env
    session = _completed(env)
    started = _preview(env, session)
    preview = env.app.state.product._previews[session["session_id"]]
    with env.engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.harness_previews").fetchone()
    identity = CommandIdentity(**row["identity"])
    # Advance only the actual Broker lease clock; PostgreSQL and cookie
    # authentication stay real and valid, isolating the preview TTL boundary.
    with monkeypatch.context() as clock:
        future = datetime.now(UTC) + timedelta(hours=1)
        clock.setattr(broker_module, "_now", lambda: future)
        expired = env.client.get(started["preview_url"])
        assert expired.status_code == 403, expired.text
        assert expired.json()["code"] == "HARNESS_LEASE_EXPIRED"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if preview.session.broker.owned_command_state(identity)[0] == "missing":
                break
            time.sleep(.03)
        assert preview.session.broker.owned_command_state(identity)[0] == "missing"
    assert env.client.delete(started["preview_control_url"]).status_code == 200
    assert env.client.get(started["preview_url"]).status_code == 404


@pytest.mark.parametrize("failure_mode", ["domain", "transport"])
def test_unknown_preview_owner_cannot_be_overwritten_or_declared_closed(
    api_env, monkeypatch, failure_mode,
):
    from mgar_v3.domain import DomainError

    env = api_env
    session = _completed(env)
    started = _preview(env, session)
    old_service = env.app.state.product
    old_broker = old_service._previews[session["session_id"]].session.broker
    with env.engine.transaction() as conn:
        original = conn.execute("SELECT * FROM app.harness_previews").fetchone()
    identity = CommandIdentity(**original["identity"])
    owner_before = json.dumps({"identity": original["identity"], "lease": original["lease"]},
                              sort_keys=True)
    original_inspect = IsolatedExecutionBroker.owned_command_state
    original_docker = IsolatedExecutionBroker._docker
    containers_before = old_broker._docker([
        "ps", "-aq", "--filter", "label=mgar.v4.h11.task=" + session["task_id"]
    ]).stdout.split()
    observed_calls = len(env.captured)
    restarted = None
    try:
        with monkeypatch.context() as unavailable:
            def deny_inspection(self, command_identity):
                raise DomainError("CONTROLLED_DOCKER_UNAVAILABLE", "Owner inspection unknown", 503)

            if failure_mode == "domain":
                unavailable.setattr(IsolatedExecutionBroker, "owned_command_state", deny_inspection)
            else:
                def unavailable_transport(self, arguments, **kwargs):
                    if arguments and arguments[0] == "inspect" and identity.name in arguments:
                        return subprocess.CompletedProcess(
                            ["docker", *arguments], 1, stdout=b"",
                            stderr=b"Cannot connect to the Docker daemon: controlled outage",
                        )
                    return original_docker(self, arguments, **kwargs)

                unavailable.setattr(IsolatedExecutionBroker, "_docker", unavailable_transport)
            restarted = HarnessProductService(env.harness, env.app.state.repo_service.web_settings)
            env.app.state.product = restarted
            with env.engine.transaction() as conn:
                unknown = conn.execute("SELECT * FROM app.harness_previews").fetchone()
            assert unknown["state"] == "unknown"
            actual = original_docker(old_broker, [
                "inspect", "--format", "{{.State.Status}}", identity.name
            ])
            assert actual.returncode == 0 and actual.stdout.strip() == b"running"
            begin = env.client.post(started["preview_control_url"], json={
                "task_id": session["task_id"],
            })
            assert begin.status_code in {409, 503}, begin.text
            stopped = env.client.delete(started["preview_control_url"])
            assert stopped.status_code in {409, 503}, stopped.text
            with env.engine.transaction() as conn:
                retained = conn.execute("SELECT * FROM app.harness_previews").fetchone()
            assert retained["state"] == "unknown"
            owner_after = json.dumps({"identity": retained["identity"], "lease": retained["lease"]},
                                     sort_keys=True)
            assert owner_after == owner_before
            assert session["session_id"] not in restarted._previews
            containers_after = old_broker._docker([
                "ps", "-aq", "--filter", "label=mgar.v4.h11.task=" + session["task_id"]
            ]).stdout.split()
            assert containers_after == containers_before
            assert len(env.captured) == observed_calls
        reconciled = env.client.delete(started["preview_control_url"])
        assert reconciled.status_code == 200, reconciled.text
        assert original_inspect(old_broker, identity)[0] == "missing"
        with env.engine.transaction() as conn:
            closed = conn.execute("SELECT * FROM app.harness_previews").fetchone()
        assert closed["state"] == "closed"
        assert json.dumps({"identity": closed["identity"], "lease": closed["lease"]},
                          sort_keys=True) == owner_before
        fresh = _preview(env, session)
        assert env.client.get(fresh["preview_url"]).status_code == 200
        assert env.client.delete(fresh["preview_control_url"]).status_code == 200
    finally:
        # The old in-memory monitor is deliberately not the restarted owner;
        # its final close is safe only after real inspection has been restored.
        old_service.close()
        if restarted is not None:
            restarted.close()


def test_public_environment_selects_pinned_image_and_rejects_project_source(api_env):
    env = api_env
    env.configure()
    endpoint = f"/api/v3/agent/projects/{env.project_id}/environment"
    response = env.client.post(endpoint, json={"image_lock": str(IMAGE_LOCK)})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    receipt = response.json()["receipt"]
    assert receipt["network"] == "none" and receipt["image_id"].startswith("sha256:")
    assert Path(receipt["image_lock"]).is_file()
    assert env.source not in Path(receipt["image_lock"]).parents
    assert not env.captured
    untrusted = env.source / "image.json"
    untrusted.write_text(json.dumps({"image_id": receipt["image_id"]}))
    rejected = env.client.post(endpoint, json={"image_lock": str(untrusted)})
    assert rejected.status_code == 403, rejected.text
