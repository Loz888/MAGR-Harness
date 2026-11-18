"""Trusted extensions preserve grants, namespaces, cancellation and lifecycle."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    HarnessToolResult,
    TaskBinding,
    ToolContext,
    ToolSpec,
    VerificationRequirement,
)
from mgar_v3.harness.extensions import (
    ExtensionAccess,
    GitHubReadExtension,
    ProductExtensionRegistry,
    configured_github_extension,
)
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.settings import Settings


def access(**changes):
    return replace(
        ExtensionAccess(
            actor="owner",
            workspace_id="ws-1",
            repository="example/mgar-test",
            execution_epoch=1,
            authorization_ref="project-read-authorization-v1",
            read_paths=("",),
            allowed_tools=("read_file",),
            network_hosts=("api.githubcopilot.com",),
        ),
        **changes,
    )


def context(**grant_changes):
    task = TaskBinding(
        task_id="h08-aabbcc112233",
        thread_id=UUID(int=1),
        run_id=UUID(int=2),
        execution_epoch=1,
        actor="owner",
        repository="example/mgar-test",
        base_commit="a" * 40,
        workspace_id="ws-1",
        acceptance_revision="product-v1",
        objective="Read project files",
        task_kind="inspect",
        requirements=(VerificationRequirement(requirement_id="evidence"),),
    )
    grant = CapabilityGrant(
        actor="owner",
        workspace_id="ws-1",
        repository="example/mgar-test",
        read_paths=("",),
        network=True,
    ).model_copy(update=grant_changes)
    return ToolContext(task=task, grant=grant, operation_id="test-read", workspace_tree="b" * 64)


def call(name="github__read_file", **arguments):
    return HarnessToolCall(
        call_id="call-1", name=name, arguments=arguments or {"path": "README.md"}
    )


class CoreTools:
    def __init__(self):
        self.calls = 0
        self.context_tools = object()
        self.names = ("read_file",)

    def specs(self):
        return tuple(ToolSpec(name=name, description="Core read") for name in self.names)

    def invoke(self, call, context, cancellation):
        cancellation.ensure_active()
        self.calls += 1
        return HarnessToolResult(call_id=call.call_id, status="ok", content="core-source")


class ControlledExtension:
    namespace = "github"
    version = "1.2.3"

    def __init__(self, *, grant=None, capability="read", delay=0, output="source"):
        self.access = grant or access()
        self.capability, self.delay, self.output = capability, delay, output
        self.started = threading.Event()
        self.calls = self.closed = 0
        self.cancelled = False

    def specs(self):
        return (
            ToolSpec(
                name="read_file", description="Controlled extension", capability=self.capability
            ),
        )

    def paths(self, call):
        return (call.arguments["path"],)

    async def invoke(self, call, context, cancellation):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
            return HarnessToolResult(call_id=call.call_id, status="ok", content=self.output)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        self.closed += 1


def test_no_extension_is_enabled_by_default_and_core_port_is_preserved():
    inner = CoreTools()
    registry = ProductExtensionRegistry(inner)
    try:
        assert [spec.name for spec in registry.specs()] == ["read_file"]
        assert registry.metadata() == []
        assert registry.context_tools is inner.context_tools
        result = registry.invoke(call("read_file"), context(), CancellationToken())
        assert result.content == "core-source" and inner.calls == 1
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "EXTENSION_TOOL_UNKNOWN" and inner.calls == 1
    finally:
        registry.close()


def test_real_cross_process_mcp_file_read_through_namespaced_registry(monkeypatch):
    monkeypatch.setenv("MGAR_V3_DEEPSEEK_API_KEY", "fixture-do-not-forward")
    client = ReadOnlyGitHubMCP(
        "example/mgar-test",
        command=sys.executable,
        args=(str(Path(__file__).parent / "fixtures/mcp_server.py"),),
    )
    extension = GitHubReadExtension(client, access=access())
    checks = []
    registry = ProductExtensionRegistry(
        CoreTools(),
        extensions=(extension,),
        authority_check=lambda ctx, paths: checks.append((ctx.task.execution_epoch, paths)),
    )
    try:
        assert [spec.name for spec in registry.specs()] == ["read_file", "github__read_file"]
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.status == "ok"
        assert result.content == "Observation: a controlled MCP source\n"
        assert result.data["ref"] == "a" * 40 and result.data["read_only"]
        assert len(checks) >= 2 and set(checks) == {(1, ("README.md",))}
    finally:
        registry.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "../private"},
        {"path": ".env"},
        {"path": ".env.local"},
        {"path": "README.md", "ref": "main"},
        {"path": "README.md", "url": "https://attacker.invalid"},
        {"path": "README.md", "command": "cat /etc/passwd"},
    ],
)
def test_github_arguments_are_rejected_before_starting_a_transport(arguments):
    client = ReadOnlyGitHubMCP("example/mgar-test", command="must-not-execute")
    registry = ProductExtensionRegistry(
        CoreTools(), extensions=(GitHubReadExtension(client, access=access()),)
    )
    try:
        result = registry.invoke(call(**arguments), context(), CancellationToken())
        assert result.status == "denied"
        assert result.code in {"EXTENSION_ARGUMENTS", "EXTENSION_SCOPE_DENIED"}
    finally:
        registry.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"network": False},
        {"read_paths": ("src",)},
        {"actor": "other"},
        {"workspace_id": "other"},
        {"repository": "different/repository"},
    ],
)
def test_current_capability_is_required_in_addition_to_trusted_extension_access(changes):
    extension = ControlledExtension()
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    try:
        result = registry.invoke(call(), context(**changes), CancellationToken())
        assert result.code == "EXTENSION_SCOPE_DENIED" and extension.calls == 0
    finally:
        registry.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_epoch": 2},
        {"read_paths": ("src",)},
        {"network_hosts": ()},
    ],
)
def test_extension_grant_cannot_expand_the_current_task(changes):
    extension = ControlledExtension(grant=access(**changes))
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    try:
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "EXTENSION_SCOPE_DENIED" and extension.calls == 0
    finally:
        registry.close()


@pytest.mark.parametrize("capability", ["write", "execute"])
def test_side_effect_capabilities_cannot_bypass_action_outbox(capability):
    with pytest.raises(DomainError) as error:
        ProductExtensionRegistry(
            CoreTools(),
            extensions=(
                ControlledExtension(
                    capability=capability,
                ),
            ),
        )
    assert error.value.code == "EXTENSION_CAPABILITY_DENIED"


def test_namespace_and_core_tool_overrides_are_rejected():
    with pytest.raises(DomainError, match="Duplicate extension namespace"):
        ProductExtensionRegistry(
            CoreTools(), extensions=(ControlledExtension(), ControlledExtension())
        )
    core = CoreTools()
    core.names = ("github__read_file",)
    with pytest.raises(DomainError, match="overrides"):
        ProductExtensionRegistry(core, extensions=(ControlledExtension(),))
    extension = ControlledExtension()
    extension.namespace = "../system"
    with pytest.raises(DomainError) as error:
        ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    assert error.value.code == "EXTENSION_CONFIG_DENIED"


def test_changed_core_registry_cannot_shadow_mounted_extension():
    core = CoreTools()
    registry = ProductExtensionRegistry(core, extensions=(ControlledExtension(),))
    try:
        core.names = ("github__read_file",)
        with pytest.raises(DomainError, match="changed"):
            registry.specs()
    finally:
        registry.close()


def test_namespaces_are_provider_safe_and_wire_name_collisions_are_rejected():
    extension = ControlledExtension()
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    try:
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tool.name) for tool in registry.specs())
    finally:
        registry.close()

    class Alternative(ControlledExtension):
        def __init__(self, namespace, name):
            super().__init__(grant=access(allowed_tools=(name,)))
            self.namespace, self.local_name = namespace, name

        def specs(self):
            return (ToolSpec(name=self.local_name, description="Read-only collision probe"),)

    with pytest.raises(DomainError) as error:
        ProductExtensionRegistry(
            CoreTools(),
            extensions=(
                Alternative("alpha", "read__file"),
                Alternative("alpha__read", "file"),
            ),
        )
    assert error.value.code == "EXTENSION_NAME_CONFLICT"


def test_large_utf8_extension_result_is_rejected_without_returning_partial_source():
    registry = ProductExtensionRegistry(
        CoreTools(), max_result_bytes=1024, extensions=(ControlledExtension(output="中文" * 1000),)
    )
    try:
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "EXTENSION_RESULT_TOO_LARGE"
        assert "中文" not in result.content
    finally:
        registry.close()


def test_timeout_cancels_async_work_and_closes_extension():
    extension = ControlledExtension(delay=10)
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,), timeout_seconds=0.03)
    try:
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "EXTENSION_TIMEOUT" and extension.cancelled
    finally:
        registry.close()
    assert extension.closed == 1


def test_user_cancellation_interrupts_active_extension_call():
    extension = ControlledExtension(delay=10)
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    cancellation = CancellationToken()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(registry.invoke(call(), context(), cancellation))
    )
    worker.start()
    assert extension.started.wait(1)
    cancellation.cancel()
    worker.join(2)
    registry.close()
    assert not worker.is_alive()
    assert results[0].status == "cancelled" and extension.cancelled


def test_unload_cancels_active_work_and_removes_tools():
    extension = ControlledExtension(delay=10)
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    results = []
    worker = threading.Thread(
        target=lambda: results.append(registry.invoke(call(), context(), CancellationToken()))
    )
    worker.start()
    assert extension.started.wait(1)
    registry.unload("github")
    worker.join(2)
    assert not worker.is_alive()
    assert extension.cancelled and extension.closed == 1
    assert registry.metadata() == []
    assert [spec.name for spec in registry.specs()] == ["read_file"]
    assert results[0].code == "EXTENSION_UNLOADED"
    registry.close()
    registry.close()
    assert extension.closed == 1 and registry.specs() == ()


def test_authority_revoked_during_io_prevents_returning_source():
    calls = []

    def check(ctx, paths):
        calls.append(paths)
        if len(calls) > 1:
            raise DomainError("FENCED", "Current lease was revoked", 409)

    registry = ProductExtensionRegistry(
        CoreTools(), extensions=(ControlledExtension(),), authority_check=check
    )
    try:
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "FENCED" and result.content != "source"
    finally:
        registry.close()


def test_settings_alone_never_enable_an_extension_without_explicit_access():
    settings = Settings(github_read_token=None, github_use_gh_token=False)
    with pytest.raises(DomainError) as error:
        configured_github_extension(settings, access=access())
    assert error.value.code == "EXTENSION_CONFIG_DENIED"
    with pytest.raises(DomainError):
        ProductExtensionRegistry(
            CoreTools(),
            extensions=(
                ControlledExtension(
                    grant=access(authorization_ref=""),
                ),
            ),
        )


def test_transport_exceptions_do_not_disclose_credentials():
    class Broken(ControlledExtension):
        async def invoke(self, call, context, cancellation):
            raise RuntimeError("secret-credential-do-not-disclose")

    registry = ProductExtensionRegistry(CoreTools(), extensions=(Broken(),))
    try:
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "EXTENSION_FAILED"
        assert "secret-credential" not in result.content
    finally:
        registry.close()


def test_synchronous_extension_registry_also_works_in_async_callers():
    registry = ProductExtensionRegistry(CoreTools(), extensions=(ControlledExtension(),))

    async def run():
        return registry.invoke(call(), context(), CancellationToken())

    try:
        assert asyncio.run(run()).status == "ok"
    finally:
        registry.close()


def test_mcp_uses_separate_narrow_network_grant_while_process_network_stays_off():
    client = ReadOnlyGitHubMCP(
        "example/mgar-test",
        command=sys.executable,
        args=(str(Path(__file__).parent / "fixtures/mcp_server.py"),),
    )
    process_context = context(network=False)
    network_grant = process_context.grant.model_copy(
        update={
            "network": True,
            "read_paths": ("README.md",),
        }
    )
    registry = ProductExtensionRegistry(
        CoreTools(),
        extensions=(GitHubReadExtension(client, access=access()),),
        network_grant=network_grant,
        authority_check=lambda ctx, paths: None,
    )
    try:
        result = registry.invoke(call(), process_context, CancellationToken())
        assert result.status == "ok" and "controlled MCP source" in result.content
        assert process_context.grant.network is False
        result = registry.invoke(call(path="src/private.py"), process_context, CancellationToken())
        assert result.code == "EXTENSION_SCOPE_DENIED"
    finally:
        registry.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"actor": "other"},
        {"workspace_id": "other"},
        {"repository": "other/repository"},
        {"read_paths": ("src",)},
    ],
)
def test_separate_network_grant_still_intersects_process_identity_and_scope(changes):
    process_context = context(network=False)
    network_grant = process_context.grant.model_copy(update={"network": True, **changes})
    extension = ControlledExtension()
    registry = ProductExtensionRegistry(
        CoreTools(),
        extensions=(extension,),
        network_grant=network_grant,
        authority_check=lambda ctx, paths: None,
    )
    try:
        result = registry.invoke(call(), process_context, CancellationToken())
        assert result.code == "EXTENSION_SCOPE_DENIED" and extension.calls == 0
    finally:
        registry.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"network": False},
        {"execute": True},
        {"write_paths": ("",)},
        {"allowed_commands": ("bash",)},
    ],
)
def test_separate_network_grant_cannot_authorize_shell_or_writes(changes):
    network_grant = context().grant.model_copy(update=changes)
    with pytest.raises(DomainError) as error:
        ProductExtensionRegistry(
            CoreTools(),
            network_grant=network_grant,
            authority_check=lambda ctx, paths: None,
        )
    assert error.value.code == "EXTENSION_CAPABILITY_DENIED"


def test_separate_network_grant_requires_current_authority_check():
    with pytest.raises(DomainError) as error:
        ProductExtensionRegistry(CoreTools(), network_grant=context().grant)
    assert error.value.code == "EXTENSION_CAPABILITY_DENIED"


def test_github_client_configuration_cannot_change_after_mount():
    client = ReadOnlyGitHubMCP("example/mgar-test", command="must-not-execute")
    extension = GitHubReadExtension(client, access=access())
    registry = ProductExtensionRegistry(CoreTools(), extensions=(extension,))
    try:
        client.url = "https://attacker.invalid/"
        result = registry.invoke(call(), context(), CancellationToken())
        assert result.code == "EXTENSION_CONFIG_DENIED"
    finally:
        registry.close()
