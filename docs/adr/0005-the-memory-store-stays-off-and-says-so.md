# ADR-0005: The memory store stays off, and says so

**Status:** Accepted. **Date:** 2026-11-11.

## 1. Context

- Build phase 7 added a SQLite memory store (`src/memory/`) for incident records, metric baselines, week-over-week
  report comparison and query patterns, with the `memory_*` tools on top. `MEMORY_DB_PATH` turns it on, and an empty
  value leaves it off.
- A read-only check of the infra VM on 2026-11-11 found the store off in production since 2026-04-24. The container
  sets no `MEMORY_DB_PATH`, and the host path mounted for the database is a root-owned directory that Docker created.
  Nothing logged or measured this, which broke the "never silently fail" principle.
- On 2026-11-11 the owner chose to make the state visible and decide about enabling the store later.

## 2. Decision

The memory store stays off in production, and the app reports its state at startup. `log_memory_status()` in
`src/memory/store.py` runs in the lifespan of `src/api/main.py` and sets the gauge
`sre_assistant_memory_store_enabled`:

- `MEMORY_DB_PATH` empty: a WARNING ("Memory store disabled: ..."), gauge 0.
- Set, but a directory or a file that does not open: an ERROR, gauge 0.
- Set, and the database opens: an INFO line, gauge 1.

## 3. Consequences

1. Production logs the WARNING at each start, and the gauge reads 0. Both were seen live after the 2026-11-11 deploy
   of PR #45.
2. The `memory_*` tools are absent from `/mcp` and from the agent, and the weekly report has no previous report to
   compare against.
3. Enabling the store takes `MEMORY_DB_PATH` and a fixed mount. The store opens SQLite in WAL mode, which writes
   sidecar files next to the database, so the mount must be a real state directory. home-server#423 tracks it.
4. Enabling the store is a new decision, recorded in a new ADR that supersedes this one.
