"""Versioned GA01 contracts, real PG revision fencing and authenticated intake."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_harness_h16 import ADMIN, ORIGIN, PASSWORD, expect, member

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.general_agent.coding import from_coding_request, to_coding_request
from mgar_v3.general_agent.contracts import (
    CriterionEvidence,
    Deliverable,
    GeneralTaskSpec,
    OutcomeArtifact,
    OutcomeContract,
    OutcomeVerificationReceipt,
    SourceRef,
    SuccessCriterion,
    TaskBudget,
    TaskPlan,
)
from mgar_v3.harness.service import CodingTaskRequest

TASKS = "/api/v3/general/tasks"
SCENARIOS = {
    "coding": ("Fix the login crash", "patch", "repository"),
    "research": ("Compare two public SDK guides", "report", "web"),
    "data": ("Compute monthly sales totals", "table", "dataset"),
    "office": ("Draft a reply from the meeting notes", "draft", "document"),
}


def spec(domain="research"):
    goal, artifact, source = SCENARIOS[domain]
    return GeneralTaskSpec(
        domain=domain,
        goal=goal,
        context=("Use the supplied versioned input.",),
        constraints=("Do not publish the result.",),
        deliverables=(
            Deliverable(id="result", kind=artifact, description=f"Produce the {artifact}"),
        ),
        success_criteria=(
            SuccessCriterion(
                id="check",
                description="Verify against the source version",
                deliverable_ids=("result",),
            ),
        ),
        risk_level="low",
        budget=TaskBudget(max_cost_micros=0, max_model_calls=0, max_tool_calls=5),
        deadline=datetime.now(UTC) + timedelta(days=1),
        approval_policy="deny_external_writes",
        source_refs=(SourceRef(kind=source, reference="input-v1", version="v1"),),
        coding_policy="bugfix" if domain == "coding" else None,
    )


def create(store, value=None, *, draft=False):
    return store.create("project", None, uuid4(), value or spec(), draft=draft)


@pytest.mark.parametrize("domain", tuple(SCENARIOS))
def test_four_domains_share_a_versioned_task_contract(domain):
    value = spec(domain)
    assert not value.clarifications()
    assert GeneralTaskSpec.model_validate_json(value.model_dump_json()) == value
    assert GeneralTaskSpec.model_json_schema()["properties"]["schema_version"]["const"] == (
        "general-task-v1"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "general-task-v2"},
        {"granted_capabilities": ["shell.exec"]},
        {"goal": " "},
        {"goal": "find\x00this"},
        {"coding_policy": "inspect"},
        {"budget": {"max_model_calls": True}},
        {"budget": {"max_tool_calls": -1}},
        {"deadline": "2026-11-19T01:02:03"},
        {"allowed_capabilities": ["read", "read"]},
        {"approval_policy": "auto_approve"},
        {"deliverables": [{"id": "other", "kind": "report", "description": "Output"}]},
    ],
)
def test_schema_rejects_unbound_values_and_implicit_authority(changes):
    with pytest.raises(ValidationError):
        GeneralTaskSpec.model_validate({**spec().model_dump(), **changes})


@pytest.mark.parametrize("kind", ["inspect", "bugfix", "feature", "refactor", "run_app"])
def test_five_coding_policy_adapters_preserve_intent_without_granting_execution(kind):
    original = CodingTaskRequest(
        project_id="work",
        objective="Work on the requested code",
        task_kind=kind,
        approved=True,
        mode="live",
        test_command="private-policy-command",
    )
    general = from_coding_request(original)
    assert general.coding_policy == kind
    assert not general.clarifications()
    assert general.allowed_capabilities == ()
    mapped = to_coding_request(general, project_id=original.project_id)
    assert mapped == CodingTaskRequest(
        project_id=original.project_id,
        objective=original.objective,
        task_kind=kind,
    )


def test_clarification_needs_an_explicit_revision_before_a_plan(engine):
    store = engine.general_tasks
    draft = create(store, GeneralTaskSpec(goal="帮我优化一下"))
    assert draft.state == "waiting" and draft.reason == "clarification_required"
    assert draft.clarifications == ("domain", "deliverables", "success_criteria")
    assert draft.spec.constraints == () and draft.spec.risk_level is None
    with pytest.raises(DomainError, match="任务尚不能接受计划"):
        store.save_plan(draft.task_id, 1, 1, TaskPlan(task_revision=1, steps=("Guess",)))
    complete = spec().model_copy(update={"open_questions": ("Which date range applies?",)})
    next_task = store.revise(draft.task_id, 1, 1, complete)
    assert next_task.state == "waiting" and next_task.clarifications == ("open_questions",)
    fixed = store.revise(next_task.task_id, 2, 1, spec())
    assert (fixed.revision, fixed.state, fixed.clarifications) == (3, "ready", ())
    assert store.get(draft.task_id, revision=1) == draft


def test_deadline_blocks_admission_even_when_all_scope_is_present(engine):
    expired = spec().model_copy(update={"deadline": datetime.now(UTC) - timedelta(seconds=1)})
    task = create(engine.general_tasks, expired)
    assert (task.state, task.reason) == ("blocked", "deadline_expired")
    with pytest.raises(DomainError) as still_expired:
        engine.general_tasks.transition(task.task_id, task.revision, task.version, "ready")
    assert still_expired.value.code == "GENERAL_TASK_NOT_READY"


def test_revision_and_plan_update_are_fenced_over_concurrent_writers(engine):
    store = engine.general_tasks
    task = create(store)
    barrier = Barrier(2, timeout=5)

    def revise(goal):
        barrier.wait()
        try:
            return store.revise(task.task_id, 1, 1, spec().model_copy(update={"goal": goal}))
        except DomainError as exc:
            assert exc.code == "GENERAL_TASK_STALE"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(revise, ("Compare SDK A", "Compare SDK B")))
    accepted = [r for r in results if r is not None]
    assert len(accepted) == 1
    current = accepted[0]
    assert current.revision == 2 and store.get(task.task_id) == current
    old_plan = TaskPlan(task_revision=1, steps=("Read previous scope",))
    with pytest.raises(DomainError) as stale:
        store.save_plan(task.task_id, 1, 1, old_plan)
    assert stale.value.code == "GENERAL_TASK_STALE"
    with pytest.raises(DomainError) as mismatch:
        store.save_plan(task.task_id, 2, 1, old_plan)
    assert mismatch.value.code == "GENERAL_PLAN_STALE"
    plan = TaskPlan(task_revision=2, steps=("Read current source",))
    written = store.save_plan(task.task_id, 2, 1, plan)
    assert written.plan == plan and written.plan_version == written.version == 2
    with pytest.raises(DomainError) as duplicate:
        store.save_plan(task.task_id, 2, 1, plan)
    assert duplicate.value.code == "GENERAL_TASK_STALE"
    assert store.get(task.task_id, revision=1) == task


def test_state_changes_and_revision_survive_restart(settings):
    from mgar_v3.evidence.artifacts import ArtifactStore

    artifacts = ArtifactStore(settings.artifact_root)
    first = Engine(settings, artifacts)
    try:
        store = first.general_tasks
        draft = create(store, draft=True)
        assert draft.state == "draft"
        ready = store.transition(draft.task_id, 1, 1, "ready")
        with pytest.raises(DomainError) as missing:
            store.transition(ready.task_id, 1, 2, "running")
        assert missing.value.code == "GENERAL_PLAN_REQUIRED"
        plan = TaskPlan(task_revision=1, steps=("Record source receipt",))
        assert store.save_plan(ready.task_id, 1, 2, plan).version == 3
        assert store.transition(ready.task_id, 1, 3, "running").version == 4
        blocked = store.transition(ready.task_id, 1, 4, "blocked", reason="source_unavailable")
    finally:
        first.close()
    second = Engine(settings, artifacts)
    try:
        assert second.general_tasks.get(draft.task_id) == blocked
        cancelled = second.general_tasks.transition(draft.task_id, 1, 5, "cancelled")
        assert cancelled.state == "cancelled" and cancelled.plan == plan
        with pytest.raises(DomainError) as terminal:
            second.general_tasks.transition(draft.task_id, 1, 6, "ready")
        assert terminal.value.code == "GENERAL_TASK_TRANSITION_DENIED"
    finally:
        second.close()


def verifying_task(engine):
    store = engine.general_tasks
    task = create(store)
    plan = TaskPlan(task_revision=1, steps=("Read source", "Save checked report"))
    store.save_plan(task.task_id, 1, 1, plan)
    store.transition(task.task_id, 1, 2, "running")
    return store.transition(task.task_id, 1, 3, "verifying")


def outcome_for(engine, current, *, changes=None):
    criterion = current.spec.success_criteria[0]
    artifact = OutcomeArtifact(
        deliverable_id="result", sha256=engine.artifacts.put(b"checked report")
    )
    receipt = OutcomeVerificationReceipt(
        task_id=current.task_id,
        task_revision=current.revision,
        plan_version=current.plan_version,
        verification_version=current.version,
        status="verified",
        criterion_id=criterion.id,
        criterion_sha256=digest(criterion.model_dump(mode="json")),
        deliverables=(artifact,),
        evidence_sha256=(engine.artifacts.put(b"source check"),),
    ).model_copy(update=changes or {})
    receipt_hash = engine.artifacts.put(receipt.model_dump_json().encode())
    return OutcomeContract(
        task_revision=current.revision,
        plan_version=current.plan_version,
        deliverables=(artifact,),
        evidence=(CriterionEvidence(criterion_id=criterion.id, receipt_sha256=receipt_hash),),
        summary="Verified the result against the pinned source.",
    )


def test_completion_requires_verified_artifact_and_criterion_binding(engine):
    store = engine.general_tasks
    current = verifying_task(engine)
    with pytest.raises(DomainError) as transition:
        store.transition(current.task_id, 1, current.version, "completed")
    assert transition.value.code == "GENERAL_TASK_TRANSITION_DENIED"
    for change in (
        {"task_id": uuid4()},
        {"task_revision": 2},
        {"plan_version": 1},
        {"verification_version": current.version - 1},
        {"criterion_sha256": "a" * 64},
        {"evidence_sha256": ("b" * 64,)},
        {"status": "unverified"},
    ):
        with pytest.raises(DomainError) as unverified:
            store.finish(
                current.task_id, 1, current.version, outcome_for(engine, current, changes=change)
            )
        assert unverified.value.code == "GENERAL_OUTCOME_UNVERIFIED"
    valid = outcome_for(engine, current)
    missing = valid.model_copy(
        update={"deliverables": (OutcomeArtifact(deliverable_id="result", sha256="c" * 64),)}
    )
    with pytest.raises(DomainError) as mismatch:
        store.finish(current.task_id, 1, current.version, missing)
    assert mismatch.value.code == "GENERAL_OUTCOME_UNVERIFIED"
    assert store.get(current.task_id) == current
    completed = store.finish(current.task_id, 1, current.version, valid)
    assert completed.state == "completed" and completed.outcome == valid
    assert completed.plan == current.plan and store.get(current.task_id) == completed


def test_outcome_schema_rejects_missing_receipts_or_unknown_version(engine):
    value = outcome_for(engine, verifying_task(engine)).model_dump(mode="json")
    for patch in ({"schema_version": "general-outcome-v2"}, {"evidence": []}):
        with pytest.raises(ValidationError):
            OutcomeContract.model_validate({**value, **patch})


def test_goal_revision_while_verifying_prevents_a_late_success(engine, monkeypatch):
    current = verifying_task(engine)
    store = engine.general_tasks
    outcome = outcome_for(engine, current)
    read = engine.artifacts.read

    def race(content_hash):
        monkeypatch.setattr(engine.artifacts, "read", read)
        store.revise(current.task_id, current.revision, current.version, spec("office"))
        return read(content_hash)

    monkeypatch.setattr(engine.artifacts, "read", race)
    with pytest.raises(DomainError) as stale:
        store.finish(current.task_id, current.revision, current.version, outcome)
    assert stale.value.code == "GENERAL_TASK_STALE"
    active = store.get(current.task_id)
    assert active.revision == 2 and active.outcome is None and active.plan is None
    assert store.get(current.task_id, revision=1) == current


def test_general_api_uses_existing_auth_and_keeps_execution_fields_server_owned(
    settings,
    engine,
    tmp_path,
):
    settings.local_token = SecretStr(ADMIN["X-MGAR-Token"])
    settings.harness_root = tmp_path / "runtime" / "harness"
    app = create_app(settings, engine, run_background=False)
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("Fixture workspace")
    with TestClient(
        app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=("127.0.0.1", 50200)
    ) as owner:
        project = expect(
            owner.post("/api/v3/harness/projects", headers=ADMIN, json={"path": str(source)}), 201
        )["project_id"]
        endpoint = f"/api/v3/agent/projects/{project}"
        expect(owner.put(endpoint + "/quota", headers=ADMIN, json={}))
        users = {}
        for name, role in (("alice", "contributor"), ("bob", "reader")):
            users[name] = expect(
                owner.post(
                    "/api/v3/agent/accounts",
                    headers=ADMIN,
                    json={
                        "username": name,
                        "password": PASSWORD,
                    },
                ),
                201,
            )["user_id"]
            expect(
                owner.put(
                    endpoint + "/members/" + users[name],
                    headers=ADMIN,
                    json={
                        "role": role,
                    },
                )
            )

        payload = {
            "project_id": project,
            "request_id": str(uuid4()),
            "spec": {"goal": "写一封邮件"},
        }
        assert owner.post(TASKS, json=payload).status_code == 401
        assert owner.get(TASKS).status_code == 401
        with closing(TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50201))) as browser:
            headers = member(browser, "alice")
            assert (
                browser.post(
                    TASKS,
                    headers=headers,
                    json={
                        **payload,
                        "spec": {"goal": "g", "plan": {"steps": ["forged"]}},
                    },
                ).status_code
                == 422
            )
            response = browser.post(TASKS, headers=headers, json=payload)
            task = expect(response, 201)
            assert response.headers["cache-control"] == "no-store"
            assert task["state"] == "waiting" and task["reason"] == "clarification_required"
            history = browser.get(TASKS, headers=headers)
            assert history.headers["cache-control"] == "no-store"
            [summary] = expect(history)
            assert summary["task_id"] == task["task_id"]
            assert summary["goal"] == payload["spec"]["goal"]
            assert set(summary) == {"task_id", "project_id", "goal", "state", "updated_at"}
            assert "membership_id" not in task
            assert expect(browser.post(TASKS, headers=headers, json=payload), 201) == task
            assert (
                browser.post(
                    TASKS,
                    headers=headers,
                    json={
                        **payload,
                        "spec": {"goal": "different"},
                    },
                ).status_code
                == 409
            )
            url = TASKS + "/" + task["task_id"]
            amendment = {
                "expected_revision": 1,
                "expected_version": 1,
                "spec": spec("office").model_dump(mode="json"),
            }
            revised = expect(browser.post(url + "/revisions", headers=headers, json=amendment), 201)
            assert revised["state"] == "ready" and revised["revision"] == 2
            assert expect(browser.get(url + "/revisions/1", headers=headers)) == task
            assert (
                browser.post(url + "/revisions", headers=headers, json=amendment).status_code == 409
            )

            other = member(browser, "bob")
            assert browser.post(TASKS, headers=other, json=payload).status_code == 403
            assert browser.get(url, headers=other).status_code == 404
            assert expect(browser.get(TASKS, headers=other)) == []
            headers = member(browser, "alice")
            expect(owner.delete(endpoint + "/members/" + users["alice"], headers=ADMIN), 204)
            assert expect(browser.get(TASKS, headers=headers)) == []
            revoked = browser.get(url, headers=headers)
            absent = browser.get(TASKS + "/" + str(uuid4()), headers=headers)
            assert revoked.status_code == absent.status_code == 404
            revoked_error, absent_error = revoked.json(), absent.json()
            assert revoked_error.pop("request_id") and absent_error.pop("request_id")
            assert revoked_error == absent_error

        assert expect(owner.get(url, headers=ADMIN)) == revised
        assert expect(owner.get(TASKS, headers=ADMIN))[0]["task_id"] == task["task_id"]
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


def test_projectless_intake_is_private_and_project_link_preserves_request_id(
    settings, engine, tmp_path
):
    settings.local_token = SecretStr(ADMIN["X-MGAR-Token"])
    app = create_app(settings, engine, run_background=False)
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "README.md").write_text("Fixture workspace")
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50300)) as owner:
        users = {
            name: expect(
                owner.post(
                    "/api/v3/agent/accounts",
                    headers=ADMIN,
                    json={
                        "username": name,
                        "password": PASSWORD,
                    },
                ),
                201,
            )["user_id"]
            for name in ("alice", "bob")
        }
        payload = {"request_id": str(uuid4()), "spec": {"goal": "写一封感谢信"}}
        assert owner.post(TASKS, headers=ADMIN, json=payload).status_code == 401
        with closing(TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50301))) as browser:
            headers = member(browser, "alice")
            assert expect(browser.get("/api/v3/harness/projects")) == []
            response = browser.post(TASKS, headers=headers, json=payload)
            task = expect(response, 201)
            assert task["project_id"] is None and task["state"] == "waiting"
            assert "owner_user_id" not in task and "membership_id" not in task
            task_url = TASKS + "/" + task["task_id"]
            assert expect(browser.get(TASKS))[0]["task_id"] == task["task_id"]
            assert expect(owner.get(TASKS, headers=ADMIN)) == []
            assert owner.get(task_url, headers=ADMIN).status_code == 404
            assert expect(browser.post(TASKS, headers=headers, json=payload), 201) == task

            bob = member(browser, "bob")
            assert browser.get(task_url).status_code == 404
            assert expect(browser.get(TASKS)) == []
            other = expect(browser.post(TASKS, headers=bob, json=payload), 201)
            assert other["task_id"] != task["task_id"]
            assert (
                browser.post(
                    task_url + "/project",
                    headers=bob,
                    json={
                        "project_id": "forged",
                        "expected_revision": 1,
                        "expected_version": 1,
                    },
                ).status_code
                == 404
            )

            headers = member(browser, "alice")
            project = expect(
                owner.post(
                    "/api/v3/harness/projects",
                    headers=ADMIN,
                    json={
                        "path": str(source),
                    },
                ),
                201,
            )["project_id"]
            endpoint = f"/api/v3/agent/projects/{project}"
            expect(owner.put(endpoint + "/quota", headers=ADMIN, json={}))
            attachment = {"project_id": project, "expected_revision": 1, "expected_version": 1}
            assert (
                browser.post(task_url + "/project", headers=headers, json=attachment).status_code
                == 404
            )
            expect(
                owner.put(
                    endpoint + "/members/" + users["alice"],
                    headers=ADMIN,
                    json={"role": "contributor"},
                )
            )
            linked = expect(browser.post(task_url + "/project", headers=headers, json=attachment))
            assert linked["revision"] == 2 and linked["task_id"] == task["task_id"]
            assert linked["project_id"] == project and linked["spec"] == task["spec"]
            assert expect(browser.post(TASKS, headers=headers, json=payload), 201) == linked
            assert (
                browser.post(
                    task_url + "/project",
                    headers=headers,
                    json=attachment
                    | {
                        "expected_revision": 2,
                    },
                ).status_code
                == 409
            )
            expect(owner.delete(endpoint + "/members/" + users["alice"], headers=ADMIN), 204)
            assert browser.get(task_url).status_code == 404
            assert browser.post(TASKS, headers=headers, json=payload).status_code == 404


def test_personal_task_plans_require_project_only_for_project_tools(settings, engine):
    app = create_app(settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50302)) as browser:
        auth = expect(
            browser.post(
                "/api/v3/auth/setup",
                headers={"Origin": ORIGIN},
                json={
                    "username": "owner",
                    "password": PASSWORD,
                },
            ),
            201,
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": auth["csrf_token"]}
        versions = {"expected_revision": 1, "expected_version": 1}
        for domain, capability in (("data", "data.analyze"), ("coding", "coding.execute.inspect")):
            value = spec(domain).model_copy(update={"allowed_capabilities": (capability,)})
            if domain == "coding":
                value = value.model_copy(update={"coding_policy": "inspect"})
            task = expect(
                browser.post(
                    TASKS,
                    headers=headers,
                    json={
                        "project_id": None,
                        "request_id": str(uuid4()),
                        "spec": value.model_dump(mode="json"),
                    },
                ),
                201,
            )
            url = TASKS + "/" + task["task_id"]
            result = browser.post(url + "/graph", headers=headers, json=versions)
            if domain == "coding":
                assert result.status_code == 409
                assert result.json()["code"] == "GENERAL_TASK_PROJECT_REQUIRED"
            else:
                assert expect(result, 201)["graph"]["nodes"][0]["domain"] == "data"
            assert (
                browser.get(url + "/knowledge/sources", params={"expected_revision": 1}).status_code
                == 404
            )
