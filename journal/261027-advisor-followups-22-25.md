# Upgrade Advisor follow-ups: verify timing, trigger deadline, Access fragment, missing tags (#22–#25)

Four findings from the advisor's final review (`261026-upgrade-advisor.md`), fixed test-first in one branch.

## #22 — the verify delay runs from when the trigger returned
- `deploys` gains `settled_at`, written by `store.settle_trigger` (now with a required `now=`). `verify_pending` waits
  `UPGRADE_ADVISOR_VERIFY_DELAY_SECONDS` from `settled_at`, falling back to `started_at` for rows that never had one.
- Existing stores get the column from `_add_missing_columns` in `store.connect` (`ALTER TABLE ... ADD COLUMN`).
  `CREATE TABLE IF NOT EXISTS` never alters a table, and the store had no migration step until now.
- A reaped row cannot know when its trigger ended. It is settled at `started_at + TRIGGER_TIMEOUT_SECONDS`, the latest
  moment the trigger could have been running, and the delay runs from there. Consequence: a row reaped within the
  delay of that moment is verified on the following run, not the same one. That is deliberate, because WUD may still
  have been deploying right up to the deadline.

## #23 — a hard total deadline on the WUD trigger
- `wud.trigger_deploy` wraps the request in `asyncio.timeout(TRIGGER_TIMEOUT_SECONDS)`. httpx's `timeout=600` is per
  operation and a slowly trickled answer could outlive it. A deadline hit raises `WudError("... timed out after 600 s")`,
  which `_trigger` already records as `trigger_error`.
- With the deadline guaranteed, the reaper margin drops from 2 × 600 s to 600 s + `REAP_GRACE_SECONDS` (60 s). The
  grace covers the settle write after the trigger returns.

## #24 — an Access sign-in can drop the `#token` fragment
- The launcher's no-token message now reads "This link has no approval token. If you just signed in, open the link
  from the email again (nothing has been used up)". The CSP hash is computed from `LAUNCH_SCRIPT`, so it follows.
- Not yet observed live. The first real approval after an Access session expiry confirms or refutes it; reopen #24
  then if the second click also arrives without the token.

## #25 — a missing image tag is an answer, not a failed lookup
- `registry.tag_digests` returns an empty set on a 404 (bare, or `MANIFEST_UNKNOWN`), so `release_for_digest` counts
  it as answered with no match: an unreleased build is a hold, not a daily error item.
- A 404 with `NAME_UNKNOWN` (the repository itself is unknown) stays a `RegistryError`. Otherwise a misconfigured
  allowlist entry would silently hold every candidate as unreleased.
- Not done: the issue's optional "running-side lookup failure falls back to `None`". With 404s answered, a running-side
  lookup fails only on network, auth or registry trouble. Turning that into "running version unknown, e.g. a dev build"
  (`needs_you`) would mislabel a transient outage, and the error item already says what failed.

Verification: `make check` green (1421 tests).

## Wrap-up (same session)

- Deployed with `make sre-upgrade` from proxmox-setup at 22:06 CET. The live store gained `deploys.settled_at`,
  and the dry run listed 6 items, all held as "too new", with no errors (confirmed).
- The wrap-up doc audit corrected one pre-existing claim. A deploy whose settle write fails is not verified on
  schedule: the scheduled `verify_deploy` skips a still-`triggering` row, and `verify_pending` reaps it later.
  `architecture.md`, `code-flow.md` and `.env.example` now say so (branch `docs/advisor-settle-wording`).

## What is deliberately not done

- No race guard on the `settled_at` migration. Two processes opening an old-format store at once could both try the
  `ALTER`. The live store is already migrated, and new stores get the column from `CREATE TABLE`, so the race can no
  longer occur.
- A 404 with no `NAME_UNKNOWN` body (empty or non-JSON) counts as "unpublished". A registry that answered that way for
  an unknown repository would show as a daily "unreleased build" hold: visible in the email, not silent.
