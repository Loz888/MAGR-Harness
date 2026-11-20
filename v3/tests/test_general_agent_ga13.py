"""Release-candidate surface, upgrade, and recovery integration on PostgreSQL."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
import uvicorn
from pydantic import SecretStr
from test_general_agent_ga01 import spec
from test_general_agent_ga05 import draft, pin, put
from test_general_agent_ga08 import ACCOUNT, OUTBOUND, Connector, approve, lease_for, scoped
from test_general_agent_mixed_executors import publish_replayed_research_data_report
from test_harness_h18 import empty_database as disposable_database
from test_harness_h18 import runtime_settings

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.effects.dispatcher import GeneralActionDispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.office import DraftMail, OfficeExecutor, SendMail
from mgar_v3.general_agent.verification_contracts import ExternalAction
from mgar_v3.harness.client import HarnessClient
from mgar_v3.ops.snapshot import backup, restore
from mgar_v3.settings import ROOT
from mgar_v3.store.migrate import migrate
from mgar_v3.web_access import LoginInput, WebAccounts

empty_database = disposable_database
_TASKS = "/api/v3/general/tasks"
_CREDENTIALS = LoginInput(username="rc-owner", password=SecretStr("ga13-fixture-password"))


@contextmanager
def _loopback(app, listener):
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", ws="none"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 10
        while not server.started and thread.is_alive() and monotonic() < deadline:
            sleep(0.01)
        assert server.started
        yield
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()


def _cli(origin, token, *args, body=None, status=0):
    env = dict(os.environ, MGAR_V3_LOCAL_TOKEN=token)
    env.pop("MGAR_V3_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "mgar_v3.cli", "--api", origin, "harness", "general", *args],
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        timeout=25,
        env=env,
        check=False,
    )
    assert result.returncode == status, result.stderr
    return json.loads(result.stdout if status == 0 else result.stderr)


def test_web_cli_and_sdk_share_task_clarification_revision_and_plan(settings, engine, tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "README.md").write_text("A task across Web, CLI and SDK.\n")
    token = settings.local_token.get_secret_value()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        settings.web_allowed_origins = (origin,)
        app = create_app(settings, engine, run_background=False)
        with (
            _loopback(app, listener),
            httpx.Client(base_url=origin, trust_env=False, headers={"Origin": origin}) as browser,
            HarnessClient(token, api=origin) as sdk,
        ):
            login = browser.post(
                "/api/v3/auth/setup",
                json={
                    "username": _CREDENTIALS.username,
                    "password": _CREDENTIALS.password.get_secret_value(),
                },
            )
            assert login.status_code == 201, login.text
            csrf = login.json()["csrf_token"]
            project_id = sdk.register_project(str(source))["project_id"]
            page = browser.get("/static/general.html")
            assert page.status_code == 200 and 'id="general-task-create"' in page.text
            assert 'href="/general"' in browser.get("/static/agent.html").text
            assert _TASKS in browser.get("/static/general.js").text
            payload = {
                "project_id": project_id,
                "request_id": str(uuid4()),
                "spec": {"goal": "研究这个项目"},
            }
            assert browser.post(_TASKS, json=payload).status_code == 403
            browser.headers["X-CSRF-Token"] = csrf
            created = browser.post(_TASKS, json=payload)
            assert created.status_code == 201, created.text
            waiting = created.json()
            task_id = waiting["task_id"]
            assert waiting["state"] == "waiting"
            assert set(waiting["clarifications"]) == {"domain", "deliverables", "success_criteria"}
            assert sdk.create_general_task(payload) == _cli(origin, token, "submit", body=payload)
            assert sdk.general_task(task_id) == waiting

            revised_body = {
                "expected_revision": waiting["revision"],
                "expected_version": waiting["version"],
                "spec": spec("research").model_dump(mode="json")
                | {"goal": "研究这个项目", "allowed_capabilities": ["research.sources"]},
            }
            ready = _cli(origin, token, "revise", task_id, body=revised_body)
            assert ready["revision"] == 2 and ready["state"] == "ready"
            assert browser.get(f"{_TASKS}/{task_id}").json() == sdk.general_task(task_id) == ready
            again = browser.post(f"{_TASKS}/{task_id}/revisions", json=revised_body)
            assert again.status_code == 409 and again.json()["code"] == "GENERAL_TASK_STALE"

            graph = sdk.plan_general_task(
                task_id,
                {
                    "expected_revision": ready["revision"],
                    "expected_version": ready["version"],
                },
            )
            assert graph["graph"]["task_revision"] == 2
            assert browser.get(f"{_TASKS}/{task_id}/graph").json() == graph
            assert _cli(origin, token, "graph", task_id) == graph
            authoritative = sdk.general_task(task_id)
            assert _cli(origin, token, "task", task_id) == authoritative
            assert authoritative["outcome"] is None


def _issue_cookie(engine):
    accounts = WebAccounts(engine)
    key = accounts.create_owner(_CREDENTIALS)
    return key, accounts.issue_session(accounts.login(_CREDENTIALS, "ga13-fixture"))


def test_h18_backup_forward_upgrade_and_different_database_rollback(
    settings, empty_database, tmp_path
):
    source_dsn, rollback_dsn = empty_database(), empty_database()
    h18 = tmp_path / "h18-migrations"
    h18.mkdir()
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        if migration.name[:4] <= "0057":
            shutil.copyfile(migration, h18 / migration.name)
    assert migrate(source_dsn, h18)[-1] == "0057_harness_tenants.sql"
    source_settings = runtime_settings(settings, source_dsn, tmp_path / "h18-runtime")
    artifacts = ArtifactStore(source_settings.artifact_root)
    source = Engine(source_settings, artifacts)
    try:
        account, cookie = _issue_cookie(source)
        payload = b"pre-upgrade content-addressed artifact"
        sha = artifacts.put(payload)
        with source.transaction() as conn:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash, byte_count) VALUES(%s,%s)",
                (sha, len(payload)),
            )
    finally:
        source.close()
    bundle = tmp_path / "before-upgrade"
    backup(source_settings, bundle)

    assert migrate(source_dsn) == sorted(
        path.name for path in (ROOT / "migrations").glob("*.sql") if path.name[:4] > "0057"
    )
    assert migrate(source_dsn) == []
    upgraded = Engine(source_settings, artifacts)
    try:
        assert WebAccounts(upgraded).session(cookie).user_id == account
        assert upgraded.artifacts.read(sha) == payload
    finally:
        upgraded.close()

    runtime = tmp_path / "rollback-runtime"
    restore(bundle, rollback_dsn, runtime)
    rollback = runtime_settings(settings, rollback_dsn, runtime)
    old = Engine(rollback, ArtifactStore(rollback.artifact_root))
    try:
        assert migrate(rollback_dsn, h18) == []
        with old.transaction() as conn:
            last = conn.execute("SELECT max(name) AS name FROM app.schema_migrations").fetchone()
        assert last["name"] == "0057_harness_tenants.sql"
        assert WebAccounts(old).session(cookie).user_id == account
        assert old.artifacts.read(sha) == payload
    finally:
        old.close()


def test_restore_keeps_verified_cross_domain_outcome_and_reconciles_office_once(
    settings,
    empty_database,
    tmp_path,
):
    provider = Connector(timeout=True)
    first = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        account, cookie = _issue_cookie(first)
        mixed = publish_replayed_research_data_report(first)
        office = OfficeExecutor(first, provider)
        assert office.connector.account == ACCOUNT
        mail = DraftMail(
            subject="Fixture handoff", body="One audited action", to=("fixture@example.test",)
        )
        sha = office.draft(scoped(("draft", OUTBOUND)), OUTBOUND, mail)
        lease = lease_for(first, "send", OUTBOUND)
        action = office.queue(
            scoped(("send", OUTBOUND)),
            lease,
            SendMail(resource=OUTBOUND, draft_sha256=sha),
            idempotency_key="ga13-recovery-test-send",
        )
        key = action.intent.action_id
        dispatcher = GeneralActionDispatcher(first, office)
        assert dispatcher.tick() is None and not provider.writes
        approved = approve(first, action, lease)
        assert approved.status == "ready" and dispatcher.tick() == key
        unknown = first.general_actions.get(key)
        assert unknown.status == "unknown" and unknown.receipt is None and len(provider.writes) == 1
        original_graph = first.general_graphs.get(mixed.task_id)
        artifacts = {
            artifact.sha256: first.artifacts.read(artifact.sha256)
            for artifact in mixed.outcome.deliverables
        }
    finally:
        first.close()
    bundle = tmp_path / "ga13-snapshot"
    backup(settings, bundle)
    dsn, runtime = empty_database(), tmp_path / "ga13-recovered"
    restore(bundle, dsn, runtime)
    assert migrate(dsn) == []
    restored = runtime_settings(settings, dsn, runtime)
    recovered = Engine(restored, ArtifactStore(restored.artifact_root))
    try:
        assert WebAccounts(recovered).session(cookie).user_id == account
        assert recovered.general_tasks.get(mixed.task_id) == mixed
        assert recovered.general_graphs.get(mixed.task_id) == original_graph
        assert all(recovered.artifacts.read(key) == value for key, value in artifacts.items())
        assert recovered.general_actions.get(key) == unknown
        dispatch = GeneralActionDispatcher(recovered, OfficeExecutor(recovered, provider))
        assert dispatch.tick() is None and dispatch.reconcile(key)
        confirmed = recovered.general_actions.get(key)
        assert confirmed.intent.identity() == unknown.intent.identity()
        assert confirmed.receipt.external_id == "external-send" and len(provider.writes) == 1
        task = recovered.general_tasks.get(action.intent.task_id)
        task = recovered.general_tasks.transition(
            task.task_id, task.revision, task.version, "verifying"
        )
        verifier, plan = pin(
            recovered,
            task,
            ExternalAction(
                id="provider",
                kind="email",
                output_id="result",
                action_id=key,
                operation="send",
            ),
        )
        proof = put(recovered, confirmed.receipt.model_dump_json().encode())
        review = verifier.verify(plan, draft(proof), verifier="ga13-independent-check")
        assert review.status == "verified"
        assert verifier.publish(review.report_id).state == "completed"
        assert len(provider.writes) == 1
    finally:
        recovered.close()


def test_release_has_public_ga12_admission_assets(tmp_path):
    archive = tmp_path / "candidate.tar.gz"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_harness_release.py"), "--output", str(archive)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    root = "magr-harness/"
    suite = "experiments/general-agent/ga12/"
    with tarfile.open(archive) as bundle:
        with bundle.extractfile(root + "RELEASE.json") as stream:
            manifest = json.load(stream)
        expected = {
            "dev.jsonl",
            "dev.freeze.json",
            "holdout.commitment.json",
            "holdout.first.receipt.json",
            "holdout.recheck.receipt.json",
        }
        assert {name for name in manifest["files"] if name.startswith(suite)} == {
            suite + name for name in expected
        }
        for field in expected:
            path = suite + field
            with bundle.extractfile(root + path) as stream:
                value = stream.read()
            assert value == (ROOT / path).read_bytes()
            assert hashlib.sha256(value).hexdigest() == manifest["files"][path]
