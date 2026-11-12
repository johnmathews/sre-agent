# 2026-04-28 — Send device timezone with every /ask/stream request

## Why

The backend (`sre-agent`) just gained per-request `user_timezone` on
`/ask/stream` (issue johnmathews/sre-agent#15). Previously the deploy-time
`USER_TIMEZONE` env var was the single source of truth, which works fine
for one user at one location but can't follow them when they travel.
The webapp now sends the device's current IANA timezone with every
question so the agent renders "now" — and the `get_current_time` tool —
in the user's actual local clock.

## What changed

1. `src/api/timezone.ts` — new helper `getDeviceTimezone()` that reads
   `Intl.DateTimeFormat().resolvedOptions().timeZone` defensively. Returns
   `undefined` if `Intl` is unavailable or the value is empty (rare on
   modern devices, but be defensive — never throw from a request-shaping
   helper).

2. `src/api/stream.ts::streamAsk` — extends `StreamRequest` with optional
   `user_timezone` and fills it in automatically per request. The value
   is read fresh on each call, not cached at module load, so the OS
   timezone change Apple/Android pick up while travelling propagates
   without a page reload.

3. `tests/e2e/user-timezone.spec.ts` — new Playwright spec with three
   cases: tz forwarded to the request body, different value for a
   different stub, omitted when `Intl` returns an empty timezone.

I stub `Intl.DateTimeFormat` directly via `addInitScript` rather than
using Playwright's `BrowserContext` timezone emulation, because (a) the
typed Playwright API does not expose `emulateTimezone` at the time of
writing, and (b) stubbing pins our `getDeviceTimezone()` contract rather
than Playwright's emulation behaviour. The test reads as "given what the
browser reports, here's what the wire looks like."

## What's deferred

Past conversations now carry `user_timezone` per turn in the JSON
history file. The webapp still displays timestamps in UTC for the agent's
prompt context. Rendering past timestamps in the saved tz on the
conversation list / chat scroll is a UX polish that could happen in a
follow-up — agent reasoning is not affected.

## Tests

37/37 webapp Playwright tests pass (3 new), `npm run typecheck` clean.
Backend companion: `sre-agent` 966 tests pass.
