# Causation vs. Correlation Rule + Documentation MCP Server Integration

**Date:** 2026-03-31

## Context

The nanoclaw morning report agent used the SRE agent to investigate overnight HDD spinups. The SRE agent reported that
the disk-status-exporter's smartctl probe "caused" the spinup, based on a 10.6s scan duration coinciding with the spinup
window. This was temporal correlation presented as confirmed causation. Investigation revealed the disk-status-exporter
uses `smartctl -n standby` which explicitly prevents waking sleeping disks — the slow scan was a consequence of the
spinup (reading a waking disk), not its cause.

## Changes

### Causal reasoning guidelines

1. **System prompt** (`src/agent/system_prompt.md`): Added a guideline requiring the agent to distinguish "confirmed
   cause" (direct evidence of disk I/O) from "possible cause" / "temporally correlated" (events close in time but no
   causal link proven). This applies to all causal claims, not just disk spinups.

2. **Disk management runbook** (`runbooks/disk-management.md`): Added "Attributing spinup causes" section with
   domain-specific guidance — what counts as confirmed vs. unconfirmed, and common false positives (disk-status-exporter
   with `-n standby`, TrueNAS middleware TNAUDIT entries).

### Disk-status-exporter runbook

3. **New runbook** (`runbooks/disk-status-exporter.md`): Documents the exporter's behavior — smartctl flags that prevent
   waking sleeping disks, pull-based model (Prometheus controls scrape timing), cooldown mechanism, scan duration
   interpretation, exported metrics and power state values, configuration options.

### Documentation MCP server integration

4. **Config** (`src/config.py`): Added `documentation_mcp_url` setting (optional, SDK/Anthropic path only).

5. **SDK agent** (`src/agent/sdk_agent.py`): When `DOCUMENTATION_MCP_URL` is set, adds the documentation server as an
   HTTP MCP server named "docs" alongside the existing "sre" server. Tools appear as `mcp__docs__search_docs`, etc.

6. **System prompt**: Added `search_docs`, `query_docs`, `get_document`, `list_sources` tools to the Tool Selection
   Guide under "For external service documentation."

7. **Test fixtures** (`tests/conftest.py`, `src/eval/runner.py`): Added `documentation_mcp_url: ""` to FakeSettings.

The documentation MCP server (`ghcr.io/johnmathews/documentation-mcp-server`) already runs on the infra VM and indexes
10 git repos including disk-status-exporter. It provides semantic search over all indexed documentation via the MCP
Streamable HTTP protocol. The SRE agent can now query it to understand how bespoke services work — e.g., "does
disk-status-exporter wake sleeping disks?" returns the relevant architecture docs with smartctl flag details.

## Deployment

To activate on infra VM, add `DOCUMENTATION_MCP_URL=http://documentation-server:8080/mcp` to the SRE agent's `.env`
file, then rebuild/redeploy the image and re-run `docker compose run --rm sre-ingest`.

## Decision

Chose to add the documentation MCP server as a proper tool source rather than duplicating its content into local
runbooks. This keeps knowledge in one place (the source repos) and lets the docs server's polling handle updates
automatically. The local runbook (`disk-status-exporter.md`) captures the most critical fact (smartctl won't wake disks)
for RAG retrieval even without the docs server, providing a fallback.
