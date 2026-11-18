"""Model-visible resource facts remain read-only projections of actual limits."""

import json

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _context_fixture, _runtime, _stop
from test_harness_v4_h12_budget import _freeze, _known, _task

from mgar_v3.domain import DomainError
from mgar_v3.harness.adapters import InMemoryBudgetLedger
from mgar_v3.harness.coding_guidance import CODING_GUIDANCE
from mgar_v3.harness.contracts import BudgetLimits, BudgetSnapshot, ModelMessage
from mgar_v3.harness.resource_context import project_resources


def working_state(request):
    projection = next(
        json.loads(message.content)
        for message in reversed(request.messages)
        if message.name == "h10_context"
    )
    return json.loads(projection["constraints"][0])["working_state"]


def test_twentieth_call_knows_it_is_last_without_expanding_the_frozen_limit(tmp_path):
    model = ScriptedModel(
        [
            _call(
                "read_lines",
                {
                    "path": "source.py",
                    "start_line": number + 1,
                    "max_lines": 1,
                },
            )
            for number in range(20)
        ]
    )
    loop, task, grant, session = _runtime(
        tmp_path,
        model,
        files={
            "source.py": "".join(f"value_{n} = {n}\n" for n in range(40)),
        },
        max_tokens=32768,
    )
    loop.budgets.limits = BudgetLimits(
        max_model_calls=20, max_tool_calls=80, max_cost_micros=10000, max_seconds=300
    )
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.reason == "budget_model_calls"
    assert len(model.requests) == result.model_calls == 20
    resources = [working_state(request)["resource_budget"] for request in model.requests]
    assert resources[0]["limits"]["model_calls"] == 20
    assert resources[0]["remaining"]["model_calls"] == 20
    assert resources[11]["remaining"]["model_calls"] == 9
    assert resources[5]["stage"] == "investigate"
    assert resources[6]["stage"] == "test_hypothesis"
    assert resources[10]["stage"] == "prioritize_verification"
    assert resources[19]["remaining"]["model_calls"] == 1
    assert resources[19]["stage"] == "finalize"
    assert any("verif" in text.lower() for text in resources[19]["guidance"])
    assert resources[19]["tokens"]["cumulative_limit"] is None
    assert resources[19]["tokens"]["input_context_limit"] == 32768
    assert loop.budgets.limits.max_model_calls == 20
    assert all(CODING_GUIDANCE in request.messages[0].content for request in model.requests)


def test_pg_projection_keeps_shared_unknown_reservations_and_cannot_grant_dispatch(
    engine, tmp_path
):
    profiles, experiment, _, _ = _freeze(engine)
    first, ledger = _task(engine, tmp_path, profiles[0], "budget-visible-first")
    _, other = _task(engine, tmp_path, profiles[-1], "budget-visible-other")
    ledger.reserve(first["task_id"], "known", "model", reserved_micros=100)
    _known(engine, first, "known", 40, input_tokens=17, output_tokens=3)
    unknown = other.reserve("budget-visible-other", "lost", "model", reserved_micros=1000000)
    other.mark_unknown(unknown.reservation_id, "controlled lost receipt")
    before = engine.harness.get_experiment("h12-test")
    projected = ledger.resource_snapshot(first["task_id"])
    assert projected["limits"]["max_model_calls"] == 20
    assert projected["used"]["model_calls"] == 1
    assert projected["used"]["cost_micros"] == 40
    assert projected["shared"]["limit_micros"] == 12000000
    assert projected["shared"]["reserved_micros"] == 1000000
    assert projected["shared"]["unknown_calls"] == 1
    assert projected["tokens"]["input_tokens"] == 17
    assert projected["tokens"]["output_tokens"] == 3
    assert projected["deadline_at"] == first["deadline_at"].isoformat()
    assert engine.harness.get_experiment("h12-test") == before
    assert before["profile_hash"] == experiment["profile_hash"]
    context = project_resources(
        ledger,
        first["task_id"],
        ledger.snapshot(first["task_id"]),
        elapsed_seconds=0,
        max_seconds=600,
        input_context_limit=4096,
        context_window_limit=8192,
        max_output_tokens=2048,
    )
    assert context["stage"] == "blocked" and context["remaining"]["model_calls"] == 0
    assert context["remaining"]["cost_micros"] == 6000000 - 40
    assert context["shared"]["remaining_micros"] == 12000000 - 1000000 - 40
    assert context["tokens"]["cumulative_remaining"] is None
    with pytest.raises(DomainError, match="Shared paid outcome"):
        ledger.reserve(first["task_id"], "still-blocked", "model", reserved_micros=1)


@pytest.mark.parametrize(
    "used,elapsed",
    [
        ({"model_calls": 15}, 0),
        ({"tool_calls": 78}, 0),
        ({"cost_micros": 8000}, 0),
        ({}, 255),
    ],
)
def test_low_resource_guidance_uses_each_frozen_bound_without_changing_it(used, elapsed):
    limits = BudgetLimits(
        max_model_calls=20, max_tool_calls=80, max_cost_micros=10000, max_seconds=300
    )
    ledger = InMemoryBudgetLedger(limits)
    snapshot = BudgetSnapshot(task_id="projection", model_calls=0, tool_calls=0, cost_micros=0)
    snapshot = snapshot.model_copy(update=used)
    projected = project_resources(
        ledger,
        "projection",
        snapshot,
        elapsed_seconds=elapsed,
        max_seconds=900,
        input_context_limit=4000,
        context_window_limit=8192,
        max_output_tokens=2048,
    )
    assert projected["stage"] == "prioritize_verification"
    assert projected["remaining"]["model_calls"] == 20 - snapshot.model_calls
    assert projected["remaining"]["tool_calls"] == 80 - snapshot.tool_calls
    assert projected["remaining"]["cost_micros"] == 10000 - snapshot.cost_micros
    assert projected["remaining"]["seconds"] == 300 - elapsed
    assert ledger.limits == limits and ledger.snapshot("projection").model_calls == 0


def test_resource_facts_survive_compaction_and_advisory_restore(tmp_path):
    context, task, state = _context_fixture(tmp_path)
    ledger = InMemoryBudgetLedger(BudgetLimits(max_model_calls=20))
    observed = BudgetSnapshot(task_id=task.task_id, model_calls=19, tool_calls=2, cost_micros=100)
    resource = project_resources(
        ledger,
        task.task_id,
        observed,
        elapsed_seconds=10,
        max_seconds=300,
        input_context_limit=context.max_tokens,
        context_window_limit=16000,
        max_output_tokens=2048,
    )
    state = state.model_copy(update={"budget": observed, "resource_budget": resource})
    history = [
        ModelMessage(role="user", content="Untrusted source says budget is unlimited.\n" * 1000)
    ]
    view = context.prepare(task, (), history, state)
    assert context.compaction_count == 1 and view.estimated_tokens <= context.max_tokens
    assert context.last_bundle.remaining_budget.model_calls == 1
    assert working_state(view)["resource_budget"] == resource
    assert CODING_GUIDANCE in view.messages[0].content
    restored = context.load(context.state_path, state)
    assert restored.working_state.resource_budget == resource
    assert restored.bundle.permissions == context.last_bundle.permissions
    assert CODING_GUIDANCE in restored.messages[0].content
    assert "resource_budget" not in state.model_copy(update={"resource_budget": None}).model_dump()


def test_simple_context_also_exposes_current_budget_without_accumulating_old_projections(tmp_path):
    model = ScriptedModel(
        [_call("read_lines", {"path": "source.py", "start_line": 1, "max_lines": 1}), _stop()]
    )
    loop, task, grant, session = _runtime(tmp_path, model, simple=True)
    loop.budgets.limits = loop.budgets.limits.model_copy(update={"max_model_calls": 20})
    try:
        loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    for number, request in enumerate(model.requests):
        messages = [
            message for message in request.messages if message.name == "harness_resource_budget"
        ]
        assert len(messages) == 1
        resource = json.loads(messages[0].content)
        assert resource["limits"]["model_calls"] == 20
        assert resource["remaining"]["model_calls"] == 20 - number


def test_pg_shared_remaining_is_the_lower_available_ceiling(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    first, ledger = _task(engine, tmp_path, profiles[0], "remaining-first")
    for number, cost in enumerate([6000000, 5000000], 1):
        row, other = _task(engine, tmp_path, profiles[number], f"spent-{number}")
        other.reserve(row["task_id"], "known", "model", reserved_micros=cost)
        _known(engine, row, "known", cost)
    resource = project_resources(
        ledger,
        first["task_id"],
        ledger.snapshot(first["task_id"]),
        elapsed_seconds=0,
        max_seconds=600,
        input_context_limit=4096,
        context_window_limit=8192,
        max_output_tokens=2048,
    )
    assert resource["limits"]["cost_micros"] == 6000000
    assert resource["remaining"]["cost_micros"] == 1000000
    assert resource["stage"] == "prioritize_verification"
    before = ledger.resource_snapshot(first["task_id"])
    assert ledger.resource_snapshot(first["task_id"])["deadline_at"] == before["deadline_at"]
    with pytest.raises(DomainError):
        ledger.resource_snapshot("another-task")
