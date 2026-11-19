"""Release, diagnostics and recovery on a disposable PostgreSQL installation."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from test_harness_h16 import client, member, private_http, setup_tenants, submit, wait_for
from test_harness_h16 import tenants as shared_tenants
from test_harness_product_service import KEY_A, prepare_bugfix
from test_harness_product_service import product_env as shared_product_env

from mgar_v3.api import create_app
from mgar_v3.cli import main
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.ops import snapshot
from mgar_v3.ops.doctor import diagnose, migration_status
from mgar_v3.ops.metrics import export_metrics
from mgar_v3.ops.snapshot import backup, restore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate
from mgar_v3.web_access import (
    AnalysisSettingsInput,
    BrowserSession,
    LoginInput,
    WebAccounts,
    WebAnalysisSettings,
    password_hash,
)

product_env = shared_product_env
tenants = shared_tenants


@pytest.fixture
def empty_database(database):
    created = []

    def create():
        name = "mgar_h18_test_" + uuid4().hex
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return make_conninfo(database, dbname=name)

    yield create
    with psycopg.connect(database, autocommit=True) as conn:
        for name in created:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def runtime_settings(base, dsn, root):
    return base.model_copy(update={
        "database_url": SecretStr(dsn),
        "artifact_root": root / "artifacts",
        "harness_root": root / "harness-v4",
    })


def test_source_release_hashes_are_reproducible_and_omit_local_data(tmp_path):
    archives = [tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"]
    for output in archives:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/build_harness_release.py"),
             "--output", str(output)],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        metadata = json.loads(result.stdout)
        assert metadata["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert archives[0].read_bytes() == archives[1].read_bytes()
    with tarfile.open(archives[0]) as bundle:
        manifest = json.load(bundle.extractfile("magr-harness/RELEASE.json"))
        expected_files = {
            "uv.lock", "pyproject.toml", "deploy/compose.yaml", "scripts/bootstrap.py",
        }
        assert expected_files <= set(manifest["files"])
        assert not {".env", ".local", "evidence"} & {
            Path(name).parts[0] for name in manifest["files"]
        }
        for name, checksum in manifest["files"].items():
            content = bundle.extractfile("magr-harness/" + name).read()
            assert hashlib.sha256(content).hexdigest() == checksum


def test_doctor_distinguishes_credentials_sandbox_network_and_configuration(
    settings, tmp_path, monkeypatch, capsys,
):
    settings.harness_image_lock = ROOT / ".local/r03/sandbox-lock.json"
    result, status = diagnose(settings)
    assert status == 0
    assert all(result[k] == "PASS" for k in ("postgres", "redis", "sandbox"))

    def blocked(url, **kwargs):
        assert kwargs["follow_redirects"] is False
        return httpx.Response(403)

    monkeypatch.setattr(httpx, "head", blocked)
    password = "fixture-incorrect-do-not-print"
    wrong = settings.model_copy(update={
        "database_url": SecretStr(
            make_conninfo(settings.database_url.get_secret_value(), password=password)
        ),
        "harness_image_lock": tmp_path / "missing-sandbox-lock",
    })
    result, status = diagnose(wrong, network_url="https://example.test/healthz")
    assert status == 2 and password not in json.dumps(result)
    assert {k: result["diagnostics"][k]["code"] for k in ("postgres", "sandbox", "network")} == {
        "postgres": "PG_AUTHENTICATION_FAILED",
        "sandbox": "SANDBOX_LOCK_MISSING",
        "network": "NETWORK_ACCESS_DENIED",
    }
    monkeypatch.delenv("MGAR_V3_DATABASE_URL")
    monkeypatch.delenv("MGAR_V3_LOCAL_TOKEN")
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    assert main(["doctor"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"]["configuration"]["fields"] == [
        "MGAR_V3_DATABASE_URL", "MGAR_V3_LOCAL_TOKEN",
    ]


def test_old_schema_backup_restore_then_forward_upgrade_preserves_real_login_and_key(
    settings, empty_database, tmp_path,
):
    source_dsn, target_dsn = empty_database(), empty_database()
    prior_migrations = tmp_path / "0056-migrations"
    prior_migrations.mkdir()
    for migration in (ROOT / "migrations").glob("*.sql"):
        if migration.name < "0057":
            shutil.copyfile(migration, prior_migrations / migration.name)
    applied = migrate(source_dsn, prior_migrations)
    assert applied[-1].startswith("0056")
    source_settings = runtime_settings(settings, source_dsn, tmp_path / "source")
    engine = Engine(source_settings, ArtifactStore(source_settings.artifact_root))
    credentials = LoginInput(username="owner", password=SecretStr("fixture-password"))
    try:
        accounts = WebAccounts(engine)
        expected_id = uuid4()
        # This is the H15 account row, before 0057 adds the is_owner column.
        with engine.transaction() as pg:
            pg.execute(
                "INSERT INTO app.web_accounts(user_id,username,password_hash) VALUES (%s,%s,%s)",
                (expected_id, credentials.username, password_hash("fixture-password")),
            )
        token = accounts.issue_session(accounts.login(credentials, "fixture"))
        web = WebAnalysisSettings(engine, source_settings)
        web.save(AnalysisSettingsInput(
            deepseek_api_key=SecretStr(KEY_A), expected_revision=0, allow_paid=False,
        ), BrowserSession(user_id=expected_id, username="owner", csrf_token="fixture"))
        with psycopg.connect(source_dsn) as pg:
            assert migration_status(pg, ROOT / "migrations") == {
                "code": "SCHEMA_UPGRADE_REQUIRED", "files": ["0057_harness_tenants.sql"],
            }
        bundle = tmp_path / "before-upgrade"
        backup(source_settings, bundle)
        expected_key_hash = hashlib.sha256(web.key_path.read_bytes()).hexdigest()
    finally:
        engine.close()
    staged = tmp_path / "rollback"
    restore(bundle, target_dsn, staged)
    target_settings = runtime_settings(settings, target_dsn, staged)
    restored = Engine(target_settings, ArtifactStore(target_settings.artifact_root))
    try:
        # Verify the recovery target before changing its schema.
        assert WebAccounts(restored).login(credentials, "fixture") == expected_id
        assert WebAnalysisSettings(restored, target_settings).snapshot()[1]["deepseek_api_key"] == (
            KEY_A
        )
        assert migrate(target_dsn) == ["0057_harness_tenants.sql"]
        assert migrate(target_dsn) == []
        web = WebAnalysisSettings(restored, target_settings)
        assert hashlib.sha256(web.key_path.read_bytes()).hexdigest() == expected_key_hash
        assert WebAccounts(restored).login(credentials, "fixture") == expected_id
        assert WebAccounts(restored).session(token).user_id == expected_id
    finally:
        restored.close()


def test_h18_same_run_cas_survives_restore_and_metrics_keep_only_operational_fields(
    product_env, empty_database, tmp_path,
):
    env = product_env
    session = prepare_bugfix(env)
    task_id = session["task_id"]
    task = env.harness.get_task(task_id)
    row = env.engine.harness.get(task_id)
    run_id = row["run_id"]
    original_cookie = WebAccounts(env.engine).issue_session(env.user.user_id)
    metrics = export_metrics(env.settings, within_hours=1, limit=100, run_id=run_id)
    assert not metrics["truncated"]
    assert {item["run_id"] for item in metrics["operations"]} == {str(run_id)}
    assert sum(item["settled_micros"] or 0 for item in metrics["operations"]) == task["cost_micros"]
    events = {item["event"]: item for item in metrics["events"]}
    for name in ("verification_result", "command_completed", "tool_completed",
                 "harness_provider_completed"):
        assert events[name]["duration_ms"] >= 0 and events[name]["operation_id"]
    raw = json.dumps(metrics)
    assert KEY_A not in raw and task["diff"] not in raw
    assert all(key not in raw for key in ('"intent"', '"stdout"', '"arguments"', '"messages"'))

    bundle = tmp_path / "snapshot"
    first = backup(env.settings, bundle)
    with pytest.raises(DomainError, match="new snapshot"):
        backup(env.settings, bundle)
    staged, new_dsn = tmp_path / "after-recovery", empty_database()
    assert restore(bundle, new_dsn, staged) == first
    recovered_settings = runtime_settings(env.settings, new_dsn, staged)
    recovered = Engine(recovered_settings, ArtifactStore(staged / "artifacts"))
    try:
        saved = recovered.harness.get(task_id)
        assert (saved["run_id"], saved["tree_hash"], saved["result_hash"]) == (
            run_id, row["tree_hash"], row["result_hash"],
        )
        assert recovered.artifacts.read(saved["result_hash"]) == (
            env.engine.artifacts.read(row["result_hash"])
        )
        assert WebAccounts(recovered).session(original_cookie).user_id == env.user.user_id
    finally:
        recovered.close()
    artifact = next((bundle / "artifacts").glob("*/*"))
    artifact.write_bytes(b"corrupted bundle")
    with pytest.raises(DomainError) as error:
        restore(bundle, empty_database(), tmp_path / "reject-bad-backup")
    assert error.value.code == "SNAPSHOT_CHECKSUM_MISMATCH"
    assert not (tmp_path / "reject-bad-backup").exists()


def test_restore_retries_same_target_after_missing_pg_restore(
    product_env, empty_database, tmp_path, monkeypatch, capsys,
):
    env = product_env
    original_cookie = WebAccounts(env.engine).issue_session(env.user.user_id)
    env.save_model()
    content = b"Restore retry fixture artifact"
    content_hash = env.engine.artifacts.put(content)
    with env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.artifacts(content_hash, byte_count) VALUES (%s, %s)",
            (content_hash, len(content)),
        )
    bundle, runtime = tmp_path / "snapshot", tmp_path / "restored"
    expected = backup(env.settings, bundle)
    dsn = empty_database()
    monkeypatch.setenv("H18_RESTORE_TEST_DSN", dsn)
    args = [
        "ops", "restore", str(bundle), "--target-dsn-env", "H18_RESTORE_TEST_DSN",
        "--runtime", str(runtime),
    ]
    without_pg_restore = tmp_path / "missing-pg-tools"
    without_pg_restore.mkdir()

    with monkeypatch.context() as missing:
        missing.setenv("PATH", str(without_pg_restore))
        assert main(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "FAIL", "code": "SNAPSHOT_POSTGRES_CLIENT_MISSING",
    }
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('app')").fetchone()[0] is None
    assert not runtime.exists()
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == expected

    def check_recovered(target_dsn, target_runtime):
        settings = runtime_settings(env.settings, target_dsn, target_runtime)
        assert ArtifactStore(settings.artifact_root).read(content_hash) == content
        backend = create_app(settings, run_background=False)
        socket = target_runtime / "svc.sock"
        with private_http(backend, socket), httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://127.0.0.1:18080",
            cookies={"reposage_session": original_cookie},
        ) as browser:
            session = browser.get("/api/v3/auth/session")
            assert session.is_success and session.json()["authenticated"]
            assert session.json()["username"] == env.user.username
            assert browser.get("/api/v3/agent/sessions").is_success

    check_recovered(dsn, runtime)

    # A later failure after import must also be retryable without another import.
    postponed_dsn, postponed_runtime = empty_database(), tmp_path / "postponed"
    monkeypatch.setenv("H18_RESTORE_TEST_DSN", postponed_dsn)
    followup_args = [*args[:-1], str(postponed_runtime)]

    def deny_publication(*_args):
        raise PermissionError("Injected runtime publication failure")

    with monkeypatch.context() as denied:
        denied.setattr(snapshot, "_publish", deny_publication)
        assert main(followup_args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "FAIL", "code": "SNAPSHOT_RUNTIME_PUBLISH_FAILED",
        "database_restored": True, "retry_same_command": True,
    }
    assert not postponed_runtime.exists()
    with psycopg.connect(postponed_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM app.web_accounts").fetchone()[0] == 1
    with monkeypatch.context() as missing:
        missing.setenv("PATH", str(without_pg_restore))
        assert main(followup_args) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert not tuple(tmp_path.glob(".mgar-restore-*"))
    check_recovered(postponed_dsn, postponed_runtime)


@pytest.mark.parametrize("tenants", [False], indirect=True)
def test_disconnected_event_stream_exports_a_correlatable_request(tenants):
    env = tenants
    setup_tenants(env)
    alice = client(env)
    headers = member(alice, "alice")
    task = submit(alice, headers, env.projects["alpha"], "解释项目", "h18-disconnect")
    task_id = task["task_id"]
    backend = create_app(env.settings, env.engine, run_background=False)
    socket = env.tmp_path / "svc.sock"
    with private_http(backend, socket), httpx.Client(
        transport=httpx.HTTPTransport(uds=str(socket)),
        base_url="http://127.0.0.1:18080", timeout=8,
        headers={"Cookie": f"reposage_session={alice.cookies.get('reposage_session')}"},
    ) as consumer, consumer.stream("GET", f"/api/v3/harness/tasks/{task_id}/events") as stream:
        assert stream.status_code == 200
        request_id = stream.headers["X-MGAR-Request-ID"]
        first = next(stream.iter_lines())
        assert first == ": keepalive" or first.startswith("id: ")

    def observed():
        metrics = export_metrics(env.settings, within_hours=1, limit=20, run_id=None)
        return next((item for item in metrics["events"] if
                     item.get("stream_request_id") == request_id), None)

    assert wait_for(observed, seconds=8)["request_id"] == task_id
    assert env.engine.harness.get(task_id)["status"] == "queued"
