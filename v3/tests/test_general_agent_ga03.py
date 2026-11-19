"""Check scoped routing and authenticated MCP reflection without remote model calls."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from test_general_agent_ga02 import SOURCE, allowed, capability, claim, grant, graph, node, start

from mgar_v3.domain import DomainError
from mgar_v3.general_agent import capabilities, mcp_discovery
from mgar_v3.general_agent.capabilities import (
    CapabilityAccess,
    CapabilityRegistry,
    DiscoveredTool,
    MCPInventory,
    MCPServerPin,
    MCPToolPin,
    Resources,
)
from mgar_v3.general_agent.mcp_discovery import AuthenticatedMCPDiscovery

REMOTE = "research.remote"
ENDPOINT = "https://mcp.example.test/v1"
REMOTE_PROVIDER = "research-mcp"
TOOL = "read_indices"


def remote():
    definition = capability(REMOTE, provider="mcp", provider_id=REMOTE_PROVIDER)
    pin = MCPServerPin(
        provider_id=REMOTE_PROVIDER,
        endpoint=ENDPOINT,
        version="v1",
        tools=(MCPToolPin(capability_id=REMOTE, tool_name=TOOL),),
    )
    return definition, pin


def inventory(descriptor, **changes):
    return MCPInventory(
        server_version="v1",
        tools=(
            DiscoveredTool.model_validate(
                {
                    "name": TOOL,
                    "input_schema": descriptor.input_schema,
                    "output_schema": descriptor.output_schema,
                    **changes,
                }
            ),
        ),
    )


def access(*items, **changes):
    return grant(
        SimpleNamespace(task_id=uuid4(), project_id="project", revision=1),
        *items,
        **changes,
    )


def test_native_mcp_and_executor_share_catalog_without_cross_domain_exposure():
    d, pin = remote()
    coding = capability("coding.read", provider="executor")
    source = capability()
    data = capability("data.analyze", provider="executor")
    office = capability("office.draft", provider="native")
    all_types = (coding, source, d, data, office)
    registry = CapabilityRegistry(all_types, mcp_servers=[pin])
    value = allowed(*(item.id for item in all_types))
    grant_all = access(*all_types)

    def route(*keys, domain="research", current=grant_all, context=4096):
        return registry.route(value, domain, keys, current, context_tokens=context)

    assert route(data.id, coding.id, source.id).chosen_id == SOURCE
    assert [item.reason for item in route(data.id, coding.id, source.id).checks] == [
        "domain_mismatch",
        "domain_mismatch",
        "selected",
    ]
    assert route(coding.id, domain="coding").chosen_id == coding.id
    assert route(data.id, domain="data").chosen_id == data.id
    assert route(office.id, domain="office").chosen_id == office.id
    assert route(d.id, source.id).checks[0].reason == "unavailable"
    assert route(source.id, context=0).checks[0].reason == "schema_context_budget"
    assert route(source.id, current=access()).checks[0].reason == "grant_required"
    exposed = registry._model_tools(route(d.id, source.id, office.id))
    assert exposed == [source.model_schema()]
    assert registry._model_tools(route(source.id, context=0)) == []
    assert (
        registry.route(allowed(), "research", (REMOTE,), grant_all, context_tokens=4096)
        .checks[0]
        .reason
        == "outside_task_scope"
    )
    mutable_copy = registry.definition(SOURCE)
    mutable_copy.input_schema["type"] = "string"
    assert registry.definition(SOURCE).identity() == source.identity()


@pytest.mark.parametrize(
    "effect, expected",
    [
        ("external_write", "action_outbox_required"),
        ("workspace_write", "workspace_write_denied"),
    ],
)
def test_route_preserves_effect_authority(effect, expected):
    # Request metadata cannot authorize either kind of write.
    d = capability(effect=effect)
    registry = CapabilityRegistry([d])
    result = registry.route(allowed(), "research", (SOURCE,), access(d), context_tokens=4096)
    assert result.status == "blocked" and result.checks[0].reason == expected
    granted = registry.route(
        allowed(),
        "research",
        (SOURCE,),
        access(d, workspace_write=True),
        context_tokens=4096,
    )
    assert granted.status == ("selected" if effect == "workspace_write" else "blocked")


def test_registry_rejects_untrusted_schema_and_version_pins():
    with pytest.raises(ValidationError, match="self-contained"):
        capability(input_schema={"type": "object", "$ref": "https://schema.example.test/file"})
    with pytest.raises(ValidationError, match="invalid"):
        capability(output_schema={"type": "object", "required": "not-an-array"})
    with pytest.raises(ValidationError, match="HTTPS"):
        MCPServerPin(
            provider_id=REMOTE_PROVIDER,
            endpoint="http://mcp.example.test",
            version="v1",
            tools=(MCPToolPin(capability_id=REMOTE, tool_name=TOOL),),
        )
    d, pin = remote()
    with pytest.raises(ValueError, match="Duplicate"):
        CapabilityRegistry([d, d], mcp_servers=[pin])
    with pytest.raises(ValueError, match="undeclared"):
        CapabilityRegistry([capability(REMOTE)], mcp_servers=[pin])
    newer = capability(version="v2")
    v1 = capability()
    assert newer.identity() != v1.identity()
    registry = CapabilityRegistry([newer])
    blocked = registry.route(allowed(), "research", (SOURCE,), access(v1), context_tokens=4096)
    assert blocked.checks[0].reason == "grant_required"


def test_remote_health_is_not_authority_and_timeout_fallback_is_explainable(monkeypatch):
    d, pin = remote()
    native = capability()
    registry = CapabilityRegistry([d, native], mcp_servers=[pin])
    value = allowed(REMOTE, SOURCE)
    granted = access(d, native)

    def route(current=granted):
        return registry.route(value, "research", (REMOTE, SOURCE), current, context_tokens=4096)

    async def reflect(reply):
        async def fetch(configured):
            assert configured == pin
            return reply

        return await registry.discover(REMOTE_PROVIDER, fetch)

    async def exercise():
        with pytest.raises(DomainError) as denied:
            await registry.discover("unapproved", lambda _: inventory(d))
        assert denied.value.code == "GENERAL_MCP_UNTRUSTED"
        assert await reflect(inventory(d)) == {REMOTE: "ready"}
        assert route().chosen_id == REMOTE
        assert route(access(native)).checks[0].reason == "grant_required"
        assert await reflect(inventory(d).model_copy(update={"server_version": "v2"})) == {
            REMOTE: "version_mismatch"
        }
        assert route().chosen_id == SOURCE and route().checks[0].reason == "version_mismatch"
        assert await reflect(inventory(d, output_schema=None)) == {REMOTE: "schema_mismatch"}
        assert route().checks[0].reason == "schema_mismatch"
        assert await reflect(inventory(d, input_schema={"type": "object", "maxProperties": 0})) == {
            REMOTE: "schema_mismatch"
        }

        async def slow(_):
            await asyncio.sleep(1)

        assert await registry.discover(REMOTE_PROVIDER, slow, timeout=0.01) == {REMOTE: "timeout"}
        assert route().checks[0].reason == "timeout"
        assert await reflect(inventory(d)) == {REMOTE: "ready"}

    asyncio.run(exercise())

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(minutes=2)

    monkeypatch.setattr(capabilities, "datetime", Later)
    assert route().checks[0].reason == "unavailable"


def test_older_concurrent_discovery_cannot_restore_outdated_health():
    d, pin = remote()
    registry = CapabilityRegistry([d], mcp_servers=[pin])

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow(_):
            entered.set()
            await release.wait()
            return inventory(d)

        async def mismatch(_):
            return inventory(d).model_copy(update={"server_version": "v2"})

        older = asyncio.create_task(registry.discover(REMOTE_PROVIDER, slow))
        await entered.wait()
        await registry.discover(REMOTE_PROVIDER, mismatch)
        release.set()
        await older
        assert registry.health(REMOTE) == "version_mismatch"

    asyncio.run(exercise())


def test_http_discovery_uses_exact_authenticated_endpoint_and_only_lists_tools(monkeypatch):
    d, pin = remote()
    observed = []

    @asynccontextmanager
    async def transport(endpoint, *, http_client):
        observed.append("connect")
        assert endpoint == ENDPOINT
        assert http_client.headers["authorization"] == "Bearer local-fixture-credential"
        assert http_client.follow_redirects is False and http_client.trust_env is False
        yield "reader", "writer", None

    class Session:
        def __init__(self, reader, writer):
            assert (reader, writer) == ("reader", "writer")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            observed.append("initialize")
            return SimpleNamespace(serverInfo=SimpleNamespace(version="v1"))

        async def list_tools(self):
            observed.append("list_tools")
            return SimpleNamespace(
                nextCursor=None,
                tools=[
                    SimpleNamespace(
                        name=TOOL, inputSchema=d.input_schema, outputSchema=d.output_schema
                    )
                ],
            )

    monkeypatch.setattr(mcp_discovery, "streamable_http_client", transport)
    monkeypatch.setattr(mcp_discovery, "ClientSession", Session)
    discovery = AuthenticatedMCPDiscovery(
        REMOTE_PROVIDER, ENDPOINT, SecretStr("local-fixture-credential")
    )
    assert asyncio.run(discovery(pin)) == inventory(d)
    assert observed == ["connect", "initialize", "list_tools"]
    with pytest.raises(DomainError) as rejected:
        asyncio.run(
            discovery(pin.model_copy(update={"endpoint": "https://untrusted.example.test"}))
        )
    assert rejected.value.code == "GENERAL_MCP_DESTINATION"


def test_graph_records_input_and_resource_fallback_and_projects_only_chosen_tool(engine):
    invalid = capability(
        "research.needs_input",
        input_schema={
            "type": "object",
            "required": ["new_input"],
        },
    )
    expensive = capability("research.expensive", upper_bound=Resources(tool_calls=2))
    d = capability()
    registry = CapabilityRegistry([invalid, expensive, d])
    store = engine.general_graphs
    store.registry = registry
    choices = (invalid.id, expensive.id, d.id)
    task, state, access = start(
        engine,
        graph(node("work", outputs=("result",), capabilities=choices)),
        allowed(*choices),
        invalid,
        expensive,
        d,
    )
    outside = CapabilityAccess.model_validate({**access.model_dump(), "project_id": "other"})
    with pytest.raises(DomainError) as denied:
        claim(store, state, "work", outside)
    assert denied.value.code == "GENERAL_GRAPH_ACCESS"
    selection = claim(store, state, "work", access)
    assert selection.capability_id == SOURCE
    assert [
        item["reason"] for item in store.events(task.task_id)[0]["payload"]["decision"]["checks"]
    ] == ["input_schema", "resource_budget", "selected"]
    assert store.model_tools(selection, access, context_tokens=4096) == [d.model_schema()]
    assert store.model_tools(selection, access, context_tokens=0) == []
