"""The SDK agent's in-process tool server, built from the tool registry.

Each tool on the registry's ``agent`` surface becomes an ``SdkMcpTool`` with the same name,
description and input schema that ``/mcp`` serves. The handler runs it through ``run_tool``, so the
SDK agent gets the same validation as ``/mcp``: unknown argument names are refused and limits are
enforced before the tool runs.
"""

import logging
from collections.abc import Awaitable, Callable, Collection
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig
from fastmcp.exceptions import ToolError
from langchain_core.tools import ToolException

from src.agent.tool_registry import ToolSpec, enabled_specs, input_schema, run_tool
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """A tool result with one text block.

    ``create_sdk_mcp_server`` reads the ``is_error`` key (not ``isError``) to set the result's
    ``isError`` flag.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


def _handler(spec: ToolSpec) -> Handler:
    async def handle(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text_result(await run_tool(spec, arguments))
        except (ToolError, ToolException) as e:
            # Invalid arguments, or the tool's own failure message (an upstream error, a bad name).
            return _text_result(str(e), is_error=True)
        except Exception as e:
            # Raising would also give an error result, but with no log line; log it here.
            logger.exception("SDK tool %s failed", spec.name)
            return _text_result(f"Tool error: {type(e).__name__}: {e}", is_error=True)

    return handle


def build_sdk_tools(settings: Settings, exclude: Collection[str] = ()) -> list[SdkMcpTool[Any]]:
    """One ``SdkMcpTool`` per spec on the registry's ``agent`` surface that ``settings`` enables."""
    return [
        SdkMcpTool(
            name=spec.name,
            description=spec.tool.description,
            input_schema=input_schema(spec),
            handler=_handler(spec),
        )
        for spec in enabled_specs(settings, "agent", exclude)
    ]


def build_mcp_server(settings: Settings | None = None, exclude: Collection[str] = ()) -> McpSdkServerConfig:
    """Build the in-process ``sre`` server for the SDK agent, minus the tools named in ``exclude``."""
    if settings is None:
        settings = get_settings()
    tools = build_sdk_tools(settings, exclude)
    logger.info("SDK MCP server: %d tools registered", len(tools))
    return create_sdk_mcp_server("sre", tools=tools)
