**Status:** superseded by [code-flow.md](../code-flow.md) ("Structured error events") and [`src/agent/agent_errors.py`](../../src/agent/agent_errors.py) (2026-11-11).

# Actionable Agent Error Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the agent's single generic failure message with typed, operator-actionable errors — starting with a specific message for a rejected LLM credential.

**Architecture:** The backend classifies each SDK failure into a `reason` code + operator-facing message + raw detail (`agent_errors.py`), emits it as a structured SSE `error` event (adding `reason` and `detail` fields), and the webapp renders `reason`-specific copy in `ErrorBubble.vue`. The `content` field stays the human message, so the change is backward-compatible in both directions.

**Tech Stack:** Backend — Python 3.13, FastAPI, Claude Agent SDK, prometheus_client, pytest. Frontend — Vue 3 (`<script setup>` + TS), Pinia, Playwright.

## Global Constraints

- Backend: Python 3.13; dependency management via **uv** only; **full type annotations** on all signatures/returns; **mypy strict** must pass; tests via **pytest**; run `make check` (lint + typecheck + test) before finishing.
- Backend design principle: **never silently fail** — every degradation visible + logged.
- Frontend: Vue 3 Composition API `<script setup lang="ts">` only; Tailwind utilities in templates; **never `v-html` raw model output**; Node 22+; `npm run typecheck` + `npm test` (Playwright, mocked backend) must pass.
- Two **separate git repos**: `sre-agent/` (backend) and `sre-webapp/` (frontend). Commit within each repo. Backend work is on branch `feature/actionable-agent-errors`.
- Operator-facing message copy (use **verbatim**):
  - `llm_auth_failed`: `"The agent can't authenticate to the LLM provider — its credential was rejected. An operator needs to renew it on the host and restart the agent."`
  - `agent_no_answer`: `"The agent processed your question but couldn't produce an answer. Retrying is usually safe."`
- `reason` vocabulary (string values): `llm_auth_failed`, `agent_no_answer` (others reserved, not implemented).

---

## Task 1: Backend — error-classification module

**Files:**
- Create: `sre-agent/src/agent/agent_errors.py`
- Test: `sre-agent/tests/test_agent_errors.py`

**Interfaces:**
- Consumes: `src.agent.oauth_refresh.get_token_health() -> tuple[str, str | None]` (existing).
- Produces:
  - `AgentErrorReason(str, Enum)` with members `LLM_AUTH_FAILED = "llm_auth_failed"`, `AGENT_NO_ANSWER = "agent_no_answer"`.
  - `AgentError` frozen dataclass with fields `reason: AgentErrorReason`, `message: str`, `detail: str`.
  - `classify_agent_failure(exc: Exception) -> AgentError` — never raises.

- [ ] **Step 1: Write the failing tests**

Create `sre-agent/tests/test_agent_errors.py`:
```python
"""Tests for agent failure classification."""

from unittest.mock import patch

from src.agent.agent_errors import (
    AgentErrorReason,
    classify_agent_failure,
)


def test_unhealthy_token_classifies_as_auth_failure() -> None:
    detail = "refresh token rejected by Anthropic (invalid_grant) — re-authenticate"
    with patch(
        "src.agent.agent_errors.get_token_health",
        return_value=("unhealthy", detail),
    ):
        err = classify_agent_failure(Exception("Command failed with exit code 1"))
    assert err.reason is AgentErrorReason.LLM_AUTH_FAILED
    assert "authenticate to the LLM provider" in err.message
    assert err.detail == detail


def test_healthy_token_classifies_as_generic_no_answer() -> None:
    with patch(
        "src.agent.agent_errors.get_token_health",
        return_value=("healthy", "access token valid (7.5h), refresh token present"),
    ):
        err = classify_agent_failure(Exception("boom"))
    assert err.reason is AgentErrorReason.AGENT_NO_ANSWER
    assert err.detail == "boom"


def test_classification_never_raises_when_health_check_errors() -> None:
    with patch(
        "src.agent.agent_errors.get_token_health",
        side_effect=RuntimeError("health check blew up"),
    ):
        err = classify_agent_failure(Exception("original failure"))
    assert err.reason is AgentErrorReason.AGENT_NO_ANSWER
    assert err.detail == "original failure"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd sre-agent && uv run pytest tests/test_agent_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.agent_errors'`.

- [ ] **Step 3: Write the module**

Create `sre-agent/src/agent/agent_errors.py`:
```python
"""Classify agent/SDK failures into typed, operator-actionable errors.

The Claude Agent SDK surfaces most failures as an opaque
``Command failed with exit code 1``.  This module maps a failure to a stable
``reason`` code plus an operator-facing message and the raw cause, so the API
can emit a structured SSE error event and the webapp can render specific copy.

Classification is best-effort and MUST NOT raise — it runs inside the failure
path, so any error here falls back to the generic bucket.
"""

from dataclasses import dataclass
from enum import Enum

from src.agent.oauth_refresh import get_token_health

_AUTH_FAILED_MESSAGE = (
    "The agent can't authenticate to the LLM provider — its credential was "
    "rejected. An operator needs to renew it on the host and restart the agent."
)
_NO_ANSWER_MESSAGE = (
    "The agent processed your question but couldn't produce an answer. "
    "Retrying is usually safe."
)


class AgentErrorReason(str, Enum):
    """Stable machine-readable failure codes shared with the webapp."""

    LLM_AUTH_FAILED = "llm_auth_failed"
    AGENT_NO_ANSWER = "agent_no_answer"
    # Reserved for later: LLM_PROVIDER_ERROR, TOOL_ERROR, RATE_LIMITED.


@dataclass(frozen=True)
class AgentError:
    """A classified failure: a reason code, a human message, and the raw cause."""

    reason: AgentErrorReason
    message: str
    detail: str


def classify_agent_failure(exc: Exception) -> AgentError:
    """Map an SDK failure to a typed, actionable error. Never raises."""
    try:
        status, detail = get_token_health()
        if status == "unhealthy":
            return AgentError(
                reason=AgentErrorReason.LLM_AUTH_FAILED,
                message=_AUTH_FAILED_MESSAGE,
                detail=detail or "OAuth credential unhealthy",
            )
    except Exception:
        # Classification must never mask the original failure.
        pass
    return AgentError(
        reason=AgentErrorReason.AGENT_NO_ANSWER,
        message=_NO_ANSWER_MESSAGE,
        detail=str(exc),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd sre-agent && uv run pytest tests/test_agent_errors.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Typecheck**

Run: `cd sre-agent && make typecheck`
Expected: mypy clean.

- [ ] **Step 6: Commit**

```bash
cd sre-agent
git add src/agent/agent_errors.py tests/test_agent_errors.py
git commit -m "feat: classify agent failures into typed actionable errors

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Backend — metric + wire into the SSE stream

**Files:**
- Modify: `sre-agent/src/observability/metrics.py` (add counter after the OAuth metrics block, ~line 128)
- Modify: `sre-agent/src/agent/sdk_agent.py` (imports near top; `except` block at ~line 475; docstring at ~line 404)
- Test: `sre-agent/tests/test_agent_errors.py` (append integration test)

**Interfaces:**
- Consumes: `classify_agent_failure` (Task 1); `AGENT_ERRORS_TOTAL` (this task).
- Produces: `stream_sdk_agent` now yields, on failure, `{"type": "error", "content": <message>, "reason": <reason value>, "detail": <raw cause>}`.

- [ ] **Step 1: Write the failing integration test**

Append to `sre-agent/tests/test_agent_errors.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_stream_emits_structured_auth_error(mock_settings: object) -> None:
    """A dead OAuth credential yields a reason-tagged error event, not an opaque one."""
    from src.agent import sdk_agent

    async def failing_query(**kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("Command failed with exit code 1")
        yield  # pragma: no cover — makes this an async generator

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    with (
        patch("src.agent.sdk_agent.query", side_effect=failing_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
        patch(
            "src.agent.agent_errors.get_token_health",
            return_value=("unhealthy", "refresh token rejected by Anthropic (invalid_grant)"),
        ),
    ):
        events = [ev async for ev in sdk_agent.stream_sdk_agent(options, "hi")]

    error_events = [ev for ev in events if ev["type"] == "error"]
    assert len(error_events) == 1
    err = error_events[0]
    assert err["reason"] == "llm_auth_failed"
    assert "authenticate to the LLM provider" in err["content"]
    assert "invalid_grant" in err["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sre-agent && uv run pytest tests/test_agent_errors.py::test_stream_emits_structured_auth_error -v`
Expected: FAIL — `KeyError: 'reason'` (current code yields only `type` + `content`).

- [ ] **Step 3: Add the metric**

In `sre-agent/src/observability/metrics.py`, after the `OAUTH_REFRESH_TOTAL` definition (~line 128), add:
```python
AGENT_ERRORS_TOTAL = Counter(
    "sre_assistant_agent_errors_total",
    "Total classified agent failures by reason",
    labelnames=["reason"],
)
```

- [ ] **Step 4: Wire the classifier into `stream_sdk_agent`**

In `sre-agent/src/agent/sdk_agent.py`, add imports alongside the other `src.` imports near the top of the file:
```python
from src.agent.agent_errors import classify_agent_failure
from src.observability.metrics import AGENT_ERRORS_TOTAL
```

Replace the `except` block (currently ~line 475):
```python
    except Exception as exc:
        logger.exception("SDK streaming failed")
        yield {"type": "error", "content": f"Agent error: {exc}"}
        return
```
with:
```python
    except Exception as exc:
        logger.exception("SDK streaming failed")
        err = classify_agent_failure(exc)
        AGENT_ERRORS_TOTAL.labels(reason=err.reason.value).inc()
        yield {
            "type": "error",
            "content": err.message,
            "reason": err.reason.value,
            "detail": err.detail,
        }
        return
```

Update the `stream_sdk_agent` docstring "Yields dicts with keys" list (~line 404) to add:
```python
      - reason (only on "error"): machine-readable failure code
      - detail (only on "error"): raw underlying cause, for a details view
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd sre-agent && uv run pytest tests/test_agent_errors.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Full backend check**

Run: `cd sre-agent && make check`
Expected: lint + mypy + tests all pass. (If mypy flags the yield dict against the `dict[str, str]` return type, that is fine — all values are `str`.)

- [ ] **Step 7: Commit**

```bash
cd sre-agent
git add src/observability/metrics.py src/agent/sdk_agent.py tests/test_agent_errors.py
git commit -m "feat: emit structured, reason-tagged SSE error events on agent failure

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Backend — docs + journal

**Files:**
- Modify: `sre-agent/docs/code-flow.md` (error-path section)
- Create: `sre-agent/journal/260722-actionable-agent-errors.md`

- [ ] **Step 1: Document the error taxonomy in code-flow.md**

In `sre-agent/docs/code-flow.md`, find the section describing `stream_sdk_agent` / SSE events and add a short subsection:
```markdown
### Structured error events

On failure, `stream_sdk_agent` calls `agent_errors.classify_agent_failure`,
which maps the exception to a stable `reason` code using
`oauth_refresh.get_token_health()`:

- `llm_auth_failed` — the OAuth credential was rejected (`invalid_grant`) or is
  otherwise unhealthy; an operator must re-authenticate on the host and restart.
- `agent_no_answer` — generic fallback; retrying is usually safe.

The emitted SSE `error` event carries `content` (operator-facing message),
`reason` (machine code), and `detail` (raw cause). `sre_assistant_agent_errors_total{reason}`
counts each; alert on `reason="llm_auth_failed"`.
```

- [ ] **Step 2: Write the journal entry**

Create `sre-agent/journal/260722-actionable-agent-errors.md`:
```markdown
# Actionable agent error messages (260722)

## Motivation

The 2026-07-22 OAuth refresh-token outage surfaced as the webapp's generic
"failed to produce an answer (often a tool error or LLM provider issue)". The
backend already knew the real cause (`get_token_health()` reported the dead
refresh token) but discarded it, forwarding the SDK's opaque
`Command failed with exit code 1`.

## Change

- New `src/agent/agent_errors.py`: `classify_agent_failure()` maps a failure to
  a typed `AgentError(reason, message, detail)`. `llm_auth_failed` when the
  token is unhealthy; `agent_no_answer` otherwise. Never raises.
- `stream_sdk_agent` emits a structured SSE error event: `content` (message),
  `reason`, `detail`. Backward-compatible — `content` stays the human message.
- New metric `sre_assistant_agent_errors_total{reason}` enables the
  `llm_auth_failed` alert the 260621 journal specced.

Design: `docs/archive/2026-07-22-actionable-agent-errors-design.md`.
Frontend half lives in the `sre-webapp` repo.
```

- [ ] **Step 3: Commit**

```bash
cd sre-agent
git add docs/code-flow.md journal/260722-actionable-agent-errors.md
git commit -m "docs: document structured error taxonomy + journal

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Frontend — parse + render reason-specific errors

**Files:**
- Modify: `sre-webapp/src/api/stream.ts` (`StreamEvent` interface, ~line 32)
- Modify: `sre-webapp/src/stores/chat.ts` (`ChatMessageError` ~line 14; `sseError` local ~line 220; capture ~line 253; construction ~line 290)
- Modify: `sre-webapp/src/components/ErrorBubble.vue` (`heading` ~line 31; `explanation` ~line 56; Details drawer ~line 122)
- Modify: `sre-webapp/tests/e2e/fixtures.ts` (widen `buildSseBody` event type if needed)
- Test: `sre-webapp/tests/e2e/error-resilience.spec.ts` (append a test)

**Interfaces:**
- Consumes: SSE `error` event fields `content`, `reason`, `detail` (Task 2).
- Produces: `ChatMessageError` gains `reason?: string` and `backendMessage?: string`; `ErrorBubble` renders reason-specific heading + backend message, with today's copy as fallback.

**Backward-compat rule (critical):** treat `content` as the operator message **only when `reason` is present**. When `reason` is absent (old backend), keep today's behavior: canned explanation + `content` shown in the Details drawer. This keeps the existing "SSE error event renders an ErrorBubble" test green.

- [ ] **Step 1: Write the failing Playwright test**

Append to `sre-webapp/tests/e2e/error-resilience.spec.ts` inside the `test.describe('error resilience and recovery', ...)` block:
```typescript
  test('structured auth error renders reason-specific copy', async ({
    page,
  }) => {
    await mockBackend(page, {
      streamBody: buildSseBody([
        { type: 'status', content: 'Thinking...' },
        {
          type: 'error',
          content:
            "The agent can't authenticate to the LLM provider — its credential was rejected. An operator needs to renew it on the host and restart the agent.",
          reason: 'llm_auth_failed',
          detail: 'refresh token rejected by Anthropic (invalid_grant)',
        },
      ]),
    })
    await page.goto('/')
    await send(page)

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible()
    // Reason-specific heading.
    await expect(alert).toContainText("Agent can't reach the LLM")
    // Operator-facing message shown directly, NOT the canned generic text.
    await expect(alert).toContainText('renew it on the host')
    await expect(alert).not.toContainText('often a tool error')
    // Raw cause + reason live behind the Details disclosure.
    await expect(alert.getByText('invalid_grant')).not.toBeVisible()
    await alert.getByRole('button', { name: 'Details' }).click()
    await expect(alert.getByText('invalid_grant')).toBeVisible()
    await expect(alert).toContainText('llm_auth_failed')
  })
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd sre-webapp && npm test -- error-resilience`
Expected: FAIL — heading shows "Agent reported an error" and the canned "often a tool error" text, so the `toContainText("Agent can't reach the LLM")` assertion fails. (If `buildSseBody`'s TS type rejects the `reason`/`detail` fields, the test file won't compile — proceed to Step 3 which widens it.)

- [ ] **Step 3: Add `reason` / `detail` to `StreamEvent`**

In `sre-webapp/src/api/stream.ts`, replace the `StreamEvent` interface (~line 32):
```typescript
export interface StreamEvent {
  type: StreamEventType
  content: string
  session_id?: string
  /** Machine-readable failure code — only on `error` events. */
  reason?: string
  /** Raw underlying cause — only on `error` events; shown in Details. */
  detail?: string
}
```

- [ ] **Step 4: Extend `ChatMessageError` and capture the fields in `chat.ts`**

In `sre-webapp/src/stores/chat.ts`, add to the `ChatMessageError` interface (after `causeMessage`, ~line 20):
```typescript
  /** Machine-readable failure code from a structured SSE error event. */
  reason?: string
  /** Operator-facing message from the backend (structured error events). */
  backendMessage?: string
```

Widen the `sseError` local (~line 220):
```typescript
    let sseError: { content: string; reason?: string; detail?: string } | null =
      null
```

Update the capture (~line 253):
```typescript
        } else if (event.type === 'error') {
          sseError = {
            content: event.content || 'Backend reported an error',
            reason: event.reason,
            detail: event.detail,
          }
        }
```

Update the error-message construction (~line 290) so `reason` present ⇒ backend message + detail; absent ⇒ today's behavior:
```typescript
      s.messages.push({
        role: 'assistant',
        content: '',
        kind: 'error',
        error: {
          category: 'sse-error-event',
          reason: sseError.reason,
          backendMessage: sseError.reason ? sseError.content : undefined,
          causeMessage: sseError.reason ? sseError.detail : sseError.content,
          originalQuestion: q,
        },
      })
```

- [ ] **Step 5: Render reason-specific copy in `ErrorBubble.vue`**

In `sre-webapp/src/components/ErrorBubble.vue`, update the `heading` `sse-error-event` case (~line 31):
```typescript
    case 'sse-error-event':
      if (props.error.reason === 'llm_auth_failed')
        return "Agent can't reach the LLM"
      return 'Agent reported an error'
```

Update the `explanation` `sse-error-event` case (~line 56):
```typescript
    case 'sse-error-event':
      return (
        props.error.backendMessage ??
        'The agent processed your question but failed to produce an answer (often a tool error or LLM provider issue). Retrying is safe.'
      )
```

Add a `Reason` row to the Details drawer, after the `Category` line (~line 122):
```html
            <div v-if="error.reason">
              <span class="font-semibold">Reason:</span> {{ error.reason }}
            </div>
```

- [ ] **Step 6: Widen the fixture event type if the test failed to compile**

If Step 2 reported a type error on `buildSseBody`, open `sre-webapp/tests/e2e/fixtures.ts`, find the `buildSseBody` parameter type, and widen the event object type to:
```typescript
Array<{
  type: string
  content: string
  reason?: string
  detail?: string
  session_id?: string
}>
```

- [ ] **Step 7: Run the new test + the existing suite**

Run: `cd sre-webapp && npm run typecheck && npm test -- error-resilience`
Expected: PASS — including the pre-existing "SSE error event renders an ErrorBubble" test (old-style event still shows the canned copy with the cause behind Details) and the new auth-error test.

- [ ] **Step 8: Commit**

```bash
cd sre-webapp
git add src/api/stream.ts src/stores/chat.ts src/components/ErrorBubble.vue tests/e2e/error-resilience.spec.ts tests/e2e/fixtures.ts
git commit -m "feat: render reason-specific agent error messages

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Frontend — API-contract docs + journal

**Files:**
- Modify: `sre-webapp/docs/api-integration.md` (SSE error event section)
- Create: `sre-webapp/journal/260722-actionable-agent-errors.md`

- [ ] **Step 1: Document the new SSE error fields**

In `sre-webapp/docs/api-integration.md`, in the `POST /ask/stream` / error-event section, document:
```markdown
An `error` event may carry, in addition to `content`:

- `reason` — machine-readable failure code. Known values: `llm_auth_failed`
  (LLM credential rejected — operator must re-authenticate), `agent_no_answer`
  (generic; retry is usually safe). Unknown/absent ⇒ frontend shows generic copy.
- `detail` — raw underlying cause, shown in the ErrorBubble "Details" disclosure.

When `reason` is present, `content` is the operator-facing message and is shown
as the bubble explanation. When absent (older backend), `content` is treated as
the raw cause and shown behind Details, preserving prior behavior.
```

- [ ] **Step 2: Write the journal entry**

Create `sre-webapp/journal/260722-actionable-agent-errors.md`:
```markdown
# Reason-specific agent error messages (260722)

Backend now emits structured SSE `error` events (`content` + `reason` +
`detail`). `ErrorBubble` keys its heading + explanation on `reason`:
`llm_auth_failed` → "Agent can't reach the LLM" + the operator-facing message;
otherwise the existing generic copy. `reason` absent ⇒ unchanged behavior
(`content` behind Details), so old backends and the prior E2E test still pass.

Contract: `docs/api-integration.md`. Backend + design spec live in `sre-agent`.
```

- [ ] **Step 3: Commit**

```bash
cd sre-webapp
git add docs/api-integration.md journal/260722-actionable-agent-errors.md
git commit -m "docs: document structured SSE error contract + journal

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** classification module (T1), SSE contract + metric (T2), backend docs (T3), frontend parse+render with backward-compat (T4), API docs (T5) — every spec section maps to a task. Reserved reason codes intentionally not implemented (YAGNI, per spec "Out of scope").
- **Backward-compat:** T4's rule (treat `content` as message only when `reason` present) is exercised by keeping the existing `error-resilience.spec.ts` test green (T4 Step 7).
- **Type consistency:** `reason` string values (`llm_auth_failed`, `agent_no_answer`) match across `AgentErrorReason` (T1), the emitted event (T2), and the frontend checks (T4). `AgentError` fields `reason`/`message`/`detail` are consistent T1→T2. Frontend `backendMessage`/`reason` fields consistent T4 store→component.
