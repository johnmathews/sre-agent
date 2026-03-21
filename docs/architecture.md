# Architecture

## System Overview

The HomeLab SRE Assistant is an AI agent that connects to live infrastructure telemetry and a knowledge base to answer
operational questions about a Proxmox homelab with 80+ services.

The agent supports two LLM backend paths, selected via `LLM_PROVIDER`:

- **OpenAI path** (`LLM_PROVIDER=openai`) — LangChain/LangGraph agent with LangChain `@tool` functions
- **Anthropic path** (`LLM_PROVIDER=anthropic`) — Claude Agent SDK via MCP tools through the Claude Code CLI subprocess

Both paths share the same tool business logic, system prompt, and observability metrics. The SDK path enables Opus access
via OAuth tokens from Claude Max/Pro subscriptions.

## Data Flow

The agent uses two distinct data access patterns:

```
                         User Question
                              |
                    FastAPI /ask or /ask/stream
                              |
                       LangChain Agent
                        (tool router)
                  /     |      |      \
                 /      |      |       \
     Live Metrics    Logs    RAG    Infrastructure
          |           |    Retrieval      |
          v           v       |           v
    +-----------+ +------+    v     +-----------+
    |Prometheus | | Loki | +-----+  |Proxmox VE |
    | (metrics) | |(logs)| |Chroma| +-----------+
    +-----------+ +------+ |Vector| +-----------+
    +-----------+          |Store | |    PBS    |
    |  Grafana  |          +-----+  +-----------+
    | (alerts)  |            |      +-----------+
    +-----------+         Runbooks  | TrueNAS  |
                          Playbooks +-----------+
```

### Live Tool Calls

Structured API queries executed in real-time. Used for questions about current system state.

- **Prometheus** (`prometheus_*` tools) — metrics: CPU, memory, disk, network, custom exporters
- **Grafana** (`grafana_*` tools) — alert states, alert rule definitions
- **Loki** (`loki_*` tools) — application logs, error search, change correlation timelines
- **TrueNAS SCALE** (`truenas_*` tools) — ZFS pools, NFS/SMB shares, snapshots, system status, apps
- **Proxmox VE** (`proxmox_*` tools) — VM/container config, node status, tasks
- **PBS** (`pbs_*` tools) — backup storage, backup groups, backup tasks

### RAG Retrieval

Embedded documents retrieved by semantic similarity. Used for operational knowledge.

- **Runbooks** — troubleshooting procedures, architecture docs, service configs
- **Ansible playbooks** — infrastructure-as-code, role definitions

The LangChain agent decides which approach to use based on the question.

## Service Dependencies

```
HomeLab SRE Assistant
  |
  +-- LLM Backend
  |     +-- OpenAI API (LLM_PROVIDER=openai, via LangChain)
  |     +-- Claude Agent SDK (LLM_PROVIDER=anthropic, via CLI subprocess)
  |
  +-- Prometheus (metrics, scraping pve_exporter, node_exporter, cadvisor, etc.)
  |
  +-- Grafana (alerting API, unified alerting)
  |
  +-- Loki (optional — log aggregation, collected by Alloy)
  |
  +-- TrueNAS SCALE API (optional — ZFS pools, shares, snapshots, apps)
  |
  +-- Proxmox VE API (optional — VM/container management)
  |
  +-- Proxmox Backup Server API (optional — backup status)
  |
  +-- Chroma vector store (local, on-disk)
```

Required: LLM API (OpenAI or Anthropic), Prometheus, Grafana. Optional: TrueNAS, Loki, Proxmox VE, PBS (tools are
conditionally registered based on config). Local: Chroma vector store (rebuilt via `make ingest`).

## Request Lifecycle

See [code-flow.md](code-flow.md) for the detailed request lifecycle.

## Failure Handling

Every external dependency has explicit error handling:

- **ConnectError** — "Cannot connect to {service} at {url}"
- **TimeoutException** — "{service} request timed out after {n}s"
- **HTTPStatusError** — "{service} API error: HTTP {code} - {body}"

All tools set `handle_tool_error = True` so errors are returned to the LLM as text (not raised as exceptions), allowing
the agent to report failures gracefully to the user.

### Query Correctness Safeguards

The Prometheus tools include defense-in-depth against common query mistakes:

1. **System prompt guidance** (preventive) — the prompt template includes patterns for both positive and negative
   metrics, explaining that `max_over_time` on negative values returns the smallest magnitude, not the peak.
2. **Tool output warnings** (reactive) — `prometheus_instant_query` detects when `max_over_time` is used with negative
   result values (or wrapped in `abs()`), and appends a warning suggesting `min_over_time` + `abs()`.
3. **Improved empty-result messages** — when a query returns no data, the error message suggests checking retention
   limits, label filters, and whether an instant query with `*_over_time` would be more appropriate.

## Self-Instrumentation (Observability)

The assistant tracks its own reliability via Prometheus metrics, exposed at `GET /metrics`.

### Metrics

| Metric                                     | Type      | Labels                                                 |
| ------------------------------------------ | --------- | ------------------------------------------------------ |
| `sre_assistant_request_duration_seconds`   | Histogram | `endpoint`                                             |
| `sre_assistant_requests_total`             | Counter   | `endpoint`, `status`                                   |
| `sre_assistant_requests_in_progress`       | Gauge     | `endpoint`                                             |
| `sre_assistant_tool_call_duration_seconds` | Histogram | `tool_name`                                            |
| `sre_assistant_tool_calls_total`           | Counter   | `tool_name`, `status`                                  |
| `sre_assistant_llm_calls_total`            | Counter   | `status`                                               |
| `sre_assistant_llm_token_usage`            | Counter   | `type` (prompt/completion)                             |
| `sre_assistant_llm_estimated_cost_dollars` | Counter   | —                                                      |
| `sre_assistant_component_healthy`          | Gauge     | `component`                                            |
| `sre_assistant_info`                       | Info      | `version`, `model`                                     |
| `sre_assistant_reports_total`              | Counter   | `trigger` (scheduled/manual), `status` (success/error) |
| `sre_assistant_report_duration_seconds`    | Histogram | —                                                      |

### Architecture

Three layers:

1. **Metric definitions** (`src/observability/metrics.py`) — module-level `prometheus_client` singletons. All 12 metrics
   are created once at import time and shared across the process. Histogram buckets are tuned for expected latencies:
   request duration `[0.5s–60s]`, tool duration `[0.1s–15s]`.

2. **LangChain callback handler** (`src/observability/callbacks.py`) — `MetricsCallbackHandler(BaseCallbackHandler)`
   transparently captures tool calls and LLM usage inside LangGraph's execution loop. A fresh instance is created per
   request (request-scoped `_start_times` dict) but writes to the shared module-level metric singletons. Key design
   choices:
   - **No tool code changes** — the handler hooks into LangGraph's callback system, so all 23 current tools (and any
     future tools) are automatically instrumented
   - **Works inside the agent loop** — LangGraph may call multiple tools in sequence before returning; the callback sees
     each individual call, unlike FastAPI middleware which only sees the outer request
   - **Error-resilient** — every callback method is wrapped in `try/except` so metrics never crash a request
   - **Cost estimation** — matches model name against a pricing table, falls back to conservative defaults for unknown
     models

3. **FastAPI instrumentation** (`src/api/main.py`) — request-level timing/counting on `/ask`, `/ask/stream`, and
   `/report` + `/metrics` endpoint + health gauge updates on `/health` + report metrics on `/report` + app info set at
   startup

### Grafana Dashboard

`dashboards/sre-assistant-sli.json` provides a pre-built dashboard with:

- SLO overview stats (availability, tool success rate, LLM success rate)
- Request latency percentiles (p50/p90/p95/p99)
- Tool call rates and errors by tool name
- LLM token usage and estimated cost
- Component health status

## Evaluation Framework

The eval framework tests the agent's end-to-end reasoning: does it pick the right tools, and does it produce good
answers? This is separate from unit/integration tests which mock the LLM entirely.

### How It Works

```
YAML eval case
  → loader.py parses into EvalCase model
  → runner.py patches settings (real OpenAI key + fake infra URLs)
  → runner.py sets up respx mocks from case definition
  → runner.py calls build_agent() + agent.ainvoke() directly
  → runner.py extracts tool calls from AIMessage.tool_calls
  → runner.py scores tool selection deterministically (must_call / must_not_call)
  → judge.py sends (question, answer, rubric) to grading LLM
  → report.py prints per-case results + summary
```

### Two Scoring Dimensions

1. **Tool selection** (deterministic) — did the agent call the expected tools? Checks `must_call` (required tools) and
   `must_not_call` (forbidden tools). `may_call` tools are allowed but not required.
2. **Answer quality** (LLM-as-judge) — a grading LLM (`gpt-4o-mini`, temperature 0) scores the answer against a
   human-written rubric. Returns pass/fail with explanation.

### Design Choices

- **HTTP-level mocking** (respx) tests the full tool implementation — URL construction, headers, response parsing.
  Function-level mocking would only test tool selection.
- **Real LLM + mocked infrastructure** — the agent calls OpenAI for reasoning but all infrastructure APIs are mocked.
  This costs tokens but validates actual agent behavior.
- **`agent.ainvoke()` not `invoke_agent()`** — we need the full message list to extract `AIMessage.tool_calls`.
  `invoke_agent()` discards messages and returns only text.
- **Runbook search disabled** — the vector store requires on-disk data. Eval focuses on tool selection and answer
  quality; RAG retrieval is tested separately.

### Running

```bash
make eval                                          # Run all 30 cases (costs tokens)
make eval ARGS="--case alert-explain-high-cpu"     # Single case
uv run pytest tests/test_eval.py -v                # Unit tests (free)
uv run pytest tests/test_eval_integration.py -v    # Integration tests (free)
```

### Eval Cases

30 cases across 8 categories: alerts (4), Prometheus (14), Proxmox (2), PBS (1), TrueNAS (2), Loki (3), memory (3),
cross-tool (1). Cases are YAML files in `src/eval/cases/`.

## Weekly Reliability Report

Phase 6 adds a scheduled weekly report that summarizes alerts, SLO status, tool usage, costs, and log errors.

### Design: Direct Query + LLM Summarization

The report module queries APIs **directly** (not through the LangChain agent) because:

- **Deterministic** — every section is always populated (partial data on failure, never empty)
- **Cheaper** — one LLM call for the narrative summary vs many agent tool calls
- **Faster and testable** — structured data collection with a single narrative generation step

### Data Flow

```
collect_report_data(lookback_days)
  |
  +-- _collect_alert_summary()      → Grafana API (rules + active alerts)
  +-- _collect_slo_status()         → Prometheus (p95, tool success, LLM errors, per-component availability)
  +-- _collect_tool_usage()         → Prometheus (tool calls by name, errors)
  +-- _collect_cost_data()          → Prometheus (tokens, estimated cost)
  +-- _collect_loki_errors()        → Loki (errors by service + week-over-week delta + error samples)
  +-- _collect_backup_health()      → PBS (datastore usage + backup freshness)
  |
  v
_load_previous_report()             → Memory store (previous report for context, if configured)
  |
  v
_generate_narrative(collected_data)  → Single LLM call for executive summary
  |
  v
format_report_markdown(report_data)  → Markdown with 7 sections
format_report_html(report_data)      → HTML email with inline CSS
  |
  v
_archive_report()                    → Memory store (auto-save, if configured)
_compute_post_report_baselines()     → Memory store (metric baselines, if configured)
```

All collectors run concurrently via `asyncio.gather()`, each wrapped in try/except. A collector failure produces `None`
for that section — the report is always generated, even with partial data.

### Report Sections

1. **Executive Summary** — LLM-generated bullet points (references previous report if available)
2. **Alert Summary** — total rules, active alerts, severity breakdown
3. **SLO Status** — table with target/actual/pass-fail for each SLI + per-component availability
4. **Tool Usage** — table with per-tool call counts and error rates (active tools only)
5. **Cost & Token Usage** — prompt/completion tokens and estimated USD
6. **Log Error Summary** — error counts by service with week-over-week delta and error samples (if Loki configured)
7. **Backup Health** — datastore usage and backup freshness with stale backup alerts (if PBS configured)

### Delivery

- **On-demand** — `POST /report` endpoint returns markdown + optional email delivery
- **Scheduled** — APScheduler `AsyncIOScheduler` with configurable cron expression (`REPORT_SCHEDULE_CRON`)
- **CLI** — `make report` prints markdown to stdout
- **Email** — multipart/alternative (HTML + plain-text fallback) via Gmail SMTP with STARTTLS (if `SMTP_*` configured). HTML uses inline CSS for email-client compatibility with styled tables, colored PASS/FAIL badges, and delta indicators.

### Metrics

| Metric                                  | Type      | Labels                                                 |
| --------------------------------------- | --------- | ------------------------------------------------------ |
| `sre_assistant_reports_total`           | Counter   | `trigger` (scheduled/manual), `status` (success/error) |
| `sre_assistant_report_duration_seconds` | Histogram | —                                                      |

## Agent Memory Store

Phase 7 adds persistent memory via SQLite, enabling the agent to accumulate knowledge across sessions.

### Storage

SQLite database at the path configured by `MEMORY_DB_PATH` (empty = disabled). Uses WAL mode for concurrent reads,
`CREATE TABLE IF NOT EXISTS` for idempotent schema initialization, and parameterized queries to prevent SQL injection.

### Schema (4 tables)

- **`reports`** — archived weekly reports with full markdown, JSON data, and summary metrics (active alerts, SLO
  failures, log errors, cost). Indexed by `generated_at`.
- **`incidents`** — incident journal with title, description, root cause, resolution, severity, services, and session
  linkage. Indexed by `alert_name` and `created_at`.
- **`metric_baselines`** — computed avg/p95/min/max per metric over a lookback window. Indexed by
  `(metric_name, computed_at)`.
- **`query_patterns`** — recent user questions and tools used, enabling the agent to see common query topics. Indexed by
  `created_at`. Auto-cleaned to keep the most recent 100 entries.

### Agent Tools (4, conditional on `MEMORY_DB_PATH`)

- `memory_search_incidents` — search past incidents by keyword, alert name, or service
- `memory_record_incident` — record a new incident during investigation
- `memory_get_previous_report` — retrieve archived weekly report(s)
- `memory_check_baseline` — check if a metric value is within the normal range

### Integration Points

- **Report generator** — after generation, auto-archives the report to the memory store and computes metric baselines
  from Prometheus. Loads the previous report as context for the LLM narrative.
- **Agent build-time** — `_get_memory_context()` loads open incidents and recent query patterns into the system prompt so
  the agent starts each session aware of ongoing issues and common user topics.
- **Agent post-response** — `_post_response_actions()` saves query patterns (question + tools used) and detects
  investigation conversations that warrant recording as incidents (suggests `memory_record_incident`).
- **Prometheus tool** — `prometheus_instant_query` enriches results with baseline context (avg/p95/min/max) when
  baselines exist for the queried metric.
- **Grafana alerts tool** — `grafana_get_alerts` appends past incident history for any active alert names found in the
  memory store, giving the agent immediate context about recurring issues.
- **System prompt** — guidance for when to use memory tools (search incidents before investigating, record root causes,
  check baselines for anomaly detection).

### Source Layout

```
src/memory/
├── __init__.py
├── store.py        # Connection management, schema init, typed CRUD (4 tables)
├── models.py       # TypedDicts: ReportRecord, IncidentRecord, BaselineRecord, QueryPatternRecord
├── tools.py        # 4 LangChain tools + get_memory_tools() for conditional registration
├── context.py      # Build-time & per-request context: enrichment, incident suggestion
└── baselines.py    # Metric baseline computation from Prometheus
```

## Configuration

Settings are loaded from environment variables via `pydantic-settings`. The `Settings` class in `src/config.py` defines
all configuration with sensible defaults. Optional integrations (TrueNAS, Loki, Proxmox VE, PBS, Memory Store) default to
empty strings, which disables their tools.

### Conversation History Persistence

The full conversation for each session — including all tool calls, tool responses, and intermediate messages — is saved
as a JSON file to `/app/conversations` after each agent invocation. This is designed for debugging and improving the
agent, not for runtime use.

- **File format:** `{datetime}_{session_id}.json` with metadata (timestamps, turn count, model) and the full LangChain
  message list serialized via `messages_to_dict()`
- **Atomic writes:** uses `tempfile.mkstemp()` + `os.replace()` to avoid partial files on crash
- **Error-safe:** all errors are logged and swallowed — conversation persistence never crashes a request
- **Preserves `created_at`:** on updates to an existing session file, the original creation timestamp is retained

In Docker, `CONVERSATION_HISTORY_HOST_DIR` from the host `.env` is bind-mounted to `/app/conversations` inside the
container. The app always writes to `/app/conversations` — the host path is purely a deployment concern.

## Deployment Plan

### Target Environment

The agent will run as Docker containers on the Infra VM (`infra`, LXC on Proxmox), managed by the existing
[home-server](https://github.com/johnmathews/home-server) Ansible project. This keeps deployment consistent with every
other service in the homelab.

### Sensitive Data Strategy

This is a **public repository**. Runbooks contain real infrastructure details (IPs, hostnames, SSH usernames, service
topology) that the RAG agent needs for useful answers. The deployment strategy handles this tension:

1. **Repository runbooks** — contain real operational content (kept as-is for now; acceptable risk for RFC1918 addresses)
2. **Ansible templates** — at deploy time, Ansible can template runbooks from inventory variables if sanitization is
   needed later
3. **`.env` file** — generated by Ansible from `templates/env.j2` with vault-encrypted secrets, never committed
4. **`docker-compose.yml`** — templated by Ansible to inject correct image tags, volume mounts, and network config

### Container Architecture

A single Docker image (multi-stage build, `python:3.13-slim`) contains all three services. The `docker-compose.yml`
overrides the command per service:

```
docker-compose.yml
  |
  +-- sre-ingest (one-shot, "setup" profile)
  |     CMD: python -m scripts.ingest_runbooks
  |     Volumes: chroma_data:/app/.chroma_db
  |     Run manually before first use and after runbook changes
  |
  +-- sre-api (FastAPI backend)
  |     CMD: uvicorn src.api.main:app --host 0.0.0.0 --port 8000
  |     Port: 8000
  |     Volumes: chroma_data:/app/.chroma_db, ${CONVERSATION_HISTORY_HOST_DIR}:/app/conversations
  |     restart: unless-stopped
  |
  +-- sre-ui (Streamlit frontend)
        CMD: streamlit run src/ui/app.py --server.port 8501 --server.address 0.0.0.0
        Port: 8501
        Env: API_URL=http://sre-api:8000
        restart: unless-stopped, starts after api is healthy
```

The `sre-ingest` service is under the `setup` profile — it won't run during normal `docker compose up`. Run it explicitly
with `docker compose run --rm sre-ingest`.

See the [README — Deploying with Docker](../readme.md#deploying-with-docker) for full setup instructions including how to
merge into an existing compose stack.

### Networking

- All containers share a Docker bridge network with access to Prometheus, Grafana, Proxmox VE, and PBS on the LAN
- No macOS local network permission issues (Linux host)
- Traefik reverse proxy provides HTTPS access via Cloudflare tunnel

### Secrets Management

All secrets are managed via Ansible Vault, consistent with the rest of the homelab:

| Secret                          | Source        | Injected Via    |
| ------------------------------- | ------------- | --------------- |
| `OPENAI_API_KEY`                | Ansible Vault | `.env` template |
| `ANTHROPIC_API_KEY`             | Ansible Vault | `.env` template |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | Ansible Vault | `.env` template |
| `PROXMOX_API_TOKEN`             | Ansible Vault | `.env` template |
| `PBS_API_TOKEN`                 | Ansible Vault | `.env` template |
| `TRUENAS_API_KEY`               | Ansible Vault | `.env` template |

### RAG Document Sources

The vector store is built from multiple directories configured via `EXTRA_DOCS_DIRS`:

- `runbooks/` — bundled in this repo, operational procedures and architecture docs
- External documentation directories — referenced by absolute path on the host, mounted into the ingest container

The ingest process is strictly read-only — it reads `.md` files via `Path.read_text()` and writes only to the
`.chroma_db/` directory.
