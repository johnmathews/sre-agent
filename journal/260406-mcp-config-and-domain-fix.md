# MCP Client Config + Domain Rename

**Date:** 2026-04-06

## What happened

Set up Claude Code to connect to the deployed SRE agent's MCP server, rather than using the
Docker MCP Gateway's generic Prometheus/Grafana servers. Along the way, fixed the stale domain
references throughout the repo.

## Key decisions

1. **Use the SRE agent's own MCP endpoint, not Docker MCP Gateway.** The Docker MCP Gateway
   runs generic community MCP servers (Prometheus, Grafana) as local Docker containers that try
   to reach infrastructure directly. This doesn't work because services are behind Cloudflare
   Access. The SRE agent — deployed on the homelab LAN — already has purpose-built tools for
   Prometheus, Grafana, Loki, Proxmox, TrueNAS, PBS, and runbook search. One authenticated
   connection from Claude Code to the deployed agent fans out to all internal services.

2. **Renamed `MCP_AUTH_TOKEN` to `SRE_AGENT_MCP_AUTH_TOKEN`.** The generic name was ambiguous
   in a multi-service environment. Used Pydantic `AliasChoices` so both names work (new name
   preferred, old name as fallback).

3. **Domain rename.** The old root domain was referenced in the
   Docker MCP config (`~/.docker/mcp/config.yaml`), runbooks (traefik, cloudflared), and docs.
   All updated.

## Changes

- `src/config.py` — `mcp_auth_token` field now uses `AliasChoices` to accept
  `SRE_AGENT_MCP_AUTH_TOKEN` (preferred) or `MCP_AUTH_TOKEN` (fallback)
- `.env.example` — updated env var name
- `docker-compose.yml` / `docker-compose.demo.yml` — renamed `sre-api` service to `sre-agent`,
  fixed `API_UPSTREAM` DNS reference that would have broken webapp->API connectivity
- `readme.md`, `docs/architecture.md` — updated all `sre-api` references to `sre-agent`
- `runbooks/traefik-reverse-proxy.md`, `runbooks/cloudflared-tunnel.md` — old domain ->
  new domain
- `~/.docker/mcp/config.yaml` (local, not in repo) — updated Prometheus URL and added Grafana URL

## Claude Code MCP setup

```bash
claude mcp add -t http \
  -H "Authorization: Bearer <token>" \
  -- sre-agent http://192.0.2.106:8001/mcp
```

Note: external access via `https://<sre-mcp-host>` requires Cloudflare Access service token
headers (`CF-Access-Client-Id`, `CF-Access-Client-Secret`) — not yet configured.

## Still TODO

- Set up Cloudflare Service Token for external MCP access (when not on home WiFi)
- Consider removing redundant Docker MCP Gateway servers (prometheus, grafana) from
  `~/.docker/mcp/registry.yaml`
