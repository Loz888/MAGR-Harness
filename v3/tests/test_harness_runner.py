"""H15 acceptance over real PostgreSQL, HTTP and local isolated processes."""

from __future__ import annotations

import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.harness.client import HarnessClient
from mgar_v3.harness.runner_contract import RUNNER_API
from mgar_v3.harness.runner_device import server_origin

ORIGIN = "http://127.0.0.1:18080"
DEVICE = RUNNER_API + "/device"
LOCK = Path(__file__).resolve().parents[1] / ".local/r03/sandbox-lock.json"
TREE = "a" * 64
MANIFEST = "b" * 64
PASS_LOG = "test_it (demo.Test) ... ok\n\nRan 1 test in 0.003s\n\nOK\n"


def owner_headers(settings):
    return {"X-MGAR-Token": settings.local_token.get_secret_value()}


def device_headers(token):
    return {"Authorization": "Bearer " + token}


def accept(response, expected=200):
    assert response.status_code == expected, response.text
    return response.json()


def code_for(http, settings):
    return accept(http.post(RUNNER_API + "/pairings", headers=owner_headers(settings)))["code"]


def pair_body(code, name):
    return {
        "code": code,
        "device_id": str(uuid4()),
        "name": name,
        "credential": secrets.token_urlsafe(32),
    }


def attach(http, body, name="Project"):
    spec = {
        "workspace_id": str(uuid4()),
        "name": name,
        "plan_hash": "c" * 64,
        "project_kind": "python",
    }
    authorized = device_headers(body["credential"])
    accept(http.post(DEVICE + "/workspaces", headers=authorized, json=spec))
    return spec


def claim(http, body, ticket):
    return accept(
        http.post(
            DEVICE + "/claim",
            headers=device_headers(body["credential"]),
            json={"ticket": ticket},
        )
    )["claim"]


def job_call(http, device, task_id, action, ticket, epoch, **body):
    return http.post(
        f"{DEVICE}/jobs/{task_id}/{action}",
        headers=device_headers(device["credential"]),
        json={"ticket": ticket, "execution_epoch": epoch, **body},
    )


def owner_transport(http):
    def send(request):
        response = http.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(response.status_code, content=response.content)

    return httpx.MockTransport(send)


@pytest.fixture
def owner_api(settings, engine):
    with (
        TestClient(
            create_app(settings, engine, run_background=False),
            base_url=ORIGIN,
            client=("127.0.0.1", 54322),
        ) as http,
        HarnessClient(
            settings.local_token.get_secret_value(),
            transport=owner_transport(http),
        ) as owner,
    ):
        yield http, owner, settings, engine


def test_pairing_auth_expiry_and_opaque_workspace_scope(owner_api):
    http, owner, settings, engine = owner_api
    assert http.post(RUNNER_API + "/pairings").status_code == 401
    expired = code_for(http, settings)
    with engine.transaction() as conn:
        conn.execute("UPDATE app.harness_runner_pairings SET expires_at=now()-interval '1 second'")
    assert http.post(DEVICE + "/pair", json=pair_body(expired, "Expired")).status_code == 403

    one = pair_body(code_for(http, settings), "Notebook")
    paired = accept(http.post(DEVICE + "/pair", json=one))
    assert paired == accept(http.post(DEVICE + "/pair", json=one))
    assert one["credential"] not in json.dumps(paired)
    assert http.post(DEVICE + "/pair", json=pair_body(one["code"], "Other")).status_code == 409

    two = pair_body(code_for(http, settings), "Desktop")
    accept(http.post(DEVICE + "/pair", json=two))
    ws = attach(http, one)
    forged = http.post(
        DEVICE + "/workspaces",
        headers=device_headers(two["credential"]),
        json=ws,
    )
    assert forged.status_code == 409
    private_path = "/home/fixture-owner/private-dir"
    altered = http.post(
        DEVICE + "/workspaces",
        headers=device_headers(one["credential"]),
        json={**ws, "path": private_path},
    )
    assert altered.status_code == 422 and private_path not in altered.text
    assert (
        http.post(
            "/api/v3/harness/projects",
            headers=device_headers(one["credential"]),
            json={"path": private_path},
        ).status_code
        == 401
    )
    assert (
        http.post(DEVICE + "/workspaces", headers=owner_headers(settings), json=ws).status_code
        == 401
    )
    assert (
        http.get(RUNNER_API + "/jobs", headers=device_headers(one["credential"])).status_code == 401
    )

    assert {row["device_id"] for row in owner.runner_devices()} == {
        one["device_id"],
        two["device_id"],
    }
    assert len(owner.runner_workspaces()) == 1
    with engine.transaction() as conn:
        credentials = conn.execute(
            "SELECT credential_hash FROM app.harness_runner_devices",
        ).fetchall()
        assert all(len(row["credential_hash"]) == 64 for row in credentials)
        assert one["credential"] not in str(credentials)
        assert one["code"] not in str(
            conn.execute(
                "SELECT code_hash FROM app.harness_runner_pairings",
            ).fetchall()
        )


def test_browser_pairing_controls_use_existing_login_and_csrf(owner_api):
    http, _, _, _ = owner_api
    page = http.get("/")
    assert page.status_code == 200 and 'id="product-runner-open"' in page.text
    auth = accept(
        http.post(
            "/api/v3/auth/setup",
            headers={"Origin": ORIGIN},
            json={"username": "owner-h15", "password": "isolated-browser-password"},
        ),
        201,
    )
    assert http.post(RUNNER_API + "/pairings", headers={"Origin": ORIGIN}).status_code == 403
    headers = {"Origin": ORIGIN, "X-CSRF-Token": auth["csrf_token"]}
    issued = http.post(RUNNER_API + "/pairings", headers=headers)
    assert issued.headers["Cache-Control"] == "no-store"
    assert len(accept(issued)["code"]) == 43


def test_parallel_claim_duplicate_and_pre_dispatch_reconnect(owner_api):
    http, owner, settings, engine = owner_api
    primary = pair_body(code_for(http, settings), "Primary")
    secondary = pair_body(code_for(http, settings), "Secondary")
    for peer in (primary, secondary):
        accept(http.post(DEVICE + "/pair", json=peer))
    workspace = attach(http, primary)
    client_id = str(uuid4())
    job = owner.queue_runner_job(workspace["workspace_id"], client_id=client_id)
    assert owner.queue_runner_job(workspace["workspace_id"], client_id=client_id) == job
    assert claim(http, secondary, secrets.token_urlsafe(32)) is None

    tickets = [secrets.token_urlsafe(32) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda ticket: claim(http, primary, ticket), tickets))
    ((index, issued),) = [(i, result) for i, result in enumerate(claims) if result is not None]
    original = tickets[index]
    assert issued["run_id"] == job["run_id"] and issued["phase"] == "claimed"
    replay = claim(http, primary, original)
    assert replay["execution_epoch"] == issued["execution_epoch"]
    for credential in (primary, secondary):
        probe = job_call(
            http,
            credential,
            job["task_id"],
            "start",
            original,
            issued["execution_epoch"] + int(credential is primary),
            tree_hash=TREE,
            manifest_hash=MANIFEST,
        )
        assert probe.status_code == 409
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.harness_runner_claims SET expires_at=now()-interval '1 second'",
        )
        conn.execute("UPDATE app.harness_runner_devices SET last_seen_at=now()-interval '1 hour'")
    assert owner.runner_jobs()[0]["status"] == "paused"
    assert claim(http, primary, original) is None
    new_ticket = secrets.token_urlsafe(32)
    recovered = claim(http, primary, new_ticket)
    assert recovered["task_id"] == job["task_id"] and recovered["run_id"] == job["run_id"]
    assert recovered["execution_epoch"] > issued["execution_epoch"]
    assert (
        job_call(
            http,
            primary,
            job["task_id"],
            "heartbeat",
            original,
            issued["execution_epoch"],
        ).status_code
        == 409
    )

    request = {"tree_hash": TREE, "manifest_hash": MANIFEST}
    accept(
        job_call(
            http,
            primary,
            job["task_id"],
            "start",
            new_ticket,
            recovered["execution_epoch"],
            **request,
        )
    )
    accept(
        job_call(
            http,
            primary,
            job["task_id"],
            "start",
            new_ticket,
            recovered["execution_epoch"],
            **request,
        )
    )
    matching = {"tree_hash": TREE, "state": "exited", "exit_code": 0, "output": PASS_LOG}
    wrong = {**matching, "tree_hash": "d" * 64}
    assert (
        job_call(
            http,
            primary,
            job["task_id"],
            "finish",
            new_ticket,
            recovered["execution_epoch"],
            **wrong,
        ).status_code
        == 409
    )
    stored = accept(
        job_call(
            http,
            primary,
            job["task_id"],
            "finish",
            new_ticket,
            recovered["execution_epoch"],
            **matching,
        )
    )
    assert stored["status"] == "completed"
    assert (
        accept(
            job_call(
                http,
                primary,
                job["task_id"],
                "finish",
                new_ticket,
                recovered["execution_epoch"],
                **matching,
            )
        )
        == stored
    )
    assert (
        job_call(
            http,
            primary,
            job["task_id"],
            "finish",
            new_ticket,
            recovered["execution_epoch"],
            **{**matching, "output": "another receipt"},
        ).status_code
        == 409
    )
    assert owner.task(job["task_id"])["tree_after"] == TREE
    page = owner.event_page(job["task_id"])
    assert page["terminal"] and [item["kind"] for item in page["events"]][-1] == "runner_result"
    kinds = [event["kind"] for event in owner.runner_events()]
    assert {
        "claim_busy",
        "claim_replayed",
        "claim_released",
        "device_offline",
        "device_reconnected",
    } <= set(kinds)
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


@pytest.mark.parametrize("result", ["empty", "truncated", "rejected"])
def test_without_complete_test_evidence_runner_cannot_pass(owner_api, result):
    http, owner, settings, _ = owner_api
    device = pair_body(code_for(http, settings), "Notebook")
    accept(http.post(DEVICE + "/pair", json=device))
    workspace = attach(http, device)
    job = owner.queue_runner_job(workspace["workspace_id"], client_id=str(uuid4()))
    ticket = secrets.token_urlsafe(32)
    epoch = claim(http, device, ticket)["execution_epoch"]
    if result == "rejected":
        report = {"state": "rejected", "failure": "RUNNER_PLAN_CHANGED"}
    else:
        accept(
            job_call(
                http,
                device,
                job["task_id"],
                "start",
                ticket,
                epoch,
                tree_hash=TREE,
                manifest_hash=MANIFEST,
            )
        )
        report = {
            "state": "exited",
            "exit_code": 0,
            "tree_hash": TREE,
            "truncated": result == "truncated",
            "output": PASS_LOG if result == "truncated" else "",
        }
    stored = accept(job_call(http, device, job["task_id"], "finish", ticket, epoch, **report))
    assert stored["status"] == "failed"
    task = owner.task(job["task_id"])
    if result == "rejected":
        assert task["reason"] == "RUNNER_PLAN_CHANGED"
    else:
        assert task["tests"]["status"] == "unverified"


def test_server_recovery_never_requeues_a_dispatched_job(settings):
    app = create_app(settings, run_background=False)
    operator = settings.local_token.get_secret_value()
    with (
        TestClient(app, base_url=ORIGIN) as http,
        HarnessClient(
            operator,
            transport=owner_transport(http),
        ) as sdk,
    ):
        devices = [pair_body(code_for(http, settings), f"device {index}") for index in range(2)]
        ids = []
        epochs = []
        tickets = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
        for index, device in enumerate(devices):
            accept(http.post(DEVICE + "/pair", json=device))
            spec = attach(http, device)
            job = sdk.queue_runner_job(spec["workspace_id"], client_id=str(uuid4()))
            ids.append(job["task_id"])
            grant = claim(http, device, tickets[index])
            epochs.append(grant["execution_epoch"])
            if index == 1:
                accept(
                    job_call(
                        http,
                        device,
                        ids[index],
                        "start",
                        tickets[index],
                        epochs[index],
                        tree_hash=TREE,
                        manifest_hash=MANIFEST,
                    )
                )
    app = create_app(settings, run_background=False)
    with (
        TestClient(app, base_url=ORIGIN) as http,
        HarnessClient(
            operator,
            transport=owner_transport(http),
        ) as sdk,
    ):
        queued, unknown = [sdk.task(task_id) for task_id in ids]
        assert queued["status"] == "paused" and unknown["status"] == "unknown"
        assert unknown["reason"] == "RUNNER_RESULT_UNKNOWN"
        assert claim(http, devices[1], secrets.token_urlsafe(32)) is None
        assert (
            http.post(
                f"/api/v3/runs/{unknown['run_id']}/resume", headers=owner_headers(settings)
            ).status_code
            == 409
        )
        new = claim(http, devices[0], secrets.token_urlsafe(32))
        assert new["run_id"] == queued["run_id"] and new["execution_epoch"] > epochs[0]
        assert (
            job_call(
                http,
                devices[1],
                ids[1],
                "finish",
                tickets[1],
                epochs[1],
                tree_hash=TREE,
                state="exited",
                exit_code=0,
                output=PASS_LOG,
            ).status_code
            == 409
        )


@contextmanager
def live_server(settings, engine, *, tls=None):
    app = create_app(settings, engine, run_background=True)
    with socket.create_server(("127.0.0.1", 0)) as listener:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_level="error",
                ws="none",
                ssl_certfile=str(tls[0]) if tls else None,
                ssl_keyfile=str(tls[1]) if tls else None,
            ),
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            wait_until(lambda: server.started)
            yield f"{'https' if tls else 'http'}://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def wait_until(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("Expected condition was not observed before the deadline")


def runner_argv(state, *args):
    return [sys.executable, "-m", "mgar_v3.cli", "runner", "--state", str(state), *args]


def device_cli(state, *args, code=None, timeout=35):
    # A device can run without the server's PostgreSQL or operator token.
    env = {key: value for key, value in os.environ.items() if not key.startswith("MGAR_V3_")}
    return subprocess.run(
        runner_argv(state, *args),
        input=code,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sample_project(root, *, slow=False):
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "runner-probe"\n')
    (root / ".env").write_text("PROJECT_SECRET=not-mounted\n")
    (root / "test_probe.py").write_text(
        "import os\nimport time\nimport unittest\nfrom pathlib import Path\n"
        "class TestProbe(unittest.TestCase):\n"
        "    def test_isolation(self):\n"
        '        self.assertFalse(Path("/var/run/docker.sock").exists())\n'
        '        protected = Path("/workspace/.env")\n'
        "        try:\n"
        "            secret = protected.read_bytes()\n"
        "        except (FileNotFoundError, PermissionError):\n"
        '            secret = b""\n'
        '        self.assertEqual(secret, b"")\n'
        '        self.assertFalse(any("MGAR_V3_" in key for key in os.environ))\n'
        + ("        time.sleep(65)\n" if slow else ""),
    )


def enroll(url, owner, state, root, name):
    pairing = owner.issue_runner_pairing()
    paired = device_cli(state, "pair", "--server", url, "--name", name, code=pairing["code"] + "\n")
    assert paired.returncode == 0, paired.stderr
    assert "credential" not in paired.stdout
    assert (state / "device.json").stat().st_mode & 0o077 == 0
    registered = device_cli(state, "attach", str(root), "--name", name, "--image-lock", str(LOCK))
    assert registered.returncode == 0, registered.stderr
    assert str(root) not in registered.stdout
    return json.loads(registered.stdout), json.loads(paired.stdout)


@pytest.mark.parametrize("language", ["python", "go"])
def test_cli_runner_uses_real_docker_and_shared_harness_receipt(
    settings, engine, tmp_path, language,
):
    project, state = tmp_path / "developer-only-project", tmp_path / "device-state"
    if language == "go":
        project.mkdir()
        (project / ".env").write_text("PROJECT_SECRET=not-mounted\n")
        (project / "go.mod").write_text("module fixture.test/runner\n\ngo 1.22\n")
        (project / "probe_test.go").write_text(
            'package fixture\nimport "testing"\n'
            'func TestProbe(t *testing.T) { if 2+3 != 5 { t.Fatal("incorrect sum") } }\n',
        )
    else:
        sample_project(project)
    token = settings.local_token.get_secret_value()
    assert LOCK.is_file()
    with live_server(settings, engine) as url, HarnessClient(token, api=url) as owner:
        spec, identity = enroll(url, owner, state, project, "Local workstation")
        alias = device_cli(
            state,
            "attach",
            str(project),
            "--name",
            "Second binding",
            "--image-lock",
            str(LOCK),
        )
        assert alias.returncode == 0, alias.stderr
        assert json.loads(alias.stdout)["workspace_id"] != spec["workspace_id"]
        job = owner.queue_runner_job(spec["workspace_id"], client_id=str(uuid4()))
        assert job["status"] == "queued" and str(project) not in json.dumps(job)
        assert job["execution_site"]["device_id"] == identity["device_id"]
        executed = device_cli(state, "work", "--once", timeout=150)
        assert executed.returncode == 0, executed.stderr
        assert json.loads(executed.stdout)["claimed"] is True
        result = owner.task(job["task_id"])
        assert result["status"] == "completed", result
        assert result["tests"] == {"status": "passed", "run": 1, "failed": 0, "skipped": 0}
        assert result["execution_site"]["name"] == "Local workstation"
        assert "PROJECT_SECRET" not in json.dumps(result)
        assert str(project) not in json.dumps(result)
        assert (project / ".env").read_text() == "PROJECT_SECRET=not-mounted\n"
        assert not (project / "__pycache__").exists()
        repeated = device_cli(state, "work", "--once")
        assert repeated.returncode == 0 and not json.loads(repeated.stdout)["claimed"]
        assert [event["kind"] for event in owner.runner_events()].count("job_started") == 1


def test_real_tls_runner_rejects_untrusted_peer_and_accepts_explicit_ca(settings, engine, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Runner evaluation")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate = tmp_path / "runner-ca.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = tmp_path / "runner-key.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ssl_context = ssl.create_default_context(cafile=str(certificate))
    with (
        live_server(settings, engine, tls=(certificate, private)) as url,
        httpx.Client(
            base_url=url,
            trust_env=False,
            verify=ssl_context,
        ) as owner,
    ):
        issued = code_for(owner, settings)
        untrusted = device_cli(
            tmp_path / "untrusted",
            "pair",
            "--server",
            url,
            "--name",
            "Untrusted",
            code=issued + "\n",
        )
        assert untrusted.returncode == 1
        assert "RUNNER_SERVER_UNREACHABLE" in untrusted.stderr
        state = tmp_path / "trusted"
        trusted = device_cli(
            state,
            "pair",
            "--server",
            url,
            "--name",
            "TLS device",
            "--ca-file",
            str(certificate),
            code=issued + "\n",
        )
        assert trusted.returncode == 0, trusted.stderr
        idle = device_cli(state, "work", "--once")
        assert idle.returncode == 0 and not json.loads(idle.stdout)["claimed"]


def owned_containers(task_id, *, running=False):
    processes = subprocess.run(
        [
            "docker",
            "ps",
            "-q" if running else "-aq",
            "--filter",
            "label=mgar.v4.h11.task=" + task_id,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return processes.stdout.splitlines()


@pytest.mark.parametrize("action", ["kill", "revoke"])
def test_lost_runner_stops_the_exact_container_without_replaying(
    settings, engine, tmp_path, action
):
    project, state = tmp_path / "slow-project", tmp_path / "device-state"
    sample_project(project, slow=True)
    with (
        live_server(settings, engine) as url,
        HarnessClient(
            settings.local_token.get_secret_value(),
            api=url,
        ) as owner,
    ):
        spec, device = enroll(url, owner, state, project, "Laptop")
        job = owner.queue_runner_job(spec["workspace_id"], client_id=str(uuid4()))
        task = job["task_id"]
        with subprocess.Popen(
            runner_argv(state, "work", "--once"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as worker:
            try:
                (container,) = wait_until(lambda: owned_containers(task, running=True))
                inspection = json.loads(subprocess.check_output(["docker", "inspect", container]))[
                    0
                ]
                assert inspection["HostConfig"]["ReadonlyRootfs"]
                assert inspection["HostConfig"]["NetworkMode"] == "none"
                assert not any(
                    mount["Destination"].endswith("docker.sock") for mount in inspection["Mounts"]
                )
                if action == "kill":
                    worker.kill()
                else:
                    assert owner.revoke_runner(device["device_id"])["revoked"] is True
                worker.communicate(timeout=12)
                wait_until(lambda: not owned_containers(task), timeout=12)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
        expected = "unknown" if action == "kill" else "cancelled"
        current = wait_until(
            lambda: row if (row := owner.task(task))["status"] == expected else None
        )
        assert current["execution_epoch"] > 1
        assert not current.get("tests")
        assert owner.event_page(task)["terminal"]
        if action == "kill":
            retry = device_cli(state, "work", "--once")
            assert retry.returncode == 0, retry.stderr
            assert not json.loads(retry.stdout)["claimed"]
            assert [event["kind"] for event in owner.runner_events()].count("claim_fenced") == 1
        else:
            retry = device_cli(state, "work", "--once")
            assert retry.returncode == 1 and "RUNNER_DEVICE_AUTH" in retry.stderr
            assert not owner.runner_workspaces()
        assert [event["kind"] for event in owner.runner_events()].count("job_started") == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://server.example",
        "https://person@server.example",
        "https://server.example/?code=secret",
        "https://server.example/api",
    ],
)
def test_protocol_requires_secure_origin_outside_loopback(url):
    with pytest.raises(ValueError, match="origin"):
        server_origin(url)
