# Dependency caps and in-major bumps

> Purpose: record why `pyproject.toml` gained upper bounds and five new declarations on 2026-11-11, and what the lock
> refresh moved.

## 1. Problem

- Every dependency had a floor and no cap. One routine `uv lock --upgrade` would pull anthropic 2.x, which rejects
  `temperature` with a 400. That breaks every no-tools call: the incident verdict, the upgrade review and the report
  narrative.
- The code imports five packages it never declared: `anthropic`, `starlette`, `langchain-core`, `langgraph`, and
  `openai` (under `langchain-openai`). Their versions were whatever the declared packages happened to pull.
- `pip-audit` on the lock found 112 advisories in 25 packages. The ones that matter here are pyjwt 2.12.1 (the approval
  JWT check), starlette 0.52.1, mcp 1.26.0 and fastmcp 3.2.0.

## 2. Change

- Caps, so a lock refresh stays inside the majors the code is written for:

| Package | Constraint | What the cap protects against |
| --- | --- | --- |
| `anthropic` | `>=0.120,<1` | 2.x rejects `temperature` (above) |
| `openai` | `>=2.34,<3` | 3.0 moves to httpx2 under langchain-openai 1.x |
| `fastmcp` | `>=3.4.10,<4` | 4.0 moves to MCP SDK 2.0. 3.4.10 is the advisory fix |
| `mcp` (uv constraint) | `>=1.30,<2` | MCP SDK 2.0 protocol types under fastmcp 3. Floor: first release free of its advisories |
| `claude-agent-sdk` | `>=0.1.51,<0.2` | 0.x minors break the API the SDK agent path uses |
| `starlette` | `>=1.3.1,<2` | The ASGI middleware and manual `/mcp` lifespan entry. Floor: first release free of its advisories (fastmcp 3.4 needs 1.0.1) |
| `fastapi` | `>=0.115,<1` | A new major of the HTTP layer |
| `pyjwt[crypto]` | `>=2.15,<3` | The approval JWT check changing under a refresh. 2.15 fixes the 2.12 advisories |
| `langchain*`, `langgraph` | `<2` | The agent, tool, chat model and vector store APIs in `src/agent/` |

- `mcp` is not imported, so it is a `[tool.uv] constraint-dependencies` entry, not a dependency.
- The lock moved only the packages named with `uv lock --upgrade-package` (pyjwt, fastmcp, fastmcp-slim, mcp,
  starlette, fastapi, authlib, cryptography, urllib3, python-multipart, joserfc, filelock, anyio, idna, certifi,
  langchain, langgraph-checkpoint, langgraph-sdk, langchain-anthropic, pydantic-settings, langsmith, oauthlib,
  dnspython, fsspec, opentelemetry-api), plus what the new floors forced. Not a blanket `uv lock --upgrade`. The main
  moves: fastmcp 3.2.0 → 3.4.11, mcp 1.26.0 → 1.30.0, starlette 0.52.1 → 1.10.0, fastapi 0.129.0 → 0.162.4,
  pyjwt 2.12.1 → 2.15.1, anthropic 0.84.0 → 0.125.0, langchain 1.2.10 → 1.5.0, langgraph 1.0.10 → 1.2.20.
- langsmith 0.14 pulls in `httpx2`, `httpcore2` and `truststore` as new transitive packages.
- `docs/dependencies.md` has the caps table with one reason each, and the two checks to run after a lock change.

## 3. Red first

- fastapi 0.162 wraps an included router in `_IncludedRouter`, so `app.routes` no longer lists the upgrade routes.
- `test_main_app_serves_the_upgrade_routes` passed on the old lock and failed on the new one:
  `AssertionError: assert ('/upgrades/batches/{batch_id}', 'GET') in {('/ask', 'POST'), ...}`.
- The rewrite resolves each route name with `app.url_path_for(...)`, which raises `NoMatchFound` if the router is not
  included, and reads path and method from `router.routes`. It passes on the new lock.
- No other test needed a change. `make lint`, `make typecheck` and the 1699 tests pass.

## 4. Result

- `pip-audit` on the new lock: 6 advisories in 2 packages, down from 112 in 25. Five are in chromadb 1.5.0 with no
  fixed version. One is in claude-agent-sdk 0.1.51, fixed only in 0.2.121, above the cap.
- `uv lock --upgrade --dry-run` moves none of anthropic, openai, fastmcp, mcp or claude-agent-sdk to a new major. It
  would still take claude-agent-sdk to 0.1.81, openai to 2.54.0 and langchain-openai to 1.7.4, inside the caps.
- The plan's audit command uses `pip-audit -r -`. pip-audit 2.10.1 rejects that ("invalid requirements input: -"), so
  the documented command reads `-r /dev/stdin`.

## 5. Not done

- The majors above the caps (anthropic 2, openai 3, fastmcp 4, mcp 2, claude-agent-sdk 0.2) need their own migrations.
  Each gets a GitHub issue rather than a change here.
