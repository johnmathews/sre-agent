# Drop p95 latency SLO; rework OAuth token health semantics

Two related cleanups prompted by the weekly reliability report.

## 1. Drop the p95 latency SLO target

The original `< 15s` p95 target was carried over from the early build phases when
"is the agent fast enough to be usable" was the open question. It is no longer
the right SLO:

- For this assistant, scope and answer accuracy matter more than speed. The
  realistic levers for cutting latency (smaller models, fewer tools, less
  retrieval) all trade off directly against quality.
- Operationally the target was already misleading — anything that does a
  multi-step tool plan trivially blows past 15 s without anything being wrong.

p95 latency is still recorded as an SLI: the histogram, the dashboard panel
and the per-query latency breakdown all remain so catastrophic regressions
stay visible. It just no longer has a fixed pass/fail threshold.

Touched:
- `CLAUDE.md` — Self-observability principle reworded.
- `readme.md` — SLI table column shows "— (tracked, no target)" and added a
  sentence explaining the priority.
- `src/memory/store.py:_extract_report_metrics` — drop the
  `p95_latency_seconds > 15` check from `slo_failures`.
- `src/report/generator.py:format_report_markdown` and the HTML formatter —
  pass `target="—"` for the P95 row, with a new optional
  `actual_unit` parameter on `_format_slo_row` so the value still renders as
  seconds when there is no unit-bearing target string to infer from.
- `dashboards/sre-assistant-sli.json` — the `Request Latency p95` stat panel
  loses its yellow/red threshold steps and gets a renamed title noting it is
  informational. The percentile timeseries panel is unchanged.
- Tests updated: `tests/test_memory.py::test_full_data` (slo_failures: 2 → 1)
  and a new `tests/test_report.py::TestFormatSloRowHumanReadable::test_no_target_with_explicit_unit`.

## 2. OAuth token health: degraded must mean "a human needs to act"

The weekly report's "components with degraded availability" section was
showing `oauth_token` regularly. Tracing it back:

- `src/api/main.py:561` writes `sre_assistant_component_healthy{component=...}`
  as `1.0` only when status is `"healthy"`. `degraded` collapses to `0.0`.
- `_collect_slo_status` does `avg_over_time(...[7d])` per component, so any
  time spent in `degraded` directly drags down the 7-day availability number
  the report renders.
- `get_token_health()` in `src/agent/oauth_refresh.py` was returning
  `degraded` whenever the access token had less than an hour remaining, even
  though `ensure_valid_token` lazily refreshes on the next LLM call when a
  refresh token is present. With ~8h tokens and quiet periods, that meant
  `oauth_token` was reported degraded for ~1 hour out of every 8 — totally
  expected, totally self-healing, but visible as fake unavailability.

Reworked the semantics so `degraded`/`unhealthy` mean a human needs to act:

- Refresh token present + token valid → `healthy`.
- Refresh token present + token expired → `healthy` ("will refresh on next
  call"). The /health detail string still surfaces the expired-ago duration
  so the underlying state is not hidden.
- No refresh token + token valid → `degraded`.
- No refresh token + token expired → `unhealthy`.
- Cannot read creds / missing `expiresAt` → `unhealthy`.
- No creds file or no `claudeAiOauth` block → `healthy` (not using OAuth).

The Prometheus gauge mapping in `src/api/main.py` is left untouched. With the
new health logic the gauge now correctly reads `1.0` whenever a refresh token
is present, so the SLO collector stops counting the natural expiry cycle
against availability.

Added a `TestGetTokenHealth` class in `tests/test_oauth_refresh.py` covering
all six branches.

## Why bundle these together

Both come out of reading the same weekly report and asking "is this fact
about the system actually a problem?" In both cases the answer was no, and
the fix was the report's definitions, not the system's behaviour.

## Verification

`make check`: ruff, ruff format, mypy strict, 974 tests, all green.
