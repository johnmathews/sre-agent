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

### 5. Single Uvicorn Worker (Concurrency)
**Root cause:** Dockerfile CMD ran uvicorn with default 1 worker. All concurrent requests competed
for one event loop thread.

**Fix:** Added `--workers 2` and `--timeout-keep-alive 130` to Dockerfile CMD. Increased
docker-compose `mem_limit` from 384m to 768m.

## Test Results

- 781 tests passing (4 new tests added)
- Lint clean (ruff)
- Type clean (mypy)

## Files Changed

- `src/agent/sdk_agent.py` — response extraction fix + async ensure_valid_token
- `src/agent/oauth_refresh.py` — full async rewrite with locking
- `src/api/main.py` — request timeout with 504
- `src/agent/tools/disk_status.py` — parallelized API calls
- `src/config.py` — `request_timeout_seconds` setting
- `Dockerfile` — 2 workers + timeout-keep-alive
- `docker-compose.yml` — 768m memory limit
- `docs/architecture.md` — timeout and concurrency docs
- `tests/test_oauth_refresh.py` — async tests + concurrency test
- `tests/test_sdk_response_extraction.py` — new: 4 tests for response extraction
- `tests/test_api_integration.py` — updated timeout test (500 → 504)
- `tests/conftest.py` — added request_timeout_seconds to FakeSettings
