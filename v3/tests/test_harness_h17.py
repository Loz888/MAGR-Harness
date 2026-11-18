"""H17 product entry point, Engine accounting, and real local workspace verification."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from test_harness_h16 import client, finish, member, setup_tenants, submit
from test_harness_h16 import tenants as shared_tenants
from test_harness_product_service import BUGGY, FIXED, SUITE, RecordedModel
from test_harness_product_service import product_env as shared_product_env

from mgar_v3.harness.contracts import HarnessToolCall, ModelResponse, ModelUsage
from mgar_v3.harness.delegation import CONSULT, EXPERT_OPERATION, ExpertCancellation
from mgar_v3.harness.product_models import product_request_input_bound
from mgar_v3.harness.recovery_io import RecoveryIO

product_env = shared_product_env
tenants = shared_tenants

USAGE = ModelUsage(input_tokens=110, output_tokens=22, cost_micros=3)


class PowerLoss(BaseException):
    pass


def consult(label="first", *, path="README.md", question="Inspect the selected file."):
    return HarnessToolCall(
        call_id=label, name=CONSULT.name, arguments={"paths": [path], "question": question},
    )


def requested(*calls, usage=USAGE):
    return ModelResponse(finish_reason="tool_calls", tool_calls=calls, usage=usage)


def install(env, *, enabled=True, consultations=(), child=None, calls=24, same_cap=False):
    env.settings.harness_experts_enabled = enabled
    env.save_model()
    env.authorize(calls=calls, allow_write=True)
    if same_cap and not enabled:
        # Equal budgets in both engineering arms. The deployed default is unchanged.
        env.harness.submit_task = lambda request: env.harness.create_task(replace(
            request, product_expert_max_tokens=env.settings.harness_expert_token_limit,
        ))
    else:
        env.harness.submit_task = env.harness.create_task
    main, experts = [], []

    class Model:
        def __init__(self, request):
            self.baseline = RecordedModel(request, [])
            self.observer = None

        def set_output_observer(self, observer):
            self.observer = observer

        def complete(self, request, cancellation):
            cancellation.ensure_active()
            if EXPERT_OPERATION in request.operation_id:
                experts.append(request)
                assert request.tools == ()
                assert request.output_continuation is None
                assert self.observer is None
                return (child(request, cancellation) if child is not None else
                        ModelResponse(text="Review the original file independently.", usage=USAGE))
            main.append(request)
            assert self.observer is not None
            assert (CONSULT.name in {tool.name for tool in request.tools}) == enabled
            history = [message for message in request.messages if message.role == "tool"]
            if consultations and not history:
                return requested(*consultations)
            # Share the exact inspection/edit/test behavior between the two arms.
            clean = tuple(m for m in request.messages if m.name != CONSULT.name)
            response = self.baseline.complete(
                request.model_copy(update={"messages": clean}), cancellation,
            )
            return response.model_copy(update={"usage": USAGE})

    env.harness.model_factory = Model
    return main, experts


def operations(env, task_id):
    return env.engine.harness.operations(task_id)


def advice_receipts(env, task_id):
    return [op["receipt"] for op in operations(env, task_id)
            if op["kind"] == "tool" and op["intent"].get("name") == CONSULT.name]


def task_result(env, session):
    result = env.harness.get_task(session["task_id"])
    assert result["task_id"] == session["task_id"]
    return result


@pytest.mark.parametrize("repair", [False, True], ids=["source-explanation", "addition-repair"])
def test_h17_paired_product_runs(product_env, request, repair):
    env = product_env
    if repair:
        (env.source / "calc.py").write_text(BUGGY)
        (env.source / "test_calc.py").write_text(SUITE)
        env.project_id = env.harness.register_project(str(env.source))["project_id"]
    results, records = [], []
    for enabled in (False, True):
        main, expert = install(
            env, enabled=enabled, same_cap=True,
            consultations=(consult(path="calc.py" if repair else "README.md"),) if enabled else (),
        )
        started = time.monotonic()
        session = env.product.create(
            env.project_id, "修复加法计算错误" if repair else "解释项目", str(enabled),
        )
        elapsed = time.monotonic() - started
        result = task_result(env, session)
        assert session["status"] == "completed", result
        assert len(expert) == int(enabled)
        assert result["model_calls"] == len(main) + len(expert)
        assert result["input_tokens"] == USAGE.input_tokens * result["model_calls"]
        assert result["output_tokens"] == USAGE.output_tokens * result["model_calls"]
        assert result["cost_micros"] == USAGE.cost_micros * result["model_calls"]
        assert result["reserved_micros"] == 0 and result["usage_known"]
        project = env.product.authorization(env.project_id)
        assert project["used_calls"] == result["model_calls"]
        assert project["settled_micros"] == result["cost_micros"]
        ledger = operations(env, session["task_id"])
        writes = [op for op in ledger if op["kind"] == "edit"]
        assert len(writes) == int(repair) and all(op["state"] == "known" for op in writes)
        if repair:
            assert result["tests"]["status"] == "passed" and result["tests"]["run"] == 1
            assert (env.source / "calc.py").read_text() == BUGGY
            assert (env.harness._runtimes[session["task_id"]].workspace.root
                    / "calc.py").read_text() == FIXED
        else:
            assert "README.md" in result["summary"]
        records.append({
            "mode": "expert" if enabled else "single", "passed": True,
            "model_calls": result["model_calls"], "expert_calls": len(expert),
            "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
            "cost_micros": result["cost_micros"], "wall_seconds": round(elapsed, 3),
            "tree_before": result["tree_before"], "tree_after": result["tree_after"],
            "test_count": result["tests"]["run"], "conflicts": 0,
        })
        results.append(result)
    meaningful = ("tree_before", "tree_after", "diff")
    assert [results[0][key] for key in meaningful] == [results[1][key] for key in meaningful]
    request.node.user_properties.append(("h17_pair", json.dumps(records)))


def test_expert_scope_same_tree_deduplicates_and_enforces_distinct_call_limit(product_env):
    env = product_env
    (env.source / "private.txt").write_text("excluded from the expert prompt")
    env.project_id = env.harness.register_project(str(env.source))["project_id"]
    main, experts = install(env, consultations=(
        consult("first"), consult("duplicate"), consult("second", question="Another question"),
        consult("limited", question="Over the limit"),
    ))
    session = env.product.create(env.project_id, "解释项目", "expert-quota")
    result = task_result(env, session)
    assert session["status"] == "completed", (result["status"], result["summary"])
    assert len(experts) == 2
    for request in experts:
        prompt = json.loads(request.messages[-1].content)
        assert [item["path"] for item in prompt["source_excerpts"]] == ["README.md"]
        assert "excluded" not in str(prompt)
        assert request.task.run_id == main[0].task.run_id
    receipts = advice_receipts(env, session["task_id"])
    assert len(receipts) == 4
    assert receipts[0]["content"] == receipts[1]["content"]
    assert json.loads(receipts[-1]["content"])["reason"] == "HARNESS_EXPERT_CALL_LIMIT"


@pytest.mark.parametrize("path", ["../other/README.md", ".env", "outside", "alias.md"])
def test_expert_rejects_escape_protected_grant_and_symlink_paths(
    product_env, monkeypatch, path,
):
    env = product_env
    (env.source / "outside").write_text("Not in the grant")
    (env.source / "alias.md").symlink_to("README.md")
    env.project_id = env.harness.register_project(str(env.source))["project_id"]
    original = env.harness._grant

    def narrow(*args, **kwargs):
        return original(*args, **kwargs).model_copy(
            update={"read_paths": ("README.md", "alias.md")},
        )

    monkeypatch.setattr(env.harness, "_grant", narrow)
    _, experts = install(env, consultations=(consult(path=path),))
    session = env.product.create(env.project_id, "解释项目", "restricted")
    assert not experts
    receipts = advice_receipts(env, session["task_id"])
    assert len(receipts) == 1
    assert receipts[0]["status"] in {"denied", "error"} and not receipts[0]["data"].get("advisory")


def test_expert_cannot_delegate_write_or_supply_verification_receipt(product_env):
    env = product_env
    main, experts = install(
        env, consultations=(consult(),),
        child=lambda request, token: requested(
            consult("nested"),
            HarnessToolCall(call_id="write", name="apply_edits", arguments={"path": "README.md"}),
            HarnessToolCall(call_id="exec", name="run_tests"),
        ),
    )
    session = env.product.create(env.project_id, "解释项目", "no-child-tools")
    result = task_result(env, session)
    assert session["status"] == "completed", result
    assert len(experts) == 1 and len(main) == 3
    receipt = advice_receipts(env, session["task_id"])[0]
    assert json.loads(receipt["content"])["status"] == "empty"
    assert receipt["data"]["advisory"] and "receipt" not in receipt["data"]
    assert not result["diff"]
    assert not {op["kind"] for op in operations(env, session["task_id"])} & {"edit", "command"}


@pytest.mark.parametrize("budget", ["calls", "tokens"])
def test_expert_spends_from_the_same_run_and_authorization_budget(product_env, budget):
    env = product_env
    known_first_call = USAGE
    if budget == "tokens":
        # Deliberately consume most of the run cap with the first confirmed main call.
        known_first_call = USAGE.model_copy(update={"input_tokens": 199_000})
    main, experts = install(
        env, calls=1 if budget == "calls" else 24, consultations=(consult(),),
    )
    base = env.harness.model_factory

    def model(request):
        wrapped = base(request)
        normal = wrapped.complete

        def complete(request, cancellation):
            result = normal(request, cancellation)
            if len(main) == 1 and not experts:
                return result.model_copy(update={"usage": known_first_call})
            return result

        wrapped.complete = complete
        return wrapped

    env.harness.model_factory = model
    session = env.product.create(env.project_id, "解释项目", budget)
    result = task_result(env, session)
    reason = "BUDGET_EXCEEDED" if budget == "calls" else "HARNESS_TOKEN_BUDGET"
    receipt = advice_receipts(env, session["task_id"])[0]
    assert json.loads(receipt["content"])["reason"] == reason
    assert not experts and len(main) == result["model_calls"] == 1
    assert result["reserved_micros"] == 0 and result["usage_known"]
    assert result["input_tokens"] == known_first_call.input_tokens
    if budget == "tokens":
        assert product_request_input_bound(main[0]) + main[0].max_output_tokens < 200_000
        assert "cumulative" in result["summary"]


def test_unknown_child_usage_pauses_without_resend_or_further_tools(product_env):
    env = product_env
    main, experts = install(
        env, consultations=(consult(),),
        child=lambda request, token: ModelResponse(
            text="Incomplete receipt", usage=ModelUsage(known=False),
        ),
    )
    session = env.product.create(env.project_id, "解释项目", "unknown-child")
    result = task_result(env, session)
    assert result["status"] == "unknown"
    assert result["usage_ledger"]["unknown_calls"] == 1 and result["reserved_micros"] > 0
    assert len(main) == len(experts) == 1
    assert env.product.create(env.project_id, "解释项目", "unknown-child")["duplicate"]
    assert len(main) == len(experts) == 1


def test_known_child_is_reused_after_controller_recovery(product_env, monkeypatch):
    env = product_env
    main, experts = install(env, consultations=(consult(),))
    invoke = RecoveryIO.invoke

    def cut(io, operation_id, kind, request, *args, **kwargs):
        value = invoke(io, operation_id, kind, request, *args, **kwargs)
        if kind == "tool" and request.get("name") == CONSULT.name:
            raise PowerLoss()
        return value

    with monkeypatch.context() as failure:
        failure.setattr(RecoveryIO, "invoke", cut)
        with pytest.raises(PowerLoss):
            env.product.create(env.project_id, "解释项目", "expert-recovery")
    task_id = main[0].task.task_id
    before = env.engine.harness.get(task_id)
    assert len(main) == len(experts) == 1
    env.engine.recover()
    result = env.harness.recover_task(task_id)
    after = env.engine.harness.get(task_id)
    assert result["status"] == "completed", result
    assert after["run_id"] == before["run_id"] and after["epoch"] > before["epoch"]
    assert result["model_calls"] == 4 and len(experts) == 1
    assert len(advice_receipts(env, task_id)) == 1


@pytest.mark.parametrize("parent_stops", [False, True], ids=["expert-expiry", "parent-cancel"])
def test_expert_stops_cooperatively_without_background_execution(product_env, parent_stops):
    env = product_env

    def child(request, token):
        assert isinstance(token, ExpertCancellation)
        assert not token.cancelled
        if parent_stops:
            token.parent.cancel()
        else:
            token.deadline = time.monotonic() - 1
        assert token.cancelled
        return ModelResponse(text="Late advisory", finish_reason="cancelled", usage=USAGE)

    main, experts = install(env, consultations=(consult(),), child=child)
    session = env.product.create(env.project_id, "解释项目", "expert-stop")
    result = task_result(env, session)
    assert len(experts) == 1 and result["reserved_micros"] == 0
    assert not result["diff"]
    if parent_stops:
        assert result["status"] == "cancelled" and len(main) == 1
    else:
        assert result["status"] == "completed"
        receipt = advice_receipts(env, session["task_id"])[0]
        assert json.loads(receipt["content"])["reason"] == "HARNESS_EXPERT_TIMEOUT"
        assert "Late advisory" not in receipt["content"]


def test_expert_transport_sends_no_tools_and_uses_the_product_thinking_policy(product_env):
    import httpx

    from mgar_v3.harness.product_models import ProductDeepSeekChatAdapter

    env = product_env
    bodies = []

    def transport(request):
        bodies.append(json.loads(request.content))
        assert 0 < request.extensions["timeout"]["read"] <= 12
        frames = [
            {"choices": [{"index": 0, "delta": {"content": "Check the README."},
                          "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 110, "completion_tokens": 22}},
        ]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            + "data: [DONE]\n\n",
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        adapter = ProductDeepSeekChatAdapter(
            api_key="fixture-only", client=client, max_attempts=1,
        )
        main, experts = install(env, consultations=(consult(),), child=adapter.complete)
        session = env.product.create(env.project_id, "解释项目", "native-wire")
    assert len(bodies) == len(experts) == 1 and len(main) == 3
    assert session["status"] == "completed", task_result(env, session)
    body = bodies[0]
    assert "tools" not in body and len(body["messages"]) == 2 and body["max_tokens"] == 768
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "max"
    assert [item["path"] for item in
            json.loads(body["messages"][-1]["content"])["source_excerpts"]] == ["README.md"]


def test_expert_money_reservation_cannot_exceed_the_existing_grant(product_env):
    env = product_env
    (env.source / "README.md").write_text("说明" * 1500)
    env.project_id = env.harness.register_project(str(env.source))["project_id"]
    main, experts = install(env, consultations=(consult(),))
    cap = 13_000
    env.product.authorize(
        env.project_id, allow_write=True, allow_paid=True,
        max_cost_micros=cap, max_model_calls=24,
    )
    original = env.harness.model_factory

    def model_factory(request):
        model = original(request)
        complete = model.complete

        def first_spends_grant(request, token):
            response = complete(request, token)
            assert len(main) == 1 and not experts
            return response.model_copy(update={"usage": USAGE.model_copy(
                update={"cost_micros": request.reservation_micros},
            )})

        model.complete = first_spends_grant
        return model

    env.harness.model_factory = model_factory
    session = env.product.create(env.project_id, "解释项目", "expert-money")
    result = task_result(env, session)
    receipt = advice_receipts(env, session["task_id"])[0]
    assert json.loads(receipt["content"])["reason"] == "BUDGET_EXCEEDED"
    assert len(main) == 1 and not experts
    assert result["reserved_micros"] == 0 and result["usage_known"]
    auth = env.product.authorization(env.project_id)
    assert auth["settled_micros"] == result["cost_micros"] == main[0].reservation_micros <= cap


def test_managed_project_expert_inherits_project_credential_and_authorization(tenants):
    env = tenants
    env.settings.harness_experts_enabled = True
    setup_tenants(env)
    alice = client(env)
    headers = member(alice, "alice")
    expert_keys = []

    class ManagedModel:
        def __init__(self, key):
            self.key = key

        def complete(self, request, cancellation):
            cancellation.ensure_active()
            if EXPERT_OPERATION in request.operation_id:
                expert_keys.append(self.key)
                return ModelResponse(text="Investigate the project README.", usage=USAGE)
            results = [message.name for message in request.messages if message.role == "tool"]
            if not results:
                return requested(consult())
            if "read_file" not in results:
                return requested(HarnessToolCall(
                    call_id="read", name="read_file", arguments={"path": "README.md"},
                ))
            return ModelResponse(text="Reviewed README.md.", usage=USAGE)

    env.app.state.harness.product_adapter_factory = (
        lambda request, key, caps: ManagedModel(key)
    )
    queued = submit(alice, headers, env.projects["alpha"], "解释项目", "managed-expert")
    result = finish(alice, queued["task_id"])
    assert result["status"] == "completed", result
    assert expert_keys == ["fixture-project-key-alpha"]
    assert result["model_calls"] == 4 and result["input_tokens"] == 440
    assert result["reserved_micros"] == 0
    operations_for_run = operations(env, queued["task_id"])
    child = [op for op in operations_for_run if EXPERT_OPERATION in op["operation_id"]]
    assert len(child) == 1 and child[0]["state"] == "known"
    assert child[0]["run_id"] == operations_for_run[0]["run_id"]
