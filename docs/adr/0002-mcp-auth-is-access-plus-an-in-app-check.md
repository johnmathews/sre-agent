# ADR-0002: MCP authentication is Cloudflare Access plus an in-app JWT check

**Status:** Accepted. **Date:** 2026-11-11.

## 1. Context

- `/mcp` runs raw PromQL and LogQL and reads stored conversations. Clients reach it through the Cloudflare Tunnel at
  `https://<sre-mcp-host>/mcp`, behind a Cloudflare Access application. The infra VM also publishes the app on port
  8001 on its LAN interfaces.
- `/mcp` first shipped behind a bearer token, `MCP_AUTH_TOKEN`. Commit `603c872` (2026-04-06) removed it, because the
  app never checked the token on a request and used it only to decide whether to mount `/mcp`. The setting looked like
  auth and gave none, so the commit left auth to Access at the edge.
- Edge-only auth leaves the LAN port open. On 2026-11-11 an unauthenticated `initialize` to port 8001 got an answer,
  while the tunnel URL answered 403 without an Access session.
- Home-server changes were out of scope on 2026-11-11, so the fix had to live in the app. The Access JWT also names
  the calling client.

## 2. Decision

Access at the edge stays. Inside the app, ASGI middleware `McpAccessCheck` (`src/api/mcp_access.py`) verifies the
`Cf-Access-Jwt-Assertion` header on each HTTP request to `/mcp` against `MCP_CF_ACCESS_AUD` and
`CF_ACCESS_TEAM_DOMAIN`. `MCP_ACCESS_MODE` sets what a failed check does:

- `off`: nothing is checked, and the client is `unknown`.
- `log` (the default): the request passes. A verified caller gets its client label, a request with no JWT is
  `anonymous` and one with a bad JWT is `invalid`. Refusals log a WARNING, rate-limited per peer.
- `enforce`: a request without a valid JWT gets 403. With no AUD or team domain set, `/mcp` answers 503.

The bearer token does not come back.

## 3. Consequences

1. Production runs `log` with no AUD, so the check is inactive. Startup logs a WARNING, and requests count as
   `access="unconfigured"`, `client="unknown"` in `sre_assistant_mcp_http_requests_total` (seen live on 2026-11-11).
   home-server#422 sets the AUD and client names, then moves the mode to `enforce`.
2. Until then the LAN port stays open. home-server#421 adds a firewall rule for port 8001 as a second layer.
3. In `enforce` mode a client must come through Access with a service token or a user session, and a direct LAN
   call gets 403.
4. `MCP_CLIENT_NAMES` maps a verified identity to the client name in logs and metrics. The app never logs the token,
   its claims or the raw identity.
5. `/ask`, `/report` and `/conversations` are outside this check. Issue #35 tracks extending it.
