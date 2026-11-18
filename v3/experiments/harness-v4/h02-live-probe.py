"""Minimal real DeepSeek Flash probe for HV4-H02.

Authorized scope: DeepSeek ``deepseek-flash`` only, hard cap USD 2 for this
card. The probe uses a synthetic workspace and prompt, never prints or stores
the credential, and does not touch Anthropic or any other provider.

The production ``harness-model-v1`` manifest stays frozen on its controlled
model entry. This probe builds an explicit Flash capability object from the
current official price observation and disables thinking mode in an
experiment-only subclass so tool-call behavior and output cost are bounded.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from mgar_v3.harness import (
    BudgetLimits,
    CapabilityGrant,
    DeepSeekChatAdapter,
    HarnessRuntime,
    HarnessToolRegistry,
    InMemoryBudgetLedger,
    InMemoryEventSink,
    LocalExecutionBroker,
    LocalVerifier,
    LocalWorkspace,
    ModelCapabilities,
    ModelPrice,
    ModelRequest,
    ModelResponse,
    SimpleContextManager,
    TaskBinding,
    ToolSpec,
    VerificationRequirement,
)
from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.model import estimate_tokens

ROOT = Path(__file__).resolve().parents[2]
CAP_MICROS = 2_000_000
RESERVATION_MICROS = 100_000
MAX_OUTPUT_TOKENS = 512
PRICE_VERSION = "DeepSeek-V4.1-Flash-1017-peak"
ENDPOINT = "https://api.deepseek.com/v1/chat/completions"

FLASH = ModelCapabilities(
    provider="deepseek",
    model_id="deepseek-flash",
    protocol="openai-chat-sse",
    context_tokens=1_048_576,
    max_output_tokens=8192,
    supports_structured_output=True,
    price=ModelPrice(
        version=PRICE_VERSION,
        input_micros_per_million=300_000,
        output_micros_per_million=1_200_000,
    ),
)


def _api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("MGAR_V3_DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("MGAR_V3_DEEPSEEK_API_KEY is not configured")


def _commit() -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()


class _FlashNoThinking(DeepSeekChatAdapter):
    """Production parser/stream with thinking disabled for a bounded probe."""

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        body = super()._body(request)
        body["thinking"] = {"type": "disabled"}
        return body


class _RecordingAdapter:
    def __init__(self, inner: DeepSeekChatAdapter) -> None:
        self.inner = inner
        self.responses: list[dict[str, Any]] = []

    def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        response = self.inner.complete(request, cancellation)
        self.responses.append(
            {
                "operation_id": request.operation_id,
                "finish_reason": response.finish_reason,
                "error_code": response.error_code,
                "usage": response.usage.model_dump(),
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
                "text_length": len(response.text),
                "text_sha256": hashlib.sha256(response.text.encode()).hexdigest(),
                "cached": response.cached,
            }
        )
        return response


def _task() -> TaskBinding:
    return TaskBinding(
        task_id="h02-live-flash",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-live",
        acceptance_revision="1",
        objective="Read calc.py with read_file, then briefly acknowledge the result.",
        requirements=(VerificationRequirement(requirement_id="live-read"),),
    )


def _grant() -> CapabilityGrant:
    return CapabilityGrant(
        actor="owner",
        workspace_id="ws-live",
        repository="example/project",
        read_paths=("",),
        write_paths=(),
        execute=False,
    )


def _tool() -> ToolSpec:
    return ToolSpec(
        name="read_file",
        description="Read one repository-relative file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


def main() -> int:
    # Hard budget guard before any paid request. Two model calls at the frozen
    # peak upper bound with 512 output tokens each must stay far below USD 2.
    tools_json = json.dumps([_tool().model_dump(mode="json")], ensure_ascii=False)
    input_bound = estimate_tokens(
        "Read calc.py with read_file, then briefly acknowledge the result."
    ) + estimate_tokens(tools_json) + 1024
    worst_case = (
        2 * input_bound * FLASH.price.input_micros_per_million
        + 2 * MAX_OUTPUT_TOKENS * FLASH.price.output_micros_per_million
        + 999_999
    ) // 1_000_000
    if worst_case > CAP_MICROS or 2 * RESERVATION_MICROS > CAP_MICROS:
        raise SystemExit(
            f"refusing paid call: worst_case={worst_case} cap={CAP_MICROS}"
        )

    cache = ROOT / ".local" / "h02-live"
    cache.mkdir(parents=True, exist_ok=True)
    workspace_root = cache / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )

    requests_seen: list[dict[str, str]] = []

    def _record_request(request: httpx.Request) -> None:
        requests_seen.append({"method": request.method, "path": request.url.path})

    client = httpx.Client(timeout=60, event_hooks={"request": [_record_request]})
    recorder = _RecordingAdapter(
        _FlashNoThinking(
            api_key=_api_key(), client=client, capabilities=FLASH, endpoint=ENDPOINT
        )
    )
    workspace = LocalWorkspace(workspace_root, workspace_id="ws-live")
    events = InMemoryEventSink()
    ledger = InMemoryBudgetLedger(
        BudgetLimits(
            max_model_calls=3,
            max_tool_calls=2,
            max_cost_micros=CAP_MICROS,
        )
    )
    broker = LocalExecutionBroker(workspace)
    runtime = HarnessRuntime(
        model=recorder,
        tools=HarnessToolRegistry(workspace, broker),
        workspace=workspace,
        broker=broker,
        context=SimpleContextManager(),
        verifier=LocalVerifier(),
        events=events,
        budgets=ledger,
        model_id=FLASH.model_id,
        price_version=FLASH.price.version,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        reservation_micros=RESERVATION_MICROS,
        max_steps=2,
    )
    task = _task()
    started = datetime.now(UTC).isoformat()
    error: dict[str, str] | None = None
    result: dict[str, Any] | None = None
    try:
        outcome = runtime.run(task, _grant())
        result = {
            "status": outcome.status,
            "tool_calls": outcome.tool_calls,
            "model_calls": outcome.model_calls,
            "unresolved": list(outcome.unresolved),
        }
    except Exception as exc:  # noqa: BLE001 - record the real failure verbatim
        error = {"type": type(exc).__name__, "message": str(exc)[:300]}

    snapshot = ledger.snapshot(task.task_id)
    reservations = [
        item.model_dump()
        for item in ledger._reservations.values()
        if item.task_id == task.task_id
    ]
    event_records = [
        {"kind": event.kind, "payload": event.payload}
        for event in events.events(task.run_id)
        if event.kind in {"model_completed", "tool_started", "tool_completed", "run_finished"}
    ]
    record = {
        "schema": "harness-h02-live-evidence-v1",
        "card": "HV4-H02",
        "authorization": {
            "approved_scope": "DeepSeek deepseek-flash only",
            "max_cost_micros_per_card": CAP_MICROS,
            "anthropic_authorized": False,
            "data": "synthetic workspace and prompt only",
        },
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "git_commit": _commit(),
        "endpoint": ENDPOINT,
        "model_id": FLASH.model_id,
        "documented_model_version": "DeepSeek-V4.1-Flash-1017",
        "protocol": FLASH.protocol,
        "price_version": FLASH.price.version,
        "price_micros_per_million": {
            "input_peak_upper": FLASH.price.input_micros_per_million,
            "output_peak_upper": FLASH.price.output_micros_per_million,
        },
        "thinking_mode": "disabled for this probe (experiment-only body field)",
        "preflight_worst_case_micros": worst_case,
        "provider_request_count": len(requests_seen),
        "provider_requests": requests_seen,
        "responses": recorder.responses,
        "result": result,
        "error": error,
        "ledger": {
            "snapshot": snapshot.model_dump(),
            "reservations": reservations,
        },
        "events": event_records,
        "live_model_gate": (
            "PASS_DEEPSEEK_FLASH"
            if result and result["status"] != "unknown" and not error
            else "FAIL"
        ),
        "anthropic_live": "NOT_RUN_NOT_AUTHORIZED",
        "credentials_recorded": False,
    }
    output = ROOT / "experiments/harness-v4/h02-live-results.json"
    output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["live_model_gate"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
