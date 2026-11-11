"""Streamable HTTP MCP server exposing SRE tools.

Every tool comes from the registry (``src/agent/tool_registry.py``): its name, description and
input schema (with each parameter's description and limits) are the LangChain tool's own, and
each call is validated against that schema before the tool runs. Served over Streamable HTTP so
MCP clients (Claude Code, the claude.ai connector, other homelab apps) can call the tools directly
without going through the agent loop.

Auth is handled externally by Cloudflare Access.
"""

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from langchain_core.tools import ToolException
from mcp.types import ToolAnnotations
from pydantic import PrivateAttr

from src.agent.tool_registry import ToolSpec, enabled_specs, input_schema, run_tool
from src.config import Settings, get_settings

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
            # The tool's own failure message (an upstream error, a bad name): an isError result.
            raise ToolError(str(e)) from e
        return ToolResult(content=text)


def build_fastmcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server from the registry's ``mcp`` surface, as ``settings`` enables it."""
    if settings is None:
        settings = get_settings()

    mcp = FastMCP("sre-assistant")
    specs = enabled_specs(settings, "mcp")
    for spec in specs:
        mcp.add_tool(RegistryTool.from_spec(spec))
    logger.info("MCP: %d tools registered", len(specs))
    return mcp
