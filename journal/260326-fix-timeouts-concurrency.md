# Fix: API Timeouts, Truncated Responses, and Concurrency Issues

**Date:** 2026-03-26

## Context

The morning report service sends multiple simultaneous queries to the SRE assistant API (`POST /ask`).
Several issues were causing 30-90+ second response times, truncated responses, and timeouts:

## Issues Found and Fixed

### 1. Truncated Responses (Critical)
**Root cause:** `sdk_agent.py` used `response_text = block.text` (assignment, not append) on every
`AssistantMessage` in the SDK ReAct loop. The `ResultMessage.result` — which contains the actual
final answer — was never used for the response text.

**Fix:** Prefer `ResultMessage.result` when available. Fall back to last `TextBlock` only if no
`ResultMessage` is received.

### 2. No Request Timeout (Critical)
**Root cause:** FastAPI's `/ask` endpoint had no timeout. A slow agent query could hold a connection
indefinitely.

**Fix:** Added `asyncio.wait_for()` with configurable timeout (default 120s via `REQUEST_TIMEOUT_SECONDS`).
Returns HTTP 504 on timeout.

### 3. Synchronous OAuth Refresh Blocking Event Loop (Critical)
**Root cause:** `ensure_valid_token()` was synchronous, calling `httpx.post()` directly. This blocked
the entire asyncio event loop for up to 15s during token refresh. No locking meant concurrent requests
could race on single-use refresh tokens.

**Fix:** Converted to `async` using `httpx.AsyncClient`. Added `asyncio.Lock` so only one refresh
happens at a time — subsequent callers see the already-refreshed token.

### 4. Sequential API Calls in hdd_power_status (Performance)
**Root cause:** The tool made 5-7 sequential HTTP requests (Prometheus + TrueNAS) with no parallelization.
A 24h range query was also duplicated between `_get_stats()` and `_find_transition_window()`.

**Fix:** Independent requests now run concurrently via `asyncio.create_task`. Stats data is reused to
skip redundant transition window queries. Estimated improvement: 10-55s → 3-15s for typical queries.

### 5. Uvicorn Worker Configuration
**Initial fix:** Added `--workers 2` to Dockerfile CMD.

**Revised:** Reverted to single worker after analysis showed `asyncio.Lock` (used for OAuth refresh)
doesn't work across processes. Single worker is correct — SDK `query()` is async (subprocess-based),
so one event loop handles concurrent requests fine. Added `--timeout-keep-alive 130` to prevent
idle connection drops. Docker `mem_limit` set to 768m (1 worker + up to ~5 concurrent CLI subprocesses).

### 6. SDK Per-Tool Duration Metrics (Observability Gap)
**Root cause:** `sdk_metrics.py` didn't populate `TOOL_CALL_DURATION` histograms — Grafana panel 7
showed no data for the Anthropic provider path.

**Fix:** Timestamp gaps between SDK message yields to approximate per-tool execution time. When
`ToolUseBlock` is seen, the next message arrives after tool execution — elapsed time ≈ tool duration.
For parallel tools in one message, total time is split evenly. Also fixed a bug where tool durations
were silently dropped because they were recorded after an early `return` on `result is None`.

### 7. Dependency Vulnerability (LangGraph)
**Issue:** LangGraph 1.0.8 had unsafe msgpack deserialization (Dependabot alert, medium severity).

**Fix:** Upgraded to LangGraph 1.0.10 via `uv lock --upgrade-package langgraph`.

Note: Pygments 2.19.2 has a low-severity ReDoS with no upstream fix yet.

### 8. CI Lint Prevention
**Root cause:** Formatting errors committed without local lint check, caught by CI.

**Fix:** Added pre-commit hook (`make hooks`) running `ruff check` + `ruff format --check` on every
commit. Pre-push hook runs full `make check` (lint + typecheck + tests).

## Test Results

- 786 tests passing (9 new tests added)
- Lint clean (ruff)
- Type clean (mypy, 46 source files)

## Files Changed

- `src/agent/sdk_agent.py` — response extraction fix, async ensure_valid_token, tool duration timing
- `src/agent/oauth_refresh.py` — full async rewrite with asyncio.Lock
- `src/api/main.py` — request timeout with 504
- `src/agent/tools/disk_status.py` — parallelized API calls
- `src/config.py` — `request_timeout_seconds` setting
- `src/observability/sdk_metrics.py` — per-tool duration recording
- `Dockerfile` — single worker + timeout-keep-alive 130
- `docker-compose.yml` — 768m memory limit
- `uv.lock` — langgraph 1.0.8 → 1.0.10
- `scripts/install-hooks.sh` — pre-commit + pre-push hooks
- `docs/architecture.md` — timeout and concurrency docs
- `docs/tool-reference.md` — hdd_power_status parallelization note
- `readme.md` — mem_limit, REQUEST_TIMEOUT_SECONDS, concurrency note, hooks
- `.env.example` — REQUEST_TIMEOUT_SECONDS
- `CLAUDE.md` — make hooks in Commands
- `tests/test_oauth_refresh.py` — async tests + concurrency test
- `tests/test_sdk_response_extraction.py` — 6 tests: response extraction + tool timing
- `tests/test_sdk_metrics.py` — 3 tests: tool duration recording
- `tests/test_api_integration.py` — updated timeout test (500 → 504)
- `tests/conftest.py` — added request_timeout_seconds to FakeSettings
