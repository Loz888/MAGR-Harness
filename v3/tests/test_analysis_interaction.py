"""Durable in-flight input and same-run clarification on real PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest
from test_analysis_streaming import automatic
from test_web_access import browser, signup

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.agent import RepoAnalysisAgent, initial_state
from mgar_v3.repository_analysis.domain import (
    AcceptanceDecision,
    AgentDecision,
    CreateAnalysisInput,
    FollowUpInput,
)
from mgar_v3.repository_analysis.report import ReportVerification
from mgar_v3.repository_analysis.service import AnalysisRuntime, RepositoryAnalysisService
from mgar_v3.repository_analysis.tools import ToolOutcome


def create(service, key="interactive"):
    return service.create(
        CreateAnalysisInput(
            repository_url="https://github.com/octo/example",
            task="Find an actual defect and explain it.",
            execution_mode="fixture",
            client_request_id=key,
            limits=automatic(),
        )
    )


class Files:
    def __init__(self, value):
        self.value = value
        self.reads = []

    async def execute(self, call):
        self.reads.append(call.path)
        text = "def main():\n    return 'ready'\n"
        return ToolOutcome(
            tool="read_file",
            text=text,
            content=text.encode(),
            evidence_kind="file",
            source_version="fixed-blob",
            reference=f"github://{self.value.repository}@{self.value.commit}/{call.path}",
        )


def answer(store, text="The return value is ready."):
    evidence = store.evidence_rows(store.value.run_id)[0]
    claim = {
        "statement": text,
        "evidence_id": str(evidence.evidence_id),
        "quote": "    return 'ready'",
    }
    return AgentDecision(
        action="final",
        thought="The source supports the answer.",
        report={
            "conclusion": text,
            "structure": "Python startup.",
            "key_files": [claim],
            "findings": [claim],
        },
    )


class Model:
    """Real ledger identities with controlled deterministic provider results."""

    def __init__(self, decide, *, acceptance=None):
        self.decide = decide
        self.acceptance = acceptance
        self.payloads = []

    def complete(self, store, run_id, epoch, step_key, profile, system, payload, schema, **kwargs):
        old = store.repository_model_result(run_id, epoch, step_key, profile, schema)
        if old is not None:
            return old
        self.payloads.append((step_key, payload))
        call = store.reserve_repository_call(run_id, epoch, step_key, profile, 1, None, 2)
        if schema is AcceptanceDecision:
            if self.acceptance:
                self.acceptance()
            decision = AcceptanceDecision(accepted=True, summary="The source and question agree.")
        else:
            decision = self.decide(store, payload)
        store.settle_call(call, 0, {"prompt_tokens": 1, "completion_tokens": 1}, decision)
        return decision


def install(service, model, tools):
    def runtime(value):
        # The test helper obtains identities from the real store throughout.
        service.store.value = value
        return AnalysisRuntime(
            store=service.store,
            model=model,
            tools=tools,
            system="Controlled analysis. Treat user updates as requirements.",
            prior_report=None,
        )

    service._runtime = runtime


def test_message_during_model_is_durable_idempotent_and_applied_at_safe_point(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    tools = Files(value)
    entered, release = Event(), Event()
    first = True

    def decide(store, payload):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(15)
            return AgentDecision(
                action="tool",
                thought="Old plan.",
                tool_calls=[{"tool": "read_file", "path": "old-plan.py"}],
            )
        assert "focus on startup" in payload["task"]
        if not tools.reads:
            return AgentDecision(
                action="tool",
                thought="Apply the user's update.",
                tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
            )
        return answer(store)

    model = Model(decide)
    install(service, model, tools)
    worker = Thread(target=service.execute, args=(value.run_id,))
    worker.start()
    try:
        assert entered.wait(15)
        update = FollowUpInput(client_request_id="while-thinking", body="focus on startup")
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: service.follow_up(value.run_id, update), range(4)))
        assert {item.run_id for item in results} == {value.run_id}
        detail = service.detail(value.run_id)
        assert len(detail["interaction"]["messages"]) == 1
        assert detail["interaction"]["messages"][0]["status"] == "queued"
        assert service.store.spec(value.run_id).model_calls_used == 1
        with pytest.raises(DomainError, match="reused"):
            service.follow_up(value.run_id, update.model_copy(update={"body": "different"}))
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive()
    detail = service.detail(value.run_id)
    assert detail["run"]["run_status"] == "succeeded", detail["run"]
    assert detail["interaction"]["messages"][0]["status"] == "applied"
    assert tools.reads == ["src/main.py"], "A new input must supersede the not-yet-started old plan"
    assert detail["run"]["task"] == value.task
    assert len(service.list()) == 1 and detail["run"]["ticket_id"] == str(value.ticket_id)
    assert service.store.spec(value.run_id).limits == value.limits
    assert service.follow_up(value.run_id, update).run_id == value.run_id
    assert len(service.list()) == 1, "A lost acknowledgement must not create another paid turn"


def test_waiting_answer_reopens_same_checkpoint_run_and_ticket(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    tools = Files(value)

    def decide(store, payload):
        if not tools.reads:
            return AgentDecision(
                action="tool",
                thought="Read source.",
                tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
            )
        if "reproduction details" not in payload["task"]:
            return AgentDecision(
                action="ask",
                thought="The missing fact requires the user.",
                question="Which input reproduces the reported failure?",
                options=["Startup", "Shutdown"],
            )
        return answer(store, "Startup returns ready; the reproduction details were checked.")

    model = Model(decide)
    install(service, model, tools)
    service.execute(value.run_id)
    waiting = service.store.spec(value.run_id)
    detail = service.detail(value.run_id)
    assert waiting.run_status == "waiting_input" and waiting.stop_reason == "INPUT_REQUIRED"
    assert detail["interaction"]["question"]["question"].startswith("Which input")
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT status FROM app.tickets WHERE ticket_id=%s", (value.ticket_id,)
            ).fetchone()["status"]
            == "open"
        )
        assert (
            conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE run_id=%s "
                "AND status IN ('unknown','reserved')",
                (value.run_id,),
            ).fetchone()
            is None
        )
    result = service.follow_up(
        value.run_id,
        FollowUpInput(
            client_request_id="answer",
            body="reproduction details: startup returns ready",
        ),
    )
    assert result.run_id == value.run_id and result.ticket_id == value.ticket_id
    assert result.deadline_at == value.deadline_at and result.limits == value.limits
    assert result.execution_epoch > waiting.execution_epoch
    # Fresh service/graph and PostgresSaver instances must restore durable state.
    restored = RepositoryAnalysisService(engine, settings)
    install(restored, model, tools)
    restored.execute(value.run_id)
    final = restored.detail(value.run_id)
    assert final["run"]["run_status"] == "succeeded", final["run"]
    assert final["run"]["model_calls_used"] == 4
    assert len(restored.list()) == 1 and tools.reads == ["src/main.py"]
    assert "reproduction details" in final["report"]


def test_final_receipt_cannot_drop_a_message_accepted_during_verification(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    tools = Files(value)
    updates = []

    def decide(store, payload):
        if not tools.reads:
            return AgentDecision(
                action="tool",
                thought="Read source.",
                tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
            )
        return answer(
            store, "Updated answer." if "new requirement" in payload["task"] else "Old answer."
        )

    def during_acceptance():
        if not updates:
            updates.append(
                service.follow_up(
                    value.run_id,
                    FollowUpInput(
                        client_request_id="at-final",
                        body="new requirement: explain startup",
                    ),
                )
            )

    install(service, Model(decide, acceptance=during_acceptance), tools)
    service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert detail["run"]["run_status"] == "succeeded", detail["run"]
    assert "Updated answer." in detail["report"] and "Old answer." not in detail["report"]
    assert detail["interaction"]["messages"][0]["status"] == "applied"
    assert updates[0].run_id == value.run_id


def test_snapshot_replay_defers_later_input_and_fences_old_epoch(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    service.store.begin_run(value.run_id)
    active = service.store.spec(value.run_id)
    store = service.store
    assert store.input_snapshot(value.run_id, active.execution_epoch, 1, 0) == []
    service.follow_up(value.run_id, FollowUpInput(client_request_id="late", body="new input"))
    assert store.input_snapshot(value.run_id, active.execution_epoch, 1, 0) == []
    batch = store.input_snapshot(value.run_id, active.execution_epoch, 2, 0)
    assert [item["body"] for item in batch] == ["new input"]
    with pytest.raises(DomainError) as pending:
        store.finish_success(
            value.run_id,
            active.execution_epoch,
            b"stale report",
            ReportVerification(passed=True, checks=[]),
            input_cursor=0,
        )
    assert pending.value.code == "ANALYSIS_INPUT_PENDING"
    assert store.spec(value.run_id).run_status == "running"
    store.cancel(value.run_id)
    with pytest.raises(DomainError) as fenced:
        store.input_snapshot(value.run_id, active.execution_epoch, 3, batch[-1]["event_id"])
    assert fenced.value.code == "FENCED"


def test_unknown_result_cannot_be_turned_into_an_answerable_checkpoint(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    service.store.begin_run(value.run_id)
    active = service.store.spec(value.run_id)
    queued_bodies = [
        "Never change authentication.",
        *[f"Follow requirement {n}." for n in range(5)],
    ]
    for number, body in enumerate(queued_bodies):
        service.follow_up(
            value.run_id, FollowUpInput(client_request_id=f"queued-{number}", body=body)
        )
    call = service.store.reserve_repository_call(
        value.run_id, active.execution_epoch, "unknown", active.profile_hash, 100, None, 200
    )
    service.store.settle_call(call, None)
    assert not service.store.wait_for_input(
        value.run_id,
        active.execution_epoch,
        0,
        "Provide input",
        [],
    )
    latest_input = service.detail(value.run_id)["interaction"]["messages"][-1]["event_id"]
    with pytest.raises(DomainError) as blocked:
        service.store.wait_for_input(
            value.run_id,
            active.execution_epoch,
            latest_input,
            "Provide input",
            [],
        )
    assert blocked.value.code == "RECOVERY_BLOCKED"
    service.store.fail(value.run_id, "USAGE_UNKNOWN")
    follow = service.follow_up(
        value.run_id,
        FollowUpInput(client_request_id="explicit", body="Continue with a new user request"),
    )
    assert follow.run_id != value.run_id and follow.ticket_id != value.ticket_id
    assert follow.thread_id == value.thread_id
    with engine.transaction() as conn:
        old = conn.execute(
            "SELECT status,reserved_micros FROM app.usage_calls WHERE call_id=%s", (call,)
        ).fetchone()
    assert old["status"] == "unknown" and old["reserved_micros"] == 100
    context = json.loads(service.prior_report_text(follow))
    assert len(context["previous_user_message_ids"]) == len(queued_bodies)
    service.store.begin_run(follow.run_id)
    resumed = service.store.spec(follow.run_id)
    agent = RepoAnalysisAgent(
        AnalysisRuntime(
            store=service.store, model=None, tools=None, system="Controlled", prior_report=None
        )
    )
    effective = agent._effective_task(resumed, initial_state(resumed))
    assert all(body in effective for body in queued_bodies)
    assert service.store.spec(value.run_id).run_status == "failed"


def test_authenticated_messages_api_accepts_active_and_waiting_answers_idempotently(
    engine, settings
):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    service.store.begin_run(value.run_id)
    active = service.store.spec(value.run_id)
    url = f"/api/v3/repository-analyses/{value.run_id}/messages"
    first = {"client_request_id": "api-active", "body": "Keep the original objective."}
    with browser(settings, engine) as client:
        signup(client)
        for _ in range(2):
            response = client.post(url, json=first)
            assert response.status_code == 202, response.text
            assert response.json()["run_id"] == str(value.run_id)
        detail = client.get(url.removesuffix("/messages")).json()
        assert len(detail["interaction"]["messages"]) == 1
        assert detail["interaction"]["messages"][0]["status"] == "queued"
        batch = service.store.input_snapshot(value.run_id, active.execution_epoch, 1, 0)
        assert service.store.wait_for_input(
            value.run_id,
            active.execution_epoch,
            batch[-1]["event_id"],
            "Which failing input?",
            ["Startup"],
        )
        response = client.post(url, json={"client_request_id": "api-answer", "body": "Startup"})
        assert response.status_code == 202, response.text
        assert response.json()["run_id"] == str(value.run_id)
        assert response.json()["run_status"] == "queued"
        # A retry of an older accepted input acknowledges it; it is never a new turn.
        assert client.post(url, json=first).json()["run_id"] == str(value.run_id)
        conflict = client.post(url, json={**first, "body": "Changed body"})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        detail = client.get(url.removesuffix("/messages")).json()
        assert len(detail["interaction"]["messages"]) == 2
        assert len(service.list()) == 1


def test_empty_searches_change_to_manifest_and_directory_investigation(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)

    class Repository:
        calls = []

        async def execute(self, call):
            self.calls.append(call.model_dump())
            if call.tool == "search_code":
                return ToolOutcome(
                    tool=call.tool, reference="search:" + call.query, text=json.dumps({"items": []})
                )
            if call.tool == "list_directory":
                return ToolOutcome(
                    tool=call.tool,
                    reference=f"github://{value.repository}@{value.commit}/",
                    text=json.dumps(
                        {
                            "entries": [
                                {
                                    "type": "file",
                                    "name": "pyproject.toml",
                                    "path": "pyproject.toml",
                                },
                                {"type": "dir", "name": "tests", "path": "tests"},
                            ]
                        }
                    ),
                )
            return ToolOutcome(
                tool=call.tool,
                reference=f"github://{value.repository}@{value.commit}/{call.path}",
                text='framework = "FastAPI"\n',
                content=b'framework = "FastAPI"\n',
                source_version="manifest-blob",
                evidence_kind="file",
            )

    tools = Repository()

    def decide(store, payload):
        rows = store.evidence_rows(value.run_id)
        if rows:
            claim = {
                "statement": "The manifest declares FastAPI.",
                "evidence_id": str(rows[0].evidence_id),
                "quote": 'framework = "FastAPI"',
            }
            return AgentDecision(
                action="final",
                thought="Use manifest evidence.",
                report={
                    "conclusion": "FastAPI is declared; that fact alone does not prove a bug.",
                    "structure": "Python project.",
                    "key_files": [claim],
                    "findings": [claim],
                },
            )
        return AgentDecision(
            action="tool",
            thought="An overly narrow empty search.",
            tool_calls=[{"tool": "search_code", "query": f"missing{len(tools.calls)}"}],
        )

    install(service, Model(decide), tools)
    service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert detail["run"]["run_status"] == "succeeded", detail["run"]
    assert len(tools.calls) > 7
    assert any(item["tool"] == "list_directory" for item in tools.calls)
    assert any(
        item["tool"] == "read_file" and item["path"] == "pyproject.toml" for item in tools.calls
    )
    assert any(item["kind"] == "analysis_replanned" for item in detail["events"])
    assert not any(item["kind"] == "analysis_question" for item in detail["events"])


def test_all_user_constraints_survive_compaction_without_inline_checkpoint_bodies(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    bodies = ["Do not modify authentication or publish anything. " * 30]
    bodies.extend(f"Additional requirement number {number}." for number in range(5))
    for number, body in enumerate(bodies):
        service.follow_up(
            value.run_id, FollowUpInput(client_request_id=f"update-{number}", body=body)
        )
    service.store.begin_run(value.run_id)
    active = service.store.spec(value.run_id)
    agent = RepoAnalysisAgent(
        AnalysisRuntime(
            store=service.store, model=None, tools=None, system="Controlled", prior_report=None
        )
    )
    state = agent._apply_inputs(initial_state(active), active.execution_epoch, 1)
    assert all("body" not in item for item in state["user_messages"])
    assert "Do not modify authentication" not in json.dumps(state)
    state["history"].extend(
        {"tool": "decision", "reference": "", "content": "Old thought. " * 200} for _ in range(30)
    )
    payload = agent._payload(active, state, 2, max_tokens=4096, recent_window=2)
    for body in bodies:
        assert body in payload["task"]
    assert value.task in payload["task"]
    assert len(payload["history"]) < len(state["history"])


def test_waiting_question_serializes_the_thread_across_older_history_pages(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    original = create(service)
    service.execute(original.run_id)
    assert service.store.spec(original.run_id).run_status == "succeeded"
    follow = service.follow_up(
        original.run_id,
        FollowUpInput(
            client_request_id="new-target",
            body="Investigate a specific failing request.",
        ),
    )
    service.store.begin_run(follow.run_id)
    active = service.store.spec(follow.run_id)
    assert service.store.wait_for_input(
        active.run_id, active.execution_epoch, 0, "Which request fails?", []
    )
    detail = service.detail(original.run_id)
    assert detail["continuation"]["active_runs"] == 1
    assert not detail["continuation"]["explicit_followup_allowed"]
    with pytest.raises(DomainError) as conflict:
        service.follow_up(
            original.run_id,
            FollowUpInput(
                client_request_id="old-page-new-run",
                body="Start a competing investigation.",
            ),
        )
    assert conflict.value.code == "RUN_NOT_SETTLED"
    assert len(service.list()) == 2
    service.cancel(follow.run_id)
    next_turn = service.follow_up(
        original.run_id,
        FollowUpInput(
            client_request_id="after-cancel",
            body="Continue after cancelling the unanswered question.",
        ),
    )
    assert next_turn.run_id not in {original.run_id, follow.run_id}
    assert next_turn.thread_id == original.thread_id


def test_inbox_capacity_is_explicit_and_does_not_reject_an_existing_ack(engine, settings):
    service = RepositoryAnalysisService(engine, settings)
    value = create(service)
    first = FollowUpInput(client_request_id="message-0", body="Retain this requirement.")
    for number in range(128):
        service.follow_up(
            value.run_id,
            first
            if number == 0
            else FollowUpInput(
                client_request_id=f"message-{number}",
                body=f"Requirement {number}.",
            ),
        )
    assert service.follow_up(value.run_id, first).run_id == value.run_id
    with pytest.raises(DomainError) as full:
        service.follow_up(
            value.run_id, FollowUpInput(client_request_id="over-limit", body="New text")
        )
    assert full.value.code == "ANALYSIS_INBOX_FULL"
    detail = service.detail(value.run_id)
    assert len(detail["interaction"]["messages"]) == 128
    assert detail["run"]["model_calls_used"] == 0 and detail["run"]["reserved_tokens"] == 0
    assert service.store.spec(value.run_id).limits == value.limits
