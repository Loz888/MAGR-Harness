"""Operator and local Runner entry points for the H15 protocol."""

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from mgar_v3.domain import DomainError
from mgar_v3.harness.client import HarnessAPIError, HarnessClient
from mgar_v3.harness.headless import _ClientSettings
from mgar_v3.harness.runner_device import DeviceClient, RunnerDirectory, run_one
from mgar_v3.settings import ROOT


def register_commands(parser: argparse.ArgumentParser) -> None:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    parser.add_argument("--state", type=Path, default=state_home / "mgar-runner")
    commands = parser.add_subparsers(dest="runner_command", required=True)
    commands.add_parser("issue-code", help="Create an owner-authorized five-minute pairing code")
    commands.add_parser("devices", help="List paired devices")
    commands.add_parser("workspaces", help="List device-owned workspaces")
    commands.add_parser("jobs", help="List Runner jobs and their execution sites")
    events = commands.add_parser("events", help="Read the Runner audit")
    events.add_argument("--after", type=int, default=0)
    revoke = commands.add_parser("revoke", help="Revoke a device and fence its jobs")
    revoke.add_argument("device_id", type=UUID)
    submit = commands.add_parser("submit", help="Run a device's fixed test plan")
    submit.add_argument("workspace_id", type=UUID)
    submit.add_argument("--client-id", type=UUID)
    pair = commands.add_parser("pair", help="Pair this device with an owner-issued code")
    pair.add_argument("--server", required=True)
    pair.add_argument("--name", required=True)
    pair.add_argument("--ca-file", type=Path)
    attach = commands.add_parser("attach", help="Bind a directory on this device")
    attach.add_argument("directory", type=Path)
    attach.add_argument("--name", required=True)
    attach.add_argument("--image-lock", type=Path, default=ROOT / ".local/r03/sandbox-lock.json")
    work = commands.add_parser("work", help="Poll the server from this device")
    work.add_argument("--once", action="store_true")


def _work(state: RunnerDirectory, once: bool) -> dict[str, object]:
    config = state.config()
    state._require_paired(config)
    synchronized = False
    with state.worker(), DeviceClient(config) as client:
        state.recover_owned_commands()
        while True:
            try:
                if not synchronized:
                    state.sync_projects(client)
                    synchronized = True
                state.flush_receipts(client)
                claimed = run_one(state, client, config)
                if once:
                    return {"device_id": str(config.device_id), "claimed": claimed}
            except httpx.RequestError:
                synchronized = False
                if once:
                    raise
            except DomainError as error:
                if once or error.status < 500 and error.code != "RUNNER_FENCED":
                    raise
            time.sleep(1.5)


def _call(args: argparse.Namespace) -> Any:
    command = args.runner_command
    if command in {"pair", "attach", "work"}:
        local = RunnerDirectory(args.state)
        if command == "pair":
            code = (
                getpass.getpass("Pairing code: ") if sys.stdin.isatty() else sys.stdin.readline()
            ).strip()
            config = local.pair(args.server, args.name, code, ca_file=args.ca_file)
            return {"device_id": str(config.device_id), "name": config.name}
        if command == "attach":
            return local.attach(args.directory, args.name, args.image_lock).model_dump(mode="json")
        return _work(local, args.once)

    token = _ClientSettings().local_token.get_secret_value()
    with HarnessClient(token, api=args.api) as client:
        if command == "submit":
            return client.queue_runner_job(
                str(args.workspace_id), client_id=str(args.client_id or uuid4())
            )
        if command == "revoke":
            return client.revoke_runner(str(args.device_id))
        if command == "events":
            return client.runner_events(after=args.after)
        available = {
            "issue-code": client.issue_runner_pairing,
            "devices": client.runner_devices,
            "workspaces": client.runner_workspaces,
            "jobs": client.runner_jobs,
        }
        return available[command]()


def run(args: argparse.Namespace) -> int:
    try:
        print(json.dumps(_call(args), ensure_ascii=False))
        return 0
    except (DomainError, HarnessAPIError) as exc:
        print(json.dumps({"code": exc.code, "message": str(exc)}), file=sys.stderr)
    except httpx.RequestError:
        print(json.dumps({"code": "RUNNER_SERVER_UNREACHABLE"}), file=sys.stderr)
    except (OSError, ValueError):
        print(json.dumps({"code": "RUNNER_LOCAL_CONFIGURATION_INVALID"}), file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 1
