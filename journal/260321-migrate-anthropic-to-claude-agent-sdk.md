# Migrate Anthropic Backend to Claude Agent SDK

**Date:** 2026-03-21

## Context

OAuth tokens from Claude Max subscription don't permit Opus/Sonnet via the raw Anthropic API — only Haiku works. But
the Claude Code CLI with the same token accesses all models. The `claude-agent-sdk` bundles the CLI and uses the same
auth path.

## What Changed

Replaced the LangChain `ChatAnthropic` agent loop with `claude-agent-sdk` for the Anthropic path. The LangChain path
is preserved for `LLM_PROVIDER=openai`.

### New Modules

- **`src/agent/mcp_tools.py`** — MCP tool bridge wrapping all 29 LangChain tools as SDK MCP tools via a single `"sre"`
  server. Each wrapper calls `tool_obj.coroutine(...)` directly on the LangChain `@tool` object — no `_impl` extraction
  needed.
- **`src/agent/sdk_agent.py`** — SDK agent with `build_sdk_options()`, `invoke_sdk_agent()`, `stream_sdk_agent()`.
  Handles system prompt templating with `mcp__sre__` tool name prefixes, conversation history injection (context
  stuffing), and post-response actions.
- **`src/observability/sdk_metrics.py`** — Extracts tool calls, cost, and token usage from SDK messages into the same
  Prometheus counters used by the LangChain callback handler.

### Modified Modules

- **`src/agent/agent.py`** — Added `SREAgent` dataclass wrapping either SDK options or LangGraph agent. `build_agent()`,
  `invoke_agent()`, and `stream_agent()` dispatch based on provider.
- **`src/agent/history.py`** — Added SDK conversation persistence (`save_sdk_conversation`, `load_sdk_history`,
  `format_history_as_prompt`).

## Key Design Decisions

1. **No `_impl` extraction** — The plan called for extracting business logic into `_impl()` functions. Discovered that
   LangChain's `@tool` decorator exposes the original function via `.coroutine` (async) and `.func` (sync), so MCP
   wrappers call these directly. Saved modifying 11 tool files.

2. **Block all built-in tools including ToolSearch** — The spike showed that blocking `ToolSearch` forces the model to
   use MCP tools directly (fewer turns, cheaper). Wildcard `allowed_tools=["mcp__sre__*"]` works.

3. **Tool name transform via regex** — Single `system_prompt.md` with short tool names, `_prefix_tool_names()` adds
   `mcp__sre__` prefix for SDK path only. Avoids maintaining two prompt files.

4. **Stateless query() with history injection** — Each SDK call is independent. Prior turns are formatted as
   `<conversation_history>` XML blocks prepended to the new message. Simpler than managing CLI subprocess sessions.

## Spike Results

All four pre-step assumptions verified before any code changes:
- `claude-agent-sdk` installs without dependency conflicts
- Auth works via mounted `~/.claude/` credentials (OAuth token)
- Custom MCP tool registered and called successfully
- `disallowed_tools` blocks built-in tools (Read, Write, Bash, etc.)

## Docker Deployment

The SDK bundles a platform-specific native CLI binary inside the pip wheel (Mach-O for macOS, ELF for Linux). No
Node.js runtime needed in the container.

- **Dockerfile** — `chmod +x` on the bundled binary (some runtimes strip execute permission)
- **docker-compose.yml** — mounts `~/.claude/` read-only into the container for OAuth auth. Harmless no-op when using
  OpenAI. Memory limits: 384m API, 96m UI.
- **`.env.example`** — documents the auth setup flow (`claude login` → mount creds)

The `ANTHROPIC_API_KEY` env var is required by the config validator but not used by the SDK path. Set it to any
non-empty placeholder; actual auth comes from the mounted credentials directory.

**Post-deploy fix:** The `ANTHROPIC_API_KEY` env var leaked into the CLI subprocess, causing "Invalid API key" errors.
See `journal/260321-fix-sdk-oauth-auth.md` for the full investigation.

## Test Impact

741 existing tests still pass. 28 new tests added (769 total). Test changes:
- Tests that passed raw `AsyncMock` to `invoke_agent()` updated to use `_invoke_langgraph_agent()` directly
- Anthropic `build_agent` test updated to verify SDK path construction
- `conftest.py` mock_settings patched at new import sites
