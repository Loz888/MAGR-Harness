"""Exercise GA02 planning and recovery through real PostgreSQL and Harness intake."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_general_agent_ga01 import ADMIN, ORIGIN, TASKS, create, expect, spec
from test_harness_h16 import PASSWORD, member

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent import graph_store
from mgar_v3.general_agent.capabilities import CapabilityAccess, CapabilityDescriptor, Resources
from mgar_v3.general_agent.contracts import Deliverable, SourceRef, SuccessCriterion
from mgar_v3.general_agent.graph import (
    Dependency,
    GraphNode,
    NodeInput,
    ReplanTrigger,
    TaskGraph,
    deterministic_plan,
    model_plan,
)
from mgar_v3.general_agent.graph_store import NodeReceipt

SOURCE = "research.sources"
REPORT = "research.report"
DATA = "data.analyze"


def allowed(*capabilities):
    return spec().model_copy(update={"allowed_capabilities": capabilities or (SOURCE,)})


def cross_domain():
    return allowed(SOURCE, REPORT, DATA).model_copy(
        update={
            "source_refs": spec().source_refs
            + (SourceRef(kind="dataset", reference="observations", version="v1"),),
            "deliverables": spec().deliverables
            + (Deliverable(id="metrics", kind="table", description="Observed metrics"),),
            "success_criteria": (
                SuccessCriterion(
                    id="check",
                    description="Check report and metrics",
                    deliverable_ids=("result", "metrics"),
                ),
            ),
        }
    )


def capability(key=SOURCE, **changes):
    domain = key.split(".")[0]
    return CapabilityDescriptor.model_validate(
        {
            "id": key,
            "version": "v1",
            "domain": domain,
            "description": "Produce checked evidence.",
            "provider": "native",
            "provider_id": key,
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "permissions": (key + ".use",),
            "idempotent": True,
            "upper_bound": Resources(tool_calls=1),
            "timeout_seconds": 30,
            "evidence_contract": "general-node-receipt-v1",
            **changes,
        }
    )


def grant(task, *descriptors, **changes):
    return CapabilityAccess.model_validate(
        {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "task_revision": task.revision,
            "capability_pins": tuple(item.identity() for item in descriptors),
            "permissions": tuple(
                permission for item in descriptors for permission in item.permissions
            ),
            "resources": Resources(tool_calls=5),
            **changes,
        }
    )


def node(key, *, outputs=(), **changes):
    return GraphNode.model_validate(
        {
            "id": key,
            "domain": "research",
            "goal": "Check " + key,
            "outputs": outputs,
            "capabilities": (SOURCE,),
            "budget": Resources(tool_calls=1),
            "completion_condition": "Record the bound result.",
            **changes,
        }
    )


def graph(*nodes, **changes):
    return TaskGraph(task_revision=1, nodes=nodes, **changes)


def start(engine, plan, task_spec=None, *descriptors):
    task = create(engine.general_tasks, task_spec or allowed())
    state = engine.general_graphs.install(task.task_id, 1, 1, plan)
    return task, state, grant(task, *descriptors)


def claim(store, state, key, access, *, context_tokens=2048):
    return store.claim(
        state.task_id,
        state.graph.task_revision,
        state.graph_revision,
        key,
        access,
        context_tokens=context_tokens,
    )


def receipt(engine, operation, *, status="succeeded", outputs=None, **changes):
    data = operation.model_dump(
        include={
            "operation_id",
            "task_id",
            "task_revision",
            "node_id",
            "node_sha256",
            "descriptor_sha256",
        }
    )
    value = NodeReceipt.model_validate(
        {**data, "status": status, "outputs": outputs or {}, **changes}
    )
    return engine.artifacts.put(value.model_dump_json().encode())


def test_cross_domain_graph_hydrates_inputs_and_still_requires_outcome_verification(engine):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry

    value = cross_domain()
    plan = deterministic_plan(value, 1)
    assert [(item.id, item.domain, item.capabilities) for item in plan.nodes] == [
        ("research", "research", (SOURCE,)),
        ("analysis", "data", (DATA,)),
        ("report", "research", (REPORT,)),
    ]
    descriptors = (
        capability(),
        capability(
            DATA,
            input_schema={
                "type": "object",
                "properties": {"upstream": {"type": "string"}},
                "required": ["upstream"],
                "additionalProperties": False,
            },
        ),
        capability(
            REPORT,
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
        ),
    )
    store = engine.general_graphs
    store.registry = CapabilityRegistry(descriptors)
    task, state, access = start(engine, plan, value, *descriptors)
    assert claim(store, state, "analysis", access) is None
    first = claim(store, state, "research", access)
    assert store.model_tools(first, access, context_tokens=2048) == [descriptors[0].model_schema()]
    assert (
        store.model_tools(first, access.model_copy(update={"permissions": ()}), context_tokens=2048)
        == []
    )
    result = receipt(engine, first, outputs={"source_evidence": "frozen source reference"})
    assert store.settle(result) == store.settle(result)
    second = claim(store, state, "analysis", access)
    assert second.inputs == {"upstream": "frozen source reference"}
    with pytest.raises(DomainError) as forged:
        store.settle(receipt(engine, second, operation_id=uuid4(), outputs={"metrics": "table"}))
    assert forged.value.code == "GENERAL_GRAPH_RECEIPT_STALE"
    store.settle(receipt(engine, second, outputs={"metrics": "table"}))
    last = claim(store, state, "report", access)
    assert last.inputs == {"upstream": "table"}
    with pytest.raises(DomainError) as invalid:
        store.settle(receipt(engine, last, outputs={"result": 42}))
    assert invalid.value.code == "GENERAL_GRAPH_RECEIPT_INVALID"
    final = store.settle(receipt(engine, last, outputs={"result": "artifact reference"}))
    assert final.spent == Resources(tool_calls=3)
    assert engine.general_tasks.get(task.task_id).state == "verifying"
    assert [
        item["payload"]["decision"]["chosen_id"]
        for item in reversed(store.events(task.task_id))
        if item["kind"] == "node_claimed"
    ] == [SOURCE, DATA, REPORT]
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


def test_planner_port_validates_graph_scope_and_bounds_before_accepting():
    value = cross_domain()
    expected = deterministic_plan(value, 1)

    def propose(payload):
        assert payload["task_revision"] == 1 and payload["task"]["goal"] == value.goal
        assert "tools" not in payload and payload["response_schema"]["title"] == "TaskGraph"
        return expected.model_dump_json()

    assert model_plan(value, 1, propose) == expected
    for raw in ('{"nodes":[]}', "x" * 2048):
        with pytest.raises(DomainError) as exc:
            model_plan(value, 1, lambda _, raw=raw: raw, max_bytes=1024)
        assert exc.value.code == "GENERAL_GRAPH_MODEL_INVALID"
    wrong = graph(node("work", outputs=("result", "metrics"), capabilities=("office.send",)))
    with pytest.raises(DomainError) as exc:
        model_plan(value, 1, lambda _: wrong.model_dump_json())
    assert exc.value.code == "GENERAL_GRAPH_SCOPE"


def test_model_proposal_outside_engine_lock_cannot_install_after_revision_change(engine):
    task = create(engine.general_tasks, allowed())
    store = engine.general_graphs

    def propose(_payload):
        with ThreadPoolExecutor(max_workers=1) as other:
            revised = other.submit(
                engine.general_tasks.revise, task.task_id, 1, 1, allowed()
            ).result(timeout=5)
        assert revised.revision == 2
        return deterministic_plan(task.spec, 1).model_dump_json()

    with pytest.raises(DomainError) as stale:
        store.plan_with_model(task.task_id, 1, 1, propose)
    assert stale.value.code == "GENERAL_TASK_STALE"
    graph = deterministic_plan(task.spec, 2)
    current = store.plan_with_model(task.task_id, 2, 1, lambda _: graph.model_dump_json())
    assert current.graph == graph


@pytest.mark.parametrize("bad", ["cycle", "missing_dependency", "missing_output", "missing_group"])
def test_graph_rejects_invalid_dependencies_before_pg(bad):
    if bad == "cycle":
        nodes = (
            node("a", dependencies=(Dependency(node_id="b"),)),
            node("b", dependencies=(Dependency(node_id="a"),)),
        )
    elif bad == "missing_dependency":
        nodes = (node("a", dependencies=(Dependency(node_id="other"),)),)
    elif bad == "missing_output":
        nodes = (node("a"), node("b", dependencies=(Dependency(node_id="a", output_id="missing"),)))
    else:
        nodes = (node("a", parallel_group="unbounded"),)
    with pytest.raises(ValidationError):
        graph(*nodes)


def test_wait_conditional_branch_join_and_approval(engine):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry

    d = capability()
    store = engine.general_graphs
    store.registry = CapabilityRegistry([d])
    selection = node("select", outputs=("include",))
    wait = node(
        "pause",
        kind="wait_event",
        capabilities=(),
        budget=Resources(),
        signal_key="sources_ready",
        dependencies=(Dependency(node_id="select"),),
    )
    branch = node(
        "optional",
        outputs=("extra",),
        dependencies=(Dependency(node_id="select", output_id="include", equals=True),),
    )
    approval = node(
        "review",
        kind="approval",
        capabilities=(),
        budget=Resources(),
        signal_key="reviewed",
        dependencies=(Dependency(node_id="pause"),),
    )
    final = node(
        "work",
        outputs=("result",),
        dependencies=(
            Dependency(node_id="review"),
            Dependency(node_id="optional", accept_skipped=True),
        ),
        inputs=(NodeInput(name="extra", node_id="optional", output_id="extra"),),
    )
    task, state, access = start(engine, graph(selection, wait, branch, approval, final), None, d)
    assert claim(store, state, "work", access) is None
    with pytest.raises(DomainError) as early:
        store.signal(
            task.task_id, 1, 1, "pause", kind="wait_event", reference="sources_ready", accepted=True
        )
    assert early.value.code == "GENERAL_GRAPH_SIGNAL_NOT_READY"
    a = claim(store, state, "select", access)
    selection_result = store.settle(receipt(engine, a, outputs={"include": False}))
    assert selection_result.progress["optional"].state == "skipped"
    with pytest.raises(DomainError) as mismatched:
        store.signal(
            task.task_id, 1, 1, "pause", kind="wait_event", reference="wrong", accepted=True
        )
    assert mismatched.value.code == "GENERAL_GRAPH_SIGNAL_MISMATCH"
    wait_result = store.signal(
        task.task_id, 1, 1, "pause", kind="wait_event", reference="sources_ready", accepted=True
    )
    assert (
        store.signal(
            task.task_id, 1, 1, "pause", kind="wait_event", reference="sources_ready", accepted=True
        )
        == wait_result
    )
    store.signal(task.task_id, 1, 1, "review", kind="approval", reference="reviewed", accepted=True)
    finish = claim(store, state, "work", access)
    assert finish.inputs == {"extra": None}
    store.settle(receipt(engine, finish, outputs={"result": "checked"}))
    assert engine.general_tasks.get(task.task_id).state == "verifying"


def test_known_failure_allows_bounded_retry_and_blocks_descendants_on_exhaustion(engine):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry

    d = capability()
    store = engine.general_graphs
    store.registry = CapabilityRegistry([d])
    first = node("source", max_attempts=2)
    final = node("final", outputs=("result",), dependencies=(Dependency(node_id="source"),))
    task, state, access = start(engine, graph(final, first), None, d)
    a = claim(store, state, "source", access)
    assert store.settle(receipt(engine, a, status="failed")).progress["source"].state == "pending"
    b = claim(store, state, "source", access)
    assert b.operation_id != a.operation_id
    with pytest.raises(DomainError) as stale:
        store.settle(receipt(engine, a))
    assert stale.value.code == "GENERAL_GRAPH_RECEIPT_STALE"
    stop = store.settle(receipt(engine, b, status="failed"))
    assert stop.progress["final"].state == "blocked"
    assert stop.spent.tool_calls == 2
    assert engine.general_tasks.get(task.task_id).state == "blocked"


def test_time_budget_and_cancellation_fence_new_work_and_late_results(engine):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry
    from mgar_v3.general_agent.contracts import TaskBudget

    d = capability()
    store = engine.general_graphs
    store.registry = CapabilityRegistry([d])
    plan = graph(node("work", outputs=("result",)))
    timed = allowed().model_copy(update={"budget": TaskBudget(max_seconds=0)})
    expired, state, access = start(engine, plan, timed, d)
    with pytest.raises(DomainError) as timed_out:
        claim(store, state, "work", access)
    assert timed_out.value.code == "GENERAL_GRAPH_DEADLINE"
    assert store.get(expired.task_id) == state

    task, state, access = start(engine, plan, allowed(), d)
    op = claim(store, state, "work", access)
    current = engine.general_tasks.get(task.task_id)
    engine.general_tasks.transition(task.task_id, 1, current.version, "cancelled")
    with pytest.raises(DomainError) as new_work:
        claim(store, state, "work", access)
    assert new_work.value.code == "GENERAL_GRAPH_STATE"
    with pytest.raises(DomainError) as old_result:
        store.settle(receipt(engine, op, outputs={"result": "stale"}))
    assert old_result.value.code == "GENERAL_GRAPH_RECEIPT_STALE"


def test_parallel_claims_reserve_once_and_obey_group_and_cumulative_budget(engine):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry

    d = capability()
    store = engine.general_graphs
    store.registry = CapabilityRegistry([d])
    plan = graph(
        node("a", parallel_group="p"),
        node("b", parallel_group="p"),
        node("c", outputs=("result",)),
        max_parallel=2,
        group_limits={"p": 1},
    )
    task, state, access = start(engine, plan, None, d)
    access = access.model_copy(update={"resources": Resources(tool_calls=2)})
    barrier = Barrier(2, timeout=5)

    def compete():
        barrier.wait()
        return claim(store, state, "a", access)

    with ThreadPoolExecutor(max_workers=2) as pool:
        submissions = (pool.submit(compete), pool.submit(compete))
        results = [submission.result() for submission in submissions]
    (a,) = [result for result in results if result is not None]
    assert claim(store, state, "b", access) is None
    c = claim(store, state, "c", access)
    assert c is not None
    store.settle(receipt(engine, a))
    assert claim(store, state, "b", access) is None
    assert store.get(task.task_id).spent.tool_calls == 2
    assert (
        store.events(task.task_id)[0]["payload"]["decision"]["checks"][0]["reason"]
        == "resource_budget"
    )


def test_replan_preserves_other_receipts_and_fault_rolls_back_reservation(engine, monkeypatch):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry

    d = capability()
    store = engine.general_graphs
    store.registry = CapabilityRegistry([d])
    a, b = node("a"), node("b")
    end = node("end", outputs=("result",), dependencies=(Dependency(node_id="b"),))
    task, state, access = start(engine, graph(a, b, end), None, d)

    def failed_event(*_args):
        raise RuntimeError("injected event write failure")

    with monkeypatch.context() as fault:
        fault.setattr(store, "_event", failed_event)
        with pytest.raises(RuntimeError, match="injected"):
            claim(store, state, "a", access)
    assert store.get(task.task_id) == state
    assert engine.general_tasks.get(task.task_id).state == "ready"

    aa = claim(store, state, "a", access)
    got = store.settle(receipt(engine, aa))
    for kind in ("new_evidence", "failure", "user_instruction", "external_event"):
        changed = graph(a, node("b", goal="Respond to " + kind), end)
        trigger = ReplanTrigger(kind=kind, reference=kind)
        new = store.replan(task.task_id, 1, got.graph_revision, got.version, changed, trigger)
        assert new.progress["a"] == got.progress["a"]
        assert new.graph_revision == got.graph_revision + 1 and new.spent == got.spent
        task_view = engine.general_tasks.get(task.task_id)
        assert task_view.plan_version == new.plan_version > got.plan_version
        assert task_view.plan.steps == tuple(item.goal for item in changed.nodes)
        with pytest.raises(DomainError) as late:
            store.replan(task.task_id, 1, got.graph_revision, got.version, changed, trigger)
        assert late.value.code == "GENERAL_GRAPH_STALE"
        assert store.events(task.task_id)[0]["payload"]["changed"] == ["b", "end"]
        got = new
    active = claim(store, got, "b", access)
    current = engine.general_tasks.get(task.task_id)
    engine.general_tasks.revise(task.task_id, 1, current.version, allowed())
    with pytest.raises(DomainError) as old_receipt:
        store.settle(receipt(engine, active))
    assert old_receipt.value.code == "GENERAL_GRAPH_TASK_STALE"


def test_controller_restart_expires_unknown_without_repeating_non_idempotent_claim(
    settings, monkeypatch
):
    from mgar_v3.general_agent.capabilities import CapabilityRegistry

    d = capability(idempotent=False)
    registry = CapabilityRegistry([d])
    artifacts = ArtifactStore(settings.artifact_root)
    first = Engine(settings, artifacts, general_capabilities=registry)
    try:
        task, state, access = start(
            first, graph(node("work", outputs=("result",), max_attempts=2)), None, d
        )
        op = claim(first.general_graphs, state, "work", access)
    finally:
        first.close()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return op.expires_at + timedelta(seconds=1)

    second = Engine(settings, artifacts, general_capabilities=registry)
    try:
        store = second.general_graphs
        assert store.get(task.task_id).progress["work"].claim == op
        monkeypatch.setattr(graph_store, "datetime", Clock)
        assert claim(store, state, "work", access) is None
        pending = store.get(task.task_id)
        assert pending.progress["work"].state == "unknown" and pending.spent.tool_calls == 1
        with pytest.raises(DomainError) as fenced:
            store.replan(
                task.task_id,
                1,
                1,
                pending.version,
                graph(node("rename", outputs=("result",))),
                ReplanTrigger(kind="failure", reference="missing receipt"),
            )
        assert fenced.value.code == "GENERAL_GRAPH_FENCED_EFFECT"
        sha = receipt(second, op, outputs={"result": "reconciled"})
        assert store.settle(sha) == store.settle(sha)
        assert second.general_tasks.get(task.task_id).state == "verifying"
    finally:
        second.close()


def test_api_plans_and_reuses_existing_inspect_coding_loop(settings, engine, tmp_path):
    settings.local_token = SecretStr(ADMIN["X-MGAR-Token"])
    settings.harness_root = tmp_path / "runtime" / "harness"
    app = create_app(settings, engine, run_background=False)
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("Project fixture for inspection\n")
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as owner:
        project = expect(
            owner.post("/api/v3/harness/projects", headers=ADMIN, json={"path": str(source)}), 201
        )["project_id"]
        value = spec("coding").model_copy(
            update={
                "goal": "Inspect README.md",
                "coding_policy": "inspect",
                "allowed_capabilities": ("coding.inspect",),
            }
        )
        created = expect(
            owner.post(
                TASKS,
                headers=ADMIN,
                json={
                    "project_id": project,
                    "request_id": str(uuid4()),
                    "spec": value.model_dump(mode="json"),
                },
            ),
            201,
        )
        url = TASKS + "/" + created["task_id"] + "/graph"
        assert owner.get(url).status_code == 401
        installed = expect(
            owner.post(
                url,
                headers=ADMIN,
                json={
                    "expected_revision": 1,
                    "expected_version": 1,
                },
            ),
            201,
        )
        assert expect(owner.get(url, headers=ADMIN)) == installed
        response = owner.get(url + "/events", headers=ADMIN)
        assert response.headers["cache-control"] == "no-store"
        assert expect(response)[0]["kind"] == "graph_installed"
        amendment = {
            "expected_revision": 1,
            "expected_graph_revision": 1,
            "expected_graph_version": 1,
            "proposal": installed["graph"],
            "trigger": {"kind": "user_instruction", "reference": "Confirm README"},
        }
        assert expect(owner.patch(url, headers=ADMIN, json=amendment))["graph_revision"] == 2
        assert owner.patch(url, headers=ADMIN, json=amendment).status_code == 409
        task = engine.general_tasks.get(UUID(created["task_id"]))
        store = engine.general_graphs
        descriptor = store.registry.definition("coding.inspect")
        access = grant(task, descriptor)
        selection = claim(store, store.get(task.task_id), "work", access)
        request = store.coding_request(selection, access, inspect_path="README.md")
        assert (
            request.task_kind == "inspect"
            and request.mode == "deterministic"
            and not request.approved
        )
        result = app.state.harness.create_task(request)
        assert result["status"] == "completed", result["reason"]
        assert (source / "README.md").read_text() == "Project fixture for inspection\n"


def test_current_membership_fences_graph_api_and_new_provider_dispatch(settings, engine, tmp_path):
    settings.local_token = SecretStr(ADMIN["X-MGAR-Token"])
    settings.harness_root = tmp_path / "runtime" / "harness"
    app = create_app(settings, engine, run_background=False)
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("Project fixture")
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as owner:
        project = expect(
            owner.post("/api/v3/harness/projects", headers=ADMIN, json={"path": str(source)}), 201
        )["project_id"]
        endpoint = f"/api/v3/agent/projects/{project}"
        expect(owner.put(endpoint + "/quota", headers=ADMIN, json={}))
        user = expect(
            owner.post(
                "/api/v3/agent/accounts",
                headers=ADMIN,
                json={"username": "alice", "password": PASSWORD},
            ),
            201,
        )["user_id"]
        membership = endpoint + "/members/" + user
        expect(owner.put(membership, headers=ADMIN, json={"role": "contributor"}))
        with closing(TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50301))) as browser:
            headers = member(browser, "alice")
            value = spec("coding").model_copy(
                update={"coding_policy": "inspect", "allowed_capabilities": ("coding.inspect",)}
            )
            body = {
                "project_id": project,
                "request_id": str(uuid4()),
                "spec": value.model_dump(mode="json"),
            }
            created = expect(browser.post(TASKS, headers=headers, json=body), 201)
            url = TASKS + "/" + created["task_id"] + "/graph"
            expect(
                browser.post(
                    url, headers=headers, json={"expected_revision": 1, "expected_version": 1}
                ),
                201,
            )
            task = engine.general_tasks.get(UUID(created["task_id"]))
            store = engine.general_graphs
            access = grant(task, store.registry.definition("coding.inspect"))
            state = store.get(task.task_id)
            expect(owner.delete(membership, headers=ADMIN), 204)
            assert browser.get(url + "/events", headers=headers).status_code == 404
            with pytest.raises(DomainError) as revoked:
                claim(store, state, "work", access)
            assert revoked.value.code == "HARNESS_MEMBER_REVOKED"
            assert store.get(task.task_id) == state
