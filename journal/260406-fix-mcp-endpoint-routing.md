# Fix MCP endpoint routing for Claude Code connectivity

**Date:** 2026-04-06

## Problem

Claude Code's `claude mcp list` showed the sre-agent MCP server as "Failed to connect" despite the
service being healthy. Investigation revealed two issues:

1. **Double path prefix:** FastMCP's `http_app()` registers its route at `/mcp` internally. When
   mounted at `/mcp` in FastAPI, the actual working endpoint was `/mcp/mcp` — not `/mcp` as
   configured in the client.

2. **Starlette 307 redirect:** Requests to `/mcp` got a 307 redirect to `/mcp/` (standard
   Starlette mount behavior), but Claude Code's MCP client doesn't follow redirects.

3. **Local scope:** The MCP config was scoped to the sre-agent project directory only, so Claude
   Code couldn't access it from other working directories.

## Fix

### Server-side (`src/api/main.py`)

- Set `path="/"` on `mcp_server.http_app()` so the sub-app route lives at the mount root,
  making the full path `/mcp/` instead of `/mcp/mcp`.
- Added `TrailingSlashForMCP` ASGI middleware that rewrites `/mcp` to `/mcp/` internally,
  bypassing the 307 redirect entirely.

### Client-side (Claude Code config)

- Removed local-scoped MCP config (`-s local`, only worked in the project directory).
- Re-added at user scope (`-s user`) with trailing slash URL, so it works from any directory.

## Key Insight

When using Starlette/FastAPI `app.mount("/prefix", sub_app)`, the sub-app's own routes are
relative to the mount point. If the sub-app also defines a route at `/prefix`, the combined
path becomes `/prefix/prefix`. Always use `path="/"` for the sub-app route when mounting at a
non-root path.
