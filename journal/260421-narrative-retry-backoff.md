# Narrative LLM Retry with Exponential Backoff

The weekly reliability report was being sent with "Narrative unavailable" when the LLM returned
a 429 rate limit error. The `_generate_narrative()` function had no retry logic — it caught the
exception and returned a fallback string, so the report was emailed incomplete.

## What Changed

Added exponential backoff retry to `_generate_narrative()` with a configurable
`max_retry_seconds` budget:

- **Initial delay:** 30s, doubling each retry (30 -> 60 -> 120 -> 240 -> ...)
- **Max per-retry delay:** capped at 30 minutes
- **Scheduled reports:** 6-hour retry budget via `_scheduled_report_job()`
- **On-demand `POST /report`:** no retry (budget = 0), preserving existing behavior

Only transient errors trigger retries: HTTP 429 (rate limit), 5xx (server errors), and
network/connection failures. Non-retryable errors (401, 403, 400) fail immediately.

## Key Decisions

- Retry logic lives in `_generate_narrative()` rather than the scheduler, so it's testable
  in isolation and could be reused by other callers in the future.
- The `create_llm()` call is inside the retry loop so each attempt gets a fresh client
  instance, avoiding stale connection state.
- Prompt and message construction is hoisted outside the loop to avoid redundant work.
- The on-demand endpoint deliberately has no retry budget — blocking an HTTP request for
  hours would be worse than returning an incomplete report interactively.

## Files Changed

- `src/report/generator.py` — `_is_retryable_llm_error()`, retry loop in `_generate_narrative()`,
  `max_narrative_retry_seconds` parameter on `generate_report()`
- `src/report/scheduler.py` — passes 6-hour budget to scheduled report generation
- `tests/test_narrative_retry.py` — 17 new tests covering retryable/non-retryable classification,
  backoff timing, budget exhaustion, and happy path
- `docs/code-flow.md`, `docs/architecture.md` — documented retry behavior in data flow diagrams
