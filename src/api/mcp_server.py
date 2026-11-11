"""Streamable HTTP MCP server exposing SRE tools.

Every tool comes from the registry (``src/agent/tool_registry.py``): its name, description and
input schema (with each parameter's description and limits) are the LangChain tool's own, and
each call is validated against that schema before the tool runs. Served over Streamable HTTP so
MCP clients (Claude Code, the claude.ai connector, other homelab apps) can call the tools directly
without going through the agent loop.

Each ``tools/call`` passes through ``ToolCallObserver``, which writes the call's one ``tool_call`` log line and
records the tool metrics (``src/observability/tool_calls.py``).

Cloudflare Access guards the tunnel route, and ``McpAccessCheck`` (``src/api/mcp_access.py``) checks the Access JWT on
every HTTP request in front of this server. It sets ``request.state.mcp_client`` (the client label) and, for a verified
caller, ``request.state.mcp_identity_ref``, which ``ToolCallObserver`` passes into the ``tool_call`` line.
"""

import logging
import time
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from langchain_core.tools import ToolException
from mcp.types import ToolAnnotations
from pydantic import PrivateAttr

from src.agent.tool_registry import ToolSpec, enabled_specs, input_schema, run_tool
from src.api.mcp_access import STATE_CLIENT, STATE_IDENTITY_REF
from src.config import Settings, get_settings
from src.observability.tool_calls import classify_tool_error, observe_tool_call

logger = logging.getLogger(__name__)


def tool_annotations(spec: ToolSpec) -> ToolAnnotations:
    """MCP hints: read tools are read-only; the write tool is non-destructive and not idempotent."""
    if spec.read_only:
        return ToolAnnotations(readOnlyHint=True, openWorldHint=spec.open_world)
    return ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=spec.open_world
    )


class RegistryTool(Tool):
    """A FastMCP tool that runs a registry spec.

    ``output_schema`` is ``None``: the tools return text, and a declared ``{"result": string}``
    schema makes clients show that text JSON-escaped.
    """

    _spec: ToolSpec = PrivateAttr()

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> "RegistryTool":
        tool = cls(
            name=spec.name,
            description=spec.tool.description,
            parameters=input_schema(spec),
            output_schema=None,
            annotations=tool_annotations(spec),
        )
        tool._spec = spec
        return tool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            text = await run_tool(self._spec, arguments)
        except ToolException as e:
            # The tool's own failure message (an upstream error, a bad name): an isError result. DEBUG, because
            # FastMCP would log it at ERROR beside the tool_call line that already records it.
            raise ToolError(str(e), log_level=logging.DEBUG) from e
        return ToolResult(content=text)


def mcp_client_label() -> str:
    """The caller's client label: ``request.state.mcp_client`` as the access check set it.

    ``unknown`` for an HTTP request without one, ``in-process`` for a call with no HTTP request (a test, or a client
    in the same process).
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return "in-process"
    client = getattr(request.state, STATE_CLIENT, None)
    return client if isinstance(client, str) and client else "unknown"


def mcp_identity_ref() -> str | None:
    """``request.state.mcp_identity_ref``: set by the access check for a verified caller only, else ``None``."""
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    ref = getattr(request.state, STATE_IDENTITY_REF, None)
    return ref if isinstance(ref, str) and ref else None


def _result_chars(result: Any) -> int:
    """The total length of the text blocks in a tool result (0 for a background-task handle)."""
    content = getattr(result, "content", None) or []
    return sum(len(text) for block in content if isinstance(text := getattr(block, "text", None), str))


class ToolCallObserver(Middleware):
    """Times each ``tools/call``, then writes its log line and metrics with ``source=mcp``.

    A failed call is classified (unknown tool, invalid arguments, timeout, the tool's own error, or internal) and
    the exception re-raised unchanged, so the client gets the same error result as without the observer.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        arguments = context.message.arguments or {}
        client = mcp_client_label()
        ref = mcp_identity_ref()
        start = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            outcome, decided_by = classify_tool_error(e)
            observe_tool_call(
                tool=name,
                source="mcp",
                client=client,
                outcome=outcome,
                error_class=type(decided_by).__name__,
                error=decided_by,
                duration_s=time.monotonic() - start,
                result_chars=0,
                arguments=arguments,
                identity_ref=ref,
            )
            raise
        observe_tool_call(
            tool=name,
            source="mcp",
            client=client,
            outcome="success",
            error_class=None,
            error=None,
            duration_s=time.monotonic() - start,
            result_chars=_result_chars(result),
            arguments=arguments,
            identity_ref=ref,
        )
        return result


def build_fastmcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server from the registry's ``mcp`` surface, as ``settings`` enables it."""
    if settings is None:
        settings = get_settings()

    mcp = FastMCP("sre-assistant", middleware=[ToolCallObserver()])
    specs = enabled_specs(settings, "mcp")
    for spec in specs:
        mcp.add_tool(RegistryTool.from_spec(spec))
    logger.info("MCP: %d tools registered", len(specs))
    return mcp
