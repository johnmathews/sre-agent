# ADR-0001: MCP at `/mcp` is the primary interface

**Status:** Superseded by [ADR-0007](0007-mcp-and-the-web-ui-are-both-first-class.md) (2026-11-12). **Date:** 2026-11-11.

## 1. Context

- The Streamable HTTP MCP server at `/mcp` shipped on 2026-04-04 (commit `bc56ae4`) as a second way into the tools,
  for use during development. The chat agent behind `/ask`, with the sre-webapp UI in front of it, was the product.
- By November 2026 most use came through MCP. Local Claude Code transcripts held 460 MCP tool calls between
  2026-09-27 and 2026-11-11, counted on 2026-11-11. The server could not count them itself, because it logged only
  `POST /mcp 200` per call.
- On 2026-11-11 the owner stated that other apps (Claude Code sessions, the claude.ai connector, the nanoclaw
  assistant) are the main users of the MCP server, and that the web UI sees little use.
- Those clients mostly build alerts and dashboards, check shipped changes against live state and troubleshoot
  services. They call the query tools directly, so the chat agent's own reasoning loop adds cost and nothing else.

## 2. Decision

The MCP server at `/mcp` is the primary interface of sre-agent. Tool design, observability and documentation serve
MCP clients first. `/ask` and the web UI stay as a secondary interface over the same tools.

## 3. Consequences

1. One registry, `TOOL_SPECS` in `src/agent/tool_registry.py`, builds the `/mcp` tool list and the chat agent's tools,
   so a tool added for MCP clients reaches the agent too unless its `surfaces` say otherwise.
2. The MCP contract is tested. `tests/snapshots/mcp_tools_contract.json` pins each tool's name, parameters, limits and
   annotations, and `test_mcp_contract_matches_snapshot` fails on a change that was not regenerated on purpose.
3. The server measures MCP use itself. Each tool call writes one `tool_call` log line and the
   `sre_assistant_tool_calls_total` metric, labelled by `source` and `client`.
4. New tools answer the clients' recurring tasks in one call (`container_status`, `scrape_targets`,
   `upgrade_status`, `service_health`).
5. A restart of the container interrupts the clients. Each deploy of `main` takes `/mcp` down for about 20 seconds,
   tracked in home-server#426.
6. How MCP clients authenticate is [ADR-0002](0002-mcp-auth-is-access-plus-an-in-app-check.md).
