"""Personal memory tenant isolation, versioning and authenticated API lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_general_agent_ga01 import outcome_for, spec
from test_harness_h16 import ADMIN, ORIGIN, PASSWORD, expect, member

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.general_agent.contracts import TaskPlan
from mgar_v3.general_agent.personal_memory import (
    CreatePersonalMemory,
    PersonalMemoryStore,
    RevisePersonalMemory,
)

MEMORY = "/api/v3/general/memory"


def seed(engine):
    alice, bob = uuid4(), uuid4()
    with engine.transaction() as conn:
        for user, name in ((alice, "alice"), (bob, "bob")):
            conn.execute(
                "INSERT INTO app.web_accounts(user_id,username,password_hash,is_owner) "
                "VALUES (%s,%s,'fixture','false')",
                (user, name),
            )
        for project in ("north", "south", "private"):
            conn.execute(
                "INSERT INTO app.harness_managed_projects VALUES (%s,%s,1,4,now())",
                (project, "a" * 64),
            )
        memberships = {}
        for user, project in ((alice, "north"), (alice, "south"), (bob, "north"), (bob, "private")):
            key = memberships[user, project] = uuid4()
            conn.execute(
                "INSERT INTO app.harness_project_memberships "
                "(membership_id,workspace_id,user_id,role) VALUES (%s,%s,%s,'contributor')",
                (key, project, user),
            )
    return alice, bob, memberships


def preference(content="Prefer markdown headings"):
    return CreatePersonalMemory(kind="preference", subject="format", content=content)


def fact(project, subject, content, *, shared=False):
    return CreatePersonalMemory(
        kind="project_fact",
        project_id=project,
        subject=subject,
        content=content,
        recall_policy="explicit_projects" if shared else "scope_only",
    )


def test_quality_recall_matches_subject_or_text_without_sibling_leaks(engine):
    alice, bob, _ = seed(engine)
    store = engine.personal_memory
    personal = store.create(alice, preference())
    local = store.create(alice, fact("north", "database", "Primary storage uses PostgreSQL"))
    share = store.create(
        alice, fact("south", "database", "Secondary PostgreSQL facts", shared=True)
    )
    store.create(alice, fact("south", "private-note", "Private PostgreSQL facts"))
    store.create(bob, fact("north", "database", "Other user's PostgreSQL facts", shared=True))
    store.create(bob, preference("Bob wants XML"))

    assert store.recall(alice) == [personal]
    assert store.recall(alice, project="north") == [local]
    result = store.recall(alice, project="north", source_project="south", query="POSTGRESQL")
    assert {row.memory_id for row in result} == {local.memory_id, share.memory_id}
    assert all(row.trust == "user_confirmed" and row.legacy_kind == "semantic" for row in result)
    assert personal in store.recall(alice, project="north", include_personal=True)
    with pytest.raises(DomainError) as outside:
        store.recall(alice, project="north", source_project="private")
    assert outside.value.status == 404
    with pytest.raises(DomainError):
        store.get(bob, personal.memory_id)
    assert PersonalMemoryStore(engine, "other-tenant").recall(alice) == []
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.harness_project_memberships SET revoked_at=now() "
            "WHERE workspace_id='south' AND user_id=%s",
            (alice,),
        )
    with pytest.raises(DomainError):
        store.recall(alice, project="north", source_project="south")
    store.delete(alice, share.memory_id, 1)


def test_conflicts_are_versioned_and_revoke_then_delete_removes_history(engine):
    alice, _, _ = seed(engine)
    store = engine.personal_memory
    recorded = store.create(alice, preference())
    with pytest.raises(DomainError, match="同名"):
        store.create(alice, preference("Different style"))
    barrier = Barrier(2)

    def change(content):
        barrier.wait(timeout=5)
        try:
            return store.revise(
                alice,
                recorded.memory_id,
                RevisePersonalMemory(expected_version=1, content=content),
            )
        except DomainError as exc:
            assert exc.code == "PERSONAL_MEMORY_STALE"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(change, ("Use tables", "Use bullets")))
    (current,) = [value for value in values if value is not None]
    assert current.version == 2 and current.content != recorded.content
    assert store.get(alice, recorded.memory_id, version=1) == recorded
    revoked = store.revoke(alice, recorded.memory_id, current.version)
    assert revoked.status == "revoked" and revoked.version == 3
    assert store.recall(alice) == []
    store.delete(alice, recorded.memory_id, revoked.version)
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.personal_memory_versions").fetchall()


def test_sensitive_memory_rejected_and_expired_facts_are_excluded(engine):
    alice, _, _ = seed(engine)
    store = engine.personal_memory
    with pytest.raises(DomainError) as secret:
        store.create(alice, preference("password: fixture-do-not-retain"))
    assert secret.value.code == "PERSONAL_MEMORY_SENSITIVE"
    row = store.create(
        alice,
        preference().model_copy(update={"valid_until": datetime.now(UTC) + timedelta(minutes=10)}),
    )
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.personal_memory_versions SET valid_until=now()-interval '1 second' "
            "WHERE memory_id=%s",
            (row.memory_id,),
        )
    assert store.recall(alice) == []
    assert store.get(alice, row.memory_id).version == 1


def test_verified_experience_pins_the_owned_current_completed_outcome(engine):
    alice, bob, memberships = seed(engine)
    tasks = engine.general_tasks
    task = tasks.create("north", memberships[alice, "north"], uuid4(), spec())
    plan = tasks.save_plan(task.task_id, 1, 1, TaskPlan(task_revision=1, steps=("Verify",)))
    running = tasks.transition(task.task_id, 1, plan.version, "running")
    checking = tasks.transition(task.task_id, 1, running.version, "verifying")
    outcome = outcome_for(engine, checking)
    completed = tasks.finish(task.task_id, 1, checking.version, outcome)
    assert completed.state == "completed"
    request = CreatePersonalMemory(
        project_id="north",
        kind="experience",
        subject="outcome",
        content=outcome.summary,
    )
    store = engine.personal_memory
    with pytest.raises(DomainError) as foreign:
        store.create(bob, request, completed_task=task.task_id)
    with pytest.raises(DomainError) as missing:
        store.create(bob, request, completed_task=uuid4())
    assert (foreign.value.code, foreign.value.status) == (missing.value.code, missing.value.status)
    with pytest.raises(DomainError):
        store.create(
            alice,
            request.model_copy(update={"content": "Invented result"}),
            completed_task=task.task_id,
        )
    recalled = store.create(alice, request, completed_task=task.task_id)
    assert recalled.trust == "verified_outcome" and recalled.source.task_revision == 1
    assert recalled.source.task_id == task.task_id and recalled.legacy_kind == "episodic"
    changed = store.revise(
        alice,
        recalled.memory_id,
        RevisePersonalMemory(expected_version=1, content="My new note"),
    )
    assert changed.trust == "user_confirmed" and changed.source.outcome_sha256 is None


def test_browser_memory_requires_account_and_csrf_and_hides_other_users(settings, engine):
    settings.local_token = SecretStr(ADMIN["X-MGAR-Token"])
    app = create_app(settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN) as operator:
        users = {
            name: expect(
                operator.post(
                    "/api/v3/agent/accounts",
                    headers=ADMIN,
                    json={"username": name, "password": PASSWORD},
                ),
                201,
            )["user_id"]
            for name in ("alice", "bob")
        }
        assert operator.get(MEMORY, headers=ADMIN).status_code == 401
        with (
            closing(TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50012))) as alice,
            closing(TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50013))) as bob,
        ):
            headers = member(alice, "alice")
            bob_headers = member(bob, "bob")
            request = preference().model_dump(mode="json")
            assert alice.post(MEMORY, headers={"Origin": ORIGIN}, json=request).status_code == 403
            response = alice.post(MEMORY, headers=headers, json=request)
            assert response.headers["cache-control"] == "no-store"
            record = expect(response, 201)
            assert UUID(record["user_id"]) == UUID(users["alice"])
            path = MEMORY + "/" + record["memory_id"]
            assert bob.get(path, headers=bob_headers).status_code == 404
            assert expect(bob.get(MEMORY, headers=bob_headers)) == []
            assert expect(alice.get(MEMORY, headers=headers, params={"q": "markdown"})) == [record]
            expect(alice.delete(path, headers=headers, params={"expected_version": 1}), 204)
            assert alice.get(path, headers=headers).status_code == 404
