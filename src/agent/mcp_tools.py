"""The SDK agent's in-process tool server, built from the tool registry.

Each tool on the registry's ``agent`` surface becomes an ``SdkMcpTool`` with the same name,
description and input schema that ``/mcp`` serves. The handler runs it through ``run_tool``, so the
SDK agent gets the same validation as ``/mcp``: unknown argument names are refused and limits are
enforced before the tool runs.

Each call the handler sees writes one ``tool_call`` log line and the tool metrics with ``source=agent client=agent``
(``src/observability/tool_calls.py``). The ``mcp`` library behind ``create_sdk_mcp_server`` checks arguments against
``inputSchema`` before the handler runs, so a call it refuses never reaches the handler and writes no line.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig
from fastmcp.exceptions import ToolError
from langchain_core.tools import ToolException

from src.agent.tool_registry import ToolSpec, enabled_specs, input_schema, run_tool
from src.config import Settings, get_settings
from src.observability.tool_calls import Outcome, classify_tool_error, observe_tool_call

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
        start = time.monotonic()
        outcome: Outcome = "success"
        failure: BaseException | None = None
        try:
            text = await run_tool(spec, arguments)
            result = _text_result(text)
        except (ToolError, ToolException) as e:
            # Invalid arguments, or the tool's own failure message (an upstream error, a bad name).
            outcome, failure = classify_tool_error(e)
            text = ""
            result = _text_result(str(e), is_error=True)
        except Exception as e:
            # Raising would also give an error result; the tool_call line below logs it with its traceback.
            outcome, failure = classify_tool_error(e)
            text = ""
            result = _text_result(f"Tool error: {type(e).__name__}: {e}", is_error=True)
        observe_tool_call(
            tool=spec.name,
            source="agent",
            client="agent",
            outcome=outcome,
            error_class=type(failure).__name__ if failure is not None else None,
            error=failure,
            duration_s=time.monotonic() - start,
            result_chars=len(text),
            arguments=arguments,
        )
        return result

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
