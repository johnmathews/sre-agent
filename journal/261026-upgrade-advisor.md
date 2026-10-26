# Upgrade Advisor implemented

Plan B (the advisor inside `sre-agent`) is built. The spec is `docs/superpowers/specs/2026-10-26-upgrade-advisor-design.md`;
the current behaviour is in `docs/architecture.md` ("Upgrade Advisor"). Decisions and deviations from the spec:

## Principle
- "Advisory only" became "Advisory by default": the one automated action is an upgrade through the approval path.
  The deploy capability is never an agent or MCP tool, enforced by a static test.

## Deviations from the spec, with the ruling behind each
- **Digest-only policy (R5).** A tag change offered by WUD is a hold, not a review. Cost: tag bumps need manual handling.
- **Own GitHub token name (R4).** `UPGRADE_ADVISOR_GITHUB_TOKEN`, so it cannot collide with the docs-server PAT in the
  shared environment.
- **Per-call connections (R8).** The job and each HTTP request open their own SQLite connection; no shared one.
- **Allowlist hardening (R10).** Strict patterns, https/URL-shape checks, context files confined to one root,
  `extra=forbid`. `auto_deploy: true` is rejected until the auto path exists.
- **Prompt and text safety (R11, R17, R18).** Remote text is fenced as untrusted with a nonce and escaped in email
  and pages. An ambiguous or unparseable model answer fails closed to `needs_you`; the candidate-not-newer and
  unknown-running-version cases follow the same fail-closed idea. `review()` itself fails closed to `needs_you` if it is ever called without a candidate release; the daily job holds unreleased builds earlier, at the gate.
  Parse failures log lengths only, never content.
- **Store hardening (R12).** Batch membership has one source (`verdicts.batch_id`), the token is generated and
  hashed in `create_batch`, consumption is one atomic UPDATE, `check_batch` gives a non-mutating GET.
- **HTTP hardening (R14, R15, R16).** Streaming size cap on every read, limited redirects, registry token realm
  allowlist (registry host, `auth.docker.io`, `ghcr.io`, https only).
- **Dry run (R21, amended by R26).** A dry run records first-seen times but saves no verdict rows, because the newest
  verdict supersedes older approval links.
- **Stale approvals (R23, R26).** A ticked item deploys only if it is the newest verdict of its container (a later
  hold supersedes it), WUD still offers the reviewed digest, and it is not already running.
- **Approval flow (R24).** Validate before consuming, then background sequential deploys and an immediate "deploying N"
  page; results arrive by email. Unknown batch returns 403 (does not reveal existence). JWKS refetch on an unknown
  key at most once a minute.
- **Loki is a warning (R20).** An unavailable Loki gives "verified with warnings", not a failure.
- **Shutdown (R27).** Wait 5 s for in-flight deploys (inside Docker's 10 s stop timeout). An interrupted trigger whose
  running digest is unchanged is "not deployed (interrupted by shutdown)", without PBS restore advice.
- **PyJWT floor (R25).** `pyjwt>=2.10.1` for CVE-2024-53861.
- **No re-review after a digest mismatch.** After a digest mismatch, the running digest is not re-reviewed on the
  next run; the immediate mismatch email (with the latest PBS backup) is the signal.

## Final-review fixes (R30 to R33)
- **Token in the fragment (R30).** The link is `.../upgrades/batches/{id}#token=...`. Browsers never send a fragment,
  so no nginx, Traefik, uvicorn or Cloudflare log line, and nothing in Loki, can hold the token. GET serves a launcher
  whose one inline script (pinned by hash in the CSP) POSTs the token to `.../{id}/view`; that checks the link without
  writing and renders the review page, whose Approve form posts to `.../{id}` as before. Cost: approval needs
  JavaScript. The uvicorn access-log redaction stays as defence in depth for a hand-made `?token=` link.
- **Approver allowlist (R31).** `UPGRADE_ADVISOR_APPROVER_EMAILS` (comma-separated). A security review then put the
  JWT check before any config-state answer, so unauthenticated callers only ever see the generic denied page; a
  signed-in user sees "Approval is not configured on this server" when no approver is listed. Emails compare as NFKC
  plus casefold on both sides, and any non-ASCII address never matches (NFKC alone would fold a full-width address
  into ASCII and let a different identity match). The home-server repo must add the setting before the first approval.
- **Deploy row before the trigger (R32).** Each deploy is written as `triggering` with the pre-deploy digest before
  WUD is asked, then settled with a structured `trigger_state` (`triggered`, `trigger_error`, `interrupted`). This
  replaces R28's match on the detail string. After the security review: a `trigger_error` is verified after the
  normal delay (a deploy WUD finishes after our client timed out must be seen); a successful trigger always schedules
  verification even if its settle write failed; the reaper for rows left by a hard kill waits for twice the trigger
  timeout and handles each row on its own. Unchanged pre-deploy digest after an unconfirmed trigger means "not
  deployed", without restore advice.
- **Minors (R33), plus two from the final review.** The scheduler's lazy import sits inside its try; failures after
  the review loop abort with an email; `min_age` has 1 h of slack for cron jitter; error items show our own cleaned
  messages for WUD, registry and GitHub errors (class name only for anything else); `verify_pending` runs once at
  startup; the last-success time is persisted (`advisor_state`) and seeds the gauge at startup; a release lookup
  where nothing matched and a tag lookup failed is an error, not "unreleased"; test and doc guest ids are placeholders.


## Security model decision
Cloudflare Access here is one wildcard application, so the JWT proves only a signed-in user. The single-use token is
the endpoint-specific factor, hence hashing at rest, keeping it out of every URL (R30), and the approver allowlist
(R31) as a second check on who is signed in.

## Not done
Rollout (allowlist, WUD credentials and API access on the hosts, Access policy, `.env`) is a separate operator step
after merge.
