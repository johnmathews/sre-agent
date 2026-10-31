# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HomeLab SRE Assistant — an AI-powered SRE assistant for homelab infrastructure built with LangChain (Python). It connects
to live infrastructure telemetry (Prometheus, Grafana, Proxmox VE, PBS) and a RAG knowledge base (runbooks, Ansible
playbooks, past incidents) to answer operational questions, explain alerts, correlate changes, and generate incident
reports.

Target environment: Proxmox homelab with 80+ services across multiple VMs and LXCs.

## Key Architectural Decision

**Not everything is RAG.** The agent uses two distinct data access patterns:

- **Live tool calls** (LangChain tools): Prometheus metrics, Grafana alerts, Proxmox VE config, PBS backups — queried
  in real-time via structured APIs
- **RAG retrieval** (vector store): Runbooks, Ansible playbooks/inventory, past incident summaries — embedded in
  Chroma/FAISS

The LangChain agent decides which approach to use based on the question.

## Tech Stack

- **Agent framework:** LangChain (Python)
- **LLM:** OpenAI API or Anthropic API (selected via `LLM_PROVIDER`)
- **Vector store:** Chroma or FAISS
- **Backend:** FastAPI (`/ask` endpoint)
- **Frontend:** Vue 3 SPA (separate repo: johnmathews/sre-webapp) or CLI
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
make test          # Unit + integration tests (mocked HTTP, no secrets needed)
make test-e2e      # E2E tests against real services (needs .env, costs money)
make check         # lint + typecheck + test in one command
make serve         # FastAPI dev server on :8000
make ingest        # Rebuild Chroma vector store from runbooks/
make chat          # Interactive CLI REPL for the agent
make report        # Generate weekly reliability report to stdout
make hooks         # Install git pre-commit + pre-push hooks
uv run pytest tests/test_foo.py::test_bar  # Run a single test
```

## Testing Strategy

Three test tiers:
- **Unit tests** (`tests/test_*.py`) — pure functions (parsing, validation, formatting). No mocks, no IO.
- **Integration tests** (`tests/test_*_integration.py`, `@pytest.mark.integration`) — test tool functions with mocked HTTP via `respx`. Verify API calls, response handling, and error paths without real services.
- **E2E tests** (`@pytest.mark.e2e`) — hit real Prometheus/Alertmanager/LLM. Skipped by default, run with `--run-e2e`. Requires `.env`.

`make test` runs unit + integration (safe for CI). `make test-e2e` runs everything.

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

- `src/agent/` — LangChain agent setup, tools (prometheus, grafana_alerts, proxmox, pbs, loki), retrieval (embeddings,
  runbooks), memory
- `src/api/` — FastAPI application
- `src/eval/` — Evaluation test cases (YAML) and runner
- `src/observability/` — Prometheus metric definitions, LangChain callback handler for cost/token tracking
- `src/report/` — Weekly reliability report generator, email delivery, APScheduler cron scheduling
- `src/upgrades/` — Upgrade Advisor: allowlist, WUD/registry/GitHub clients, review, store, deploy, verify, email; the
  approval endpoint is `src/api/upgrades.py`, the daily job is scheduled in `src/report/scheduler.py`
- `src/incidents/` — Incident explainer: Gatus webhook models, endpoint key, incident store, explanation job; the
  webhook is `src/api/incidents.py` (see `docs/incident-explainer.md`)
- `docs/` — Design documentation (architecture, tool reference, code flow, dependencies)
- `runbooks/` — Operational runbooks (markdown)
- `ansible/` — Symlink/submodule to ansible home-server project
- `dashboards/` — Grafana dashboard JSON exports

## Secrets

This is a **public repository**. Never commit secrets, API keys, tokens, or internal hostnames/IPs. Secrets are loaded at runtime from `.env` (gitignored). The `.env.example` file documents required variables without real values. For production/deployment, secrets should be managed via environment variables or a secrets manager (the homelab infra uses Ansible Vault).

## Design Principles

- **Never silently fail.** Every degradation is visible to the user and logged. Each tool has explicit failure handling
  with graceful fallback.
- **Self-observability.** The assistant tracks its own SLIs: response latency p95 (recorded, no target — scope and
  accuracy take priority over speed), tool call success rate (> 99%), RAG relevance (> 80% top-3), availability
  (> 99.5%), LLM error rate (< 1%).
- **Cost awareness.** Every query tracks token usage, estimated cost, tool call count, and latency breakdown.
- **Advisory by default.** The only automated action is a container upgrade through the Upgrade
  Advisor's approval path (and, once enabled per container, its allowlisted auto path). The deploy
  capability is never an agent tool or an MCP tool.
