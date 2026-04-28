# 2026-04-28 — Date/time and elapsed-duration clarity

## Trigger

Reviewed conversation `4e594980` in prod (deployed sre-agent on infra). The
agent correctly identified that TrueNAS rebooted at 2026-04-27 21:58 UTC
(epoch 1777327099), but then claimed "Sunday evening" and "uptime ~34 hours"
when the truth was Monday night and ~10 hours. Off by exactly +24h on the
duration, off by one day on the weekday — classic LLM mental-modular-arithmetic
failure. The user corrected it ("21:58 on 27 April is Monday night") and asked
for a structural fix so it never gets confused about current time, dates, or
elapsed durations again.

## Diagnosis

Five contributing weaknesses in how time was provided to the agent:

1. The system prompt rendered date as `2026-04-28` only — no day-of-week.
   Models reliably get day-of-week from a date wrong.
2. No local timezone was injected. The agent guessed CEST (correct by luck
   on this date — DST in effect) but DST transitions and traveling users
   would expose the fragility.
3. Replayed conversation history in `format_history_as_prompt` had no per-turn
   timestamps. Once turn 2 wrote "uptime ~34 hours" (off by +24h), turn 3 saw
   that as ground truth with nothing to detect the contradiction.
4. No explicit elapsed-time arithmetic guidance. The prompt taught Prometheus
   range syntax but never mandated epoch-second subtraction.
5. No `get_current_time` tool. Once a wrong claim entered history, the agent
   couldn't re-anchor.

Plus a minor consistency bug: prompt text said retention "approximately 100
days" but `retention_cutoff` was computed as `now - 90d`.

## Changes

1. New `src/agent/tools/clock.py` containing both:
   - `render_prompt_time_fields(settings)` — single source of truth for the
     time placeholders the system prompt expects. Rendered fresh on each
     agent invocation by both `sdk_agent.py:_build_system_prompt` and
     `agent.py:build_agent`.
   - `get_current_time` LangChain tool — returns UTC ISO, UTC epoch seconds,
     weekday, today's date, the user's IANA timezone, and the user's local
     time. Registered in both the LangChain agent (`_get_tools`) and the
     SDK MCP server (`_clock_tools`).

2. New `USER_TIMEZONE` env var (default `UTC`) added to `Settings`,
   `.env.example`, `readme.md`, and the test fixtures. The system prompt
   and the clock tool both consume it via `zoneinfo.ZoneInfo`. Invalid
   timezone names fall back to UTC with a warning rather than crashing.

3. `system_prompt.md` rewrite of the "Current Date and Time" section:
   - Now renders `**Tuesday, 2026-04-28 08:01:00 UTC**` (weekday + time)
   - Adds an explicit local-time line for the user's timezone
   - Fixes the 100-vs-90-day retention inconsistency

4. New `system_prompt.md` section "Computing Elapsed Time and Durations"
   with five numbered rules: epoch subtraction, show arithmetic for >12h
   durations, look up weekdays from given data not mental modular arithmetic,
   and cross-check before answering. The rule is explicit that
   "weekday + elapsed-hours that disagree" means recompute from epoch.

5. `format_history_as_prompt` in `src/agent/history.py` now annotates each
   replayed turn with its UTC timestamp:
   `Human [2026-04-28 07:59 UTC]: did truenas reboot?`
   Backward-compatible: turns missing or with unparseable timestamps fall
   through to the legacy `Human:` form.

6. New eval YAML `src/eval/cases/elapsed-time-arithmetic.yaml` that asks
   "When did truenas last boot, and how many hours ago?" with a Prometheus
   mock returning the documented prod boot epoch `1777327099`. The rubric
   fails any answer drifting by ~24h or naming the wrong weekday.

7. Tooling adjustments needed to support the above:
   - `tests/conftest.py::mock_settings` refactored from nested `with` to
     `ExitStack` because adding the new `clock.get_settings` patch site
     pushed it past CPython's 20-context-manager static block limit.
   - `src/eval/runner.py` patches `clock.get_settings` and seeds
     `user_timezone="UTC"` in `_build_fake_settings`.
   - `.github/workflows/ci.yml` gained `workflow_dispatch:` trigger so
     the workflow can be re-run manually from the Actions tab.

## Tests

- New `tests/test_clock.py` (14 tests): timezone resolution, weekday
  correctness for the documented date that confused the agent
  (2026-04-27 = Monday), local-time rendering with `Europe/Madrid` (CEST
  in late April), epoch round-trip, all prompt-field placeholders present,
  retention 90-days-back, fallback to UTC on bad timezone.
- Extended `tests/test_history.py` with 4 cases covering ISO-with-tz,
  naive-as-UTC, missing timestamp, and unparseable timestamp.
- Extended `tests/test_sdk_agent.py::TestBuildSystemPrompt` with weekday
  assertion, MCP-prefixed tool name assertion, elapsed-time guidance
  assertion, and a regex sweep for unfilled `{placeholder}` patterns.
- Full suite: 946 passed, ruff and mypy clean, clock.py at 100%.

## Why this should hold up

The five fixes attack the failure mode at three different levels: the prompt
itself (so the model has the weekday and the math rule), the conversation
history replay (so stale claims can't masquerade as current), and a runtime
tool (so the model has recourse mid-conversation). Even if any single layer
fails, the others still anchor the agent to correct epoch arithmetic.
