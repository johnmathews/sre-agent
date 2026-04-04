# MCP Server Endpoint

Added a Streamable HTTP MCP server endpoint at `/mcp`, exposing the SRE assistant's ~25 tools
directly to MCP clients (Claude Code, Claude Desktop, Cursor) without going through the agent loop.

## Why

The existing `/ask` endpoint runs a full agent loop (system prompt, ReAct reasoning, multi-step
tool orchestration) for every question. This is the right approach for complex investigations
but heavyweight for simple queries. During development, wanting to check a Prometheus metric
from Claude Code required switching to the Streamlit UI or hitting `/ask` manually.

MCP gives a second interface to the same tools: direct, single-call, composable with other
MCP servers in the same client session. It also eliminates the double-agent overhead when
Claude Code calls `/ask` (two LLMs reasoning when only one is needed).

## Architecture Decisions

- **FastMCP library** (v3.2.0) over raw `mcp` SDK — provides `@mcp.tool()` decorator,
  `http_app()`, and auth providers. The SDK approach would require manual transport/session plumbing.
- **Stateless HTTP mode** — no session affinity needed across uvicorn workers. Trades off
  elicitation/sampling (server asking client questions), which we don't use.
- **Bearer token auth** via `MCP_AUTH_TOKEN` — MCP endpoint exposes raw PromQL/LogQL execution,
  must not be unauthenticated. Disabled by default (empty token = endpoint not mounted).
- **Separate from `mcp_tools.py`** — the existing SDK bridge wraps tools as `SdkMcpTool` for
  stdio transport. The FastMCP server wraps tools for HTTP transport. Different protocols,
  different libraries, same underlying LangChain tool functions.
- **Lifespan via `AsyncExitStack`** — the MCP Starlette app has its own lifespan for the
  `StreamableHTTPSessionManager`. Entered via `stack.enter_async_context()` inside the existing
  FastAPI lifespan, avoiding import-time settings resolution (test-friendly).

## What Changed

- New: `src/api/mcp_server.py` — FastMCP server builder with 10 factory functions for tool registration
- New: `tests/test_mcp_server_integration.py` — 16 tests (build, conditional registration, MCP protocol execution)
- Modified: `src/api/main.py` — conditional MCP mount in lifespan, MCP health check component
- Modified: `src/config.py` — `mcp_auth_token` setting
- Modified: `pyproject.toml` — `fastmcp>=3.2` dependency, `coverage>=7.0` dev dependency
- Updated: `docs/architecture.md`, `docs/tool-reference.md`, `.env.example`

## Test Results

815 tests passing (was 781). 16 new MCP server tests including in-memory MCP protocol
execution via `fastmcp.Client`.

## Usage

```bash
# Enable in .env
MCP_AUTH_TOKEN=some-secret-token

# Add to Claude Code
claude mcp add --transport http sre-assistant \
  --header "Authorization: Bearer some-secret-token" \
  http://192.168.2.106:8001/mcp
```
