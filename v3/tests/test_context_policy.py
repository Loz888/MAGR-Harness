"""Adaptive context uses the same request bound as the Adapter; no paid transport."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_analysis_streaming import Files, final_decision, run
from test_harness_product_service import product_env as product_env
from test_v32_repository_analysis import FakeStore, runtime, spec

from mgar_v3.context.policy import ContextPolicy, analysis_input_bound, message_input_bound
from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import HarnessToolCall, ModelMessage
from mgar_v3.repository_analysis.agent import RepoAnalysisAgent
from mgar_v3.repository_analysis.domain import (
    AcceptanceDecision,
    AgentDecision,
    AnalysisLimits,
    AnalysisModelConfig,
)
from mgar_v3.web_access import AnalysisSettingsInput


@pytest.mark.parametrize(
    "policy",
    [
        {"prepare_ratio": 0.85, "force_ratio": 0.65},
        {"target_ratio": 0.7},
        {"mode": "manual"},
        {"window_tokens": 32768},
        {"force_ratio": 1},
        {"reserve_tokens": 0},
    ],
)
def test_invalid_policy_does_not_silently_expand_context(policy):
    with pytest.raises(ValidationError):
        ContextPolicy(**policy)


def test_thresholds_and_provider_window_are_separate_from_authorization():
    policy = ContextPolicy()
    assert policy.pressure(6499, 10000) == ("normal", 10000)
    assert policy.pressure(6500, 10000) == ("prepare", 5000)
    assert policy.pressure(8500, 10000) == ("forced", 5000)
    assert policy.input_limit(131072, 8192) == 121856
    manual = ContextPolicy(mode="manual", window_tokens=32768)
    assert manual.input_limit(131072, 8192) == 23552
    for api, output in [(16384, 2048), (32768, 32000)]:
        with pytest.raises(DomainError) as error:
            manual.input_limit(api, output)
        assert error.value.code == "CONTEXT_POLICY_INVALID"


def history(count):
    return [
        {
            "tool": "read_file",
            "reference": f"github://octo/example/src/{i}.py",
            "content": '中文源码🙂 and quoted text "\n' * 200,
            "evidence_id": f"evidence-{i}",
            "source_version": "a" * 40,
            "artifact_hash": f"{i:064x}",
            "truncated": False,
        }
        for i in range(count)
    ]


def test_eight_tool_reproduction_compacts_to_real_adapter_bound_without_resetting_budget():
    value = spec(
        AnalysisLimits(
            max_model_calls=None,
            max_tool_calls=None,
            max_input_tokens=60000,
            max_total_tokens=60000,
        )
    )
    value = value.model_copy(
        update={
            "input_tokens_used": 28917,
            "output_tokens_used": 326,
            "model": AnalysisModelConfig(context_tokens=1048576, max_output_tokens=8192),
        }
    )
    store = FakeStore(value)
    agent = RepoAnalysisAgent(runtime(store, None, None))
    state = {"history": history(8), "round": 5, "run_id": str(value.run_id)}
    raw = agent._payload(value, state, 5)
    assert analysis_input_bound("system", raw) > 60000 - 28917
    prepared = agent._prepared_payload(value, state, 5)
    bound = analysis_input_bound("system", prepared)
    assert bound <= 60000 - 28917 - 326 - 8192
    assert prepared["context_policy"]["stage"] == "forced"
    assert {row["evidence_id"] for row in state["history"]} <= set(
        prepared["compaction_summary"]["evidence_ids"]
    )
    assert prepared["permissions"] == raw["permissions"]
    assert prepared["available_tools"] == raw["available_tools"]
    assert store.value.input_tokens_used == 28917
    assert store.value.limits.max_input_tokens == 60000
    messages = [
        {"content": "system"},
        {"content": json.dumps(prepared, ensure_ascii=False, separators=(",", ":"))},
    ]
    assert message_input_bound(messages) == bound


def test_long_analysis_rebuilds_from_full_history_and_completes_after_sixteen_tools():
    value = spec(
        AnalysisLimits(
            max_model_calls=None,
            max_tool_calls=None,
            max_input_tokens=500000,
            max_total_tokens=500000,
        )
    )
    value = value.model_copy(
        update={
            "model": AnalysisModelConfig(
                context_policy=ContextPolicy(mode="manual", window_tokens=32768)
            )
        }
    )
    store = FakeStore(value)
    stages = []

    class LongFiles(Files):
        async def execute(self, call):
            result = await super().execute(call)
            text = result.text + "# source details\n" * 350
            content = text.encode()
            version = hashlib.sha1(
                b"blob " + str(len(content)).encode() + b"\0" + content
            ).hexdigest()
            return replace(result, text=text, content=content, source_version=version)

    class Model:
        def complete(self, ledger, run_id, epoch, step, profile, system, payload, schema, **kw):
            bound = analysis_input_bound(system, payload)
            assert bound <= 32768 - 2048 - 1024
            if schema is AcceptanceDecision:
                return AcceptanceDecision(accepted=True, summary="Controlled report is supported")
            stages.append(payload["context_policy"]["stage"])
            ledger.value = ledger.value.model_copy(
                update={
                    "input_tokens_used": ledger.value.input_tokens_used + bound // 3,
                    "output_tokens_used": ledger.value.output_tokens_used + 100,
                    "model_calls_used": ledger.value.model_calls_used + 1,
                }
            )
            if len(stages) <= 16:
                return AgentDecision.model_validate(
                    {
                        "action": "tool",
                        "thought": f"Inspect distinct source {len(stages)}",
                        "tool_calls": [{"tool": "read_file", "path": f"src/f{len(stages)}.py"}],
                    }
                )
            return final_decision(ledger)

    files = LongFiles()
    run(store, Model(), files)
    assert store.success is not None and store.partial is None, (store.partial, store.failed)
    assert len(files.reads) == 16 and len(stages) == 17
    assert "forced" in stages or "prepare" in stages
    assert store.value.input_tokens_used < 500000


def test_true_cumulative_exhaustion_stops_without_calling_model():
    value = spec().model_copy(update={"input_tokens_used": 59999})
    agent = RepoAnalysisAgent(runtime(FakeStore(value), None, None))
    with pytest.raises(DomainError) as error:
        agent._prepared_payload(value, {"history": history(8)}, 5)
    assert error.value.code == "INPUT_TOKEN_BUDGET"


def test_coding_thresholds_keep_tool_pairs_and_full_recoverable_state(tmp_path):
    from test_harness_v4_h10 import _context_fixture

    context, task, state = _context_fixture(tmp_path)
    context.max_tokens = 32000
    context.policy = ContextPolicy()
    messages = []
    stages = set()
    for number in range(24):
        call_id = f"read-{number}"
        messages.extend(
            [
                ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        HarnessToolCall(
                            call_id=call_id, name="read_file", arguments={"path": "source.py"}
                        ),
                    ),
                ),
                ModelMessage(
                    role="tool",
                    name="read_file",
                    tool_call_id=call_id,
                    content="bounded original source\n" * 180,
                ),
            ]
        )
        state = state.model_copy(update={"sequence": number + 2})
        view = context.prepare(task, (), messages, state, overhead_tokens=1000)
        messages = list(view.messages)
        stages.add(context.metrics[-1].policy_stage)
        assert view.estimated_tokens <= 32000
        assert context.last_state == state
        latest = [m for m in messages if m.role == "tool"][-1]
        assert latest.tool_call_id == call_id
    assert stages & {"prepare", "forced"}
    assert context.compaction_count >= 2
    restored = context.load(context.state_path, state)
    assert restored.working_state == state
    assert restored.working_state.permissions == state.permissions


def test_pg_policy_is_frozen_per_turn_and_does_not_change_shared_budget(product_env):
    env = product_env
    env.save_model()
    env.authorize(allow_write=False)
    revision, old_payload = env.web.snapshot()
    policy = ContextPolicy(mode="manual", window_tokens=65536)
    env.web.save(
        AnalysisSettingsInput(
            model_id="deepseek-flash",
            allow_paid=True,
            expected_revision=revision,
            context_policy=policy,
        ),
        env.user,
    )
    session = env.product.create(env.project_id, "只读分析项目", client_id="context-one")
    assert session["status"] == "completed"
    request = env.requests[-1]
    assert request.product_context_policy == policy.model_dump(mode="json")
    env.product.resolve_model(request)
    with pytest.raises(DomainError) as error:
        env.product.resolve_model(
            replace(request, product_context_policy=ContextPolicy().model_dump())
        )
    assert error.value.code == "HARNESS_CONTEXT_POLICY_CHANGED"
    assert env.product._model_snapshot(revision)[1] == old_payload
    grant = env.product.authorization(env.project_id)
    assert grant["max_cost_micros"] == 250000 and grant["max_model_calls"] == 32
    assert "context_policy" not in AnalysisModelConfig().model_dump()
