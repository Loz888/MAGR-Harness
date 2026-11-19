"""Trusted capability catalog and per-node model schema projection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field, JsonValue, model_validator

from mgar_v3.context.compactor import estimate_tokens
from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.general_agent.contracts import (
    GeneralTaskSpec,
    Identifier,
    Limit,
    Revision,
    Sha256,
    TaskDomain,
    Text,
    unique,
)

Risk = Literal["low", "medium", "high"]
Effect = Literal["read_only", "workspace_write", "external_write"]
Health = Literal["ready", "unavailable", "timeout", "version_mismatch", "schema_mismatch"]
_RISK = {"low": 0, "medium": 1, "high": 2}
Seconds = Annotated[int, Field(strict=True, ge=1, le=3600)]


class Resources(Model):
    cost_micros: Limit = 0
    model_calls: Limit = 0
    tool_calls: Limit = 0
    tokens: Limit = 0


def check_schema(value: dict[str, JsonValue]) -> None:
    """Check a bounded self-contained JSON Schema without external resolution."""
    if value.get("type") != "object" or len(json.dumps(value).encode()) > 8192:
        raise ValueError("Tool schemas must be bounded JSON Schema objects")

    def walk(item: JsonValue, depth: int) -> None:
        if depth > 24:
            raise ValueError("Tool schema nesting is too deep")
        if isinstance(item, dict):
            # No resolver behavior or presentation override can come from discovery.
            if {"$ref", "$dynamicRef", "$schema", "$id"} & item.keys():
                raise ValueError("Tool schemas must be self-contained")
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)

    walk(value, 0)
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise ValueError("Tool JSON Schema is invalid") from exc


class CapabilityDescriptor(Model):
    schema_version: Literal["general-capability-v1"] = "general-capability-v1"
    id: Identifier
    version: Identifier
    domain: TaskDomain
    description: Text
    provider: Literal["native", "mcp", "executor"]
    provider_id: Identifier
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    permissions: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    risk: Risk = "low"
    effect: Effect = "read_only"
    idempotent: Annotated[bool, Field(strict=True)] = False
    upper_bound: Resources = Field(default_factory=Resources)
    timeout_seconds: Seconds
    evidence_contract: Identifier

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        unique(self.permissions, "permissions")
        check_schema(self.input_schema)
        check_schema(self.output_schema)
        return self

    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                # Supported function names are normalized without aliases. The
                # actual capability ID and descriptor hash stay server-side.
                "name": "cap_" + self.identity()[:24],
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class MCPToolPin(Model):
    capability_id: Identifier
    tool_name: Identifier


class MCPServerPin(Model):
    provider_id: Identifier
    endpoint: Text
    version: Identifier
    tools: Annotated[tuple[MCPToolPin, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def validate_pin(self) -> Self:
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Remote MCP requires an exact configured HTTPS endpoint")
        unique(tuple(tool.capability_id for tool in self.tools), "capability_id")
        unique(tuple(tool.tool_name for tool in self.tools), "tool_name")
        return self


class DiscoveredTool(Model):
    name: Identifier
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue] | None = None


class MCPInventory(Model):
    server_version: Identifier
    tools: Annotated[tuple[DiscoveredTool, ...], Field(max_length=128)]


@dataclass(frozen=True)
class ObservedHealth:
    state: Health
    valid_until: datetime | None = None


class CapabilityAccess(Model):
    """A grant projected by trusted service code for one task revision."""

    task_id: UUID
    project_id: Identifier
    task_revision: Revision
    capability_pins: Annotated[tuple[Sha256, ...], Field(max_length=64)] = ()
    permissions: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    max_risk: Risk = "low"
    resources: Resources = Field(default_factory=Resources)
    workspace_write: Annotated[bool, Field(strict=True)] = False


class RouteCheck(Model):
    capability_id: Identifier
    reason: Identifier


class RouteDecision(Model):
    status: Literal["selected", "blocked"]
    chosen_id: Identifier | None = None
    descriptor_sha256: Sha256 | None = None
    checks: tuple[RouteCheck, ...]


Discovery = Callable[[MCPServerPin], Awaitable[MCPInventory]]


class CapabilityRegistry:
    """Register operator definitions. Discovery can only update their health."""

    def __init__(
        self,
        descriptors: Sequence[CapabilityDescriptor],
        *,
        mcp_servers: Sequence[MCPServerPin] = (),
        unavailable: Sequence[str] = (),
    ) -> None:
        self._lock = RLock()
        # Store validated copies because JSON Schema uses nested mutable objects.
        self._definitions = {
            item.id: CapabilityDescriptor.model_validate_json(item.model_dump_json())
            for item in descriptors
        }
        self._servers = {pin.provider_id: pin for pin in mcp_servers}
        if len(self._definitions) != len(descriptors) or len(self._servers) != len(mcp_servers):
            raise ValueError("Duplicate capability or MCP provider ID")
        for pin in mcp_servers:
            for tool in pin.tools:
                descriptor = self._definitions.get(tool.capability_id)
                if (
                    descriptor is None
                    or descriptor.provider != "mcp"
                    or descriptor.provider_id != pin.provider_id
                ):
                    raise ValueError("MCP pin references an undeclared provider capability")
        self._health: dict[str, ObservedHealth] = {
            item.id: ObservedHealth(
                "unavailable" if item.provider == "mcp" or item.id in unavailable else "ready"
            )
            for item in descriptors
        }
        self._discovery_generations: dict[str, int] = {}

    def definition(self, capability_id: str) -> CapabilityDescriptor | None:
        value = self._definitions.get(capability_id)
        return value.model_copy(deep=True) if value is not None else None

    def health(self, capability_id: str) -> Health:
        with self._lock:
            value = self._health[capability_id]
            if value.valid_until and value.valid_until <= datetime.now(UTC):
                return "unavailable"
            return value.state

    async def discover(
        self,
        provider_id: str,
        fetch: Discovery,
        *,
        timeout: float = 5,
    ) -> dict[str, Health]:
        pin = self._servers.get(provider_id)
        if pin is None:
            raise DomainError("GENERAL_MCP_UNTRUSTED", "MCP provider has no deployment pin.", 403)
        if not 0 < timeout <= 30:
            raise ValueError("Invalid discovery timeout")
        with self._lock:
            generation = self._discovery_generations.get(provider_id, 0) + 1
            self._discovery_generations[provider_id] = generation
        state: Health = "ready"
        inventory: MCPInventory | None = None
        try:
            async with asyncio.timeout(timeout):
                inventory = await fetch(pin)
        except TimeoutError:
            state = "timeout"
        except Exception:
            # Do not return credentials, remote text, or transport details.
            state = "unavailable"
        if inventory is not None:
            if inventory.server_version != pin.version:
                state = "version_mismatch"
            elif len({tool.name for tool in inventory.tools}) != len(inventory.tools):
                state = "schema_mismatch"

        by_name = {tool.name: tool for tool in inventory.tools} if inventory else {}
        observations: dict[str, Health] = {}
        for pin_tool in pin.tools:
            current = state
            if current == "ready":
                observed = by_name.get(pin_tool.tool_name)
                locked = self._definitions[pin_tool.capability_id]
                if observed is None:
                    current = "unavailable"
                elif digest(observed.input_schema) != digest(locked.input_schema) or digest(
                    observed.output_schema
                ) != digest(locked.output_schema):
                    current = "schema_mismatch"
            observations[pin_tool.capability_id] = current
        # Registry data are advisory health. Task grants remain external to it.
        until = datetime.now(UTC) + timedelta(minutes=1)
        with self._lock:
            if self._discovery_generations[provider_id] == generation:
                self._health.update(
                    {key: ObservedHealth(value, until) for key, value in observations.items()}
                )
        return observations

    def route(
        self,
        spec: GeneralTaskSpec,
        domain: TaskDomain,
        alternatives: Sequence[str],
        access: CapabilityAccess,
        *,
        context_tokens: int,
        eligible: Callable[[CapabilityDescriptor], str | None] | None = None,
    ) -> RouteDecision:
        checks = []
        for key in alternatives:
            descriptor = self.definition(key)
            reason = self._reason(spec, domain, descriptor, access, context_tokens)
            if reason == "selected" and descriptor is not None and eligible is not None:
                reason = eligible(descriptor) or reason
            checks.append(RouteCheck(capability_id=key, reason=reason))
            if reason == "selected" and descriptor is not None:
                return RouteDecision(
                    status="selected",
                    chosen_id=key,
                    descriptor_sha256=descriptor.identity(),
                    checks=tuple(checks),
                )
        return RouteDecision(status="blocked", checks=tuple(checks))

    def _reason(
        self,
        spec: GeneralTaskSpec,
        domain: TaskDomain,
        descriptor: CapabilityDescriptor | None,
        access: CapabilityAccess,
        context_tokens: int,
    ) -> str:
        if descriptor is None:
            return "unknown_capability"
        if descriptor.domain != domain:
            return "domain_mismatch"
        if descriptor.id not in spec.allowed_capabilities:
            return "outside_task_scope"
        if (
            descriptor.identity() not in access.capability_pins
            or not set(descriptor.permissions) <= set(access.permissions)
            or _RISK[descriptor.risk] > _RISK[access.max_risk]
            or spec.risk_level is not None
            and _RISK[descriptor.risk] > _RISK[spec.risk_level]
        ):
            return "grant_required"
        if descriptor.effect == "external_write":
            return "action_outbox_required"
        if descriptor.effect == "workspace_write" and not access.workspace_write:
            return "workspace_write_denied"
        state = self.health(descriptor.id)
        if state != "ready":
            return state
        if estimate_tokens([descriptor.model_schema()]) > context_tokens:
            return "schema_context_budget"
        return "selected"

    def _model_tools(self, decision: RouteDecision) -> list[dict[str, Any]]:
        """An audited single-node decision exposes only the selected schema."""
        descriptor = self.definition(decision.chosen_id) if decision.chosen_id else None
        if (
            decision.status == "selected"
            and descriptor is not None
            and descriptor.identity() == decision.descriptor_sha256
            and self.health(descriptor.id) == "ready"
        ):
            return [descriptor.model_schema()]
        return []
