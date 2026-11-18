"""Test-local Docker ownership checks; never sweep or remove another task's containers."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from mgar_v3.harness.broker import IsolatedExecutionBroker

_INSPECT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}},'
    '"image":{{json .Image}},"created":{{json .Created}},'
    '"running":{{json .State.Running}},"status":{{json .State.Status}}}'
)
Docker = Callable[..., CompletedProcess[bytes]]


@dataclass(frozen=True)
class OwnedContainer:
    container_id: str
    name: str
    broker: IsolatedExecutionBroker
    docker: Docker


class DockerOwnership:
    def __init__(self, workspace_parent: Path) -> None:
        self.workspace_parent = workspace_parent.resolve()
        self.created: list[OwnedContainer] = []
        self.external_changes: list[dict[str, Any]] = []
        self._external: dict[str, dict[str, Any]] | None = None
        self._bound: set[int] = set()
        self._lock = threading.Lock()
        self.checked = False

    @staticmethod
    def _inspect(docker: Docker, identifier: str) -> dict[str, Any] | None:
        result = docker(["inspect", "--format", _INSPECT, identifier])
        if result.returncode == 0:
            value = json.loads(result.stdout)
            if re.fullmatch(r"[a-f0-9]{64}", identifier):
                assert value["id"] == identifier, "Docker returned a different container ID"
            else:
                assert value["name"] == "/" + identifier, "Docker returned a different name"
            return value
        error = result.stderr.decode("utf-8", "replace").strip()
        missing = {
            f"error: no such object: {identifier}".casefold(),
            f"error: no such container: {identifier}".casefold(),
            f"error response from daemon: no such container: {identifier}".casefold(),
        }
        if result.returncode == 1 and not result.stdout.strip() and error.casefold() in missing:
            return None
        raise AssertionError(f"Docker inspect failed, rather than proving absence: {error}")

    @classmethod
    def _snapshot(cls, docker: Docker) -> dict[str, dict[str, Any]]:
        result = docker(["ps", "-aq", "--no-trunc", "--filter", "label=mgar.v4.h04.broker=1"])
        assert result.returncode == 0, "Docker discovery failed"
        snapshot = {}
        for identifier in result.stdout.decode().split():
            assert re.fullmatch(r"[a-f0-9]{64}", identifier), "Docker omitted a full container ID"
            value = cls._inspect(docker, identifier)
            # Another owner may remove its own container between ps and inspect.
            if value is not None:
                snapshot[identifier] = value
        return snapshot

    def bind(self, broker: IsolatedExecutionBroker, monkeypatch: pytest.MonkeyPatch) -> None:
        assert broker.root.resolve().is_relative_to(self.workspace_parent)
        assert id(broker) not in self._bound, "A broker must have only one test owner"
        self._bound.add(id(broker))
        docker = broker._docker
        if self._external is None:
            self._external = self._snapshot(docker)

        def guarded(args: Sequence[str], *, timeout: float = 60) -> CompletedProcess[bytes]:
            verb = args[0]
            if verb in {"start", "stop", "kill", "rm", "pause", "unpause", "restart"}:
                targets = [item for item in args[1:] if not item.startswith("-")]
                self._authorize(broker, targets)
            elif verb == "exec":
                # Broker readiness/resource probes use this exact argv shape.
                assert len(args) > 1 and not args[1].startswith("-")
                self._authorize(broker, [args[1]])
            elif verb not in {"create", "inspect", "ps", "logs", "stats", "info", "version"}:
                raise AssertionError(f"Unscoped Docker operation in owned test: {verb}")
            result = docker(args, timeout=timeout)
            if verb == "create" and result.returncode == 0:
                identifier = result.stdout.decode().strip()
                assert re.fullmatch(r"[a-f0-9]{64}", identifier)
                name = args[args.index("--name") + 1]
                assert name.startswith("mgar-h06-")
                value = self._inspect(docker, identifier)
                assert value is not None and value["name"] == "/" + name
                assert value["labels"].get("mgar.v4.h04.broker") == "1"
                assert identifier not in (self._external or {})
                with self._lock:
                    self.created.append(OwnedContainer(identifier, name, broker, docker))
                    self.checked = False
            return result

        monkeypatch.setattr(broker, "_docker", guarded)

    def _authorize(self, broker: IsolatedExecutionBroker, targets: Sequence[str]) -> None:
        with self._lock:
            allowed = {
                value: item
                for item in self.created
                if item.broker is broker
                for value in (item.container_id, item.name)
            }
        assert targets and all(target in allowed for target in targets), (
            f"Docker operation targeted a container not created by this test broker: {targets}"
        )
        for target in targets:
            item = allowed[target]
            current = self._inspect(item.docker, target)
            assert current is None or current["id"] == item.container_id, (
                "A previously owned name now resolves to somebody else's container"
            )

    def assert_running(self) -> None:
        assert self.created, "The isolation witness was never actually created"
        for item in self.created:
            value = self._inspect(item.docker, item.container_id)
            assert value is not None and value["running"], (
                "A different session touched its neighbor"
            )

    def assert_clean(self) -> None:
        assert self.created, "No real created container was recorded by this test"
        leaked = [
            item.container_id
            for item in self.created
            if self._inspect(item.docker, item.container_id) is not None
        ]
        assert not leaked, f"This test leaked its own exact container IDs: {leaked}"
        # This is observation only. Independent owners may naturally finish;
        # the execution gate above prevents our brokers touching any of them.
        docker = self.created[0].docker
        self.external_changes = []
        for identifier, before in (self._external or {}).items():
            after = self._inspect(docker, identifier)
            if before != after:
                self.external_changes.append(
                    {"container_id": identifier, "before": before, "after": after}
                )
        self.checked = True

    def remove_owned_exact(self) -> None:
        """Only the test-created isolation witness uses this cleanup path."""
        for item in self.created:
            item.broker.stop_command(item.container_id)
            # stop_command normally receives the recorded name. Release its
            # local mount pins after the exact-ID removal as well.
            item.broker._remove_placeholders(item.name)
