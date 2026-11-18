"""One explicit reasoning policy for every new DeepSeek request.

Verified against the official Chat Completions and thinking-mode guides on
2026-11-15. ``max`` is the highest supported effort; ``high`` is the default.
Output limits and the existing reserve/settle ledger remain authoritative.
"""

from typing import Any

DEEPSEEK_REASONING_POLICY = "deepseek-thinking-max-20261115-v1"


def deepseek_request_options() -> dict[str, Any]:
    # Return new objects so a caller cannot mutate the policy for later calls.
    return {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
