"""Current product model wire and conservative cost bounds, without paid calls."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    CancellationToken,
    ModelMessage,
    ModelRequest,
    TaskBinding,
    ToolSpec,
    VerificationRequirement,
)
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.product_models import (
    DEFAULT_PRODUCT_MODEL,
    PRODUCT_MODEL_SOURCES,
    PRODUCT_PRICE_VERSION,
    ProductDeepSeekChatAdapter,
    product_model_capabilities,
    product_model_reservation,
    product_request_reservation,
)
from mgar_v3.repository_analysis.domain import AcceptanceDecision, AnalysisModelConfig
from mgar_v3.repository_analysis.model import DeepSeekAnalysisModel
from mgar_v3.web_access import (
    AnalysisSettingsInput,
    BrowserSession,
    LoginInput,
    WebAccounts,
    WebAnalysisSettings,
)


def request(model_id=DEFAULT_PRODUCT_MODEL, *, messages=None, tools=None, reservation=10000000):
    capabilities = product_model_capabilities(model_id)
    return ModelRequest(
        operation_id="product-model-1",
        task=TaskBinding(
            task_id="product-model-task",
            thread_id=uuid4(),
            run_id=uuid4(),
            execution_epoch=1,
            actor="owner",
            repository="example/project",
            base_commit="a" * 40,
            workspace_id="workspace",
            acceptance_revision="product-v1",
            objective="Read the repo",
            requirements=(VerificationRequirement(requirement_id="evidence"),),
        ),
        messages=messages or (ModelMessage(role="user", content="Read README.md"),),
        tools=tools
        if tools is not None
        else (
            ToolSpec(
                name="github__read_file",
                description="Read a granted immutable GitHub file",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        ),
        model_id=capabilities.model_id,
        price_version=capabilities.price.version,
        max_output_tokens=8192,
        reservation_micros=reservation,
        context_revision="product-context-v1",
        workspace_tree="b" * 64,
    )


def sse(*, tool=False, prompt_tokens=1000, output_tokens=200):
    delta = {"reasoning_content": "PRIVATE_PROVIDER_REASONING_NOT_FOR_HISTORY"}
    if tool:
        delta["tool_calls"] = [
            {
                "index": 0,
                "id": "call_ext",
                "type": "function",
                "function": {"name": "github__read_file", "arguments": '{"path":"README.md"}'},
            }
        ]
    else:
        delta["content"] = "Read the granted file."
    chunks = [
        {"choices": [{"delta": delta}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": output_tokens},
        },
    ]
    return (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()


def test_default_catalog_uses_verified_peak_prices_and_bounded_harness_output():
    flash, pro = product_model_capabilities(), product_model_capabilities("deepseek-v4-pro")
    assert flash.model_id == DEFAULT_PRODUCT_MODEL == "deepseek-flash"
    assert flash.price.input_micros_per_million == 300000
    assert flash.price.output_micros_per_million == 1200000
    assert pro.price.input_micros_per_million == 1320000
    assert pro.price.output_micros_per_million == 3960000
    assert flash.context_tokens == pro.context_tokens == 1048576
    assert flash.max_output_tokens == pro.max_output_tokens == 8192
    assert flash.price.version == pro.price.version == PRODUCT_PRICE_VERSION
    assert "peak-cache-miss" in PRODUCT_PRICE_VERSION
    assert PRODUCT_MODEL_SOURCES == (
        "https://api-docs.deepseek.com/quick_start/pricing/",
        "https://api-docs.deepseek.com/guides/thinking_mode/",
        "https://api-docs.deepseek.com/api/create-chat-completion/",
    )


@pytest.mark.parametrize("alias", ["deepseek-v4-flash", "deepseek-v4-flash-vision-exp"])
def test_accepted_flash_aliases_resolve_to_current_model_and_price(alias):
    assert product_model_capabilities(alias) == product_model_capabilities("deepseek-flash")


@pytest.mark.parametrize(
    "model_id,code",
    [
        ("deepseek-chat", "HARNESS_MODEL_RETIRED"),
        ("deepseek-reasoner", "HARNESS_MODEL_RETIRED"),
        ("unknown-model", "HARNESS_MODEL_UNSUPPORTED"),
        ("DEEPSEEK-FLASH", "HARNESS_MODEL_UNSUPPORTED"),
    ],
)
def test_retired_or_unpriced_models_are_actionably_rejected(model_id, code):
    with pytest.raises(DomainError) as error:
        product_model_capabilities(model_id)
    assert error.value.code == code


def test_product_catalog_does_not_mutate_historical_h12_capabilities():
    old = DEEPSEEK_CHAT.model_dump(mode="json")
    product_model_capabilities("deepseek-v4-pro")
    assert DEEPSEEK_CHAT.model_dump(mode="json") == old
    assert DEEPSEEK_CHAT.model_id == "deepseek-chat"
    assert DEEPSEEK_CHAT.context_tokens == 131072


def test_pro_quotes_use_pro_prices_and_round_up_without_float():
    flash, pro = product_model_capabilities(), product_model_capabilities("deepseek-v4-pro")
    assert product_model_reservation(flash, max_input_tokens=1000, max_output_tokens=200) == 540
    assert product_model_reservation(pro, max_input_tokens=1000, max_output_tokens=200) == 2112
    assert product_model_reservation(pro, max_input_tokens=1, max_output_tokens=1) == 6
    assert product_request_reservation(request("deepseek-v4-pro"), pro) > (
        product_request_reservation(request(), flash)
    )


def test_wire_enables_max_thinking_and_preserves_native_tool_call_history():
    sent = []

    def handler(wire):
        payload = json.loads(wire.content)
        sent.append(payload)
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "max"
        assert payload["model"] == "deepseek-flash"
        assert payload["tools"][0]["function"]["name"] == "github__read_file"
        if len(sent) == 1:
            return httpx.Response(200, content=sse(tool=True))
        assistant, tool_message = payload["messages"][-2:]
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["function"]["name"] == "github__read_file"
        assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'
        assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_ext"
        assert assistant["reasoning_content"] == "PRIVATE_PROVIDER_REASONING_NOT_FOR_HISTORY"
        return httpx.Response(200, content=sse())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ProductDeepSeekChatAdapter(api_key="fixture-key", client=client)
    initial = request()
    first = adapter.complete(initial, CancellationToken())
    assert first.finish_reason == "tool_calls"
    followup = initial.model_copy(
        update={
            "operation_id": "product-model-2",
            "messages": (
                *initial.messages,
                ModelMessage(role="assistant", content=first.text, tool_calls=first.tool_calls,
                             provider_context=first.provider_context),
                ModelMessage(
                    role="tool", content="Authorized README content", tool_call_id="call_ext"
                ),
            ),
        }
    )
    final = adapter.complete(followup, CancellationToken())
    assert final.finish_reason == "stop" and final.usage.cost_micros == 540
    assert "PRIVATE_PROVIDER_REASONING" not in first.model_dump_json() + final.model_dump_json()
    assert len(sent) == 2


def test_pro_sse_usage_is_conservatively_priced_without_old_flash_rates():
    capabilities = product_model_capabilities("deepseek-v4-pro")
    adapter = ProductDeepSeekChatAdapter(
        api_key="fixture-key",
        capabilities=capabilities,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda wire: httpx.Response(200, content=sse()))
        ),
    )
    result = adapter.complete(request("deepseek-v4-pro"), CancellationToken())
    assert result.usage.known and result.usage.cost_micros == 2112


def test_insufficient_reservation_is_rejected_before_any_http_request():
    sent = []
    capabilities = product_model_capabilities("deepseek-v4-pro")
    adapter = ProductDeepSeekChatAdapter(
        api_key="fixture-key",
        capabilities=capabilities,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda wire: sent.append(wire) or httpx.Response(200, content=sse())
            )
        ),
    )
    with pytest.raises(DomainError) as error:
        adapter.complete(request("deepseek-v4-pro", reservation=1), CancellationToken())
    assert error.value.code == "MODEL_RESERVATION_REQUIRED" and not sent


def test_chinese_reference_material_is_quoted_by_bytes_before_dispatch():
    capabilities = product_model_capabilities()
    english = request(messages=(ModelMessage(role="user", content="a" * 1000),))
    chinese = request(messages=(ModelMessage(role="user", content="中" * 1000),))
    assert (
        product_request_reservation(chinese, capabilities)
        - product_request_reservation(english, capabilities)
        == 600
    )


def test_invalid_provider_tool_names_are_rejected_before_http():
    sent = []
    adapter = ProductDeepSeekChatAdapter(
        api_key="fixture-key",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda wire: sent.append(wire) or httpx.Response(200, content=sse())
            )
        ),
    )
    invalid = request(
        tools=(ToolSpec(name="github.read_file", description="Invalid provider name"),)
    )
    with pytest.raises(DomainError) as error:
        adapter.complete(invalid, CancellationToken())
    assert error.value.code == "MODEL_TOOL_NAME_INVALID" and not sent


@pytest.mark.parametrize("inputs,outputs", [(1048576, 8192), (-1, 1), (0, 8193), (1, 0)])
def test_quotes_reject_out_of_context_or_unbounded_output(inputs, outputs):
    with pytest.raises(DomainError) as error:
        product_model_reservation(
            product_model_capabilities(), max_input_tokens=inputs, max_output_tokens=outputs
        )
    assert error.value.code == "MODEL_CONTEXT_EXCEEDED"


@pytest.mark.parametrize("model_id", ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat"])
def test_shared_analysis_wire_enables_max_thinking_for_every_model(model_id, settings):
    current = model_id != "deepseek-chat"
    if current:
        capabilities = product_model_capabilities(model_id)
        config = AnalysisModelConfig(
            model_id=capabilities.model_id,
            price_version=capabilities.price.version,
            input_micros_per_million=capabilities.price.input_micros_per_million,
            output_micros_per_million=capabilities.price.output_micros_per_million,
            context_tokens=capabilities.context_tokens,
            max_output_tokens=capabilities.max_output_tokens,
        )
    else:
        config = AnalysisModelConfig()
    settings.deepseek_api_key = SecretStr("fixture-shared-model-key")
    sent = []

    class Ledger:
        accounts_usage = True

        def __init__(self):
            self.held, self.costs = [], []

        def repository_model_result(self, *args):
            return None

        def record_model_input(self, *args):
            pass

        def record_model_response(self, *args):
            pass

        def reserve_repository_call(self, *args, **kwargs):
            self.held.append(args[4])
            return uuid4()

        def settle_call(self, call_id, cost, *args):
            self.costs.append(cost)

    def handle(wire):
        payload = json.loads(wire.content)
        sent.append(payload)
        assert payload["model"] == model_id
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "max"
        return httpx.Response(
            200,
            json={
                "model": model_id,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "accepted": True,
                                    "summary": "The controlled answer covers the question.",
                                }
                            )
                        },
                    }
                ],
            },
        )

    ledger = Ledger()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = DeepSeekAnalysisModel(
            settings, config, client=client, rate_limit=lambda *args: None
        )
        result = model.complete(
            ledger,
            uuid4(),
            1,
            "acceptance",
            "profile",
            "Check the answer",
            {"question": "Explain project"},
            AcceptanceDecision,
            call_limit=1,
            deadline_check=lambda: None,
        )
    assert result.accepted and len(sent) == 1
    assert ledger.costs == [212 if model_id == "deepseek-v4-pro" else 54]
    assert ledger.held[0] >= ledger.costs[0]


def test_real_pg_shared_pro_authorization_matches_product_prices_and_preserves_old_rows(
    engine,
    settings,
    tmp_path,
):
    from mgar_v3.harness.product import HarnessProductService
    from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    user_id = WebAccounts(engine).create_owner(
        LoginInput(
            username="model-owner",
            password=SecretStr("fixture-password"),
        )
    )
    user = BrowserSession(user_id=user_id, username="model-owner", csrf_token="fixture-csrf")
    web = WebAnalysisSettings(engine, settings)
    web.save(
        AnalysisSettingsInput(model_id="deepseek-chat", allow_paid=False, expected_revision=0), user
    )
    legacy_revision, legacy = web.snapshot()
    assert legacy["authorization"]["model"]["price_version"] == "repository-analysis-v1"
    web.save(
        AnalysisSettingsInput(
            model_id="deepseek-v4-pro",
            allow_paid=True,
            expected_revision=legacy_revision,
            deepseek_api_key=SecretStr("fixture-shared-pro-key"),
        ),
        user,
    )
    revision, payload = web.snapshot()
    analysis = RepositoryAnalysisService(engine, settings).authorization().authorization.model
    capabilities = product_model_capabilities("deepseek-v4-pro")
    assert analysis.model_id == "deepseek-v4-pro"
    assert (
        analysis.input_micros_per_million == capabilities.price.input_micros_per_million == 1320000
    )
    assert (
        analysis.output_micros_per_million
        == capabilities.price.output_micros_per_million
        == 3960000
    )
    assert analysis.price_version == capabilities.price.version
    assert analysis.context_tokens == capabilities.context_tokens
    assert analysis.max_output_tokens == capabilities.max_output_tokens

    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("Shared pricing probe\n")
    harness = HarnessCodingService(
        settings.harness_root, actor="owner", engine=engine, image_lock=settings.harness_image_lock
    )
    product = HarnessProductService(harness, web)
    try:
        project_id = harness.register_project(str(root))["project_id"]
        grant = product.authorize(
            project_id,
            allow_write=False,
            allow_paid=True,
            max_cost_micros=250000,
            max_model_calls=8,
        )
        _, product_capabilities = product.resolve_model(
            CodingTaskRequest(
                project_id=project_id,
                objective="解释项目",
                task_kind="inspect",
                mode="live",
                product_session_id="hs-" + "a" * 24,
                product_model_revision=revision,
                product_authorization_revision=grant["revision"],
            )
        )
        assert product_capabilities == capabilities
        assert product._model_snapshot(legacy_revision)[1] == legacy
        with engine.transaction() as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM app.web_analysis_settings").fetchone()["n"]
                == 2
            )
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
        assert payload["authorization"]["model"]["price_version"] == PRODUCT_PRICE_VERSION
    finally:
        product.close()
