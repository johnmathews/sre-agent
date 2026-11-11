"""Tests for the Streamable HTTP MCP server (src/api/mcp_server.py), built from the tool registry."""

import difflib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import mcp.types
import pytest
import respx
from fastapi.testclient import TestClient
from fastmcp import Client

from src.api.mcp_server import build_fastmcp_server
from tests.test_mcp_http_integration import _initialize, _rpc

LOKI = "http://loki.test:3100"
PROM = "http://prometheus.test:9090"
SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_tools_contract.json"
SNAPSHOT_ENV = "UPDATE_MCP_SNAPSHOT"
REGENERATE = (
    f"{SNAPSHOT_ENV}=1 uv run pytest tests/test_mcp_server_integration.py -k test_mcp_contract_matches_snapshot"
)


async def _tool_names(server: Any) -> list[str]:
    """Get tool names from a FastMCP server instance."""
    tools = await server.list_tools()
    return [t.name for t in tools]


def _enable_everything(settings: Any, tmp_path: Path) -> None:
    """Turn on every optional integration, so /mcp lists every tool."""
    history = tmp_path / "history"
    history.mkdir(exist_ok=True)
    settings.conversation_history_dir = str(history)
    settings.memory_db_path = str(tmp_path / "memory.db")


async def _mcp_tools(settings: Any) -> list[mcp.types.Tool]:
    async with Client(build_fastmcp_server(settings)) as client:
        return await client.list_tools()


@contextmanager
def _http_client() -> Iterator[TestClient]:
    """The real app, its lifespan entered and an MCP session initialized."""
    with patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")):
        from src.api.main import app

        with TestClient(app) as client:
            _initialize(client)
            yield client


def _http_tools(client: TestClient) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = _rpc(client, 2, "tools/list", {})["result"]["tools"]
    return tools


def _http_call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = _rpc(client, 3, "tools/call", {"name": name, "arguments": arguments})["result"]
    return result


class TestBuildFastmcpServer:
    """Test FastMCP server construction and conditional tool registration."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    async def test_returns_fastmcp_instance(self, mock_settings: Any) -> None:
        server = build_fastmcp_server(mock_settings)
        assert server.name == "sre-assistant"

    async def test_always_on_tools_registered(self, mock_settings: Any) -> None:
        """Clock, Prometheus, Grafana alert, Grafana dashboard and runbook tools are always present."""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "get_current_time" in names
        assert "prometheus_search_metrics" in names
        assert "prometheus_instant_query" in names
        assert "prometheus_range_query" in names
        assert "grafana_get_alerts" in names
        assert "grafana_get_alert_rules" in names
        assert "grafana_get_dashboard" in names
        assert "grafana_search_dashboards" in names
        assert "runbook_search" in names

    async def test_conditional_proxmox_tools_enabled(self, mock_settings: Any) -> None:
        mock_settings.proxmox_url = "https://pve.test:8006"
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "proxmox_list_guests" in names
        assert "proxmox_node_status" in names

    async def test_conditional_proxmox_tools_disabled(self, mock_settings: Any) -> None:
        mock_settings.proxmox_url = ""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "proxmox_list_guests" not in names

    async def test_conditional_loki_tools_enabled(self, mock_settings: Any) -> None:
        mock_settings.loki_url = "http://loki.test:3100"
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "loki_query_logs" in names
        assert "loki_correlate_changes" in names

    async def test_conditional_loki_tools_disabled(self, mock_settings: Any) -> None:
        mock_settings.loki_url = ""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "loki_query_logs" not in names

    async def test_conditional_pbs_tools_enabled(self, mock_settings: Any) -> None:
        mock_settings.pbs_url = "https://pbs.test:8007"
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "pbs_datastore_status" in names

    async def test_conditional_pbs_tools_disabled(self, mock_settings: Any) -> None:
        mock_settings.pbs_url = ""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "pbs_datastore_status" not in names

    async def test_conditional_truenas_tools_enabled(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = "https://truenas.test"
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "truenas_pool_status" in names
        assert "truenas_apps" in names
        assert "hdd_power_status" in names

    async def test_conditional_truenas_tools_disabled(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "truenas_pool_status" not in names
        assert "hdd_power_status" not in names

    async def test_memory_tools_follow_memory_db_path(self, mock_settings: Any, tmp_path: Path) -> None:
        mock_settings.memory_db_path = ""
        assert "memory_search_incidents" not in await _tool_names(build_fastmcp_server(mock_settings))
        mock_settings.memory_db_path = str(tmp_path / "memory.db")
        names = await _tool_names(build_fastmcp_server(mock_settings))
        assert {"memory_search_incidents", "memory_record_incident"} <= set(names)


class TestMcpToolExecution:
    """Test tool execution through the MCP protocol using in-memory client."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    @pytest.mark.integration
    async def test_list_tools_via_client(self, mock_settings: Any) -> None:
        """Verify tool listing through the MCP client protocol."""
        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]
            assert "prometheus_search_metrics" in tool_names
            assert "grafana_get_alerts" in tool_names

    @pytest.mark.integration
    @respx.mock
    async def test_call_prometheus_search(self, mock_settings: Any) -> None:
        """Call prometheus_search_metrics through the MCP protocol with mocked HTTP."""
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": ["node_cpu_seconds_total", "node_cpu_guest_seconds_total"]},
            )
        )
        # Metadata endpoint may also be called
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )

        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            result = await client.call_tool("prometheus_search_metrics", {"search_term": "cpu"})
            assert "cpu" in str(result).lower()

    @pytest.mark.integration
    @respx.mock
    async def test_call_grafana_get_alerts(self, mock_settings: Any) -> None:
        """Call grafana_get_alerts through the MCP protocol with mocked HTTP."""
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(200, json=[])
        )

        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            result = await client.call_tool("grafana_get_alerts", {})
            assert result is not None

    @pytest.mark.integration
    @respx.mock
    async def test_call_loki_list_label_values_with_window(self, mock_settings: Any) -> None:
        """start and end pass through the MCP wrapper to Loki."""
        mock_settings.loki_url = "http://loki.test:3100"
        route = respx.get("http://loki.test:3100/loki/api/v1/label/container/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": ["portainer"]})
        )

        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            result = await client.call_tool(
                "loki_list_label_values",
                {"label": "container", "start": "2026-10-24T00:00:00Z", "end": "2026-10-31T00:00:00Z"},
            )
        params = route.calls.last.request.url.params
        assert params["start"] == "1792800000000000000"
        assert params["end"] == "1793404800000000000"
        assert "from 2026-10-24T00:00:00Z to 2026-10-31T00:00:00Z" in str(result)

    @pytest.mark.integration
    @respx.mock
    async def test_tool_error_propagates(self, mock_settings: Any) -> None:
        """HTTP errors from tools should propagate through MCP as errors."""
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            with pytest.raises(  # noqa: PT011
                (Exception,),
                match="500|Internal Server Error|ToolError",
            ):
                await client.call_tool("prometheus_search_metrics", {"search_term": "cpu"})

    @pytest.mark.integration
    async def test_get_current_time_on_mcp(self, mock_settings: Any) -> None:
        """The clock tool is on /mcp, so clients can anchor relative time ranges."""
        async with Client(build_fastmcp_server(mock_settings)) as client:
            result = await client.call_tool("get_current_time", {})
        text = result.content[0].text  # type: ignore[union-attr]
        assert "UTC ISO:" in text
        assert "User timezone:  UTC" in text


class TestMcpConversationTools:
    """Test conversation history MCP tools."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    async def test_conversation_tools_registered(self, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "sre_agent_list_conversations" in names
        assert "sre_agent_get_conversation" in names

    async def test_conversation_tools_disabled_when_no_dir(self, mock_settings: Any) -> None:
        mock_settings.conversation_history_dir = ""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "sre_agent_list_conversations" not in names
        assert "sre_agent_get_conversation" not in names

    @pytest.mark.integration
    async def test_list_conversations_empty(self, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            result = await client.call_tool("sre_agent_list_conversations", {})
            assert "No conversations found" in str(result)

    @pytest.mark.integration
    async def test_list_and_get_conversation(self, mock_settings: Any, tmp_path: Any) -> None:
        from src.agent.history import save_turn

        mock_settings.conversation_history_dir = str(tmp_path)

        save_turn(str(tmp_path), "abc12345", "user", "What alerts are firing?", "gpt-4o", "openai")
        save_turn(str(tmp_path), "abc12345", "assistant", "No alerts firing.", "gpt-4o", "openai")

        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            list_result = await client.call_tool("sre_agent_list_conversations", {})
            assert "abc12345" in str(list_result)
            assert "What alerts are firing?" in str(list_result)

            get_result = await client.call_tool("sre_agent_get_conversation", {"session_id": "abc12345"})
            assert "What alerts are firing?" in str(get_result)
            assert "No alerts firing." in str(get_result)

    @pytest.mark.integration
    async def test_get_conversation_not_found(self, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        server = build_fastmcp_server(mock_settings)
        async with Client(server) as client:
            result = await client.call_tool("sre_agent_get_conversation", {"session_id": "nonexist"})
            assert "not found" in str(result)

    @pytest.mark.integration
    async def test_get_conversation_rejects_glob_session_id(self, mock_settings: Any, tmp_path: Any) -> None:
        """A wildcard session_id is refused by the schema, so it cannot match another session's file."""
        from src.agent.history import save_turn

        mock_settings.conversation_history_dir = str(tmp_path)
        save_turn(str(tmp_path), "other123", "user", "A secret question", "gpt-4o", "openai")
        async with Client(build_fastmcp_server(mock_settings)) as client:
            result = await client.call_tool("sre_agent_get_conversation", {"session_id": "*"}, raise_on_error=False)
        assert result.is_error
        text = result.content[0].text  # type: ignore[union-attr]
        assert "Invalid arguments for sre_agent_get_conversation: session_id: String should match pattern" in text
        assert "A secret question" not in text

    @pytest.mark.integration
    async def test_list_conversations_limit_bounded(self, mock_settings: Any, tmp_path: Any) -> None:
        from src.agent.history import save_turn

        mock_settings.conversation_history_dir = str(tmp_path)
        for session in ("first111", "second22"):
            save_turn(str(tmp_path), session, "user", f"question {session}", "gpt-4o", "openai")
        async with Client(build_fastmcp_server(mock_settings)) as client:
            too_many = await client.call_tool("sre_agent_list_conversations", {"limit": 101}, raise_on_error=False)
            one = await client.call_tool("sre_agent_list_conversations", {"limit": 1})
        assert too_many.is_error
        assert "less than or equal to 100" in too_many.content[0].text  # type: ignore[union-attr]
        assert "Found 1 conversation(s)" in one.content[0].text  # type: ignore[union-attr]


async def test_truenas_snapshots_exposes_list_snapshots(mock_settings: Any) -> None:
    """/mcp must accept list_snapshots, or clients cannot reach the snapshot listing."""
    mock_settings.truenas_url = "https://truenas.test"
    server = build_fastmcp_server(mock_settings)
    tool = next(t for t in await server.list_tools() if t.name == "truenas_snapshots")
    assert "list_snapshots" in tool.parameters["properties"]


# ---------------------------------------------------------------------------
# The /mcp contract: annotations and the snapshot
# ---------------------------------------------------------------------------


async def test_read_only_annotations(mock_settings: Any, tmp_path: Path) -> None:
    _enable_everything(mock_settings, tmp_path)
    tools = await _mcp_tools(mock_settings)
    not_read_only = sorted(t.name for t in tools if not (t.annotations and t.annotations.readOnlyHint))
    assert not_read_only == ["memory_record_incident"]
    local = {t.name for t in tools if t.annotations and t.annotations.openWorldHint is False}
    assert "prometheus_instant_query" not in local
    assert {"get_current_time", "runbook_search", "sre_agent_get_conversation"} <= local


async def test_memory_record_incident_not_read_only(mock_settings: Any, tmp_path: Path) -> None:
    _enable_everything(mock_settings, tmp_path)
    tool = next(t for t in await _mcp_tools(mock_settings) if t.name == "memory_record_incident")
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is False


_CONSTRAINT_KEYS = (
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
)


def _param_contract(schema: dict[str, Any], required: bool) -> dict[str, Any]:
    """Type, default, limits and required for one parameter (descriptions are left out on purpose)."""
    branches: list[dict[str, Any]] = schema.get("anyOf") or schema.get("oneOf") or [schema]
    types: list[str] = []
    contract: dict[str, Any] = {}
    for branch in branches:
        if "type" in branch:
            types.append(str(branch["type"]))
        if "items" in branch:
            contract["items"] = branch["items"].get("type")
        contract.update({k: branch[k] for k in _CONSTRAINT_KEYS if k in branch})
    contract.update({k: schema[k] for k in _CONSTRAINT_KEYS if k in schema})
    contract["type"] = "|".join(types)
    if "default" in schema:
        contract["default"] = schema["default"]
    contract["required"] = required
    return dict(sorted(contract.items()))


def _contract(tools: list[mcp.types.Tool]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tool in sorted(tools, key=lambda t: t.name):
        required = set(tool.inputSchema.get("required", []))
        properties: dict[str, Any] = tool.inputSchema.get("properties", {})
        out[tool.name] = {
            "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else None,
            "parameters": {name: _param_contract(p, name in required) for name, p in sorted(properties.items())},
        }
    return {"tools": out}


@pytest.mark.integration
async def test_mcp_contract_matches_snapshot(mock_settings: Any, tmp_path: Path) -> None:
    """Names, parameters, defaults, limits and annotations of every /mcp tool, with every integration on.

    Renaming a parameter, changing a default or a limit, or dropping a tool turns this red. If the
    change is intended, regenerate the snapshot (see REGENERATE) and commit it with the change.
    """
    _enable_everything(mock_settings, tmp_path)
    actual = json.dumps(_contract(await _mcp_tools(mock_settings)), indent=2, sort_keys=True) + "\n"
    if os.environ.get(SNAPSHOT_ENV) == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(actual)
        return
    expected = SNAPSHOT.read_text() if SNAPSHOT.exists() else ""
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{SNAPSHOT.name} (committed)",
                tofile=f"{SNAPSHOT.name} (from /mcp now)",
            )
        )
        pytest.fail(
            f"The /mcp tool contract changed:\n{diff}\n"
            f"If this change is intended, regenerate the snapshot and commit it:\n  {REGENERATE}",
            pytrace=False,
        )


# ---------------------------------------------------------------------------
# The /mcp contract over real HTTP (the W4 harness)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_every_parameter_has_description(mock_settings: Any, tmp_path: Path) -> None:
    _enable_everything(mock_settings, tmp_path)
    with _http_client() as client:
        tools = _http_tools(client)
    params = [(t["name"], p, s) for t in tools for p, s in t["inputSchema"].get("properties", {}).items()]
    assert params
    missing = [f"{tool}.{param}" for tool, param, schema in params if not schema.get("description")]
    assert not missing, f"{len(missing)} of {len(params)} parameters have no description: {missing}"


@pytest.mark.integration
@respx.mock
def test_limits_enforced_over_mcp(mock_settings: Any) -> None:
    """An out-of-range limit is refused with the schema's own message, before any upstream call."""
    route = respx.get(url__startswith=f"{LOKI}/").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": []}})
    )
    with _http_client() as client:
        result = _http_call(client, "loki_query_logs", {"query": '{job="x"}', "limit": 1500})
    assert result["isError"] is True, result
    text = result["content"][0]["text"]
    assert "less than or equal to 1000" in text
    assert "limit" in text
    assert not route.called


@pytest.mark.integration
def test_no_output_schema(mock_settings: Any, tmp_path: Path) -> None:
    """Text tools declare no outputSchema, so clients show the text rather than a JSON-escaped string."""
    _enable_everything(mock_settings, tmp_path)
    with _http_client() as client:
        tools = _http_tools(client)
    with_schema = sorted(t["name"] for t in tools if t.get("outputSchema"))
    assert not with_schema, f"{len(with_schema)} tools declare an outputSchema: {with_schema}"


@pytest.mark.integration
@respx.mock
def test_tool_exception_reaches_client_as_error(mock_settings: Any) -> None:
    """A ToolException from the tool is an isError result carrying the tool's own message."""
    respx.get(f"{PROM}/api/v1/label/__name__/values").mock(return_value=httpx.Response(500, text="boom upstream"))
    with _http_client() as client:
        result = _http_call(client, "prometheus_search_metrics", {"search_term": "cpu"})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == "Prometheus API error: HTTP 500 - boom upstream"


@pytest.mark.integration
@respx.mock
def test_unknown_argument_rejected_over_mcp(mock_settings: Any) -> None:
    """A guessed parameter name fails with the accepted names, instead of silently using defaults."""
    route = respx.get(url__startswith=f"{LOKI}/").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": []}})
    )
    with _http_client() as client:
        result = _http_call(client, "loki_query_logs", {"query": '{job="x"}', "since": "2d"})
    assert result["isError"] is True, result
    text = result["content"][0]["text"]
    assert "since" in text
    assert "start" in text  # names an accepted parameter
    assert not route.called


@pytest.mark.integration
def test_input_schemas_forbid_extra_properties(mock_settings: Any, tmp_path: Path) -> None:
    """Every input schema says additionalProperties is false, so clients know unknown names are refused."""
    _enable_everything(mock_settings, tmp_path)
    with _http_client() as client:
        tools = _http_tools(client)
    loose = sorted(t["name"] for t in tools if t["inputSchema"].get("additionalProperties") is not False)
    assert not loose, loose
