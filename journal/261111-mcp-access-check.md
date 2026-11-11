# Access check on /mcp in off, log and enforce modes (W12)

> Purpose: record why `/mcp` now checks the Cloudflare Access JWT itself, why it ships in `log` mode, and how its
> callers are named.

## 1. Problem

- `/mcp` trusted that Cloudflare Access sat in front of it. The container port is also published on the LAN, so any
  LAN device could call every tool without a JWT (F2). The docstring and `docs/architecture.md` said "Auth is handled
  externally".
- MCP callers were invisible: the tool metrics and the `tool_call` line could not say which client made a call (F3), so
  whether claude.ai, nanoclaw or other apps use the server at all stayed unconfirmed (F37).

## 2. Change

- `src/api/mcp_access.py`: `McpAccessCheck`, ASGI middleware passed to `http_app(middleware=...)`, so it runs inside the
  mounted FastMCP app ahead of the transport, on http scopes only. It verifies `Cf-Access-Jwt-Assertion` with
  `verify_access_identity` (W8) against `MCP_CF_ACCESS_AUD`.
- Modes (`MCP_ACCESS_MODE`):
  - `off`: no check, client `unknown`.
  - `log` (default): a verified caller is named; a missing JWT is `anonymous`, a bad one `invalid`; every request
    passes. A refusal logs one WARNING per (peer, reason) per 10 minutes, from a cache bounded at 1024 pairs.
  - `enforce`: the same check, and a failure gets 403 `{"error":"Cloudflare Access JWT required"}`.
- Configuration is read once, when the lifespan builds the MCP app, and logged:
  `MCP access check: mode=log aud_configured=yes team_domain_configured=yes client_names=2`. `enforce` with an empty
  AUD or team domain logs an ERROR and `/mcp` answers 503 `{"error":"MCP access check misconfigured"}`; the rest of the
  app runs. `log` with either empty warns once and checks nothing.
- New counter `sre_assistant_mcp_http_requests_total{client, access}`, `access` one of `verified`, `missing`,
  `invalid`, `unconfigured`, `off`.
- `ToolCallObserver` reads `request.state.mcp_identity_ref` and passes it to `observe_tool_call`, so a verified call's
  `tool_call` line carries `identity_ref`.

## 3. Decisions

- **Log mode first.** `enforce` would refuse every caller the moment it shipped, because the deployed env has no
  `MCP_CF_ACCESS_AUD` yet. `log` names callers and refuses none. The owner sets the AUD and client names in home-server
  (home-server #422), watches `sre_assistant_mcp_http_requests_total` until only expected clients show as `verified`
  for at least 7 days, then sets `enforce`. `MCP_ACCESS_MODE=off` and a restart undo it.
- **The identity is the client label.** The verified Access identity (a service token's Client ID or a person's email)
  maps through `MCP_CLIENT_NAMES` to a short name, else `unmapped-service-token` / `unmapped-user`. Clients cannot
  spoof it, unlike `clientInfo` from `initialize`. The raw identity never reaches a label or a log line; `identity_ref`
  (sha256[:8]) lets one caller's lines be matched.
- **Unconfigured log mode counts as `unconfigured`, not `off`,** so the metric shows that the check is meant to be on
  but has nothing to verify against.
- **Missing team domain counts as misconfigured too.** The plan named only the AUD; without a team domain no JWT can be
  verified either, so both lead to the same startup ERROR (enforce) or WARNING (log).
- **An unexpected exception in the check** is logged at ERROR with its class only (its message could quote the
  token). Log mode still passes the request; enforce mode refuses it.

## 4. How verified

- Each new test in `tests/test_mcp_access.py` failed against a passthrough stub before the code went in.
  `test_non_http_scope_passes_through` passed against the stub (a pin) and failed when the http-only guard was removed.
- `test_enforce_mode_rejects_missing_jwt_403` failed (`assert 200 == 403`) with the middleware removed from
  `http_app(...)` in `src/api/main.py`, and passed once it was put back.
- `test_verified_client_label_reaches_tool_metric` shows, over real HTTP, that `scope["state"]` set in the middleware
  reaches `get_http_request().state` in `ToolCallObserver` (fastmcp 3.4.11, starlette 1.10.0).
- JWKS is a respx mock on a placeholder team domain in every test.
