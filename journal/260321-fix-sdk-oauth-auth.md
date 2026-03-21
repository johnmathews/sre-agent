# Fix SDK OAuth Authentication in Docker

**Date:** 2026-03-21

## Problem

The deployed SRE assistant on the infra VM (192.168.2.106) returned 500 errors on every `/ask` request. The error from
the Claude CLI subprocess was: `Invalid API key · Fix external API key`.

The agent had been working in early March 2026 but broke around March 18.

## Root Causes (Three Issues)

### 1. ANTHROPIC_API_KEY env var conflict

The `.env` file sets `ANTHROPIC_API_KEY` (required by `src/config.py` Settings validator). The `claude-agent-sdk`
spawns a CLI subprocess that inherits `os.environ`. Per the [Claude CLI auth precedence](https://code.claude.com/docs/en/authentication):

- Item 3: `ANTHROPIC_API_KEY` → sent as `X-Api-Key` header
- Item 5: OAuth credentials from `.credentials.json` → sent as `Authorization: Bearer`

The env var contained an OAuth token (`sk-ant-oat01-*`), but the CLI tried to use it as an API key (wrong auth
method). The CLI never fell through to the correct OAuth credentials.

**Fix:** Pass `env={"ANTHROPIC_API_KEY": ""}` in `ClaudeAgentOptions` to clear the env var from the subprocess.

### 2. Options rebuild dropping env override

`invoke_sdk_agent()` and `stream_sdk_agent()` rebuild `ClaudeAgentOptions` each call to refresh the system prompt
timestamp. The rebuild copied `model`, `mcp_servers`, `allowed_tools`, etc. but **not** `env`, so the
`ANTHROPIC_API_KEY` fix was silently dropped on every query.

**Fix:** Add `env=options.env` to both rebuild sites.

### 3. OAuth access token expiry

OAuth access tokens expire every ~8 hours (`expiresAt` field in `.credentials.json`). The CLI does not auto-refresh
them in headless/Docker environments ([claude-code#12447](https://github.com/anthropics/claude-code/issues/12447)).

**Fix:** Added `src/agent/oauth_refresh.py` — checks `expiresAt` before each SDK query, refreshes via
`POST https://api.anthropic.com/v1/oauth/token` with the stored refresh token and client ID
`9d1c250a-e61b-44d9-88ed-5944d1962f5e`. Writes refreshed credentials back atomically. Best-effort: failures are
logged and swallowed so the query proceeds regardless.

## Docker Compose Changes

- Mount `.claude` at `/app/.claude` (was `/root/.claude`) — matches deployed config
- Changed from `:ro` to read-write — required for OAuth token auto-refresh
- Added `CLAUDE_CONFIG_DIR=/app/.claude` and `HOME=/app` environment variables

## Investigation Notes

- The basic SDK `query()` (no MCP tools) worked even with the old token because it used a simpler auth path
- The full SRE agent with MCP tools triggered the auth precedence issue because `ANTHROPIC_API_KEY` was set
- OAuth refresh tokens are **single-use** — consuming one without saving the new one invalidates the credentials
  (learned the hard way during debugging)
- macOS stores Claude credentials in the Keychain (`security find-generic-password -s "Claude Code-credentials"`),
  not in `~/.claude/.credentials.json`
- The `claude login` command on headless servers fails with a redirect URI error; `claude setup-token` is the
  correct command for headless auth

## Files Changed

- `src/agent/sdk_agent.py` — `env` override in `build_sdk_options()` + preserved in options rebuild
- `src/agent/oauth_refresh.py` — new module for auto-refresh
- `docker-compose.yml` — mount path, permissions, env vars
- `tests/test_oauth_refresh.py` — 6 tests for refresh logic
- `tests/test_sdk_agent.py` — test for env override
- `docs/architecture.md`, `docs/code-flow.md`, `docs/dependencies.md` — updated

## Test Impact

775 tests passing (6 new OAuth + 1 new env override test).
