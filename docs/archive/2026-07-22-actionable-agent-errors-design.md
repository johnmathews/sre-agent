**Status:** superseded by [code-flow.md](../code-flow.md) ("Structured error events") and [`src/agent/agent_errors.py`](../../src/agent/agent_errors.py) (2026-11-11).

# Actionable Agent Error Messages — Design

**Date:** 2026-07-22
**Status:** approved, pending implementation
**Repos:** `sre-agent` (backend, primary), `sre-webapp` (frontend)

## Problem

When the agent fails mid-request, the webapp shows a single generic message —
*"The agent processed your question but failed to produce an answer (often a
tool error or LLM provider issue). Retrying is safe."* — regardless of the real
cause.

This was exposed by a production incident (2026-07-22): the OAuth **refresh
token expired** (`400 invalid_grant`), the Claude Agent SDK subprocess exited 1,
and `stream_sdk_agent` caught the opaque `Command failed with exit code 1` and
forwarded it. The webapp showed the generic "LLM provider issue" copy. The
operator only discovered the true cause — and that a re-auth attempt had
*failed* — by reading container logs.

The backend **already knows** the real cause at failure time
(`oauth_refresh.get_token_health()` returns `unhealthy` with the exact
remediation once the pre-query refresh flags the token), but discards it. The
frontend has a solid error-category system (`ErrorBubble.vue`) but nothing
specific to render for this class.

## Goal

Operator-actionable error messages. When the agent fails, name the real fault
class and the fix. Chosen audience: **operator-actionable** (this is a
public/portfolio repo, but the operator is the primary user; internal cause
detail is acceptable).

## Approach (chosen: A)

Backend classifies failures into a typed `reason` + operator-facing message +
raw detail, emits a **structured SSE `error` event**, and the frontend renders
`reason`-specific copy. Rejected alternatives: frontend-only string heuristics
(brittle, ignores the authoritative health signal) and health-endpoint polling
after failure (extra round-trip + race, since the health flag self-heals on the
next successful refresh).

## Components

### 1. Backend classification — `sre-agent/src/agent/agent_errors.py` (new)

```python
from enum import Enum
from dataclasses import dataclass

class AgentErrorReason(str, Enum):
    LLM_AUTH_FAILED = "llm_auth_failed"   # credential rejected — operator must act
    AGENT_NO_ANSWER = "agent_no_answer"   # honest generic fallback
    # reserved for later: LLM_PROVIDER_ERROR, TOOL_ERROR, RATE_LIMITED

@dataclass(frozen=True)
class AgentError:
    reason: AgentErrorReason
    message: str   # operator-facing, human readable
    detail: str    # raw cause (exception text or health detail)

def classify_agent_failure(exc: Exception) -> AgentError:
    """Map an SDK failure to a typed, actionable error. Never raises."""
    try:
        status, detail = get_token_health()
        if status == "unhealthy":
            return AgentError(
                AgentErrorReason.LLM_AUTH_FAILED,
                "The agent can't authenticate to the LLM provider — its "
                "credential was rejected. An operator needs to renew it on the "
                "host and restart the agent.",
                detail or "OAuth credential unhealthy",
            )
    except Exception:
        pass  # classification must never mask the original failure
    return AgentError(
        AgentErrorReason.AGENT_NO_ANSWER,
        "The agent processed your question but couldn't produce an answer. "
        "Retrying is usually safe.",
        str(exc),
    )
```

**Why the auth signal is reliable:** `ensure_valid_token()` runs before every
SDK query. When the refresh token is dead, `_do_refresh` sets
`_rejected_refresh_token_hash` (and other bad states set their own), so by the
time the `except` fires, `get_token_health()` returns `unhealthy`. Any unhealthy
token state routes to `LLM_AUTH_FAILED`; everything else is the generic bucket.

### 2. SSE contract change — `sdk_agent.py` `except` block

Before:
```python
except Exception as exc:
    logger.exception("SDK streaming failed")
    yield {"type": "error", "content": f"Agent error: {exc}"}
    return
```
After:
```python
except Exception as exc:
    logger.exception("SDK streaming failed")
    err = classify_agent_failure(exc)
    AGENT_ERRORS_TOTAL.labels(reason=err.reason.value).inc()
    yield {
        "type": "error",
        "content": err.message,       # human message (unchanged field, better value)
        "reason": err.reason.value,   # NEW: machine code
        "detail": err.detail,         # NEW: raw cause
    }
    return
```

`content` remains the human message → **backward-compatible**: an older frontend
still shows something sensible. `reason` and `detail` are additive.

### 3. Metric — `src/observability/metrics.py`

`AGENT_ERRORS_TOTAL = Counter("sre_assistant_agent_errors_total", "...",
["reason"])`. Enables a Grafana/Prometheus alert on
`sre_assistant_agent_errors_total{reason="llm_auth_failed"}` — the alert the
2026-06-21 journal specced but never provisioned.

### 4. Frontend parse — `sre-webapp/src/stores/chat.ts`

The `sse-error-event` path captures `event.reason` and `event.detail`.
`ChatMessageError` gains `reason?: string`; `detail` populates the existing
`causeMessage` field.

### 5. Frontend render — `sre-webapp/src/components/ErrorBubble.vue`

For `category === 'sse-error-event'`:
- **explanation** prefers the backend `content` (falls back to today's canned
  string when absent).
- **heading** keys on `reason`: `llm_auth_failed` → "Agent can't reach the LLM";
  otherwise the current "Agent reported an error".
- **Details drawer** additionally shows `reason` alongside the existing cause.

Retry stays available for all `sse-error-event` (harmless; works once the
operator fixes auth).

## Data flow

```
SDK query() raises
  → except in stream_sdk_agent
    → classify_agent_failure(exc)  [reads get_token_health()]
      → AGENT_ERRORS_TOTAL.labels(reason).inc()
      → yield {type:error, content, reason, detail}   (SSE)
        → chat.ts parses reason+detail  → ChatMessageError{category:'sse-error-event', reason, causeMessage}
          → ErrorBubble: reason-specific heading + backend message + detail drawer
```

## Edge cases

- **API-key (non-OAuth) deploys:** `get_token_health()` returns `healthy`/"no
  credentials file" → generic bucket. Correct.
- **Old backend / missing fields:** frontend falls back to canned copy. No hard
  dependency on the new fields.
- **`classify_agent_failure` must never throw** — it runs inside the failure
  path; wrapped in try/except, worst case → generic bucket.

## Testing

- **Backend (`sre-agent/tests/`):**
  - unit: `classify_agent_failure` — unhealthy→`llm_auth_failed`,
    healthy→`agent_no_answer`, `get_token_health` raising→generic (never throws),
    detail passthrough.
  - integration: a raised `query()` in `stream_sdk_agent` yields a structured
    error event with the expected `reason`; metric increments.
- **Frontend (`sre-webapp/tests/e2e/`):**
  - Playwright: mock SSE emitting `{type:error, reason:"llm_auth_failed",
    content, detail}`; assert the specific heading + explanation render and the
    detail appears in the Details drawer. Also assert graceful fallback when
    `reason` is absent.

## Docs

- `sre-webapp/docs/api-integration.md` — document the `error` event's new
  `reason` and `detail` fields and the `reason` vocabulary.
- `sre-agent/docs/code-flow.md` (or `tool-reference.md`) — note the error
  taxonomy.
- Journal entry in each repo.

## Out of scope (YAGNI)

- Additional `reason` codes (`tool_error`, `llm_provider_error`,
  `rate_limited`) — the enum is left extensible but only auth + generic are
  wired now.
- Auto-notifying the operator (Slack/email) on `llm_auth_failed` — the metric +
  alert covers detection; notification is a separate change.
- Suppressing Retry for auth failures — retry is harmless and correct once the
  operator re-auths.
