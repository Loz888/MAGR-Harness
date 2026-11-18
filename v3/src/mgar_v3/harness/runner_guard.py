"""Local subprocess that removes one journalled sandbox if its Runner exits."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import CommandIdentity, IsolatedExecutionBroker, issue_lease


class RunnerBroker(IsolatedExecutionBroker):
    """Local daemon calls with a cutoff short enough for the watchdog."""

    def _docker(
        self,
        args: Sequence[str],
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        host = os.environ.get("DOCKER_HOST", "")
        if host and not host.startswith("unix://"):
            raise DomainError("RUNNER_DOCKER_LOCAL", "Use a local Docker Unix socket", 403)
        result = super()._docker(args, timeout=min(timeout, 4.0))
        if (
            args[0] == "inspect"
            and result.returncode
            and not any(
                marker in result.stderr.lower()
                for marker in (b"no such object", b"no such container")
            )
        ):
            raise DomainError("RUNNER_DOCKER_UNKNOWN", "Docker inspection was inconclusive", 502)
        return result


def owner(supplied: dict[str, Any]) -> tuple[RunnerBroker, CommandIdentity]:
    identity = CommandIdentity(**supplied["identity"])
    lease = issue_lease(
        actor="runner",
        execution_epoch=identity.execution_epoch,
        mode="investigate",
        workspace_id="cleanup",
        repository="runner/cleanup",
        allowed_commands=(),
    )
    return RunnerBroker(Path(supplied["root"]), lease, image_id=supplied["image_id"]), identity


def remove(journal: Path, broker: RunnerBroker, identity: CommandIdentity) -> bool:
    # A Docker create/start request can still be unwinding when the parent's
    # pipe closes. Wait for the exact journalled name, never a broad sweep.
    deadline = time.monotonic() + 8
    observed = False
    while time.monotonic() < deadline:
        try:
            if broker.owned_command_state(identity)[0] != "missing":
                observed = True
                broker.remove_owned_command(identity)
            elif (journal / "done").is_file() or observed or not (journal / "creating").is_file():
                return True
        except (OSError, DomainError):
            pass
        time.sleep(0.15)
    return broker.owned_command_state(identity)[0] == "missing"


def main() -> int:
    # The caller and this monitor share a private local journal and pipe.
    supplied = json.loads(sys.stdin.readline())
    journal = Path(supplied["journal"])
    broker, identity = owner(supplied)
    print("ready", flush=True)
    while True:
        if (journal / "done").is_file():
            return 0
        try:
            expires = float((journal / "deadline").read_text())
        except (OSError, ValueError):
            break
        if time.monotonic() >= expires or select.select([sys.stdin], [], [], 0.1)[0]:
            break
    if remove(journal, broker, identity):
        (journal / "done").touch()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
