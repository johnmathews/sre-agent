# Fix "Stream closed" errors in SDK agent path

**Date:** 2026-03-28

## Problem

Users experienced "Stream closed" errors during multi-tool agent conversations. The agent's MCP tool calls (Prometheus,
Loki, etc.) would start failing mid-conversation, preventing the agent from completing its analysis. The agent would
waste turns retrying failed tools until hitting `error_max_turns`.

## Root Cause Investigation

Investigated the most recent failing conversation (`fd5f92cb`) and found:

1. The conversation started at 16:34 UTC. After ~60-70 seconds, Loki tool calls began returning `"Stream closed"` errors
2. The agent retried Loki 3 times, then fell back to Prometheus — which also failed with "Stream closed"
3. The conversation hit `error_max_turns` (11 turns, 103s, $0.47) without completing

Web research confirmed this is a known bug in `claude-agent-sdk`:

- **anthropics/claude-agent-sdk-python#730**: The SDK's `wait_for_result_and_end_input()` applied a 60-second timeout
  before closing stdin, even when MCP servers required the bidirectional pipe. Fixed in v0.1.51.
- **anthropics/claude-agent-sdk-typescript#114**: The CLI's inactivity timer (`lastActivityTime`) is not reset when MCP
  server responses arrive (still open).

The deployed version was `0.1.50` — one version behind the fix.

## Additional Finding

Server logs revealed `loki_correlate_changes` was sending an empty `{}` stream selector when no hostname/service filters
were provided. Loki requires at least one label matcher, so it returned HTTP 400. This was a separate code bug.

## Fixes Applied

### 1. Upgrade `claude-agent-sdk` to `>=0.1.51`

Updated `pyproject.toml` dependency floor. The fix in PR #731 removes the 60-second stdin timeout entirely when MCP
servers are configured.

### 2. CLI inactivity timer workaround

Added `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=3600000` (1 hour) to the CLI subprocess environment in `build_sdk_options()`.
This overrides the CLI's internal inactivity timer that isn't properly reset by MCP responses (bug #114).

### 3. Fix Loki empty stream selector

Changed `loki_correlate_changes` to use `{hostname=~".+"}` (match all hosts) instead of `{}` when no filters are
provided. This ensures both the error query and the lifecycle query use valid LogQL stream selectors.

### 4. SSE heartbeat events

Added `_with_heartbeats()` async generator wrapper in `src/api/main.py` that injects `{"type": "heartbeat"}` SSE events
every 15 seconds during long agent processing. This prevents:
- Cloudflare tunnel idle timeout (100s)
- Streamlit httpx client timeout (120s)

The Streamlit UI ignores heartbeat events via a `continue` in the event loop.

## Files Changed

- `pyproject.toml` — SDK version floor bump
- `uv.lock` — Resolves to `claude-agent-sdk==0.1.51`
- `src/agent/sdk_agent.py` — `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT` env var
- `src/agent/tools/loki.py` — Fixed empty selector in `loki_correlate_changes`
- `src/api/main.py` — `_with_heartbeats()` wrapper on `/ask/stream`
- `src/ui/app.py` — Ignore heartbeat events
- `docs/architecture.md` — "SDK Stream Resilience" section
- `tests/test_sdk_agent.py` — Test env var presence and value
- `tests/test_loki_integration.py` — Test valid selector when no filters
- `tests/test_heartbeat.py` — 7 tests for heartbeat wrapper

## Test Results

795 tests passing (was 781). All lint and type checks pass.
