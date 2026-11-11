"""Tests for the SDK agent's in-process tool server (src/agent/mcp_tools.py)."""

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from claude_agent_sdk import SdkMcpTool
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ListToolsRequest

from src.agent.mcp_tools import build_mcp_server, build_sdk_tools
from src.agent.tool_registry import enabled_specs, get_spec, input_schema


def _tool(settings: Any, name: str) -> SdkMcpTool[Any]:
    return next(t for t in build_sdk_tools(settings) if t.name == name)


def _text(result: dict[str, Any]) -> str:
    return "".join(block["text"] for block in result["content"])


async def _list_via_server(settings: Any, exclude: set[str] | None = None) -> list[str]:
    """Tool names as the SDK lists them to the CLI."""
    server = build_mcp_server(settings, exclude=exclude or set())["instance"]
    result = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    return [tool.name for tool in result.root.tools]


async def _call_via_server(settings: Any, name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Call a tool the way the SDK does for the CLI: through the server's tools/call handler."""
    server = build_mcp_server(settings)["instance"]
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    result = await server.request_handlers[CallToolRequest](request)
    assert isinstance(result.root, CallToolResult)
    return result.root


class TestBuildMcpServer:
    def test_returns_sdk_server_config(self, mock_settings: Any) -> None:
        server = build_mcp_server()
        assert server["name"] == "sre"
        assert server["type"] == "sdk"

    def test_tools_are_the_registry_agent_surface(self, mock_settings: Any) -> None:
        mock_settings.proxmox_url = ""
        tools = build_sdk_tools(mock_settings)
        assert [t.name for t in tools] == [s.name for s in enabled_specs(mock_settings, "agent")]
        assert "proxmox_list_guests" not in [t.name for t in tools]
        for t in tools:
            spec = get_spec(t.name)
            assert spec is not None
            assert t.input_schema == input_schema(spec)
            assert t.description == spec.tool.description

    async def test_exclude_reaches_the_server(self, mock_settings: Any) -> None:
        names = await _list_via_server(mock_settings, exclude={"runbook_search"})
        assert "runbook_search" not in names
        assert "prometheus_instant_query" in names


class TestSdkHandler:
    @pytest.mark.integration
    @respx.mock
    async def test_sdk_handler_rejects_out_of_range_limit(self, mock_settings: Any) -> None:
        """The handler validates through run_tool, so Loki is never asked for 1500 lines."""
        empty = {"status": "success", "data": {"resultType": "streams", "result": []}}
        route = respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json=empty)
        )
        result = await _tool(mock_settings, "loki_query_logs").handler({"query": '{job="x"}', "limit": 1500})

        assert result.get("is_error") is True
        assert "limit: Input should be less than or equal to 1000" in _text(result)
        assert not route.called

    async def test_sdk_handler_refuses_unknown_argument(self, mock_settings: Any) -> None:
        result = await _tool(mock_settings, "prometheus_search_metrics").handler({"search_term": "cpu", "limt": 5})
        assert result.get("is_error") is True
        assert "Unknown argument(s) for prometheus_search_metrics: limt" in _text(result)

    @pytest.mark.integration
    async def test_tool_exception_reaches_the_cli_as_is_error(self, mock_settings: Any) -> None:
        """A ToolException is an error result on the SDK server, not a success the model reads as data."""
        result = await _call_via_server(
            mock_settings, "prometheus_range_query", {"query": "up", "start": "not-a-time", "end": "now"}
        )
        assert result.isError is True
        assert "Invalid range query parameters" in result.content[0].text  # type: ignore[union-attr]

    async def test_unexpected_exception_is_logged_and_an_error(
        self, mock_settings: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("src.agent.mcp_tools.run_tool", AsyncMock(side_effect=RuntimeError("socket gone"))),
            caplog.at_level(logging.ERROR, logger="src.agent.mcp_tools"),
        ):
            result = await _call_via_server(mock_settings, "get_current_time", {})

        assert result.isError is True
        assert result.content[0].text == "Tool error: RuntimeError: socket gone"  # type: ignore[union-attr]
        [record] = caplog.records
        assert record.getMessage() == "SDK tool get_current_time failed"
        assert record.exc_info is not None

    @pytest.mark.integration
    @respx.mock
    async def test_loki_label_values_passes_window(self, mock_settings: Any) -> None:
        """The SDK handler passes start and end to the tool, which sends them to Loki."""
        route = respx.get("http://loki.test:3100/loki/api/v1/label/container/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": ["portainer"]})
        )
        labels_tool = _tool(mock_settings, "loki_list_label_values")
        assert {"start", "end"} <= set(labels_tool.input_schema["properties"])  # type: ignore[index]

        result = await labels_tool.handler(
            {"label": "container", "start": "2026-10-24T00:00:00Z", "end": "2026-10-31T00:00:00Z"}
        )
        params = route.calls.last.request.url.params
        assert params["start"] == "1792800000000000000"
        assert params["end"] == "1793404800000000000"
        assert "is_error" not in result
        assert "from 2026-10-24T00:00:00Z to 2026-10-31T00:00:00Z" in _text(result)
