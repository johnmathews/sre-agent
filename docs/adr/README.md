# Architecture decision records

> Purpose
>
> The log of hard-to-reverse decisions on sre-agent. Each ADR states one decision and what it costs. Read an ADR to
> learn why the code is built a certain way. The code and the living docs say what it does now.

## 1. How the log works

- Each ADR has a status (`Proposed`, `Accepted` or `Superseded by ADR-NNNN`), a date, and three sections: Context,
  Decision and Consequences.
- ADRs are point-in-time records. A changed decision gets a new ADR that supersedes the old one, and the old one moves
  to the Superseded table.
- A date with "recorded" after it means the decision predates its ADR.
- Finished plans and specs live in [`docs/archive/`](../archive/).

## 2. Active

| ADR | Decision | Date |
| --- | --- | --- |
| [ADR-0002](0002-mcp-auth-is-access-plus-an-in-app-check.md) | MCP authentication is Cloudflare Access plus an in-app JWT check | 2026-11-11 |
| [ADR-0003](0003-deploy-is-never-an-agent-or-mcp-tool.md) | The deploy capability is never an agent or MCP tool | 2026-10-26, recorded 2026-11-11 |
| [ADR-0004](0004-the-langgraph-path-stays-for-now.md) | The LangGraph/OpenAI agent path stays for now | 2026-03-21, recorded 2026-11-11 |
| [ADR-0005](0005-the-memory-store-stays-off-and-says-so.md) | The memory store stays off, and says so | 2026-11-11 |
| [ADR-0006](0006-the-web-ui-lives-in-this-repo.md) | The web UI lives in this repo under `web/` | 2026-11-12 |
| [ADR-0007](0007-mcp-and-the-web-ui-are-both-first-class.md) | `/mcp` and the web UI are both first-class interfaces | 2026-11-12 |

## 3. Superseded

| ADR | Decision | Superseded by |
| --- | --- | --- |
| [ADR-0001](0001-mcp-is-the-primary-interface.md) | MCP at `/mcp` is the primary interface | [ADR-0007](0007-mcp-and-the-web-ui-are-both-first-class.md), 2026-11-12 |
