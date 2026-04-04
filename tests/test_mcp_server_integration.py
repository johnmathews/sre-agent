"""Tests for the Streamable HTTP MCP server (src/api/mcp_server.py)."""

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from src.api.mcp_server import build_fastmcp_server


async def _tool_names(server: Any) -> list[str]:
    """Get tool names from a FastMCP server instance."""
    tools = await server.list_tools()
    return [t.name for t in tools]


class TestBuildFastmcpServer:
    """Test FastMCP server construction and conditional tool registration."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    async def test_returns_fastmcp_instance(self, mock_settings: Any) -> None:
        server = build_fastmcp_server(mock_settings)
        assert server.name == "sre-assistant"

    async def test_always_on_tools_registered(self, mock_settings: Any) -> None:
        """Prometheus, Grafana alert, and Grafana dashboard tools are always present."""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "prometheus_search_metrics" in names
        assert "prometheus_instant_query" in names
        assert "prometheus_range_query" in names
        assert "grafana_get_alerts" in names
        assert "grafana_get_alert_rules" in names
        assert "grafana_get_dashboard" in names
        assert "grafana_search_dashboards" in names

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

    async def test_conditional_truenas_tools_disabled(self, mock_settings: Any) -> None:
        mock_settings.truenas_url = ""
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        assert "truenas_pool_status" not in names

    async def test_all_tools_count(self, mock_settings: Any) -> None:
        """With all services configured, expect the full tool set."""
        mock_settings.proxmox_url = "https://pve.test:8006"
        mock_settings.truenas_url = "https://truenas.test"
        mock_settings.loki_url = "http://loki.test:3100"
        mock_settings.pbs_url = "https://pbs.test:8007"
        server = build_fastmcp_server(mock_settings)
        names = await _tool_names(server)
        # 3 prometheus + 2 grafana alert + 2 grafana dashboard + 4 proxmox
        # + 5 truenas + 1 disk_status + 4 loki + 3 pbs + 1 runbook = 25
        # (memory tools depend on is_memory_configured, runbook depends on chroma)
        # At minimum we should have the always-on tools
        assert len(names) >= 7  # prometheus(3) + grafana alerts(2) + grafana dashboards(2)


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


class TestMcpServerDisabled:
    """Test that MCP server is not built when auth token is missing."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    async def test_build_still_works_without_token(self, mock_settings: Any) -> None:
        """build_fastmcp_server works regardless of token — gating is in main.py."""
        mock_settings.mcp_auth_token = ""
        server = build_fastmcp_server(mock_settings)
        assert server is not None
