# ADR-0007: `/mcp` and the web UI are both first-class interfaces

**Status:** Accepted. Supersedes [ADR-0001](0001-mcp-is-the-primary-interface.md). **Date:** 2026-11-12.

## 1. Context

- ADR-0001 made `/mcp` the primary interface and kept `/ask` and the web UI as a secondary one. It was right that most
  calls come through MCP: Claude Code sessions, the claude.ai connector and the nanoclaw assistant.
- On 2026-11-12 the owner stated that the web UI is still in use and must be a first-class interface for the server.
- "Secondary" had a cost: the UI's contract was untested and the UI lived in another repo, so server work could break
  it unseen ([ADR-0006](0006-the-web-ui-lives-in-this-repo.md)).

## 2. Decision

`/mcp` and the web UI (through `/ask/stream`, `/conversations*` and `/health`) are both first-class interfaces of
sre-agent. A change that serves one must not break the other, and both are tested, documented and shipped from this
repo.

## 3. Consequences

1. ADR-0001's consequences still hold for `/mcp`:
   - `TOOL_SPECS` in `src/agent/tool_registry.py` builds both the `/mcp` tool list and the chat agent's tools.
   - `tests/snapshots/mcp_tools_contract.json` pins the MCP contract.
   - Every tool call writes a `tool_call` log line and `sre_assistant_tool_calls_total`, labelled by `source` and
     `client`.
   - A deploy of the server takes `/mcp` down for about 20 seconds (home-server#426).
   - MCP authentication is [ADR-0002](0002-mcp-auth-is-access-plus-an-in-app-check.md).
2. The UI's contract is tested too: `tests/test_web_contract.py` checks the UI's TypeScript types, the stream event
   types and the error reasons the UI handles against the server.
3. CI runs the UI's typecheck and Playwright suite when `web/` changes, and publishes the UI image only after they pass.
4. The living docs describe both interfaces. `docs/mcp-clients.md` is the MCP client guide; `docs/web/` documents the
   UI.
5. Tool design still answers MCP clients' tasks in one call. A tool built for MCP reaches the chat agent, and so the
   UI, unless its `surfaces` say otherwise.
