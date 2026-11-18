"""Product-only current DeepSeek catalog; frozen H12 capabilities stay intact.

Sources verified 2026-11-12:
https://api-docs.deepseek.com/quick_start/pricing/
https://api-docs.deepseek.com/guides/thinking_mode/
https://api-docs.deepseek.com/api/create-chat-completion/

Prices use peak, cache-miss rates as conservative upper bounds. Off-peak and
cached requests may cost less; these figures are not a provider invoice.
"""

from __future__ import annotations

import json
import re

import httpx

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import ModelRequest
from mgar_v3.harness.model import DeepSeekChatAdapter, ModelCapabilities, ModelPrice
from mgar_v3.harness.provider_context import provider_context_bytes

DEFAULT_PRODUCT_MODEL = "deepseek-flash"
PRODUCT_PRICE_VERSION = "deepseek-product-2026-11-12-peak-cache-miss-v1"
PRODUCT_MODEL_SOURCES = (
    "https://api-docs.deepseek.com/quick_start/pricing/",
    "https://api-docs.deepseek.com/guides/thinking_mode/",
    "https://api-docs.deepseek.com/api/create-chat-completion/",
)
_ALIASES = {
    "deepseek-v4-flash": DEFAULT_PRODUCT_MODEL,
    "deepseek-v4-flash-vision-exp": DEFAULT_PRODUCT_MODEL,
}
_PRICES = {DEFAULT_PRODUCT_MODEL: (300000, 1200000), "deepseek-v4-pro": (1320000, 3960000)}
PRODUCT_MODEL_IDS = (*_PRICES, *_ALIASES)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


def product_model_capabilities(model_id: str = DEFAULT_PRODUCT_MODEL) -> ModelCapabilities:
    if model_id in {"deepseek-chat", "deepseek-reasoner"}:
        raise DomainError(
            "HARNESS_MODEL_RETIRED",
            "该旧模型已退役，请在模型设置中选择 deepseek-flash 或 deepseek-v4-pro。",
            409,
        )
    canonical = _ALIASES.get(model_id, model_id)
    if canonical not in _PRICES:
        raise DomainError(
            "HARNESS_MODEL_UNSUPPORTED", "模型尚无已核对的能力与价格，请选择受支持模型。", 409
        )
    input_price, output_price = _PRICES[canonical]
    return ModelCapabilities(
        provider="deepseek",
        model_id=canonical,
        protocol="openai-chat-sse",
        context_tokens=1048576,
        max_output_tokens=8192,
        supports_structured_output=True,
        thinking="enabled",
        reasoning_effort="max",
        price=ModelPrice(
            version=PRODUCT_PRICE_VERSION,
            input_micros_per_million=input_price,
            output_micros_per_million=output_price,
        ),
    )


def product_model_reservation(
    capabilities: ModelCapabilities, *, max_input_tokens: int, max_output_tokens: int
) -> int:
    """Quote a request bound for the existing reserve/settle ledger."""
    if (
        max_input_tokens < 0
        or not 1 <= max_output_tokens <= capabilities.max_output_tokens
        or max_input_tokens + max_output_tokens > capabilities.context_tokens
    ):
        raise DomainError("MODEL_CONTEXT_EXCEEDED", "请压缩上下文或降低本轮输出上限。", 413)
    return (
        max_input_tokens * capabilities.price.input_micros_per_million
        + max_output_tokens * capabilities.price.output_micros_per_million
        + 999999
    ) // 1000000


def product_request_input_bound(request: ModelRequest) -> int:
    """Count serialized UTF-8 bytes as a conservative token upper bound.

    This avoids treating four Chinese characters as one token. Wrapper/role
    overhead is bounded separately; the product loop must reserve this amount
    before dispatch, and the adapter independently checks the reservation.
    """
    encoded = json.dumps(
        {
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "tools": [tool.model_dump(mode="json") for tool in request.tools],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return len(encoded) + provider_context_bytes(request.messages) + 1024


def product_request_reservation(request: ModelRequest, capabilities: ModelCapabilities) -> int:
    return product_model_reservation(
        capabilities,
        max_input_tokens=product_request_input_bound(request),
        max_output_tokens=request.max_output_tokens,
    )


class ProductDeepSeekChatAdapter(DeepSeekChatAdapter):
    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.Client | None = None,
        capabilities: ModelCapabilities | None = None,
        endpoint: str = "https://api.deepseek.com/v1/chat/completions",
        max_attempts: int = 2,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            client=client,
            capabilities=capabilities or product_model_capabilities(),
            endpoint=endpoint,
            max_attempts=max_attempts,
            artifacts=artifacts,
        )

    def _validate(self, request: ModelRequest) -> None:
        super()._validate(request)
        if request.reservation_micros < product_request_reservation(request, self.capabilities):
            raise DomainError(
                "MODEL_RESERVATION_REQUIRED", "本轮模型调用尚未获得足额预算预占。", 403
            )
        if any(_TOOL_NAME.fullmatch(tool.name) is None for tool in request.tools):
            raise DomainError("MODEL_TOOL_NAME_INVALID", "模型工具名称不符合供应商协议。", 422)
