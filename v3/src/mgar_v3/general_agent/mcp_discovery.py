"""Authenticated reflection for an exact operator-pinned MCP server."""

from __future__ import annotations

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from mgar_v3.domain import DomainError
from mgar_v3.general_agent.capabilities import DiscoveredTool, MCPInventory, MCPServerPin


class AuthenticatedMCPDiscovery:
    def __init__(self, provider_id: str, endpoint: str, token: SecretStr) -> None:
        if not token.get_secret_value():
            raise ValueError("MCP discovery credential is required")
        self.provider_id, self.endpoint, self.token = provider_id, endpoint, token

    async def __call__(self, pin: MCPServerPin) -> MCPInventory:
        if (pin.provider_id, pin.endpoint) != (self.provider_id, self.endpoint):
            raise DomainError("GENERAL_MCP_DESTINATION", "MCP endpoint differs from its pin.", 403)
        headers = {"Authorization": "Bearer " + self.token.get_secret_value()}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            timeout=10.0,
        ) as client:
            async with streamable_http_client(pin.endpoint, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    hello = await session.initialize()
                    response = await session.list_tools()
        # Reject incomplete inventories. A compatible endpoint must publish the
        # pinned schemas on one bounded discovery page.
        if response.nextCursor or len(response.tools) > 128:
            raise DomainError("GENERAL_MCP_INVENTORY", "MCP inventory exceeds its bound.", 422)
        return MCPInventory(
            server_version=hello.serverInfo.version,
            tools=tuple(
                DiscoveredTool.model_validate(
                    {
                        "name": tool.name,
                        "input_schema": tool.inputSchema,
                        "output_schema": tool.outputSchema,
                    }
                )
                for tool in response.tools
            ),
        )
