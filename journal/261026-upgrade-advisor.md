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
  unknown-running-version cases and the model-less path for an unreleased build follow the same fail-closed idea.
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

## Security model decision
Cloudflare Access here is one wildcard application, so the JWT proves only a signed-in user. The single-use token is
the endpoint-specific factor, hence hashing at rest and log redaction.

## Not done
Rollout (allowlist, WUD credentials and API access on the hosts, Access policy, `.env`) is a separate operator step
after merge.
