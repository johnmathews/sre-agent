# ADR-0003: The deploy capability is never an agent or MCP tool

**Status:** Accepted. **Date:** 2026-10-26, recorded 2026-11-11.

## 1. Context

- The Upgrade Advisor (`src/upgrades/`), built on 2026-10-26, deploys container upgrades through What's Up Docker
  after the operator approves them with a single-use link behind Cloudflare Access.
- The chat agent and `/mcp` act on text from LLMs and from any MCP client, and in October 2026 `/mcp` answered
  unauthenticated requests from the LAN ([ADR-0002](0002-mcp-auth-is-access-plus-an-in-app-check.md)). A deploy reachable
  from either path would let a prompt or an unauthenticated caller change production.
- The project's principle was "Advisory only". On 2026-10-26 the owner amended it to "Advisory by default" (commit
  `8e79082`) and kept deploys inside the app instead of moving them to a separate service.

## 2. Decision

The deploy capability is never an agent tool or an MCP tool. Only the approval endpoint (`src/api/upgrades.py`), the
advisor's scheduled job, the app lifespan in `src/api/main.py` (post-deploy verification at startup and shutdown) and
the `make advisor` dry run (`src/upgrades/__main__.py`) import the deploy stack: `src.upgrades.deploy`, `src.upgrades.wud`,
`src.upgrades.verify`, `src.upgrades.job` and `src.api.upgrades`. Code structure keeps them apart inside the one
process.

## 3. Consequences

1. Two tests in `tests/test_upgrades_api_integration.py` enforce the rule:
   - `test_deploy_is_not_an_agent_or_mcp_tool` parses each file under `src/agent/`, plus `src/api/mcp_server.py` and
     `src/memory/tools.py`. It fails if one imports or names a deploy module, `deploy_approved` or `trigger_deploy`.
   - `test_agent_and_mcp_do_not_load_the_deploy_stack_transitively` imports the tool modules and the MCP server in a
     fresh interpreter, then fails if a deploy module is in `sys.modules`.
2. Read-only views of the advisor are allowed. The `upgrade_status` tool reads the advisor's store
   (`src.upgrades.store`) and Prometheus, and imports no deploy module.
3. Code shared by both sides lives outside the deploy stack, as `src.upgrades.access` and `src.upgrades.store` do.
   Moving a helper into a deploy module can turn the import test red.
4. Any automated deploy, including a future per-container auto path, runs from the scheduled job and never from a
   tool call.
