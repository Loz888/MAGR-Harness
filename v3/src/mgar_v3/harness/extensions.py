"""Trusted, read-only extensions for the product ToolRegistry port.

Project files can describe skills, but cannot create instances of these
extensions. The composition root supplies code, clients and an explicit,
epoch-bound access record. Remote mutations remain outside this registry and
must use the existing Action/Outbox services.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    HarnessToolResult,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    normalize_relative_path,
    sha256_text,
)
from mgar_v3.harness.project_context import _safe_path
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.mcp.factory import github_client
from mgar_v3.settings import Settings

EXTENSION_VERSION = "harness-product-extension-v1"
_NAMESPACE = re.compile(r"[a-z][a-z0-9_-]{1,23}")
_TOOL = re.compile(r"[a-z][a-z0-9_]{1,30}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


@dataclass(frozen=True)
class ExtensionAccess:
    """Explicit trusted grant projection, never reconstructed from project text."""

    actor: str
    workspace_id: str
    repository: str
    execution_epoch: int
    authorization_ref: str
    read_paths: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    network_hosts: tuple[str, ...] = ()


class TrustedExtension(Protocol):
    namespace: str
    version: str
    access: ExtensionAccess

    def specs(self) -> Sequence[ToolSpec]: ...

    def paths(self, call: HarnessToolCall) -> tuple[str, ...]: ...

    async def invoke(
        self, call: HarnessToolCall, context: ToolContext, cancellation: CancellationToken
    ) -> HarnessToolResult: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _Mounted:
    extension: TrustedExtension
    version: str
    access: ExtensionAccess
    specs: tuple[ToolSpec, ...]


def _within(path: str, roots: Sequence[str]) -> bool:
    return any(not root or path == root or path.startswith(root + "/") for root in roots)


class ProductExtensionRegistry:
    """Wrap a registry without changing the coding loop or adding authority.

    Each invocation validates both its trusted extension grant and the current
    ToolContext. A separate read-only network grant keeps process networking
    disabled; supplying it requires an authority callback. The callback checks the current Broker
    lease/epoch; it is rechecked after remote I/O before any data is returned.
    Coroutine execution uses a private thread, so synchronous callers may also
    invoke this wrapper from a thread that already has an asyncio event loop.
    """

    def __init__(
        self,
        inner: ToolRegistry,
        *,
        extensions: Sequence[TrustedExtension] = (),
        authority_check: Callable[[ToolContext, tuple[str, ...]], None] | None = None,
        network_grant: CapabilityGrant | None = None,
        max_result_bytes: int = 65536,
        timeout_seconds: float = 30,
    ) -> None:
        if not 1024 <= max_result_bytes <= 1048576 or not 0 < timeout_seconds <= 60:
            raise ValueError("Extension result and time limits must be bounded")
        self.inner = inner
        self.authority_check = authority_check
        if network_grant is not None and (
            not network_grant.network
            or network_grant.execute
            or network_grant.write_paths
            or network_grant.allowed_commands
            or authority_check is None
        ):
            raise DomainError(
                "EXTENSION_CAPABILITY_DENIED", "A separate MCP grant must be read-only", 403
            )
        self.network_grant = network_grant
        self.max_result_bytes = max_result_bytes
        self.timeout_seconds = timeout_seconds
        self._mounted: dict[str, _Mounted] = {}
        self._tools: dict[str, str] = {}
        self._closed = False
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-mcp")
        names = [spec.name for spec in inner.specs()]
        if len(set(names)) != len(names):
            raise DomainError("EXTENSION_NAME_CONFLICT", "Duplicate core tool names", 409)
        for extension in extensions:
            namespace, version, access = extension.namespace, extension.version, extension.access
            if (
                _NAMESPACE.fullmatch(namespace) is None
                or namespace in {"core", "system", "mgar"}
                or _VERSION.fullmatch(version) is None
                or not access.authorization_ref
                or access.execution_epoch < 1
                or not access.actor
                or not access.workspace_id
            ):
                raise DomainError(
                    "EXTENSION_CONFIG_DENIED", "Invalid trusted extension config", 403
                )
            if namespace in self._mounted:
                raise DomainError("EXTENSION_NAME_CONFLICT", "Duplicate extension namespace", 409)
            for path in access.read_paths:
                if path != normalize_relative_path(path):
                    raise DomainError(
                        "EXTENSION_CONFIG_DENIED", "Invalid extension read scope", 403
                    )
            specs = tuple(extension.specs())
            if not specs or len(specs) > 32:
                raise DomainError("EXTENSION_CONFIG_DENIED", "Extension tool count is invalid", 403)
            seen: set[str] = set()
            for spec in specs:
                full_name = namespace + "__" + spec.name
                if (
                    _TOOL.fullmatch(spec.name) is None
                    or spec.capability != "read"
                    or spec.name not in access.allowed_tools
                ):
                    raise DomainError(
                        "EXTENSION_CAPABILITY_DENIED", "Only granted read tools may be mounted", 403
                    )
                if full_name in names or full_name in self._tools or spec.name in seen:
                    raise DomainError(
                        "EXTENSION_NAME_CONFLICT", "Tool overrides are forbidden", 409
                    )
                seen.add(spec.name)
                self._tools[full_name] = namespace
            self._mounted[namespace] = _Mounted(extension, version, access, specs)

    def __getattr__(self, name: str) -> Any:
        # Keep existing context/persistence/command interfaces available to the
        # loop. Model invocations still enter this wrapper's invoke method.
        return getattr(self.inner, name)

    def metadata(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "namespace": namespace,
                    "version": mounted.version,
                    "schema_version": EXTENSION_VERSION,
                    "tools": [namespace + "__" + spec.name for spec in mounted.specs],
                    "authorization_ref": mounted.access.authorization_ref,
                    "read_only": True,
                }
                for namespace, mounted in sorted(self._mounted.items())
            ]

    def specs(self) -> tuple[ToolSpec, ...]:
        with self._lock:
            if self._closed:
                return ()
            core = tuple(self.inner.specs())
            names = [spec.name for spec in core]
            if len(set(names)) != len(names) or set(names) & self._tools.keys():
                raise DomainError("EXTENSION_NAME_CONFLICT", "Core tool names changed", 409)
            extra = tuple(
                spec.model_copy(update={"name": namespace + "__" + spec.name})
                for namespace, mounted in sorted(self._mounted.items())
                for spec in mounted.specs
            )
            return (*core, *extra)

    def _authorize(self, mounted: _Mounted, context: ToolContext, paths: tuple[str, ...]) -> None:
        access, task, grant = mounted.access, context.task, context.grant
        network_grant = self.network_grant or grant
        if self._closed or mounted not in self._mounted.values():
            raise DomainError("EXTENSION_UNLOADED", "Extension is no longer active", 409)
        if (
            (access.actor, access.workspace_id, access.repository, access.execution_epoch)
            != (task.actor, task.workspace_id, task.repository, task.execution_epoch)
            or (grant.actor, grant.workspace_id, grant.repository)
            != (task.actor, task.workspace_id, task.repository)
            or (network_grant.actor, network_grant.workspace_id, network_grant.repository)
            != (task.actor, task.workspace_id, task.repository)
            or not access.network_hosts
            or not network_grant.network
        ):
            raise DomainError(
                "EXTENSION_SCOPE_DENIED", "Extension is outside current authority", 403
            )
        if not paths:
            raise DomainError("EXTENSION_SCOPE_DENIED", "Extension requires a read scope", 403)
        for path in paths:
            if path and _safe_path(path) is None:
                raise DomainError("EXTENSION_SCOPE_DENIED", "Protected extension source path", 403)
            if (
                not grant.can_read(path)
                or not network_grant.can_read(path)
                or not _within(path, access.read_paths)
            ):
                raise DomainError(
                    "EXTENSION_SCOPE_DENIED", "Source is outside current read scope", 403
                )
        if self.authority_check is not None:
            self.authority_check(context, paths)

    async def _invoke_extension(
        self,
        mounted: _Mounted,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
        paths: tuple[str, ...],
    ) -> HarnessToolResult:
        pending = asyncio.create_task(mounted.extension.invoke(call, context, cancellation))
        deadline = time.monotonic() + min(self.timeout_seconds, context.grant.max_seconds)
        try:
            while True:
                cancellation.ensure_active()
                if self._closed or mounted not in self._mounted.values():
                    raise DomainError("EXTENSION_UNLOADED", "Extension was unloaded", 409)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DomainError("EXTENSION_TIMEOUT", "Read-only extension timed out", 504)
                done, _ = await asyncio.wait((pending,), timeout=min(0.05, remaining))
                if done:
                    result = pending.result()
                    cancellation.ensure_active()
                    self._authorize(mounted, context, paths)
                    if result.call_id != call.call_id:
                        raise DomainError(
                            "EXTENSION_RESULT_DENIED", "Extension call identity changed", 403
                        )
                    if len(result.model_dump_json().encode()) > self.max_result_bytes:
                        raise DomainError(
                            "EXTENSION_RESULT_TOO_LARGE", "Extension result exceeds limit", 413
                        )
                    return result
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    def invoke(
        self, call: HarnessToolCall, context: ToolContext, cancellation: CancellationToken
    ) -> HarnessToolResult:
        try:
            cancellation.ensure_active()
            with self._lock:
                if self._closed:
                    raise DomainError("EXTENSION_UNLOADED", "Registry is closed", 409)
                namespace = self._tools.get(call.name)
                if namespace is None:
                    if call.name not in {spec.name for spec in self.inner.specs()}:
                        raise DomainError("EXTENSION_TOOL_UNKNOWN", "Tool is not registered", 403)
                    return self.inner.invoke(call, context, cancellation)
                mounted = self._mounted[namespace]
                local_call = call.model_copy(update={"name": call.name[len(namespace) + 2 :]})
                paths = mounted.extension.paths(local_call)
                self._authorize(mounted, context, paths)
                future = self._executor.submit(
                    asyncio.run,
                    self._invoke_extension(mounted, local_call, context, cancellation, paths),
                )
            return future.result()
        except DomainError as exc:
            cancelled = exc.code in {"HARNESS_CANCELLED", "EXTENSION_UNLOADED"}
            return HarnessToolResult(
                call_id=call.call_id,
                status="cancelled"
                if cancelled
                else "denied"
                if exc.status in {400, 403, 409}
                else "error",
                code=exc.code,
                content=str(exc),
            )
        except Exception:
            # Plugin or transport errors may carry credentials in their text.
            return HarnessToolResult(
                call_id=call.call_id,
                status="error",
                code="EXTENSION_FAILED",
                content="Read-only extension failed",
            )

    def unload(self, namespace: str) -> None:
        with self._lock:
            mounted = self._mounted.pop(namespace, None)
            self._tools = {name: owner for name, owner in self._tools.items() if owner != namespace}
        if mounted is not None:
            self._executor.submit(asyncio.run, self._close_extension(mounted)).result(
                timeout=self.timeout_seconds + 1
            )

    async def _close_extension(self, mounted: _Mounted) -> None:
        async with asyncio.timeout(self.timeout_seconds):
            await mounted.extension.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            namespaces = tuple(self._mounted)
        try:
            for namespace in namespaces:
                self.unload(namespace)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


class GitHubReadExtension:
    namespace = "github"
    version = "1.0.0"

    def __init__(self, client: ReadOnlyGitHubMCP, *, access: ExtensionAccess) -> None:
        if (
            client.readonly is not True
            or client.repository != access.repository
            or "api.githubcopilot.com" not in access.network_hosts
        ):
            raise DomainError("EXTENSION_CONFIG_DENIED", "GitHub read grant differs", 403)
        self.client, self.access = client, access
        self._client_config = self._configuration()
        self._closed = False

    def _configuration(self) -> tuple[object, ...]:
        return (
            self.client.repository,
            self.client.readonly,
            self.client.command,
            self.client.args,
            self.client.url,
            self.client.max_bytes,
        )

    def specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="read_file",
                description="Read a granted GitHub file at this task's immutable base commit.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                capability="read",
            ),
        )

    def paths(self, call: HarnessToolCall) -> tuple[str, ...]:
        if call.name != "read_file" or set(call.arguments) != {"path"}:
            raise DomainError(
                "EXTENSION_ARGUMENTS", "Expected only a repository-relative path", 400
            )
        path = call.arguments["path"]
        if not isinstance(path, str) or _safe_path(path) is None:
            raise DomainError(
                "EXTENSION_SCOPE_DENIED", "Invalid or protected GitHub source path", 403
            )
        return (path,)

    async def invoke(
        self, call: HarnessToolCall, context: ToolContext, cancellation: CancellationToken
    ) -> HarnessToolResult:
        if (
            self._closed
            or not self.client.readonly
            or self.client.repository != self.access.repository
            or self._configuration() != self._client_config
        ):
            raise DomainError("EXTENSION_CONFIG_DENIED", "GitHub client changed or closed", 403)
        path = self.paths(call)[0]
        cancellation.ensure_active()
        source = await self.client.read_file(path, context.task.base_commit)
        cancellation.ensure_active()
        if self._configuration() != self._client_config:
            raise DomainError("EXTENSION_CONFIG_DENIED", "GitHub client config changed", 403)
        try:
            content = source.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("EXTENSION_BINARY", "GitHub source is not UTF-8", 415) from exc
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=content,
            data={
                "uri": source.uri,
                "version": source.version,
                "sha256": sha256_text(content),
                "ref": context.task.base_commit,
                "read_only": True,
            },
        )

    async def close(self) -> None:
        # ReadOnlyGitHubMCP owns a fresh async context per call. Its stdio
        # process/HTTP session is closed before read_file returns or is cancelled.
        self._closed = True


def configured_github_extension(
    settings: Settings, *, access: ExtensionAccess
) -> GitHubReadExtension:
    """Resolve the operator's pinned MCP config only after explicit egress grant."""
    if (
        settings.github_read_token is None
        or not access.authorization_ref
        or "api.githubcopilot.com" not in access.network_hosts
        or access.allowed_tools != ("read_file",)
    ):
        raise DomainError(
            "EXTENSION_CONFIG_DENIED", "An explicit GitHub read grant is required", 403
        )
    return GitHubReadExtension(
        github_client(settings, access.repository, readonly=True, max_bytes=32768), access=access
    )
