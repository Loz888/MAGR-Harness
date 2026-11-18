"""Explicit follow-up retains unknown calls and shares the conversation ceiling."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import httpx
import pytest
from test_analysis_budget_policy import config, request
from test_analysis_budget_policy import resolved as resolved
from test_web_access import browser, signup

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.domain import AgentDecision, FollowUpInput
from mgar_v3.repository_analysis.model import DeepSeekAnalysisModel
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.repository_analysis.tools import ToolOutcome


def stopped_unknown(service, *, fee=20000, tokens=10000):
    value = service.create(request())
    service.store.begin_run(value.run_id)
    value = service.store.spec(value.run_id)
    call = service.store.reserve_repository_call(
        value.run_id, value.execution_epoch, "old-unknown", value.profile_hash,
        fee, None, tokens,
    )
    service.store.settle_call(call, None)
    return value, call


def test_unknown_followup_keeps_memory_and_reservations_and_settles_original_mandate(
    settings, engine, resolved,
):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        assert client.put("/api/v3/settings/analysis", json=config("cost")).status_code == 200
        parent, unknown_call = stopped_unknown(service)
        tool = service.store.start_tool(
            parent.run_id, parent.execution_epoch, "old-source", "read_file", "f" * 64,
        )
        old_evidence = service.store.finish_tool(
            tool.call_id, parent.execution_epoch, outcome=ToolOutcome(
                tool="read_file", text='framework = "FastAPI"',
                content=b'framework = "FastAPI"\n', evidence_kind="file",
                reference=f"github://{parent.repository}@{parent.commit}/pyproject.toml",
                source_version="pinned-source",
            ),
        )
        service.store.stream_event(parent.run_id, parent.execution_epoch, "answer_delta",
                                   field="conclusion", text="Uses FastAPI.", offset=0)
        service.store.fail(parent.run_id, "USAGE_UNKNOWN")
        with engine.transaction() as conn:
            old_mandate = conn.execute("SELECT mandate_id FROM app.tickets WHERE ticket_id=%s",
                                       (parent.ticket_id,)).fetchone()["mandate_id"]
            frozen_call = conn.execute("SELECT * FROM app.usage_calls WHERE call_id=%s",
                                       (unknown_call,)).fetchone()
        response = client.post(f"/api/v3/repository-analyses/{parent.run_id}/messages", json={
            "client_request_id": "explicit-new-message", "body": "继续，解释这个框架的入口。",
        })
        assert response.status_code == 202, response.text
        follow = service.store.spec(UUID(response.json()["run_id"]))
        assert follow.thread_id == parent.thread_id and follow.commit == parent.commit
        assert follow.ticket_id != parent.ticket_id
        assert follow.parent_run_id == parent.run_id
        assert str(old_evidence) in follow.prior_evidence_map
        assert follow.limits.max_cost_micros == 980000
        assert follow.limits.max_input_tokens == follow.limits.max_total_tokens == 9990000
        context = json.loads(service.prior_report_text(follow))
        assert context["previous_tasks"][0]["task"] == parent.task
        assert context["unverified_drafts"][0]["conclusion"] == "Uses FastAPI."
        assert context["unverified_drafts"][0]["verified"] is False
        assert context["evidence"][0]["source_version"] == "pinned-source"
        with engine.transaction() as conn:
            assert conn.execute("SELECT * FROM app.usage_calls WHERE call_id=%s",
                                (unknown_call,)).fetchone() == frozen_call
            assert conn.execute("SELECT mandate_id FROM app.tickets WHERE ticket_id=%s",
                                (parent.ticket_id,)).fetchone()["mandate_id"] == old_mandate

        sent = []

        def provider(req):
            sent.append(req)
            return httpx.Response(200, json={
                "model": follow.model.model_id,
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                    "action": "insufficient", "thought": "Read the entry point next.",
                    "insufficient_reason": "Need one more source file.",
                })}}],
            })

        service.store.begin_run(follow.run_id)
        follow = service.store.spec(follow.run_id)
        with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
            model = DeepSeekAnalysisModel(service.settings, follow.model, client=remote,
                                          rate_limit=lambda *args: None)
            result = model.complete(
                service.store, follow.run_id, follow.execution_epoch, "new-user-message",
                follow.profile_hash, "system", {"task": follow.task}, AgentDecision,
                call_limit=None, deadline_check=lambda: None,
            )
        assert result.action == "insufficient" and len(sent) == 1
        detail = service.detail(follow.run_id)
        assert detail["budget"]["settled_micros"] == 150
        assert detail["conversation_budget"]["settled_micros"] == 150
        assert detail["conversation_budget"]["reserved_micros"] == 20000
        assert detail["conversation_budget"]["limit_micros"] == 1000000
        assert detail["continuation"]["state"] == "unresolved"
        assert not detail["continuation"]["explicit_followup_allowed"]
        # A later trusted receipt must settle the original immutable mandate,
        # never charge the new message's mandate or revive its predecessor.
        service.store.settle_call(unknown_call, 3024, {
            "prompt_tokens": 10000, "completion_tokens": 20,
        })
        with engine.transaction() as conn:
            old_budget = conn.execute("SELECT * FROM app.budgets WHERE mandate_id=%s",
                                      (old_mandate,)).fetchone()
            new_mandate = conn.execute("SELECT mandate_id FROM app.tickets WHERE ticket_id=%s",
                                       (follow.ticket_id,)).fetchone()["mandate_id"]
            new_budget = conn.execute("SELECT * FROM app.budgets WHERE mandate_id=%s",
                                      (new_mandate,)).fetchone()
        assert old_budget["reserved_micros"] == 0 and old_budget["settled_micros"] == 3024
        assert new_budget["reserved_micros"] == 0 and new_budget["settled_micros"] == 150
        assert service.store.spec(parent.run_id).run_status == "failed"
        assert service.store.spec(follow.run_id).limits.max_cost_micros == 980000
        service.cancel(follow.run_id)
        assert service.detail(follow.run_id)["continuation"]["explicit_followup_allowed"]
        third = service.follow_up(follow.run_id, FollowUpInput(
            client_request_id="after-reconciliation", body="Continue with the retained sources.",
        ))
        assert third.limits.max_cost_micros == 1000000 - 3024 - 150
        assert third.limits.max_total_tokens == 10000000 - 10000 - 20 - 100 - 100


@pytest.mark.parametrize("kind", ["money", "tokens"])
def test_unknown_reservation_still_constrains_whole_page(settings, engine, resolved, kind):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        client.put("/api/v3/settings/analysis", json=config("cost"))
        parent, call = stopped_unknown(
            service, fee=1000000 if kind == "money" else 20000,
            tokens=9999000 if kind == "tokens" else 10000,
        )
        service.store.fail(parent.run_id, "USAGE_UNKNOWN")
        with pytest.raises(DomainError) as error:
            service.follow_up(parent.run_id, FollowUpInput(
                client_request_id="no-reset", body="Do not reset this page's allowance.",
            ))
        assert error.value.code == ("BUDGET_EXHAUSTED" if kind == "money"
                                    else "TOKEN_BUDGET_EXHAUSTED")
        assert len(service.list()) == 1
        with engine.transaction() as conn:
            receipt = conn.execute("SELECT status,reserved_micros FROM app.usage_calls "
                                   "WHERE call_id=%s", (call,)).fetchone()
        assert receipt["status"] == "unknown"
        assert receipt["reserved_micros"] == (1000000 if kind == "money" else 20000)


def test_unknown_followup_idempotency_and_concurrent_distinct_messages(settings, engine, resolved):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        client.put("/api/v3/settings/analysis", json=config("cost"))
        parent, _ = stopped_unknown(service)
        service.store.fail(parent.run_id, "USAGE_UNKNOWN")
        message = FollowUpInput(client_request_id="same-explicit-message", body="Continue.")
        with ThreadPoolExecutor(max_workers=2) as pool:
            duplicates = list(pool.map(
                lambda _: service.follow_up(parent.run_id, message), range(2),
            ))
        assert duplicates[0].run_id == duplicates[1].run_id
        assert len(service.list()) == 2
        service.cancel(duplicates[0].run_id)

        def concurrent(index):
            try:
                return service.follow_up(parent.run_id, FollowUpInput(
                    client_request_id=f"distinct-{index}", body=f"Question {index}.",
                )).run_id
            except DomainError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(concurrent, range(2)))
        assert len([x for x in results if isinstance(x, UUID)]) == 1
        assert results.count("RUN_NOT_SETTLED") == 1
        assert len(service.list()) == 3


def test_multiturn_limits_do_not_subtract_residual_twice(settings, engine, resolved):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        client.put("/api/v3/settings/analysis", json=config("cost"))
        parent, _ = stopped_unknown(service)
        service.store.fail(parent.run_id, "USAGE_UNKNOWN")
        for index in range(3):
            follow = service.follow_up(parent.run_id, FollowUpInput(
                client_request_id=f"new-message-{index}", body="Continue with the same evidence.",
            ))
            assert follow.limits.max_cost_micros == 980000 - 150 * index
            assert follow.limits.max_total_tokens == 9990000 - 200 * index
            service.store.begin_run(follow.run_id)
            follow = service.store.spec(follow.run_id)
            call = service.store.reserve_repository_call(
                follow.run_id, follow.execution_epoch, "known", follow.profile_hash,
                1000, None, 1000,
            )
            service.store.settle_call(call, 150, {"prompt_tokens": 100, "completion_tokens": 100})
            service.cancel(follow.run_id)
            parent = follow
