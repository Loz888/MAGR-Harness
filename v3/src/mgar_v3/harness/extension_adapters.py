"""Packaged H14 adapters. All source, tool and role bindings are operator-owned."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    HarnessToolCall,
    HarnessToolResult,
    ToolContext,
    ToolSpec,
    sha256_text,
)
from mgar_v3.harness.extensions import configured_github_extension
from mgar_v3.harness.model import ModelCapabilities
from mgar_v3.harness.plugins import ModelPlugin, PluginCatalog, ToolMount, ToolPlugin
from mgar_v3.harness.product_models import ProductDeepSeekChatAdapter, product_model_capabilities
from mgar_v3.harness.project_context import _safe_path
from mgar_v3.mcp.r4 import TaskTools
from mgar_v3.settings import ROOT

_KNOWLEDGE = ("search_knowledge", "read_knowledge")
_DATA = ("describe_tables", "query_readonly")
_PROBE = """import codecs
import hashlib
import json
import sys
from pathlib import Path
with (Path("/workspace") / sys.argv[1]).open("rb") as source:
    raw = codecs.getincrementaldecoder("utf-8")().decode(source.read(8192), final=False).encode()
print(json.dumps({"utf8_bytes": len(raw), "lines": len(raw.splitlines()),
                  "sha256": hashlib.sha256(raw).hexdigest()}))
"""


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class R4McpExtension:
    version = "1.0.0"

    def __init__(self, mount: ToolMount) -> None:
        self.mount, self.access = mount, mount.access
        self.namespace = mount.pin.plugin_id
        task = mount.task
        self.token = mount.engine.tools.issue(
            task.run_id,
            task.execution_epoch,
            task.task_id + ":" + self.namespace,
            self.namespace,
            1,
        )
        self.client = TaskTools(
            mount.engine, self.token, self.namespace,
            base_url=mount.engine.settings.r4_tool_base_url or "",
        )

    def specs(self) -> tuple[ToolSpec, ...]:
        if self.namespace == "knowledge":
            snapshots = {"type": "string", "enum": list(self.mount.pin.knowledge_snapshots)}
            return (
                ToolSpec(
                    name="search_knowledge",
                    description="Search the approved knowledge snapshot.",
                    input_schema=_object(
                        {
                            "query": {"type": "string", "maxLength": 2000},
                            "snapshot_id": snapshots,
                            "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
                        },
                        ("query", "snapshot_id"),
                    ),
                ),
                ToolSpec(
                    name="read_knowledge",
                    description="Read a chunk in the approved snapshot.",
                    input_schema=_object(
                        {"chunk_id": {"type": "string"}, "snapshot_id": snapshots},
                        ("chunk_id", "snapshot_id"),
                    ),
                ),
            )
        return (
            ToolSpec(
                name="describe_tables",
                description="Describe the frozen read-only dataset.",
                input_schema=_object({}),
            ),
            ToolSpec(
                name="query_readonly",
                description="Query the frozen dataset with a read-only SQL statement.",
                input_schema=_object(
                    {
                        "sql": {"type": "string", "maxLength": 4096},
                        "parameters": {"type": "array", "maxItems": 64},
                    },
                    ("sql", "parameters"),
                ),
            ),
        )

    def paths(self, call: HarnessToolCall) -> tuple[str, ...]:
        args = call.arguments
        if self.namespace == "knowledge":
            if args.get("snapshot_id") not in self.mount.pin.knowledge_snapshots:
                raise DomainError("TOOL_SNAPSHOT_DENIED", "Snapshot outside approved scope", 403)
            if call.name == "search_knowledge":
                query, top = args.get("query"), args.get("top_k", 6)
                if (
                    set(args) - {"query", "snapshot_id", "top_k"}
                    or not isinstance(query, str)
                    or not 1 <= len(query) <= 2000
                    or type(top) is not int
                    or not 1 <= top <= 8
                ):
                    raise DomainError("EXTENSION_ARGUMENTS", "Invalid knowledge query", 400)
            elif set(args) != {"chunk_id", "snapshot_id"}:
                raise DomainError("EXTENSION_ARGUMENTS", "Invalid knowledge read", 400)
            else:
                try:
                    UUID(args["chunk_id"])
                except (AttributeError, ValueError, TypeError) as exc:
                    raise DomainError("EXTENSION_ARGUMENTS", "Invalid chunk identity", 400) from exc
        elif call.name == "describe_tables":
            if args:
                raise DomainError("EXTENSION_ARGUMENTS", "The data source is fixed", 400)
        elif (
            set(args) != {"sql", "parameters"}
            or not isinstance(args["sql"], str)
            or not 1 <= len(args["sql"]) <= 4096
            or not isinstance(args["parameters"], list)
            or len(args["parameters"]) > 64
        ):
            raise DomainError("EXTENSION_ARGUMENTS", "Expected a bounded read-only query", 400)
        return ()

    async def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        self.paths(call)
        cancellation.ensure_active()
        result = await self.client.call_async(call.name, call.arguments)
        cancellation.ensure_active()
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=json.dumps(result, ensure_ascii=False),
            data={"evidence_id": result.get("evidence_id"), "read_only": True},
        )

    async def close(self) -> None:
        await asyncio.to_thread(self.mount.engine.tools.revoke_token, self.token)


class WorkspaceProbeExtension:
    """Example read adapter with a single-file grant in the offline sandbox."""

    namespace = "workspace_probe"
    version = "1.0.0"

    def __init__(self, mount: ToolMount) -> None:
        self.mount, self.access = mount, mount.access

    def specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="measure",
                description="Measure a permitted UTF-8 file in isolation.",
                input_schema=_object({"path": {"type": "string"}}, ("path",)),
            ),
        )

    def paths(self, call: HarnessToolCall) -> tuple[str, ...]:
        path = call.arguments.get("path")
        if (
            set(call.arguments) != {"path"}
            or not isinstance(path, str)
            or _safe_path(path) != path
        ):
            raise DomainError("EXTENSION_ARGUMENTS", "Expected a single file path", 400)
        return (path,)

    async def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        path = self.paths(call)[0]
        text = self.mount.workspace.read(path, follow_symlinks=False, prefix_bytes=8192)
        limits = issue_lease(
            actor=context.task.actor,
            workspace_id=context.task.workspace_id,
            repository=context.task.repository,
            execution_epoch=context.task.execution_epoch,
            mode="investigate",
            read_paths=(path,),
            allowed_commands=("python3",),
            ttl_seconds=30,
            timeout_seconds=5,
            max_output_bytes=4096,
            max_calls=1,
        )
        isolated = IsolatedExecutionBroker(
            self.mount.workspace.root, limits, lock_path=self.mount.image_lock,
            allow_scoped_root_cwd=True,
        )
        if self.mount.recovery_io is not None:
            isolated.bind_recovery(self.mount.recovery_io)
        session = CommandSession(isolated)
        grant = CapabilityGrant(
            actor=limits.actor,
            workspace_id=limits.workspace_id,
            repository=limits.repository,
            read_paths=(path,),
            execute=True,
            allowed_commands=("python3",),
        )
        request = ExecutionRequest(
            operation_id=context.operation_id + ":sandbox-probe",
            purpose="command",
            argv=("python3", "-I", "-B", "-S", "-c", _PROBE, path),
            timeout_seconds=5,
            execution_epoch=context.task.execution_epoch,
        )
        launch = asyncio.create_task(asyncio.to_thread(session.run, request, grant, cancellation))
        try:
            handle = await asyncio.shield(launch)
            while (
                handle := await asyncio.to_thread(session.poll, handle.handle_id)
            ).state == "running":
                cancellation.ensure_active()
                await asyncio.sleep(0.05)
            if handle.state != "exited":
                raise DomainError("EXTENSION_EXECUTION", "Isolated probe did not finish", 409)
            log = await asyncio.to_thread(session.read_log, handle.handle_id)
            measured = json.loads(log.data)
            source_hash = sha256_text(text)
            if measured.pop("sha256") != source_hash:
                raise DomainError("EXTENSION_SOURCE_CHANGED", "Probe source changed", 409)
            content = json.dumps({"path": path, "measurement": measured})
        finally:
            await asyncio.gather(launch, return_exceptions=True)
            await asyncio.to_thread(session.close)
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=content,
            data={"sha256": source_hash, "read_only": True},
        )

    async def close(self) -> None:
        pass


def _default_model(
    key: str, capabilities: ModelCapabilities, artifacts: ArtifactStore
) -> ProductDeepSeekChatAdapter:
    return ProductDeepSeekChatAdapter(api_key=key, capabilities=capabilities, artifacts=artifacts)


def default_catalog(path: Path | None) -> PluginCatalog:
    current = Path(__file__)
    package = current.parent.parent
    r4_dependencies = (package / "mcp/r4.py", package / "agents/tools.py")
    return PluginCatalog(
        path,
        (
            ToolPlugin(
                "github",
                "1.0.0",
                current.with_name("extensions.py"),
                (
                    package / "mcp/client.py",
                    package / "mcp/factory.py",
                    ROOT / "config/github-mcp-lock.json",
                ),
                ("read_file",),
                lambda mount: configured_github_extension(mount.settings, access=mount.access),
            ),
            ToolPlugin("knowledge", "1.0.0", current, r4_dependencies, _KNOWLEDGE, R4McpExtension),
            ToolPlugin(
                "data",
                "1.0.0",
                current,
                (*r4_dependencies, package / "agents/data.py"),
                _DATA,
                R4McpExtension,
            ),
            ToolPlugin(
                "workspace_probe",
                "1.0.0",
                current,
                (current.with_name("broker.py"), current.with_name("commands.py")),
                ("measure",),
                WorkspaceProbeExtension,
            ),
            ModelPlugin(
                "flash_compat",
                "1.0.0",
                current,
                (current.with_name("product_models.py"),),
                product_model_capabilities(),
                _default_model,
            ),
        ),
    )
