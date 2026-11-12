# HomeLab SRE Assistant

**Status:** active. **Last updated:** 2026-11-12. **Last verified:** 2026-11-12 (web UI and CI claims checked against
`web/` and `.github/workflows/ci.yml` on branch `eng-web-monorepo`; the rest against `src/` at `69a7f84`. Live
deployment not re-verified).

An MCP server, AI agent and web UI for a Proxmox homelab. It serves read-only tools for Prometheus, Grafana, Loki,
Proxmox VE, Proxmox Backup Server (PBS), TrueNAS and a runbook knowledge base over MCP at `/mcp`, and the same tools to a
chat agent at `/ask`, which the Vue web UI in `web/` puts in front of a person.

`/mcp` and the web UI are both first-class interfaces (ADR-0007). MCP clients are Claude Code sessions, the claude.ai
connector and other homelab apps. The chat agent backs the web UI and a CLI. The app also writes a
weekly reliability report, explains Gatus outages and reviews pending container image upgrades.

---

## Table of Contents

- [Using it from an MCP client](#using-it-from-an-mcp-client)
- [Getting Started](#getting-started)
  - [macOS: Local Network Access](#macos-local-network-access)
- [Deploying with Docker](#deploying-with-docker)
  - [Endpoints](#endpoints)
  - [Adding to an Existing Docker Compose Stack](#adding-to-an-existing-docker-compose-stack)
  - [Environment Variables](#environment-variables)
  - [Networking](#networking)
  - [Vector Store Persistence](#vector-store-persistence)
  - [Updating](#updating)
  - [Building from Source](#building-from-source)
- [CI/CD](#cicd)
- [Architecture](#architecture)
- [Capabilities](#capabilities)
- [Failure Modes \& Handling](#failure-modes--handling)
- [Conversation Memory](#conversation-memory)
- [Self-Observability](#self-observability)
- [Weekly Reliability Report](#weekly-reliability-report)
- [Evaluation Framework](#evaluation-framework)
- [Tech Stack](#tech-stack)
- [Motivation \& Goals](#motivation--goals)
- [Build Order](#build-order)
  - [Phase 1: Alert Explainer (Core Agent)](#phase-1-alert-explainer-core-agent)
  - [Phase 2: Synthetic Incident Generator (Shelved)](#phase-2-synthetic-incident-generator-shelved)
  - [Phase 3: Loki Log Tools](#phase-3-loki-log-tools)
  - [Phase 4: SLI/SLO Dashboard \& Instrumentation](#phase-4-slislo-dashboard--instrumentation)
  - [Phase 5: Evaluation Framework](#phase-5-evaluation-framework)
  - [Phase 6: Weekly Reliability Report](#phase-6-weekly-reliability-report)
  - [Phase 7: Agent Memory Store](#phase-7-agent-memory-store)
  - [Phase 8: Upgrade Advisor](#phase-8-upgrade-advisor)
  - [Deployment](#deployment)
- [Repository Structure](#repository-structure)
- [Non-Goals](#non-goals)
- [License](#license)

---

## Using it from an MCP client

`/mcp` is a Streamable HTTP MCP server in stateless mode. Each tool carries its parameter descriptions and limits, and
the server's instructions explain the time formats the tools accept. Clients reach it through a Cloudflare tunnel
behind Cloudflare Access, so a client sends an Access service token:

```bash
claude mcp add --transport http \
  --header "CF-Access-Client-Id: <client-id>" \
  --header "CF-Access-Client-Secret: <client-secret>" \
  -- sre-agent https://<sre-mcp-host>/mcp
```

The [MCP client guide](docs/mcp-clients.md) covers getting a service token, other clients, the authentication modes,
which tools need which settings, limits and errors, how to see your own calls in the logs and metrics, and
troubleshooting.

---

## Getting Started

Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/), and Docker for the image smoke test.

```bash
# Install dependencies, including dev tools
make dev

# Copy the example settings, then edit .env.
# Required: PROMETHEUS_URL, GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN, OPENAI_API_KEY (runbook embeddings),
# and ANTHROPIC_API_KEY when LLM_PROVIDER=anthropic.
cp .env.example .env

# Build the runbook vector store
make ingest

# Start the API (with /mcp) on :8000
make serve

# Or chat with the agent in the terminal
make chat

# Print a weekly reliability report to stdout
make report

# Lint, typecheck and tests
make check
```

The web UI lives in `web/` (Vue 3, npm, Node 22). `make web-install` once, then `make web-dev` serves it on `:5173` and
proxies `/api` to `make serve` on `:8000`. `make web-check` runs its typecheck and Playwright suite. See
[docs/web/development.md](docs/web/development.md).

### macOS: Local Network Access

On macOS 15 (Sequoia) and later, `make chat` inside tmux fails its Prometheus and Grafana calls with `[Errno 65] No
route to host`. tmux runs under `launchd`, so the Python process loses the terminal's local network permission. Run
`make chat` directly in the terminal app instead. Docker deployments on Linux are unaffected.

---

## Deploying with Docker

The backend builds one image that runs as two services. The web UI builds its own image from `web/`.

| Service      | Image                            | Port | Description                                                   |
| ------------ | -------------------------------- | ---- | ------------------------------------------------------------- |
| `sre-ingest` | `ghcr.io/johnmathews/sre-agent`  | none | One-shot job that builds the Chroma vector store              |
| `sre-agent`  | `ghcr.io/johnmathews/sre-agent`  | 8000 | FastAPI app: `/mcp`, the chat agent, reports and the jobs     |
| `sre-webapp` | `ghcr.io/johnmathews/sre-webapp` | 8080 | Vue 3 SPA built from `web/`, nginx proxying `/api` to `sre-agent` |

The intended host is a Linux VM or LXC on the same LAN as Prometheus, Grafana and the other monitored systems. CI publishes
each image on a push to `main` that changes its files (see CI/CD).

### Endpoints

| Endpoint                                     | Purpose                                                                    |
| -------------------------------------------- | -------------------------------------------------------------------------- |
| `POST /mcp`                                  | MCP server, with the Cloudflare Access JWT check (`MCP_ACCESS_MODE`)       |
| `POST /ask`, `POST /ask/stream`              | Chat agent, as JSON or as Server-Sent Events                               |
| `POST /report`                               | Weekly reliability report on demand                                        |
| `GET /health`                                | Status of each component, `mcp_server` included                            |
| `GET /metrics`                               | Prometheus metrics                                                         |
| `/conversations`, `/conversations/{id}`      | List, search, read, rename and delete stored conversations (web UI)        |
| `/upgrades/batches/{id}`                     | Upgrade Advisor approval page and deploy (404 unless the advisor is configured) |
| `POST /incidents/gatus`                      | Gatus alert webhook for the incident explainer (bearer token)              |

> **A published port exposes `/mcp`.** The compose files publish port 8000 on every interface, so any device on the
> LAN reaches `/mcp` and the other endpoints without passing Cloudflare Access. `/mcp` checks the Access JWT but refuses
> a caller only with `MCP_ACCESS_MODE=enforce`, and `/ask`, `/report` and `/conversations` have no authentication.
> Firewall the port so only the tunnel and Prometheus reach it.

### Adding to an Existing Docker Compose Stack

Add these services and the `chroma_data` volume to the host's `docker-compose.yml`:

```yaml
  sre-ingest:
    image: ghcr.io/johnmathews/sre-agent:latest
    command: ["python", "-m", "scripts.ingest_runbooks"]
    env_file: .env
    volumes:
      - chroma_data:/app/.chroma_db
    profiles: ["setup"]

  sre-agent:
    image: ghcr.io/johnmathews/sre-agent:latest
    ports:
      - "8000:8000"
    env_file: .env
    environment:
      - CLAUDE_CONFIG_DIR=/app/.claude  # LLM_PROVIDER=anthropic: the SDK reads its OAuth login here
      - HOME=/app
    restart: unless-stopped
    mem_limit: 768m  # 1 uvicorn worker + up to ~5 concurrent SDK CLI subprocesses
    volumes:
      - chroma_data:/app/.chroma_db
      - ${CONVERSATION_HISTORY_HOST_DIR:-./conversations}:/app/conversations
      - ${CLAUDE_CONFIG_DIR:-~/.claude}:/app/.claude  # from `claude login` on the host
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s

  sre-webapp:
    image: ghcr.io/johnmathews/sre-webapp:latest
    ports:
      - "8080:80"
    restart: unless-stopped
    environment:
      - API_UPSTREAM=http://sre-agent:8000
    depends_on:
      sre-agent:
        condition: service_healthy

volumes:
  chroma_data:
```

`sre-ingest` sits in the `setup` profile, so `docker compose up` skips it. Create `.env` next to the compose file, then
build the vector store and start the services:

```bash
docker compose run --rm sre-ingest
docker compose up -d sre-agent sre-webapp
```

### Environment Variables

`.env.example` lists every setting. The required ones:

| Variable                        | Example                  | Notes                                                        |
| ------------------------------- | ------------------------ | ------------------------------------------------------------ |
| `LLM_PROVIDER`                  | `openai` or `anthropic`  | Default `openai`                                             |
| `OPENAI_API_KEY`                | `sk-proj-...`            | Chat with `LLM_PROVIDER=openai`, and runbook embeddings always |
| `ANTHROPIC_API_KEY`             | any non-empty value      | Required when `LLM_PROVIDER=anthropic`; the SDK authenticates with the mounted `~/.claude` login, not this key |
| `PROMETHEUS_URL`                | `http://192.0.2.50:9090` | Must be reachable from inside the container                  |
| `GRAFANA_URL`                   | `http://192.0.2.50:3000` | Must be reachable from inside the container                  |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | `glsa_...`               | Grafana service account token                                |

Optional settings. An empty value turns the feature off:

| Variable                                    | Enables                                                                       |
| ------------------------------------------- | ----------------------------------------------------------------------------- |
| `PROXMOX_URL`, `PROXMOX_API_TOKEN`          | Proxmox VE tools (4)                                                          |
| `PBS_URL`, `PBS_API_TOKEN`                  | PBS tools (3)                                                                 |
| `TRUENAS_URL`, `TRUENAS_API_KEY`            | TrueNAS tools and `hdd_power_status` (6). The URL must be `https`             |
| `LOKI_URL`                                  | Loki tools (4), and the Loki parts of `service_health` and the report         |
| `MEMORY_DB_PATH`                            | Memory store and its tools (4)                                                |
| `MCP_ACCESS_MODE`, `MCP_CF_ACCESS_AUD`, `CF_ACCESS_TEAM_DOMAIN`, `MCP_CLIENT_NAMES` | The `/mcp` Access check (`off`, `log` (default) or `enforce`) and client names |
| `EXTRA_DOCS_DIRS`                           | More directories for the runbook store (comma-separated absolute paths)       |
| `CONVERSATION_HISTORY_HOST_DIR`             | Host path bind-mounted to `/app/conversations` for conversation files         |
| `REQUEST_TIMEOUT_SECONDS`                   | `/ask` timeout (default 120). A request that runs longer gets HTTP 504        |
| `USER_TIMEZONE`                             | IANA timezone for the prompt, `get_current_time` and the cron jobs. Default `UTC` |
| `REPORT_SCHEDULE_CRON`, `SMTP_*`            | Scheduled weekly report and its email                                         |
| `UPGRADE_ADVISOR_*`                         | Upgrade Advisor                                                               |
| `INCIDENT_*`, `GATUS_*`, `PUSHOVER_*`       | Incident explainer                                                            |

> **Concurrency:** the API runs one uvicorn worker. The SDK `query()` is async, so one event loop serves concurrent
> requests, and an `asyncio.Lock` serializes OAuth token refresh because refresh tokens are single-use. More workers
> would break that lock. If concurrent queries run out of memory then raise `mem_limit`.

### Networking

Use LAN addresses such as `http://192.0.2.50:9090` in `.env`. Inside a container `localhost` is the container itself. If
Prometheus and Grafana run in Docker on the same host, a shared Docker network lets you use container names instead.

### Vector Store Persistence

The Chroma store lives in the `chroma_data` volume and survives restarts and image updates. Ingest reads `.md` files
from the `runbooks/` directory baked into the image and from any directory in `EXTRA_DOCS_DIRS`. To add host docs such
as an Ansible repo, mount them read-only into `sre-ingest`:

```yaml
  sre-ingest:
    image: ghcr.io/johnmathews/sre-agent:latest
    command: ["python", "-m", "scripts.ingest_runbooks"]
    env_file: .env
    volumes:
      - chroma_data:/app/.chroma_db
      - /path/to/home-server:/app/ansible:ro
    environment:
      - EXTRA_DOCS_DIRS=/app/ansible
    profiles: ["setup"]
```

Re-run ingest after a new image or a change to the external docs, then restart the agent so it reads the rebuilt store:

```bash
docker compose run --rm sre-ingest
docker compose restart sre-agent
```

Ingest checks its settings and makes one embedding call before it clears the old store, so a missing key leaves the
current index in place. It needs `OPENAI_API_KEY` and, with `LLM_PROVIDER=anthropic`, a non-empty `ANTHROPIC_API_KEY`.

### Updating

```bash
docker compose pull sre-agent sre-webapp sre-ingest
docker compose up -d sre-agent sre-webapp
```

If the runbooks changed in the new image then re-run ingest as above.

### Building from Source

```bash
git clone https://github.com/johnmathews/sre-agent.git
cd sre-agent
cp .env.example .env   # then fill in the values
docker compose up -d
```

The repo's `docker-compose.yml` builds the image locally (`build: .`). [`docker-compose.demo.yml`](docker-compose.demo.yml)
runs the same stack from the published images:

```bash
docker compose -f docker-compose.demo.yml --profile setup run --rm sre-ingest
docker compose -f docker-compose.demo.yml up -d
```

---

## CI/CD

One GitHub Actions workflow, `.github/workflows/ci.yml`, tests both halves and publishes two images. A `changes` job
sorts each push into the server's files (`src/`, `scripts/`, `runbooks/`, `pyproject.toml`, `uv.lock`, `Dockerfile`,
`.dockerignore`) and `web/`; a change to the workflow counts as both.

1. **check** runs on pushes to `main` and on pull requests, whatever changed: `make lint`, `make typecheck` and
   `make test`. `make test` runs the unit and integration tests under `coverage` and fails below the floor
   (`fail_under` in `pyproject.toml`). It includes `tests/test_web_contract.py`, which checks the UI's types against
   the server.
2. **web-test** runs when `web/` changed: the UI's typecheck and its Playwright suite on four browser projects.
3. **build** runs on pushes to `main` that changed the server's files, after `check` passes. It builds the image, smoke-tests it, then pushes
   `ghcr.io/johnmathews/sre-agent:latest` and `:sha-<commit>`. The smoke test (`scripts/smoke_image.sh`, or `make smoke`
   locally) starts the image with dummy config and checks `/health`, the `/mcp` `initialize` and `tools/list` calls (at
   least 10 tools) and the bundled Claude CLI. If it fails then the job prints the container logs and pushes nothing.
4. **web-build** runs on pushes to `main` that changed `web/`, after `web-test` passes. It pushes
   `ghcr.io/johnmathews/sre-webapp:latest` and `:sha-<commit>`.

The home server's What's Up Docker (WUD) redeploys each `:latest` about a minute after a push, so `main` reaches
production only through these gates. A UI-only push leaves the server, and `/mcp`, running.

`make hooks` installs a pre-commit hook (ruff lint and format check) and a pre-push hook (`make check`). It works from a
git worktree too.

---

## Architecture

```
MCP clients (Claude Code, claude.ai,          Web UI (web/)              CLI (make chat)
other homelab apps)                                  │                         │
        │                                            │                         │
Cloudflare tunnel + Access                           │                         │
        │                                            ▼                         │
        ▼                                     /ask, /ask/stream                │
/mcp: McpAccessCheck → FastMCP                       │◄────────────────────────┘
        │                                            ▼
        │                  LLM_PROVIDER=anthropic: Claude Agent SDK (in-process MCP server)
        │                  LLM_PROVIDER=openai:    LangGraph agent
        │                                            │
        ▼                                            ▼
     Tool registry: TOOL_SPECS in src/agent/tool_registry.py (validates every call)
        │
        ├── Prometheus, Grafana, Loki              (HTTP APIs)
        ├── Proxmox VE, PBS, TrueNAS               (HTTP APIs)
        ├── Chroma runbook store                   (RAG)
        └── memory store, conversation files       (local SQLite / JSON)
```

One registry builds the three tool surfaces: `/mcp`, the SDK agent's in-process server and the LangGraph agent's tools.
They show the same names, descriptions and input schemas, and every call is checked against the schema before the tool
runs. Live state comes from tool calls against the APIs. Static knowledge (runbooks, Ansible docs) comes from the
vector store.

Background jobs run in the same process: the scheduled weekly report, the daily Upgrade Advisor run and the incident
explainer, which the Gatus webhook starts. [docs/architecture.md](docs/architecture.md) describes each part, and the
[ADR log](docs/adr/README.md) records the decisions behind them.

---

## Capabilities

`/mcp` serves up to 36 tools, the agent up to 34. The registry turns each group on only when its settings exist:

| Group         | Tools | Enabled when                                   | Covers                                                                                                   |
| ------------- | ----- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| core          | 13    | always                                         | Prometheus queries and scrape targets, container state, Grafana alerts, rules and dashboards, upgrade status, service health, runbook search, current time |
| proxmox       | 4     | `PROXMOX_URL`                                  | Guests, guest config, node status, tasks                                                                 |
| truenas       | 6     | `TRUENAS_URL`                                  | Pools, shares, snapshots, system status, apps, HDD power state                                           |
| loki          | 4     | `LOKI_URL`                                     | Log queries, log metrics, label values, change correlation                                               |
| pbs           | 3     | `PBS_URL`                                      | Datastores, backups, tasks                                                                               |
| memory        | 4     | `MEMORY_DB_PATH`                               | Past incidents (search and record), previous report, metric baselines                                   |
| conversations | 2     | `CONVERSATION_HISTORY_DIR` (default set)       | The chat agent's stored conversations. `/mcp` only                                                       |

Every tool reads, except `memory_record_incident`, which writes one incident record. [docs/tool-reference.md](docs/tool-reference.md)
documents each tool's parameters and output.

Typical questions:

- "Is Jellyfin healthy?" (`service_health`)
- "Which containers restarted in the last day?" (`container_status`)
- "Which scrape targets are down?" (`scrape_targets`)
- "Which images have updates waiting, and what did the advisor decide?" (`upgrade_status`)
- "What errors appeared before this alert fired?" (`loki_correlate_changes`)
- "Is the tank pool healthy, and how much space is left?" (`truenas_pool_status`)

Beyond the tools:

- **Weekly reliability report**, on demand or on a schedule.
- **Incident explainer:** after a Gatus outage resolves, it collects evidence and sends a Pushover message saying why
  the service went down and how it came back. Off unless configured. See [docs/incident-explainer.md](docs/incident-explainer.md).
- **Upgrade Advisor:** a daily review of pending image updates for an allowlist of containers, emailed with one
  single-use approval link. Approved updates deploy through WUD then get verified. Off unless configured.

---

## Failure Modes & Handling

No failure is silent. Each one shows up in the response, the logs or a metric.

| Failure                              | What happens                                                                                              |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| Upstream API down, slow or erroring  | The tool fails with a message naming the cause. `/mcp` returns it as an `isError` result, and the agent sees it as a tool error and can try another source |
| One source of a combined tool fails  | `service_health` marks that source `unavailable: <reason>` and answers from the others |
| Bad tool arguments                   | Unknown argument names and values outside a parameter's limits are refused before the tool runs, with an `isError` saying why |
| Integration not configured           | Its tools are not registered, and startup logs each disabled group once with the missing setting          |
| `/ask` runs past `REQUEST_TIMEOUT_SECONDS` | HTTP 504                                                                                              |
| Agent failure on `/ask/stream`       | An SSE `error` event. The SDK path adds a `reason` (`llm_auth_failed` or `agent_no_answer`) and a `detail` |
| LLM rate limit, 5xx or network error | The report narrative, incident verdict and upgrade review calls retry with doubling backoff inside a time budget |
| A component is down                  | `/health` reports each component, `mcp_server` included, sets `sre_assistant_component_healthy`, and returns `degraded` |
| Memory store off or unusable         | Startup logs a WARNING (unset) or an ERROR (unusable) and sets `sre_assistant_memory_store_enabled` to 0  |
| Vector store missing or empty        | `runbook_search` finds nothing, and `/health` marks `vector_store` unhealthy                               |

---

## Conversation Memory

The chat agent stores each session's turns (user and assistant text, without tool calls) as one JSON file in
`CONVERSATION_HISTORY_DIR` (`/app/conversations` in Docker, bind-mounted from `CONVERSATION_HISTORY_HOST_DIR`). The two
agent paths use that file differently:

- **LangGraph (`LLM_PROVIDER=openai`):** an in-process checkpointer keeps each session's messages. After a restart, the
  first request of a session reloads its turns from the file.
- **Claude Agent SDK (`LLM_PROVIDER=anthropic`):** each request runs a fresh SDK query, so the session's last 20 turn
  pairs go into the prompt, each with its timestamp.

MCP calls are stateless and keep no history. MCP clients can read the agent's stored conversations through
`sre_agent_list_conversations` and `sre_agent_get_conversation`. [docs/conversation-history.md](docs/conversation-history.md)
has the file format.

---

## Self-Observability

The app exports 25 Prometheus metrics at `GET /metrics` (26 with `LLM_PROVIDER=anthropic`), shown in the Grafana dashboard
`dashboards/sre-assistant-sli.json`. Each tool call, on `/mcp` and in the agent, writes one `tool_call` log line with
the tool, source, client, outcome, duration and result size, then updates the tool metrics.
`sre_assistant_mcp_http_requests_total{client, access}` counts every HTTP request to `/mcp`.

| SLI                          | Target SLO             | How it's measured                                                                                     |
| ---------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------- |
| Agent response latency (p95) | none (tracked)         | `sre_assistant_request_duration_seconds` histogram                                                    |
| Tool call success rate       | > 99%                  | `sre_assistant_tool_calls_total`: `status="success"` over calls not `invalid_args` or `unknown_tool` (client mistakes), `/mcp` and agent |
| LLM API error rate           | < 1%                   | `sre_assistant_llm_calls_total` by status. An SDK request with no result, an error result or an exception is an `error` |
| End-to-end availability      | > 99.5%                | `sre_assistant_component_healthy` gauge, set by `/health` for each component, `mcp_server` included   |

Latency has no SLO because scope and answer accuracy come first, and most latency cuts cost quality. The histogram and
p95 panel stay so a large regression is still visible. Each query also records token usage, estimated cost and tool
call count.

## Weekly Reliability Report

The report collects alerts, SLO status, tool usage, cost, backup health and Loki errors with direct API queries, then
makes one LLM call for the narrative.

- **On demand:** `POST /report` or `make report`
- **Scheduled:** `REPORT_SCHEDULE_CRON`, evaluated in `USER_TIMEZONE` (APScheduler)
- **Email:** plain-text markdown over SMTP with STARTTLS, if `SMTP_*` is set

## Evaluation Framework

`make eval` runs 32 YAML cases (`src/eval/cases/`) through the agent with a real LLM against mocked infrastructure
APIs. It scores tool selection deterministically and answer quality with an LLM judge on a different model.
[docs/eval-quality.md](docs/eval-quality.md) records known judge limits and how to write cases.

---

## Tech Stack

| Component       | Technology                                                       |
| --------------- | ---------------------------------------------------------------- |
| MCP server      | FastMCP (Streamable HTTP), mounted in FastAPI                    |
| MCP auth        | Cloudflare Access, with the in-app JWT check (pyjwt)             |
| Agent           | Claude Agent SDK (`anthropic`) or LangGraph with LangChain (`openai`) |
| LLM             | Anthropic API or OpenAI API                                      |
| Vector store    | Chroma, with OpenAI embeddings                                   |
| Backend         | FastAPI on uvicorn                                               |
| Frontend        | Vue 3 SPA in `web/` (Vite, Pinia, Tailwind v4) and a CLI         |
| Scheduling      | APScheduler                                                      |
| Storage         | SQLite (memory, Upgrade Advisor, incidents), JSON (conversations) |
| Observability   | Prometheus, Grafana, Loki                                        |
| Infrastructure  | Proxmox, Ansible, Docker, WUD                                    |

---

## Motivation & Goals

Most AI portfolio projects are chatbots over static documents. This one connects to live telemetry from a real
homelab (Proxmox, 80+ services across VMs and LXCs) and applies SRE practice to its own operation. Its goals:

1. **AI engineering depth:** tool design, retrieval, evaluation and cost tracking.
2. **SRE fluency:** observability, alerting, change correlation, incident response and reliability targets.
3. **Demo-ready on real signals:** the live homelab supplies real alerts, metrics and logs.
4. **Honest trade-offs:** the docs state what works and what doesn't.

---

## Build Order

The project grew in phases, each one a working system. [journal/](journal/) holds the history of each step.

### Phase 1: Alert Explainer (Core Agent)

Complete. Prometheus and Grafana tools, Proxmox VE, PBS and TrueNAS tools, runbook RAG, the system prompt, the FastAPI
backend and the CLI.

### Phase 2: Synthetic Incident Generator (Shelved)

Shelved. The live homelab produces enough real incidents to demo and test the agent.

### Phase 3: Loki Log Tools

Complete. Four Loki tools: log queries, log metrics, label values and change correlation.

### Phase 4: SLI/SLO Dashboard & Instrumentation

Complete. Prometheus metrics for the app itself and the Grafana dashboard in `dashboards/`.

### Phase 5: Evaluation Framework

Complete. YAML cases, deterministic tool scoring and an LLM judge, run with `make eval`.

### Phase 6: Weekly Reliability Report

Complete. Direct API collectors, one LLM narrative, email delivery and a cron schedule.

### Phase 7: Agent Memory Store

Built, and off in production. SQLite store for reports, incidents and metric baselines, with four tools. Startup warns
while `MEMORY_DB_PATH` is unset.

### Phase 8: Upgrade Advisor

Complete (2026-10-26). Daily review of pending image updates, one emailed approval link, deploys through WUD and
verification afterwards. Rollout (allowlist, WUD credentials, Access policy) is an operator step.

### Deployment

The app runs as Docker containers on the infra VM, deployed by the [home-server](https://github.com/johnmathews/home-server)
Ansible project. Production secrets come from Ansible Vault as environment variables and are never committed here.
[Deploying with Docker](#deploying-with-docker) covers the container setup.

---

## Repository Structure

```
sre-agent/
├── .github/workflows/ci.yml   # check and web-test, then build and push each changed image
├── .env.example               # every setting, with placeholder values
├── CLAUDE.md                  # guidance for Claude Code in this repo
├── Dockerfile                 # two-stage build on python:3.13-slim, uvicorn on :8000
├── docker-compose.yml         # sre-ingest, sre-agent, sre-webapp, built locally
├── docker-compose.demo.yml    # the same stack from published images
├── Makefile                   # dev, check, serve, ingest, chat, eval, report, advisor, smoke, hooks, web-*
├── pyproject.toml, uv.lock    # dependencies, ruff, mypy, pytest and coverage settings
├── pyrightconfig.json         # pyright settings
├── dashboards/
│   └── sre-assistant-sli.json # Grafana dashboard for the app's SLIs
├── docs/
│   ├── adr/                   # architecture decision records
│   ├── archive/               # superseded design docs
│   ├── web/                   # the web UI: architecture, API integration, deployment, development
│   ├── architecture.md        # components, data flow, MCP, observability, deployment
│   ├── code-flow.md           # request lifecycle, tool registration, metrics flow
│   ├── conversation-history.md # conversation file format and resume
│   ├── dependencies.md        # Python packages and external services
│   ├── eval-quality.md        # eval judge limits and case-writing rules
│   ├── incident-explainer.md  # Gatus outage explanations
│   ├── mcp-clients.md         # connecting an MCP client
│   └── tool-reference.md      # every tool, its parameters and output
├── journal/                   # dated development journal
├── runbooks/                  # operational runbooks, ingested into Chroma
├── scripts/
│   ├── ingest_runbooks.py     # rebuild the Chroma store
│   ├── install-hooks.sh       # git pre-commit and pre-push hooks
│   ├── run_eval.py            # eval entry point
│   ├── run_report.py          # print a report to stdout
│   └── smoke_image.sh         # smoke-test a built image
├── src/
│   ├── agent/                 # tool registry, tools, SDK and LangGraph agents, retrieval, history
│   ├── api/                   # FastAPI app, MCP server and access check, upgrade and incident routes
│   ├── eval/                  # eval runner, judge and YAML cases
│   ├── incidents/             # incident explainer
│   ├── memory/                # SQLite memory store and its tools
│   ├── observability/         # metrics, tool_call log line, logging setup
│   ├── report/                # weekly report, email, scheduler
│   ├── upgrades/              # Upgrade Advisor
│   ├── cli.py                 # interactive CLI
│   └── config.py              # settings (pydantic-settings)
├── tests/                     # unit and integration tests, MCP contract snapshot, web UI contract
└── web/                       # the Vue web UI: src/, tests/e2e/ (Playwright), Dockerfile, nginx template
```

---

## Non-Goals

- A general-purpose chatbot. The assistant answers homelab SRE questions.
- Remediation. It takes no remediation actions. The one automated action is a container image upgrade through the
  Upgrade Advisor's approval path. The deploy capability is never an agent or MCP tool (ADR 0003, in the
  [ADR log](docs/adr/README.md)).
- Multi-tenancy or RBAC. It serves one homelab owner.

---

## License

MIT
