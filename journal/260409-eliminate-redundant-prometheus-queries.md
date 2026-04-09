# Eliminate redundant Prometheus queries in hdd_power_status

## Context

The `hdd_power_status` tool was timing out when asked about overnight HDD spinups in
the `tank` datapool. The 12-hour time range caused the overall 120s request timeout to
be exceeded — not because individual Prometheus queries were slow (only 4 HDD series),
but because redundant queries consumed budget from the LLM + tool-call chain.

Investigation was prompted by reviewing real SRE agent conversations that asked about
overnight spinups, some of which produced truncated responses (session `02942e22`).

## Root cause

The tool made the **same Prometheus range query twice** — once in `_get_stats()` to
compute per-disk statistics, and again in `_find_transition_times()` to find when each
disk last changed state. Both functions queried `disk_power_state{type="hdd"}` over the
same time range with the same step, but `_get_stats` discarded the raw values after
computing summary stats.

Additionally, when no transitions were found in the requested duration, the progressive
widening search (`_find_transition_window`) tried all windows 1h → 6h → 24h → 7d
sequentially — even though windows smaller than the already-queried duration were
guaranteed empty.

## Changes

### disk_status.py — fetch once, compute twice

Replaced the async `_get_stats()` and `_find_transition_times()` functions with:

- `_fetch_range_data(duration_seconds)` — single async fetch, returns raw series data
- `_compute_stats_from_data(range_data)` — pure function, computes DiskStats
- `_extract_transitions_from_data(range_data)` — pure function, extracts transitions

The composite tool now fetches data once in Phase 1 and passes it to both pure functions.

`_find_transition_window` gained a `skip_below_seconds` parameter to skip windows
already covered by the initial fetch, and now returns the range data directly so the
caller can extract transitions without re-querying.

Query savings by scenario:

| Scenario               | Before | After | Reduction |
|------------------------|--------|-------|-----------|
| 24h, has transitions   | 2      | 1     | 50%       |
| 24h, no transitions    | 5      | 2     | 60%       |
| 12h, no transitions    | 5      | 3     | 40%       |
| 1w, no transitions     | 5      | 1     | 80%       |

### loki.py — parallelize loki_correlate_changes

The two independent Loki queries (error logs + lifecycle events) in
`loki_correlate_changes` were sequential. Changed to fire both concurrently via
`asyncio.create_task()`, roughly halving wall-clock time for this tool.

### disk_status.py — report all transitions, not just the most recent

After deploying the query deduplication fix, overnight spinup queries still timed out at
125s. Direct MCP testing showed the tool itself returned in ~2s — the bottleneck was the
LLM inference loop, not query speed.

The tool reported "3 changes per disk" but only showed the **last** transition timestamp.
The LLM then called `prometheus_range_query` to find all timestamps (481 samples per
series), consuming multiple inference round trips (~15-20s each) and blowing the timeout.

Fix: `_extract_transitions_from_data` now walks **forward** through the data and returns
every group transition in chronological order. The output section was renamed from
"Last power state change" to "Power state transitions" to reflect this.

Key insight: when an agent tool reports a summary count, it should also enumerate the
details. A mismatch between "N changes" and "here's 1 timestamp" forces the LLM to make
N-1 follow-up queries.

## Testing

- 8 new unit tests for `_compute_stats_from_data` and `_extract_transitions_from_data`
- Updated all 12 integration test mocks to match reduced query counts
- Full suite: 907 passed, lint clean, mypy clean
