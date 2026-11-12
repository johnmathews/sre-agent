# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HomeLab SRE Assistant — an MCP server, AI agent and web UI for homelab infrastructure (Python server, Vue UI in
`web/`). Its read-only tools query live telemetry (Prometheus, Grafana, Loki, Proxmox VE, PBS, TrueNAS) and a RAG
knowledge base (runbooks).

Two first-class interfaces ([ADR-0007](docs/adr/0007-mcp-and-the-web-ui-are-both-first-class.md)). A change that serves one must not break the other:

- **`/mcp`.** Claude Code sessions, the claude.ai connector, the nanoclaw assistant and other homelab apps call the
  tools directly over Streamable HTTP. `docs/mcp-clients.md` is the client guide.
- **The web UI (`web/`)**, through `/ask/stream`, `/conversations*` and `/health`. The chat agent behind `/ask` explains
  alerts, correlates changes and writes incident reports; the CLI uses it too. `tests/test_web_contract.py` checks the
  UI's TypeScript types against the server, so change both sides in the same PR. `web/CLAUDE.md` holds the UI's own
  conventions (iOS PWA details included).
- **Background jobs:** the weekly reliability report, the Upgrade Advisor and the incident explainer.

Target environment: Proxmox homelab with 80+ services across multiple VMs and LXCs.

## Key Architectural Decision

**Not everything is RAG.** The agent uses two distinct data access patterns:

- **Live tool calls** (LangChain tools): Prometheus metrics, Grafana alerts, Proxmox VE config, PBS backups — queried
  in real-time via structured APIs
- **RAG retrieval** (vector store): Runbooks, Ansible playbooks/inventory, past incident summaries — embedded in
  Chroma/FAISS

On `/ask` the agent decides which approach to use based on the question. On `/mcp` the client's model decides.

## Tech Stack

- **MCP server:** FastMCP, Streamable HTTP at `/mcp`, behind Cloudflare Access plus an in-app Access JWT check
  (`MCP_ACCESS_MODE`)
- **Tools:** LangChain `@tool` functions with Pydantic schemas, listed once in `TOOL_SPECS`
  (`src/agent/tool_registry.py`), which builds `/mcp` and both agent paths
- **Agent:** Claude Agent SDK when `LLM_PROVIDER=anthropic`, which production runs. LangChain/LangGraph when
  `LLM_PROVIDER=openai`, which the eval framework is written against
- **Vector store:** Chroma
- **Backend:** FastAPI (`/mcp`, `/ask`, `/ask/stream`, `/report`, `/health`, `/metrics`)
- **Frontend:** Vue 3 SPA in `web/` (Vite, Pinia, Tailwind v4, Playwright; npm, Node 22), or the CLI
- **Observability:** Prometheus metrics, Grafana dashboards, Loki logs
- **Infrastructure:** Proxmox, Ansible

## Development Conventions

- **Package manager:** uv (not pip, not poetry)
- **Python version:** latest stable with long-term support
- **All Python code must be fully typed** — type annotations on all function signatures, return types, and variables where not obvious. Use `mypy` for verification.

## Build Plan

See `readme.md` → "Build Order" for detailed phase-by-phase build steps with progress tracking.

## Commands

```bash
make dev           # Install all dependencies (including dev tools)
make lint          # Ruff check + format check
make format        # Auto-fix lint and formatting
make typecheck     # mypy strict mode
make test          # Unit + integration tests under coverage (mocked HTTP, no secrets needed); fails below the floor
make test-e2e      # E2E tests against real services (needs .env, costs money)
make check         # lint + typecheck + test in one command
make smoke         # Build the Docker image and run the CI smoke test on it (/health, /mcp initialize + tools/list)
make serve         # FastAPI dev server on :8000
make ingest        # Rebuild Chroma vector store from runbooks/
make chat          # Interactive CLI REPL for the agent
make report        # Generate weekly reliability report to stdout
make eval          # Run the eval cases (real LLM calls, costs money)
make advisor       # Upgrade Advisor dry run (no email, no batch, no deploy)
make hooks         # Install git pre-commit + pre-push hooks (works from a worktree; installs into the shared hooks dir)
uv run pytest tests/test_foo.py::test_bar  # Run a single test

make web-install   # npm ci in web/ plus the Playwright browsers
make web-dev       # Vite dev server on :5173, proxying /api/* to make serve on :8000
make web-lint      # ESLint on web/ (no warnings allowed)
make web-check     # Typecheck + lint + the Playwright suite (mocked backend, no server needed)
make web-image     # docker build the UI image as sre-webapp:local
```

## Shipping

- **Lands by:** pull request (squash-merge once `check` passes)
- **Environments:**
  - production (infra VM): CI on `main` runs `check`; when the server's files changed it then smoke-tests and pushes
    `ghcr.io/johnmathews/sre-agent:latest` (`.github/workflows/ci.yml`); WUD redeploys it about a minute later. `/mcp`
    drops for about 20 s during the swap.
    Check: Prometheus `max(process_start_time_seconds{job="sre-agent"})` is after the merge (for example through the
    sre-agent MCP `prometheus_instant_query`)
  - production web UI (infra VM): when `web/` changes, CI on `main` runs `web-test`, then pushes
    `ghcr.io/johnmathews/sre-webapp:latest` (job `web-build`); WUD redeploys it. A change outside `web/` and the
    server's image files publishes neither image.
    Check: `docker inspect sre-webapp --format '{{.Created}}'` on the infra VM is after the merge

## Testing Strategy

Three test tiers:
- **Unit tests** (`tests/test_*.py`) — pure functions (parsing, validation, formatting). No mocks, no IO.
- **Integration tests** (`tests/test_*_integration.py`, `@pytest.mark.integration`) — test tool functions with mocked HTTP via `respx`. Verify API calls, response handling, and error paths without real services.
- **E2E tests** (`tests/test_e2e_live.py`, `@pytest.mark.e2e`) — hit real Prometheus/Grafana/LLM. Skipped by default, run with `--run-e2e`. Requires `.env`. Each test skips with a reason when its inputs are unset: the `/mcp` test needs `SRE_MCP_URL` plus an Access service token (`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`); the model call runs only with `E2E_LLM=1`.

Web UI: `tests/test_web_contract.py` (in `make test`) checks the UI's TypeScript types against the server, and
`make web-check` runs the UI's typecheck, ESLint and Playwright suite (`web/tests/e2e/`, mocked backend).

`make test` runs unit + integration (safe for CI) under `coverage` with `-rf`, then prints the report (`src/` only). The
floor is `fail_under` in `pyproject.toml` (91 against a 92.43% baseline); `make test` exits non-zero below it. Raise it
when coverage rises, never lower it to get a change through. `make test-e2e` runs everything.

The `mock_settings` fixture in `tests/conftest.py` provides fake config so tests never need a `.env` file. When adding new modules that import `get_settings`, add a corresponding patch to this fixture.

The autouse `_no_dotenv` fixture blocks `.env` loading for all non-e2e tests by setting `Settings.model_config['env_file'] = None`. This ensures any test that forgets `mock_settings` fails locally with the same validation error as CI — not silently passes because `.env` exists on the developer machine.

## User Shorthand

- **DCP** = update/create documentation, commit changes, push to remote
- **MDCP** = update/add documentation, commit changes, merge to main, push to remote

## Build Phases

The project is built incrementally. Each phase produces a working, demonstrable system:

1. **Alert Explainer** — Core LangChain agent + Prometheus/Grafana/Proxmox/PBS tools + runbook RAG + FastAPI + basic UI
2. ~~**Synthetic Incident Generator**~~ — *Shelved.* Real homelab provides adequate test signals.
3. **Loki Log Tools** — Log queries, label discovery, change correlation via Loki
4. **SLI/SLO Dashboard** — Self-instrumentation with Prometheus metrics, Grafana dashboard for the assistant's own
   reliability
5. **Evaluation Framework** — Automated test cases scoring tool selection, retrieval relevance, answer quality
6. **Weekly Reliability Report** — Scheduled summarization of alerts, changes, SLO status
7. **Agent Memory Store** — Persistent SQLite memory for reports, incidents and metric baselines
8. **Upgrade Advisor** — Daily review of pending third-party image updates (found by What's Up Docker), emailed
   verdicts, one single-use approval link, guarded deploys through WUD and post-deploy verification

## Eval Quality

When debugging eval failures or writing new eval cases, see `docs/eval-quality.md`. It documents known judge
limitations, common mock gaps, rubric writing guidelines, and a history of past improvements. Refer to it before
modifying eval cases.

## Source Layout

- `src/agent/` — agent setup and tools:
  - `tool_registry.py`: `TOOL_SPECS`, the one list of tools, with `enabled_specs` and `run_tool`
  - `tools/`: the tool modules
  - `retrieval/`: embeddings, `runbook_search`, and `index.py` (the runbook index: fingerprint, manifest, rebuild and the server's check loop)
  - `sdk_agent.py`: the Claude Agent SDK path, with `mcp_tools.py`, its in-process `sre` tool server
  - `agent.py`: `build_agent`, the LangGraph path and the dispatch between the two
- `src/api/` — FastAPI application (`main.py`). `mcp_server.py` builds `/mcp` from the registry, and `mcp_access.py`
  and `mcp_identity.py` check the Access JWT and name the client
- `src/eval/` — Evaluation test cases (YAML) and runner
- `src/observability/` — Prometheus metric definitions, `tool_calls.py` (the per-call `tool_call` line and tool
  metrics on every surface), LangChain callback handler, SDK LLM metrics, logging setup
- `src/report/` — Weekly reliability report generator, email delivery, APScheduler cron scheduling
- `src/upgrades/` — Upgrade Advisor: allowlist, WUD/registry/GitHub clients, review, store, deploy, verify, email; the
  approval endpoint is `src/api/upgrades.py`, the daily job is scheduled in `src/report/scheduler.py`
- `src/incidents/` — Incident explainer: evidence, verdict, message, pushover, job, store, `__main__` dry run; the
  webhook is `src/api/incidents.py`, shared models and the Gatus endpoint key are in `models.py` and `gatus.py` (see
  `docs/incident-explainer.md`)
- `docs/` — Design documentation: `architecture.md`, `code-flow.md`, `tool-reference.md` (one section per tool, kept
  in step by `tests/test_docs_tool_reference.py`), `mcp-clients.md` (connecting MCP clients), `dependencies.md`,
  `incident-explainer.md`, and `adr/` (decision records, index in `docs/adr/README.md`)
- `web/` — the Vue web UI: `src/api/` (fetch wrappers, SSE parser), `src/stores/`, `src/components/`,
  `tests/e2e/` (Playwright), `docker/nginx.conf.template`, its own `Dockerfile`. Docs in `docs/web/`
- `runbooks/` — Operational runbooks (markdown)
- `ansible/` — Symlink/submodule to ansible home-server project
- `dashboards/` — Grafana dashboard JSON exports

## Secrets

This is a **public repository**. Never commit secrets, API keys, tokens, or internal hostnames/IPs. Secrets are loaded at runtime from `.env` (gitignored). The `.env.example` file documents required variables without real values. For production/deployment, secrets should be managed via environment variables or a secrets manager (the homelab infra uses Ansible Vault).

## Design Principles

- **Never silently fail.** Every degradation is visible to the user and logged. Each tool has explicit failure handling
  with graceful fallback.
- **Self-observability.** Every tool call on every surface writes one `tool_call` log line and the tool metrics. The
  SLIs (`docs/architecture.md`, "SLIs") can each go red: tool call success rate (> 99%, success over calls that are
  not `invalid_args` or `unknown_tool`, `/mcp` and agent together), availability (> 99.5%, the average of
  `sre_assistant_component_healthy`, `mcp_server` included), LLM error rate (< 1%), and `/ask` latency p95 (recorded,
  no target — scope and accuracy take priority over speed).
- **Cost awareness.** Every query tracks token usage, estimated cost, tool call count, and latency breakdown.
- **Advisory by default.** The only automated action is a container upgrade through the Upgrade
  Advisor's approval path (and, once enabled per container, its allowlisted auto path). The deploy
  capability is never an agent tool or an MCP tool.
