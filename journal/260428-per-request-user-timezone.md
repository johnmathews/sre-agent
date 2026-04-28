# 2026-04-28 — Per-request user timezone

## Why

Earlier today I shipped the date/time clarity fix, which added `USER_TIMEZONE`
as an env var. That solves the case of "agent reasons in your local clock"
for a single deployment, but the var is read once at process start — it can't
follow you when you travel. Issue #15 captured the follow-up: have the
client send the device's IANA timezone with every request, validated and
used to render "now" in the user's *current* timezone, not the deploy-time
default.

This is the second of two PRs that came out of the prod conversation
4e594980 incident.

## Design

ContextVar-driven, one tz per request, no agent-function signature changes.

1. `AskRequest.user_timezone: str | None` accepted by `/ask` and `/ask/stream`.
   Validated at the request boundary by `field_validator` calling
   `is_valid_timezone()` — non-IANA values (`"CEST"`, `"+02:00"`) are
   rejected with HTTP 422 before the agent even sees the request.

2. `request_user_timezone(tz)` contextmanager in `src/agent/tools/clock.py`
   sets a `ContextVar[str | None]` for the duration of the with-block.
   The API handler wraps the agent invocation in this:

   ```python
   with request_user_timezone(request.user_timezone):
       async for event in stream_agent(...):
           ...
   ```

   ContextVars propagate correctly across `await` points within the same
   task, so both the system-prompt build (called every invocation) and any
   `get_current_time` tool calls during the agent loop see the same value.

3. `effective_timezone(settings)` is the single accessor — returns the
   contextvar override when set, falls back to `settings.user_timezone`
   when not. Both `render_prompt_time_fields` and the `get_current_time`
   tool now use it. No more direct reads of `settings.user_timezone` in
   the rendering code.

4. `save_turn(..., user_timezone=...)` persists the tz on each turn. The
   field is optional in the `Turn` TypedDict (`total=False`) so legacy
   history files without it remain valid. SDK and LangGraph paths both
   pass `effective_timezone(settings)` through. A user travelling
   mid-conversation gets different `user_timezone` values on different
   turns, which is the correct record of what happened.

## What I considered and rejected

1. **Threading `user_timezone` through every agent function signature.**
   Cleaner type-wise but cluttered: `invoke_agent`, `stream_agent`,
   `_invoke_langgraph_agent`, `invoke_sdk_agent`, `stream_sdk_agent` would
   all need a new optional kwarg, plus the tool runtime would need to
   receive it from somewhere. ContextVar is exactly the right tool —
   per-request scope, propagates through async, no signature noise.

2. **Storing `user_timezone` only on user turns, not assistant.** Saves
   bytes but loses information. If a user changes timezones partway
   through a conversation, knowing which assistant reply happened in
   which zone matters for replay/display.

3. **Making `LangGraph` rebuild the agent per request to inject the
   timezone-bearing system prompt.** Too expensive — `create_agent` is
   not cheap. The contextvar approach lets us keep the agent built once
   and still have per-request behavior. The system prompt is only
   rebuilt fresh in the SDK path (which already does so on every
   invocation). For the LangGraph path, the system prompt baked at
   startup uses the env-var default, but `get_current_time` (which
   reads the contextvar at call time) still honours the override —
   so the agent gets the correct local time when it asks. Acceptable
   trade-off; prod uses Anthropic anyway.

## Tests

20 new test cases:
- `test_clock.py` — contextvar override (no override, override wins,
  nested overrides restore correctly, `None` is a no-op, render and tool
  both see the override) and `is_valid_timezone` (accepts IANA, rejects
  CEST/PDT/BST). Note: `EST` and `CET` are technically valid IANA names
  (legacy aliases in tzdata) so we don't try to reject them — `.env.example`
  documents that they're fixed-offset and recommends `Europe/Amsterdam`-
  style names.
- `test_api_integration.py` — request flow: `user_timezone` sets the
  contextvar, missing field falls back to settings, invalid IANA returns
  422, `+02:00` returns 422, empty string treated as unset.
- `test_history.py` — `save_turn` persists the field when given,
  omits it when `None`, supports per-turn timezones (travelling user).

Full suite: 966 passed (20 new), ruff and mypy clean.

## What's still in scope but not done

The webapp displays past timestamps in UTC ("Human [2026-04-28 07:59 UTC]")
even when the conversation has `user_timezone` data. Rendering the saved tz
in the timestamp display is a UX polish — added to the issue followups.
The agent's reasoning is unaffected since it reads the current timezone
from `effective_timezone()`, not from history turn metadata.

## Webapp side

Companion webapp PR ships in `sre-webapp` — see its journal for the
client-side changes.
