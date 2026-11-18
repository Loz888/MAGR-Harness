"""Real HTTP MCP endpoints; task identity comes only from Engine-issued tokens."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import Context, FastMCP

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError


def server(role: str, authority: Callable[[], Engine]) -> FastMCP:
    app = FastMCP("MGAR " + role, stateless_http=True, json_response=True, streamable_http_path="/")

    def invoke(ctx: Context[Any, Any, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request = ctx.request_context.request
        header = request.headers.get("authorization", "") if request is not None else ""
        if not header.startswith("Bearer "):
            raise DomainError("TOOL_AUTH_REQUIRED", "Engine task capability is required", 401)
        try:
            return authority().tools.call(header[7:], tool, arguments, role)
        except DomainError as exc:
            if exc.code != "FENCED":
                authority().tools.denial(header[7:], tool, exc.code, arguments)
            raise

    if role == "knowledge":

        @app.tool()
        def search_knowledge(
            query: str, snapshot_id: str, ctx: Context[Any, Any, Any], top_k: int = 6
        ) -> dict[str, Any]:
            return invoke(
                ctx,
                "search_knowledge",
                {"query": query, "snapshot_id": snapshot_id, "top_k": top_k},
            )

        @app.tool()
        def read_knowledge(
            chunk_id: str, snapshot_id: str, ctx: Context[Any, Any, Any]
        ) -> dict[str, Any]:
            return invoke(ctx, "read_knowledge", {"chunk_id": chunk_id, "snapshot_id": snapshot_id})

        @app.tool()
        def recall_memory(ctx: Context[Any, Any, Any]) -> dict[str, Any]:
            return invoke(ctx, "recall_memory", {})
    elif role == "data":

        @app.tool()
        def describe_tables(ctx: Context[Any, Any, Any]) -> dict[str, Any]:
            return invoke(ctx, "describe_tables", {})

        @app.tool()
        def query_readonly(
            sql: str, parameters: list[Any], ctx: Context[Any, Any, Any]
        ) -> dict[str, Any]:
            return invoke(ctx, "query_readonly", {"sql": sql, "parameters": parameters})

    return app


class TaskTools:
    def __init__(self, engine: Engine, token: str, role: str, *, base_url: str | None = None):
        self.engine, self.token, self.role = engine, token, role
        self.base_url = base_url

    def _endpoint(self) -> str | None:
        return self.engine.settings.r4_tool_base_url if self.base_url is None else self.base_url

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._endpoint() and self.role in {"knowledge", "data"}:
            return asyncio.run(self.call_async(tool, arguments))
        return self.engine.tools.call(self.token, tool, arguments, self.role)

    async def call_async(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        base = self._endpoint()
        if not base or self.role not in {"knowledge", "data"}:
            return await asyncio.to_thread(
                self.engine.tools.call, self.token, tool, arguments, self.role,
            )
        try:
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + self.token}, trust_env=False,
            ) as http:
                async with streamable_http_client(
                    base.rstrip("/") + "/mcp/" + self.role + "/", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
        except (ExceptionGroup, httpx.HTTPError) as exc:
            raise DomainError(
                "MCP_TRANSPORT_FAILED", "Readonly MCP transport did not complete"
            ) from exc
        if result.isError or result.structuredContent is None:
            raise DomainError("MCP_TOOL_FAILED", "Readonly MCP operation failed")
        return dict(result.structuredContent)
