"""Per-request context policy. It never increases cumulative authorization."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model


def message_input_bound(messages: Sequence[dict[str, Any]]) -> int:
    """Same conservative UTF-8 bound used by the analysis Adapter/ledger."""
    return sum(len(message["content"].encode("utf-8")) for message in messages) + 1024


def analysis_input_bound(system: str, payload: dict[str, Any]) -> int:
    return message_input_bound(
        [
            {"content": system},
            {"content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        ]
    )


class ContextPolicy(Model):
    mode: Literal["auto", "manual"] = "auto"
    window_tokens: int | None = Field(default=None, strict=True, ge=4096, le=1048576)
    prepare_ratio: float = Field(default=0.65, ge=0.1, le=0.9)
    force_ratio: float = Field(default=0.85, ge=0.2, le=0.95)
    target_ratio: float = Field(default=0.50, ge=0.1, le=0.8)
    reserve_tokens: int = Field(default=1024, strict=True, ge=1024, le=16384)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.target_ratio < self.prepare_ratio < self.force_ratio:
            raise ValueError("压缩目标必须小于准备阈值，准备阈值必须小于强制阈值")
        if (self.mode == "manual") != (self.window_tokens is not None):
            raise ValueError("自定义模式需要窗口大小；自动模式跟随模型能力")
        return self

    def window(self, api_tokens: int, output_tokens: int) -> int:
        selected = self.window_tokens if self.mode == "manual" else api_tokens
        assert selected is not None
        if selected > api_tokens:
            raise DomainError("CONTEXT_POLICY_INVALID", "配置窗口超过此模型已声明的API能力。", 422)
        if selected - output_tokens - self.reserve_tokens < 2048:
            raise DomainError("CONTEXT_POLICY_INVALID", "窗口不足以容纳输出预留和输入。", 422)
        return selected

    def input_limit(self, api_tokens: int, output_tokens: int) -> int:
        return self.window(api_tokens, output_tokens) - output_tokens - self.reserve_tokens

    def pressure(self, observed: int, limit: int) -> tuple[str, int]:
        if observed >= limit * self.force_ratio:
            return "forced", max(1, int(limit * self.target_ratio))
        if observed >= limit * self.prepare_ratio:
            return "prepare", max(1, int(limit * self.target_ratio))
        return "normal", limit
