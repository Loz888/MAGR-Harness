"""Machine-readable CLI for the Harness product endpoints."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from mgar_v3.harness.client import HarnessAPIError, HarnessClient, JsonObject
from mgar_v3.settings import ROOT

_DEPENDENCY_ERRORS = {
    "HARNESS_ENVIRONMENT_REQUIRED",
    "HARNESS_ENVIRONMENT_STALE",
    "SANDBOX_IMAGE_REQUIRED",
    "PRODUCT_DEPENDENCIES_MISSING",
    "MISSING_DEPENDENCY",
    "DEPENDENCY_MISSING",
}
_INCOMPLETE = {
    "idle",
    "submitted",
    "queued",
    "preparing",
    "running",
    "cancelling",
    "pausing",
    "paused",
    "waiting_input",
    "waiting_approval",
    "incomplete",
    "budget_exhausted",
    "unknown",
    "running:start",
}
_INPUT_FIELDS = {
    "register": (("path",), ()),
    "submit": (("project_id", "text"), ("client_id",)),
    "send": (("text",), ("client_id",)),
    "answer": (("text", "reply_to", "message_id"), ()),
}


class _ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MGAR_V3_", env_file=ROOT / ".env", extra="ignore")
    local_token: SecretStr


def register_commands(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="harness_command", required=True)
    commands.add_parser("projects")
    commands.add_parser("sessions")
    general = commands.add_parser("general", help="Create, clarify, plan and inspect general tasks")
    general_commands = general.add_subparsers(dest="general_command", required=True)
    for name in ("submit", "task", "revise", "plan", "graph"):
        cmd = general_commands.add_parser(name)
        if name != "submit":
            cmd.add_argument("resource_id")
        if name in ("submit", "revise", "plan"):
            cmd.add_argument(
                "--input", type=Path, default=Path("-"), help="JSON file or - for stdin"
            )
    for name in ("register", "submit", "send", "answer"):
        cmd = commands.add_parser(name)
        if name in ("send", "answer"):
            cmd.add_argument("resource_id")
        cmd.add_argument("--input", type=Path, default=Path("-"), help="JSON file or - for stdin")
    for name in (
        "readiness",
        "session",
        "task",
        "pause",
        "resume",
        "cancel",
        "messages",
        "events",
        "event-page",
        "artifacts",
        "diff",
        "patch",
        "export",
    ):
        cmd = commands.add_parser(name)
        cmd.add_argument("resource_id")
        if name == "cancel":
            cmd.add_argument("--reason", default="")
        if name in ("events", "event-page"):
            cmd.add_argument("--after", type=int, default=0)
        if name == "events":
            cmd.add_argument("--follow", action="store_true")
        if name == "event-page":
            cmd.add_argument("--limit", type=int, default=200)
        if name == "patch":
            cmd.add_argument("--output", type=Path, required=True, help="New patch file")


def _input(name: str, path: Path) -> dict[str, str]:
    raw = sys.stdin.read() if path == Path("-") else path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    required, optional = _INPUT_FIELDS[name]
    if (
        not isinstance(payload, dict)
        or not set(required) <= payload.keys() <= set(required + optional)
        or any(not isinstance(v, str) for v in payload.values())
    ):
        raise ValueError("Invalid JSON input fields for this Harness command")
    return {key: value for key, value in payload.items()}


def _print(value: Any, *, error: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False), file=sys.stderr if error else sys.stdout, flush=True
    )


def _status(result: JsonObject) -> int:
    if result.get("reason") in _DEPENDENCY_ERRORS or result.get("status") == "waiting_environment":
        return 3
    state = result.get("status")
    if state == "completed":
        return 0
    if state in _INCOMPLETE:
        return 2
    return 1


def _general(client: HarnessClient, args: argparse.Namespace) -> JsonObject:
    name = args.general_command
    if name == "task":
        return client.general_task(args.resource_id)
    if name == "graph":
        return client.general_task_graph(args.resource_id)
    raw = sys.stdin.read() if args.input == Path("-") else args.input.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected a General Task JSON request object")
    if name == "submit":
        return client.create_general_task(payload)
    if name == "revise":
        return client.revise_general_task(args.resource_id, payload)
    return client.plan_general_task(args.resource_id, payload)


def _call(client: HarnessClient, args: argparse.Namespace) -> int:
    name = args.harness_command
    resource = getattr(args, "resource_id", "")
    if name == "general":
        _print(_general(client, args))
        return 0
    if name in _INPUT_FIELDS:
        payload = _input(name, args.input)
        if name == "register":
            result = client.register_project(payload["path"])
        elif name == "submit":
            result = client.submit(
                payload["project_id"], payload["text"], client_id=payload.get("client_id", "")
            )
        elif name == "send":
            result = client.send(resource, payload["text"], client_id=payload.get("client_id", ""))
        else:
            result = client.answer(
                resource,
                payload["text"],
                reply_to=payload["reply_to"],
                message_id=payload["message_id"],
            )
        status_source = result.get("submission", result)
        if name in {"submit", "send"} and not isinstance(status_source, dict):
            raise HarnessAPIError("INVALID_API_RESPONSE", "Expected submitted turn status")
        _print(result)
        return _status(status_source) if name in {"submit", "send"} else 0

    if name == "events":
        for frame in client.events(resource, after=args.after, follow=args.follow):
            _print(frame)
        return 0
    if name == "event-page":
        _print(client.event_page(resource, after=args.after, limit=args.limit))
        return 0
    if name == "patch":
        content = client.patch(resource)
        with args.output.open("xb") as stream:
            stream.write(content)
        _print({"task_id": resource, "path": str(args.output), "bytes": len(content)})
        return 0
    if name in ("projects", "sessions"):
        _print(client.projects() if name == "projects" else client.sessions())
        return 0

    match name:
        case "readiness":
            readiness = client.readiness(resource)
            environment = readiness.get("environment")
            if not isinstance(environment, dict) or environment.get("status") not in {
                "ready",
                "waiting_environment",
            }:
                raise HarnessAPIError("INVALID_API_RESPONSE", "Expected project environment status")
            _print(readiness)
            return 0 if environment["status"] == "ready" else 3
        case "session" | "task":
            state = client.session(resource) if name == "session" else client.task(resource)
            _print(state)
            return _status(state)
        case "cancel":
            _print(client.cancel(resource, reason=args.reason))
        case "pause":
            _print(client.pause(resource))
        case "resume":
            _print(client.resume(resource))
        case "messages":
            _print(client.messages(resource))
        case "artifacts":
            _print(client.artifacts(resource))
        case "diff":
            _print({"task_id": resource, "diff": client.diff(resource)})
        case "export":
            _print(client.export_session(resource))
    return 0


def run(args: argparse.Namespace) -> int:
    try:
        token = _ClientSettings().local_token.get_secret_value()
        with HarnessClient(token, api=args.api) as client:
            return _call(client, args)
    except ValidationError:
        _print(
            {"code": "OPERATOR_TOKEN_REQUIRED", "message": "Set MGAR_V3_LOCAL_TOKEN."}, error=True
        )
    except HarnessAPIError as exc:
        _print(
            {
                "code": exc.code,
                "message": str(exc),
                "status_code": exc.status_code,
                "request_id": exc.request_id,
            },
            error=True,
        )
        if exc.code in _DEPENDENCY_ERRORS:
            return 3
        if exc.code == "EVENT_STREAM_INTERRUPTED":
            return 2
    except httpx.RequestError:
        _print(
            {"code": "API_TRANSPORT_ERROR", "message": "Harness API request was interrupted."},
            error=True,
        )
        if args.harness_command == "events":
            return 2
    except (ValueError, OSError) as exc:
        _print({"code": "HEADLESS_INPUT_ERROR", "message": str(exc)}, error=True)
    except KeyboardInterrupt:
        return 130
    return 1
