"""Operator-owned lock and SDK for packaged Harness tool and model adapters."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import ModelAdapter, TaskBinding, normalize_relative_path
from mgar_v3.harness.extensions import ExtensionAccess, TrustedExtension
from mgar_v3.harness.model import ModelCapabilities
from mgar_v3.harness.workspace import TaskWorkspace, _write_json_atomic

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.harness.recovery_io import RecoveryIO
    from mgar_v3.settings import Settings

LOCK_VERSION: Final = "harness-plugin-lock-v1"
_ID = r"^[a-z][a-z0-9_-]{1,23}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
_HASH = r"^[a-f0-9]{64}$"


class PluginPin(Model):
    plugin_id: str = Field(pattern=_ID)
    version: str = Field(pattern=_VERSION)
    kind: Literal["tool", "model"]
    source_sha256: str = Field(pattern=_HASH)
    model_capabilities_sha256: str | None = Field(default=None, pattern=_HASH)
    dependency_sha256: tuple[str, ...] = ()
    enabled: bool = False
    projects: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ("",)
    max_result_bytes: int = Field(default=32768, ge=1024, le=65536)
    timeout_seconds: float = Field(default=15, gt=0, le=60)
    knowledge_snapshots: tuple[str, ...] = Field(default=(), max_length=8)
    sql_profile_sha256: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def check_scope(self) -> PluginPin:
        if len(set(self.projects)) != len(self.projects) or any(
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value) for value in self.projects
        ):
            raise ValueError("Invalid plugin project scope")
        if len(set(self.tools)) != len(self.tools) or (
            (self.kind == "model") != (self.model_capabilities_sha256 is not None)
        ) or self.kind == "model" and self.tools:
            raise ValueError("Invalid plugin tool scope")
        if any(path != normalize_relative_path(path) for path in self.read_paths):
            raise ValueError("Plugin read paths must be normalized")
        if any(not re.fullmatch(_HASH, value) for value in self.dependency_sha256):
            raise ValueError("Invalid dependency fingerprint")
        if self.knowledge_snapshots and (
            self.plugin_id != "knowledge"
            or len(set(self.knowledge_snapshots)) != len(self.knowledge_snapshots)
            or any(str(UUID(value)) != value for value in self.knowledge_snapshots)
        ):
            raise ValueError("Invalid approved knowledge snapshots")
        if self.enabled and self.plugin_id == "knowledge" and not self.knowledge_snapshots:
            raise ValueError("An enabled knowledge adapter needs approved snapshots")
        if (self.sql_profile_sha256 is not None and self.plugin_id != "data") or (
            self.enabled and self.plugin_id == "data" and self.sql_profile_sha256 is None
        ):
            raise ValueError("The data adapter needs a pinned SQL profile")
        return self

    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


class PluginLock(Model):
    schema_version: Literal["harness-plugin-lock-v1"] = LOCK_VERSION
    plugins: tuple[PluginPin, ...] = Field(default=(), max_length=30)

    @model_validator(mode="after")
    def unique_ids(self) -> PluginLock:
        if len({item.plugin_id for item in self.plugins}) != len(self.plugins):
            raise ValueError("Duplicate plugin identity")
        return self


@dataclass(frozen=True)
class ToolMount:
    engine: Engine
    settings: Settings
    task: TaskBinding
    workspace: TaskWorkspace
    access: ExtensionAccess
    pin: PluginPin
    image_lock: Path
    recovery_io: RecoveryIO | None = None


@dataclass(frozen=True)
class ToolPlugin:
    plugin_id: str
    version: str
    source: Path
    dependencies: tuple[Path, ...]
    tools: tuple[str, ...]
    create: Callable[[ToolMount], TrustedExtension]


@dataclass(frozen=True)
class ModelPlugin:
    plugin_id: str
    version: str
    source: Path
    dependencies: tuple[Path, ...]
    capabilities: ModelCapabilities
    create: Callable[[str, ModelCapabilities, ArtifactStore], ModelAdapter]


Definition = ToolPlugin | ModelPlugin


def _hash_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise DomainError(
            "PLUGIN_SOURCE_CHANGED", "A pinned plugin file is unavailable", 409
        ) from exc


class PluginCatalog:
    """Bind a trusted package catalog to the operator's atomically updated lock."""

    def __init__(self, path: Path | None, definitions: Sequence[Definition]) -> None:
        self.path = path
        self.definitions = {(item.plugin_id, item.version): item for item in definitions}
        if len(self.definitions) != len(definitions) or any(
            not re.fullmatch(_ID, item.plugin_id)
            or not re.fullmatch(_VERSION, item.version)
            or item.plugin_id in {"core", "mgar", "system"}
            for item in definitions
        ):
            raise DomainError("PLUGIN_CONFLICT", "Invalid or duplicate packaged plugin", 409)

    def definition(self, pin: PluginPin) -> Definition:
        item = self.definitions.get((pin.plugin_id, pin.version))
        if item is None:
            raise DomainError("PLUGIN_NOT_INSTALLED", "The pinned package is unavailable", 409)
        tool = isinstance(item, ToolPlugin)
        if (
            (pin.kind == "tool") != tool
            or pin.tools != (item.tools if isinstance(item, ToolPlugin) else ())
            or pin.model_capabilities_sha256 != (
                digest(item.capabilities.model_dump(mode="json"))
                if isinstance(item, ModelPlugin) else None
            )
            or pin.source_sha256 != _hash_file(item.source)
            or pin.dependency_sha256 != tuple(_hash_file(path) for path in item.dependencies)
        ):
            raise DomainError(
                "PLUGIN_SOURCE_CHANGED", "The plugin package differs from its lock", 409
            )
        return item

    def lock(self) -> PluginLock:
        if self.path is None:
            return PluginLock()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                    or info.st_uid not in {0, os.geteuid()} or info.st_size > 65536
                ):
                    raise ValueError("Plugin lock permissions or size")
                data = stream.read(65537)
                if len(data) > 65536:
                    raise ValueError("Plugin lock size")
            return PluginLock.model_validate_json(data)
        except (OSError, ValueError, ValidationError) as exc:
            raise DomainError(
                "PLUGIN_LOCK_INVALID", "Operator plugin lock is unavailable", 409
            ) from exc

    def pin(
        self,
        plugin_id: str,
        version: str,
        *,
        projects: tuple[str, ...],
        enabled: bool = False,
        read_paths: tuple[str, ...] = ("",),
        knowledge_snapshots: tuple[str, ...] = (),
        sql_profile_sha256: str | None = None,
    ) -> PluginPin:
        item = self.definitions.get((plugin_id, version))
        if item is None:
            raise DomainError("PLUGIN_NOT_INSTALLED", "The packaged plugin is unavailable", 404)
        tool = isinstance(item, ToolPlugin)
        return PluginPin(
            plugin_id=plugin_id,
            version=version,
            kind="tool" if tool else "model",
            source_sha256=_hash_file(item.source),
            dependency_sha256=tuple(_hash_file(path) for path in item.dependencies),
            tools=item.tools if isinstance(item, ToolPlugin) else (),
            model_capabilities_sha256=(
                digest(item.capabilities.model_dump(mode="json"))
                if isinstance(item, ModelPlugin) else None
            ),
            projects=projects,
            enabled=enabled,
            read_paths=read_paths,
            knowledge_snapshots=knowledge_snapshots,
            sql_profile_sha256=sql_profile_sha256,
        )

    def write(self, lock: PluginLock) -> None:
        """Operator installation, upgrade, disable and removal use the same atomic file."""
        if self.path is None:
            raise DomainError("PLUGIN_LOCK_INVALID", "Choose an operator plugin lock path", 409)
        for pin in lock.plugins:
            if pin.enabled:
                self.definition(pin)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_json_atomic(self.path.parent, self.path.name, lock.model_dump(mode="json"))
        except OSError as exc:
            raise DomainError("PLUGIN_LOCK_INVALID", "Operator plugin lock is unwritable", 409
                              ) from exc

    def selected(self, project_id: str, *, github: bool) -> dict[str, PluginPin]:
        if self.path is None and github:
            # Honor the existing explicit GitHub project permission on installs
            # that have no separate plugin lock.
            return {"github": self.pin("github", "1.0.0", projects=(project_id,), enabled=True)}
        return {
            pin.plugin_id: pin
            for pin in self.lock().plugins
            if pin.enabled and project_id in pin.projects and (pin.plugin_id != "github" or github)
        }

    def resolve(
        self,
        project_id: str,
        bindings: dict[str, str],
        *,
        github: bool,
    ) -> dict[str, PluginPin]:
        if not bindings:
            return {}
        current = self.selected(project_id, github=github)
        result: dict[str, PluginPin] = {}
        for plugin_id, identity in bindings.items():
            pin = current.get(plugin_id)
            if pin is None or pin.identity() != identity:
                raise DomainError("PLUGIN_REVOKED", "A frozen plugin was changed or removed", 403)
            self.definition(pin)
            result[plugin_id] = pin
        return result
