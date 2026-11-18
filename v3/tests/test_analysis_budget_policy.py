"""Real PostgreSQL budgets and stopped-analysis continuation, without network calls."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from test_web_access import browser, signup

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.agent import RepoAnalysisAgent, initial_state
from mgar_v3.repository_analysis.domain import CreateAnalysisInput, FollowUpInput
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.repository_analysis.tools import RepositoryAnalysisTools, ToolOutcome


def config(mode="manual", *, tokens=60000):
    return {
        "deepseek_api_key": "controlled-key",
        "github_read_token": "controlled-read-key",
        "allow_paid": True,
        "model_id": "deepseek-flash",
        "token_budget_mode": mode,
        "limits": {
            "max_model_calls": None, "max_tool_calls": None,
            "max_input_tokens": tokens, "max_total_tokens": tokens,
            "max_cost_micros": 1000000, "max_seconds": 3600,
        },
    }


def request():
    return CreateAnalysisInput(
        repository_url="https://github.com/octo/example",
        task="Trace the startup, retaining the source evidence.",
        execution_mode="live", client_request_id=str(uuid4()),
    )


@pytest.fixture
def resolved(monkeypatch):
    async def commit(*args):
        return "c" * 40

    monkeypatch.setattr(RepositoryAnalysisTools, "resolve_commit", commit)


@pytest.mark.parametrize("mode,tokens,expected", [
    ("manual", 60000, 60000), ("manual", 500000, 500000), ("cost", 60000, 10000000),
])
def test_saved_mode_and_explicit_limits_freeze_into_real_run(
    settings, engine, resolved, mode, tokens, expected,
):
    with browser(settings, engine) as client:
        signup(client)
        saved = client.put("/api/v3/settings/analysis", json=config(mode, tokens=tokens))
        assert saved.status_code == 200, saved.text
        profile = saved.json()["profile"]
        assert profile["token_budget_mode"] == mode
        assert profile["limits"]["max_total_tokens"] == expected
        value = RepositoryAnalysisService(engine, settings).create(request())
        assert value.limits.max_input_tokens == value.limits.max_total_tokens == expected
        assert value.limits.max_cost_micros == 1000000
        assert value.limits.max_model_calls is value.limits.max_tool_calls is None
        with engine.transaction() as conn:
            calls = conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()
            assert calls["n"] == 0


def test_cost_mode_does_not_expand_old_run_and_original_money_ledger_still_limits(
    settings, engine, resolved,
):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        first = client.put("/api/v3/settings/analysis", json=config()).json()
        original = service.create(request())
        service.store.begin_run(original.run_id)
        active = service.store.spec(original.run_id)
        call = service.store.reserve_repository_call(
            active.run_id, active.execution_epoch, "first", active.profile_hash,
            20000, None, 45000,
        )
        service.store.settle_call(call, 13151, {
            "prompt_tokens": 40198, "completion_tokens": 906,
        })
        service.cancel(active.run_id)
        update = config("cost") | {"expected_revision": first["revision"]}
        assert client.put("/api/v3/settings/analysis", json=update).status_code == 200
        frozen = service.store.spec(original.run_id)
        assert frozen.limits.max_total_tokens == 60000
        assert frozen.input_tokens_used + frozen.output_tokens_used == 41104
        detail = service.detail(original.run_id)
        assert detail["budget"] == {
            "limit_micros": 1000000, "reserved_micros": 0,
            "settled_micros": 13151, "remaining_micros": 986849,
            "used_calls": 1, "call_limit": None, "unknown_calls": 0,
            "legacy_reserved_calls": 0, "unresolved_calls": 0, "inflight_calls": 0,
        }
        follow = service.follow_up(original.run_id, FollowUpInput(
            client_request_id="cost-followup", body="Continue the previous startup trace.",
        ))
        assert follow.limits.max_total_tokens == 10000000 - 41104
        assert follow.limits.max_cost_micros == 1000000 - 13151
        assert follow.thread_id == original.thread_id and follow.commit == original.commit
        assert follow.authorization_hash != original.authorization_hash
        assert frozen.run_status == "cancelled"
        service.store.begin_run(follow.run_id)
        follow = service.store.spec(follow.run_id)
        with pytest.raises(DomainError) as rejected:
            service.store.reserve_repository_call(
                follow.run_id, follow.execution_epoch, "too-expensive", follow.profile_hash,
                1000001, None, 100,
            )
        assert rejected.value.code == "BUDGET_EXHAUSTED"
        current = service.store.spec(follow.run_id)
        assert current.model_calls_used == current.reserved_tokens == 0
        assert service.detail(follow.run_id)["budget"]["settled_micros"] == 0


def test_legacy_settings_without_mode_stay_manual_on_read_and_save(
    settings, engine, resolved,
):
    with browser(settings, engine) as client:
        signup(client)
        original_config = config()
        original_config.pop("token_budget_mode")
        saved = client.put("/api/v3/settings/analysis", json=original_config).json()
        assert saved["profile"]["token_budget_mode"] == "manual"
        service = RepositoryAnalysisService(engine, settings)
        revision, payload = service.web_settings.snapshot()
        # Reproduce a pre-feature encrypted payload without altering its authorization.
        payload.pop("token_budget_mode")
        encrypted = service.web_settings._cipher().encrypt(json.dumps(payload).encode()).decode()
        with engine.transaction() as conn:
            row = conn.execute(
                "INSERT INTO app.web_analysis_settings(encrypted_payload,updated_by) "
                "SELECT %s,user_id FROM app.web_accounts LIMIT 1 RETURNING revision",
                (encrypted,),
            ).fetchone()
            revision = row["revision"]
        before = service.profile()
        assert before["token_budget_mode"] == "manual"
        assert before["limits"]["max_total_tokens"] == 60000
        update = original_config | {"expected_revision": revision}
        after = client.put("/api/v3/settings/analysis", json=update).json()["profile"]
        assert after["token_budget_mode"] == "manual"
        assert after["limits"]["max_total_tokens"] == 60000
        latest = client.get("/api/v3/settings/analysis").json()
        unchanged = client.put("/api/v3/settings/analysis", json={
            "allow_paid": True, "expected_revision": latest["revision"],
        }).json()["profile"]
        assert unchanged["token_budget_mode"] == "manual"
        assert unchanged["limits"] == after["limits"]


@pytest.mark.parametrize(
    "pending_kind", ["model_reserved", "model_unknown", "tool_running", "tool_unknown"],
)
def test_cancel_followup_waits_only_for_inflight_receipt(
    settings, engine, resolved, pending_kind,
):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        client.put("/api/v3/settings/analysis", json=config("cost"))
        value = service.create(request())
        service.store.begin_run(value.run_id)
        value = service.store.spec(value.run_id)
        if pending_kind.startswith("model"):
            call = service.store.reserve_repository_call(
                value.run_id, value.execution_epoch, "inflight", value.profile_hash,
                20000, None, 10000,
            )
            if pending_kind.endswith("unknown"):
                service.store.settle_call(call, None)
        else:
            call = service.store.start_tool(
                value.run_id, value.execution_epoch, "read-inflight", "read_file", "a" * 64,
            ).call_id
            if pending_kind.endswith("unknown"):
                with engine.transaction() as conn:
                    conn.execute(
                        "UPDATE app.repo_tool_calls SET status='unknown' WHERE call_id=%s", (call,),
                    )
        service.cancel(value.run_id)
        epoch = service.store.spec(value.run_id).execution_epoch
        message = FollowUpInput(client_request_id="after-stop", body="Continue with the evidence.")
        if not pending_kind.endswith("unknown"):
            with pytest.raises(DomainError) as blocked:
                service.follow_up(value.run_id, message)
            assert blocked.value.code == "ANALYSIS_CALL_PENDING"
            assert len(service.list()) == 1
        if pending_kind == "model_reserved":
            service.store.settle_call(call, 123, {"prompt_tokens": 10, "completion_tokens": 5})
        elif pending_kind == "tool_running":
            assert service.store.finish_tool(
                call, value.execution_epoch,
                outcome=ToolOutcome(
                    tool="read_file", text="late", content=b"late", reference="late",
                ),
            ) is None
        follow = service.follow_up(value.run_id, message)
        assert follow.parent_run_id == value.run_id
        assert follow.ticket_id != value.ticket_id and follow.thread_id == value.thread_id
        if pending_kind.endswith("unknown"):
            table = "usage_calls" if pending_kind.startswith("model") else "repo_tool_calls"
            with engine.transaction() as conn:
                assert conn.execute(f"SELECT status FROM app.{table} WHERE call_id=%s",
                                    (call,)).fetchone()["status"] == "unknown"
            if pending_kind.startswith("model"):
                assert service.detail(value.run_id)["budget"]["reserved_micros"] == 20000
                assert follow.limits.max_cost_micros == 980000
                assert follow.limits.max_total_tokens == 9990000
        assert service.follow_up(value.run_id, message).run_id == follow.run_id
        with pytest.raises(DomainError) as duplicate_active:
            service.follow_up(value.run_id, FollowUpInput(
                client_request_id="different-after-stop", body="Start another copy.",
            ))
        assert duplicate_active.value.code == "RUN_NOT_SETTLED"
        stopped = service.store.spec(value.run_id)
        assert stopped.run_status == "cancelled" and stopped.execution_epoch == epoch
        assert not service.store.evidence_rows(value.run_id)


def test_stopped_reportless_run_retains_task_tools_and_scoped_source_evidence(
    settings, engine, resolved,
):
    service = RepositoryAnalysisService(engine, settings)
    with browser(settings, engine) as client:
        signup(client)
        client.put("/api/v3/settings/analysis", json=config("cost"))
        parent = service.create(request())
        service.store.begin_run(parent.run_id)
        parent = service.store.spec(parent.run_id)
        tool = service.store.start_tool(
            parent.run_id, parent.execution_epoch, "read:startup", "read_file", "b" * 64,
        )
        evidence_id = service.store.finish_tool(
            tool.call_id, parent.execution_epoch, outcome=ToolOutcome(
                tool="read_file", text="def startup(): return 7",
                content=b"def startup(): return 7\n", evidence_kind="file",
                reference=f"github://{parent.repository}@{parent.commit}/startup.py",
                source_version="source-blob-1",
            ),
        )
        assert client.post(f"/api/v3/repository-analyses/{parent.run_id}/cancel").status_code == 200
        reply = client.post(f"/api/v3/repository-analyses/{parent.run_id}/messages", json={
            "client_request_id": "source-context", "body": "Continue tracing its callers.",
        })
        assert reply.status_code == 202, reply.text
        follow = service.store.spec(UUID(reply.json()["run_id"]))
        assert follow.thread_id == parent.thread_id and follow.commit == parent.commit
        assert follow.prior_report_hash is None
        assert str(evidence_id) in follow.prior_evidence_map
        context = json.loads(service.prior_report_text(follow))
        assert context["previous_tasks"][-1]["task"] == parent.task
        assert context["previous_status"] == "cancelled"
        assert context["recent_tools"][0]["tool"] == "read_file"
        assert context["copied_evidence_count"] == 1
        assert context["source_excerpts"][0]["source_excerpt"] == "def startup(): return 7\n"
        copied = service.store.evidence_rows(follow.run_id)[0]
        assert UUID(context["evidence"][0]["evidence_id"]) == copied.evidence_id
        assert context["evidence"][0]["content_hash"] == copied.content_hash
        assert "thought" not in context and "authorization" not in context
        assert len(service.prior_report_text(follow)) < 8000
        for index in range(4):
            service.cancel(follow.run_id)
            follow = service.follow_up(follow.run_id, FollowUpInput(
                client_request_id=f"continue-{index}", body=f"Continue step {index}.",
            ))
        continued = json.loads(service.prior_report_text(follow))
        assert continued["previous_tasks"][0] == {
            "run_id": str(parent.run_id), "task": parent.task,
        }
        assert [item["task"] for item in continued["previous_tasks"]][1:] == [
            "Continue step 0.", "Continue step 1.", "Continue step 2.",
        ]
        assert continued["copied_evidence_count"] == 1


def test_long_original_constraint_survives_repeated_stop_and_serialized_context_bound(
    settings, engine, resolved,
):
    service = RepositoryAnalysisService(engine, settings)
    original_task = "\\" * 7900 + "ORIGINAL_TAIL: preserve this exact constraint."
    with browser(settings, engine) as client:
        signup(client)
        client.put("/api/v3/settings/analysis", json=config("cost"))
        first = service.create(request().model_copy(update={"task": original_task}))
        parent = first
        for index in range(4):
            service.cancel(parent.run_id)
            parent = service.follow_up(parent.run_id, FollowUpInput(
                client_request_id=f"long-stop-{index}",
                body="\\" * 7900 + f"FOLLOWUP_TAIL_{index}",
            ))
        prior = service.prior_report_text(parent)
        assert prior is not None and 8000 < len(prior) <= 48000
        context = json.loads(prior)
        assert context["previous_tasks"][0]["task"] == original_task
        assert context["previous_tasks"][-1]["task"].endswith("FOLLOWUP_TAIL_2")
        assert len(context["omitted_task_run_ids"]) == 2
        # Protected in-flight user inputs are read only for the active epoch.
        # Follow the real execution path before constructing a model payload.
        service.store.begin_run(parent.run_id)
        parent = service.store.spec(parent.run_id)
        agent = RepoAnalysisAgent(service._runtime(parent))
        payload = agent._prepared_payload(parent, initial_state(parent), 1)
        carried = json.loads(payload["prior_report"])
        assert carried["previous_tasks"][0]["task"] == original_task
        assert payload["task"].endswith("FOLLOWUP_TAIL_3")
        assert payload["prior_report"] == prior
