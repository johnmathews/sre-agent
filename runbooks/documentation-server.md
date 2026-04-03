# Documentation Server (docserver)

## Overview

The documentation server (`documentation-mcp-server`, also called "docserver") provides semantic
search over indexed git repositories. It embeds documents using ONNX and stores them in ChromaDB
for vector similarity search. The SRE agent uses it to look up source code, architecture docs,
and implementation details for bespoke homelab tools.

## Infrastructure

| Property | Value |
|----------|-------|
| Host | infra VM (192.168.2.106) |
| Container image | `ghcr.io/johnmathews/documentation-mcp-server:latest` |
| Docker container name | `documentation-server` |
| Protocol | MCP Streamable HTTP at `/mcp` |
| Internal URL (Docker network) | `http://documentation-server:8080/mcp` |
| External URL | `http://192.168.2.106:8085/mcp` |
| Health endpoint | `GET /health` |
| Container memory limit | 1536 MB |

## Loki Labels

| Label | Value |
|-------|-------|
| `hostname` | `infra` |
| `service_name` | `docserver` |
| `container` | `documentation-server` |

## Architecture

- **Embedding model**: ONNX runtime (~800 MB resident memory for the Python process with model loaded)
- **Vector store**: ChromaDB (in-container storage)
- **Git fetch**: `ThreadPoolExecutor` for parallel repository cloning/fetching
- **Source config**: `sources.yaml` defines which repos to index
- **Poll interval**: Controlled by `DOCSERVER_POLL_INTERVAL` env var (default 300s / 5 minutes)

### Indexed Sources

The server indexes multiple git repos including: disk-status-exporter, container-status-exporter,
SRE-agent (this project), home-server (Ansible), nanoclaw, timer-app, journal-insights-agent,
unified-documentation-server, Document Stream.

## Common Failure Modes

### 1. Memory Exhaustion from Parallel Git Fetches (Critical)

**Symptoms**: All repository fetches fail simultaneously, `BlockingIOError: [Errno 11] Resource
temporarily unavailable`, `RuntimeError: can't start new thread`.

**Root cause**: `ThreadPoolExecutor(max_workers=N)` forks parallel git fetch processes. Each
`fork()` duplicates the ~800 MB Python process (due to ONNX + ChromaDB memory footprint). With
`max_workers=4`, the container needs 4 × 800 MB = 3.2 GB just for git fetches, but the container
limit is 1536 MB. The OS refuses to allocate memory or threads, producing `BlockingIOError`.

**Key diagnostic clue**: Both public repos (no auth needed) AND private repos fail identically.
If this were an authentication issue, public repos would succeed. Uniform failure across all
repos points to a host-level or container-level resource problem.

**Fix**: Reduce `max_workers` to 2 (or 1 for safety). With 2 workers: 2 × 800 MB = 1.6 GB,
which is tight but feasible with Linux memory overcommit. Alternatively, increase the container
memory limit.

**Prevention**: Monitor container memory usage via Prometheus
(`container_memory_usage_bytes{container_name="documentation-server"}`).

### 2. Git Authentication Failure

**Symptoms**: Only private repos fail to fetch. Public repos succeed normally. Logs show
`fatal: Authentication failed` or `remote: Bad credentials`.

**Root cause**: GitHub Personal Access Token (PAT) expired or was revoked.

**Key diagnostic clue**: Public repos still fetch successfully — the failure is auth-specific,
not resource-specific.

**Fix**: Generate a new GitHub fine-grained PAT and update the docker-compose config.

### 3. Stale Data / No Updates

**Symptoms**: Document search returns outdated content despite recent commits to source repos.

**Possible causes**:
- Git fetch is failing silently (check logs for fetch errors)
- `DOCSERVER_POLL_INTERVAL` env var overrides `sources.yaml` `poll_interval` — the env var
  takes precedence
- Reindexing after fetch is failing (check for embedding or ChromaDB errors)

**Diagnosis**: Query Loki logs for the last successful fetch cycle:
```
{service_name="docserver"} |= "fetch" | detected_level="info"
```

### 4. Duplicate Source Configuration

**Symptoms**: Wasted CPU/memory fetching the same repo twice under different names.

**Example**: `unified-documentation-server` and `unified-documentation-web-app` both pointing
to the same `documentation-mcp-server` repo.

**Fix**: Deduplicate entries in `sources.yaml`.

## Troubleshooting Queries

**Check recent errors:**
```
{service_name="docserver", detected_level=~"error|warn"}
```

**Check fetch activity:**
```
{service_name="docserver"} |= "fetch"
```

**Check container memory (Prometheus):**
```
container_memory_usage_bytes{container_name="documentation-server"}
```

**Check container restarts:**
```
changes(container_last_seen{container_name=~".*documentation.*"}[24h])
```
