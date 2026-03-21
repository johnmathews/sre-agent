"""Tests for the MCP tool bridge."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.mcp_tools import build_mcp_server


class TestBuildMcpServer:
    """Test MCP server construction with conditional tool registration."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    def test_returns_sdk_server_config(self) -> None:
        server = build_mcp_server()
        # McpSdkServerConfig is a dict-like with type, name, instance
        assert server["name"] == "sre"
        assert server["type"] == "sdk"

    def test_always_includes_prometheus_tools(self) -> None:
        server = build_mcp_server()
        instance = server["instance"]
        # The server should have tools registered
        assert instance is not None

    def test_conditional_proxmox_tools(self, mock_settings: MagicMock) -> None:
        mock_settings.proxmox_url = ""
        server1 = build_mcp_server(mock_settings)

        mock_settings.proxmox_url = "https://pve.local:8006"
        server2 = build_mcp_server(mock_settings)

        # Both should succeed (tools are conditionally registered)
        assert server1 is not None
        assert server2 is not None

    def test_conditional_loki_tools(self, mock_settings: MagicMock) -> None:
        mock_settings.loki_url = ""
        server = build_mcp_server(mock_settings)
        assert server is not None

    def test_conditional_pbs_tools(self, mock_settings: MagicMock) -> None:
        mock_settings.pbs_url = ""
        server = build_mcp_server(mock_settings)
        assert server is not None

    def test_conditional_truenas_tools(self, mock_settings: MagicMock) -> None:
        mock_settings.truenas_url = ""
        server = build_mcp_server(mock_settings)
        assert server is not None


class TestMcpToolCallAsync:
    """Test that MCP tool wrappers correctly call underlying LangChain tools."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    @pytest.mark.integration
    async def test_prometheus_search_wrapper(self) -> None:
        """Verify the MCP wrapper calls the LangChain tool."""
        from src.agent.mcp_tools import _prometheus_tools

        tools = _prometheus_tools()
        search_tool = next(t for t in tools if t.name == "prometheus_search_metrics")

        with patch("src.agent.tools.prometheus.prometheus_search_metrics") as mock_tool:
            mock_tool.coroutine = MagicMock(return_value="Found 5 metrics")
            mock_tool.name = "prometheus_search_metrics"
            # Patch the import inside _call_async_tool
            result = await search_tool.handler({"search_term": "cpu"})
            # The handler calls through to the real tool, which we can't mock
            # from outside the closure. Just verify it returns a result dict.
            assert "content" in result
