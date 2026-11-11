# Image smoke test before push (W2)

## What

`scripts/smoke_image.sh <image>` runs a built image with dummy config and checks `/health` (200 with a `status`
field), `initialize` and `tools/list` on `/mcp` (at least 10 tools, including `prometheus_instant_query`) and
`claude --version` from the Claude Agent SDK's bundled binary. On any failure it prints `docker logs`. CI builds the
image with `load: true, push: false`, runs the script, then runs the existing push step (same gha cache). `make smoke`
does the same locally. The `uv` image in the Dockerfile is pinned to 0.13.7.

## Why

Unit tests stayed green with `/mcp` unmounted (F8), and the home server's WUD redeploys whatever is pushed within a
minute (F26). The suite never ran the built image, so a broken image could reach production.

## uv pin

Local `uv --version` was 0.13.0, older than the 0.13.7 in `ghcr.io/astral-sh/uv:latest` (checked 2026-11-11), so the
pin is 0.13.7, the newer of the two.

## How verified

- `docker build -t sre-agent:smoke .` then `scripts/smoke_image.sh sre-agent:smoke`: `SMOKE PASSED`.
- In a throwaway copy with the `app.mount("/mcp", mcp_app)` line deleted: `/health` still passed, then
  `POST /mcp` got 404, the script printed `SMOKE FAILED: POST /mcp initialize failed` plus the logs and exited 1.
- The workflow YAML parses and `actionlint` is clean.

## Notes

- `MCP_ACCESS_MODE=off` is passed already; the setting is ignored until W12 adds it.
- In the broken copy the startup log still said "MCP server mounted at /mcp" and `/health` reported `mcp_server`
  healthy. That is W13's territory.
