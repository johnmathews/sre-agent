# Upgrade Advisor — Design

**Date:** 2026-10-26
**Status:** implemented 2026-10-26 (see journal/261026-upgrade-advisor.md for deviations)
**Repos:** `sre-agent` (primary), `sre-webapp` (approval page, if built there), `home-server`
(Ansible: WUD on two more hosts)

## Why

The homelab's "Container image stale" alert pushes when a third-party image falls behind its
registry. Each push means the same chore: read the release notes, decide whether anything in them
matters for this setup, then pull and recreate the container. In practice the operator almost
always upgrades. The reading is the chore, and it needs judgement, which a fixed delay or a
version rule cannot give.

The Upgrade Advisor does the reading. Once a day it finds pending updates for a short allowlist
of third-party containers, asks Claude whether each one has breaking changes relevant to *this*
configuration, a known regression, or a security problem, and emails a recommendation. One link
in the email approves any number of them.

John's own images are out of scope: What's Up Docker (WUD) already auto-deploys them within a
minute of a gated CI publish, and that stays as it is.

## Goals

- Turn "read release notes, decide, pull, recreate" into "read one email, click Approve".
- Judge each update against the actual compose file and config, not in general.
- Collect evidence (agent verdict vs operator decision) to decide later whether the advisor may
  act without approval (Phase 2).

## Non-goals

- John's own images (WUD auto-deploy covers them).
- Major-version upgrades done automatically. They are always surfaced as "needs you".
- Rollback. Every guest has a daily PBS backup; a failed upgrade is reported with the backup to
  restore from, and the restore stays manual.
- Retuning every image-freshness alert. Only allowlisted containers leave the stale-image rule
  (see §8); a wider alert review is a separate piece of work.
- Detecting a compromised image by reading text (see §5: that is code's job, and only partly
  possible).

## Anti-goals (would make this a failure even if it works)

- A crafted release note, GitHub issue or log line that can cause a deploy.
- A link that deploys just by being opened. Mail security scanners open links.
- Deploying a digest that was never reviewed without the operator hearing about it at once
  (see §6 for the accepted race).
- A daily email that says nothing. Silence must mean "nothing pending", and a dead job must be
  visible some other way.
- The advisor drifting the Ansible compose templates (it must never change an `image:` line).

## Design principle amendment (decided)

`CLAUDE.md` lists **"Advisory only. The assistant advises but never takes automated remediation
actions."** Phase 1 deploys after an explicit human approval and Phase 2 without one, so the
operator chose to amend the principle rather than move deploying into a separate service. The
implementation changes it to:

> **Advisory by default.** The only automated action is a container upgrade through the Upgrade
> Advisor's approval path (and, once enabled per container, its allowlisted auto path). The deploy
> capability is never an agent tool or an MCP tool.

The guardrail that replaces process isolation is code structure: the deploy client is callable
only from the approval handler and the scheduled job (§6).

## Phases

- **Phase 1 (this design):** daily review, recommendation email, one-click batch approval.
- **Phase 2 (later, operator's decision):** per-container `auto_deploy: true` lets "upgrade"
  verdicts deploy without approval, under the same checks, and the email reports afterwards.
  Built only after Phase 1's verdict history shows the agent agrees with the operator.

## 1. Scope (v1 allowlist)

```
+-------------+-------------------+--------------------+-----------------------------------------+
| Container   | Host              | Tag to watch       | Notes                                   |
+-------------+-------------------+--------------------+-----------------------------------------+
| prometheus  | Prometheus LXC    | prom/prometheus:v3 | Today `latest`, which can jump a major. |
|             |                   |                    | `v3` is published upstream and currently|
|             |                   |                    | the same digest, so the switch is no-op.|
| open-webui  | Open WebUI LXC    | open-webui:latest  | Today `main` (dev builds; running a     |
|             |                   |   (release builds) | 0.11.3 build from 2026-09-06). Latest   |
|             |                   |                    | release is v0.12.4, so the switch is an |
|             |                   |                    | upgrade: a 0.x minor, i.e. "needs you". |
+-------------+-------------------+--------------------+-----------------------------------------+
```

Deliberately not in v1: Portainer (server and agents must move together; agents are pinned in
Ansible), the OneCLI Postgres on the agent host (its compose file belongs to OneCLI's installer,
not to Ansible), `journal-chromadb` (John's own image; excluded from WUD for start-order and
one-way data-format reasons).

The allowlist is configuration, not code: per container its host's WUD endpoint, WUD container
name, GitHub repo for release notes, the files to show the model (the compose template and any
config it reads), and `auto_deploy` (always false in Phase 1). This repo is public, so the
allowlist is rendered by Ansible on the infra VM and mounted or passed as env, never committed
here.

**The Open WebUI tag switch happens outside the advisor, once, before go-live.** WUD only watches
the tag a container runs, so while it runs `:main` the advisor can never offer `latest`. The
switch (0.11.3 dev build → v0.12.4) is a manual pull plus `make open-webui` after the operator
reads the 0.11 → 0.12 release notes, and WUD is added to that host only after it lands.

## 2. Moving parts

```
 WUD on each host            sre-agent (infra VM)                         operator
 (detects, deploys)          ------------------------------------
                             daily job (APScheduler)
  GET /api/containers  <---  1. collect pending updates (code)
                             2. supply-chain checks (code)
                             3. one Claude call per candidate (no tools)
                             4. store verdicts, build batch, send email  ---> email
                                                                               |
                             approval page (behind Cloudflare Access)  <------ click
                             5. on POST: re-check digest, then fire  ------>
  POST .../triggers/... <--- 6. verify after deploy, email the outcome  ---> email
```

**Code collects and acts; the model only judges.** This is the weekly report's existing pattern
(direct API queries, one LLM call), reused.

## 3. Detection: WUD in "watch, do not deploy" mode

- Add WUD (same pinned version as the other hosts) to the Prometheus and Open WebUI LXCs, through
  their Ansible roles, following `documentation/wud.md` in the home-server repo.
- Watched services get `wud.watch`, `wud.watch.digest` and a `dockercompose` deploy trigger with
  **`AUTO=false`**: WUD detects but never deploys on its own.
- Digest watching on a moving tag only. WUD then never rewrites the `image:` line, so the Ansible
  template stays the source of truth and a later converge does not undo an upgrade.
- WUD's API must be reachable from the infra VM and nothing else (today it is unpublished).
  Publish it on a LAN port and restrict the source to the infra VM. Port 3000 is taken on the
  Open WebUI host.
- The advisor authenticates with a WUD user holding the **`rw`** role (WUD 9.x roles:
  `admin`/`rw`/`ro`; users live in WUD's DB, the env-var seeding is deprecated). Credentials in
  Ansible Vault, passed to `sre-agent` as env.

## 4. The daily job

1. **Collect.** For each allowlisted container, read WUD's container record: running digest,
   candidate digest, candidate `created`. No candidate → nothing to do.
2. **Supply-chain checks** (code, §5). A failed check is a "hold" with the reason; no LLM call.
3. **Review.** One Claude call per candidate, no tools. Input:
   - release notes for every release between the running version and the candidate (GitHub
     releases API),
   - GitHub issues opened since the candidate's release that mention its version (regressions),
   - the container's compose template and config files from the allowlist,
   - running and candidate versions, and whether the change is major (for 0.x, a minor bump is
     major).

   Output, schema-validated: `verdict` (`upgrade` | `hold` | `needs_you`), `relevant_changes`
   (what touches this config), `risks`, `links`. Majors are forced to `needs_you` by code,
   whatever the model says. An invalid or failed response is `needs_you` with the error, never
   dropped (Never silently fail).
4. **Store** every verdict with the reviewed digest (memory store or a new table).
5. **Email** only when there is something to report: pending items or outcomes. Content:
   upgrades recommended (one approval link for all), holds with reasons, needs-you items with the
   model's analysis, and yesterday's deploy outcomes.

All text from GitHub is untrusted. The model has no tools, so at worst a crafted note skews a
verdict; in Phase 1 the operator still approves, and §5's checks do not depend on the model.

## 5. Supply-chain checks (code, never the model)

- **Released build:** the candidate digest must match a release tag (e.g. `v3` → the digest of
  `v3.15.1`). A digest with no matching release tag is "hold: unreleased build".
- **Age, from our own clock:** the advisor records when it first saw each digest and requires
  `min_age` (configurable) before recommending. It does not trust the image's `created` field,
  which the publisher sets and an attacker could backdate. Known trade-off: an image republished
  more often than `min_age` never qualifies; it shows up as a hold, not silently.
- Signature or attestation verification (cosign) where upstream signs: left as a v2 item.

What the checks cannot catch: a compromised release that is tagged, aged and has normal-looking
notes. The delay helps only where the compromise is found and pulled within `min_age`.

## 6. Approval (Phase 1)

- The email carries **one link** to an approval page listing the batch: each `upgrade` item
  pre-ticked, `needs_you` items shown unticked with their analysis. One **Approve** button deploys
  every ticked item. The operator can untick any first.
- **Opening the link changes nothing.** GET renders the page; only the POST from the button acts.
- The link carries a batch id and a signed, single-use token that expires (suggest 48 h).
- The approve endpoint verifies the **Cloudflare Access JWT** itself (signature and identity). The
  `sre-agent` API has no auth of its own and its port is reachable on the LAN, so relying on
  Cloudflare Access at the edge is not enough for an endpoint that deploys.
- On POST, per ticked item: re-read WUD's candidate digest; fire WUD's
  `POST /api/containers/:id/triggers/dockercompose/<name>` **only if it still equals the reviewed
  digest**. Otherwise skip it and re-review on the next run.
- **Accepted race (operator decision).** WUD pulls by tag, not digest, so a publish landing in
  the seconds between the re-check and WUD's pull deploys an unreviewed image. Closing it would
  mean pulling by digest (rewrites `image:`, drifts Ansible) or acting over SSH; neither is worth
  it. Instead, after every deploy compare the running digest with the reviewed one. A mismatch
  emails the operator at once and the running digest is reviewed on the next run.
- The deploy capability is **not** exposed as an agent tool or on `/mcp`. Only the approval
  handler (and, in Phase 2, the scheduled job) can call it.
- Unticked items are recorded as operator disagreement: that is Phase 2's evidence.

## 7. Verify after deploy

A few minutes after each deploy: container running and healthy (WUD/Docker), the service answers
its own readiness endpoint, no burst of errors in Loki against its baseline. For Prometheus, do
not rely on Prometheus to judge itself; use its `/-/ready` and the container state. A failure
emails at once, naming the guest and its latest PBS backup; success goes into the next digest.

## 8. Alerts and observability

- Exclude allowlisted containers from the Grafana "Container image stale" rule once the advisor
  covers them (the rule lives in Grafana's DB, not Ansible).
- Metrics, following the report's pattern: runs by status, verdicts by type, deploys by outcome,
  and a last-success timestamp. A Grafana rule on that timestamp catches a dead daily job, since
  an empty day sends no email.
- Schedule is a cron setting, daily during Phase 1, to be relaxed later without a code change.

## Decided by the operator

- Recommendation email first; acting alone only after testing (Phase 2).
- Approval by a link in the email; one click approves several.
- Lives in the SRE agent.
- Daily for now, less often later.
- Third-party images; own images stay on WUD auto-deploy.
- Amend the "Advisory only" principle (wording above); one component, in `sre-agent`.
- Accept the seconds-wide tag-pull race, with a post-deploy digest check that emails on mismatch.

## Calls made in this design (check these)

- v1 allowlist is Prometheus and Open WebUI only.
- WUD as detector and deployer (rather than SSH from the SRE agent): narrowest lever; it can only
  deploy what WUD itself found, for allowlisted containers.
- Prometheus moves to `v3`; Open WebUI moves to `latest`.
- `min_age` from the advisor's own first-seen clock, not the image's `created`.
- Majors (including 0.x minors) always `needs_you`.
- No email on empty days; a freshness alert covers a dead job.
- The Open WebUI `main` → `latest` switch is a one-off manual step before go-live, not an
  advisor review.
- The allowlist comes from Ansible, not from this public repo.

## Left to the builder

- Where the approval page lives (`sre-webapp` vs a minimal page served by `sre-agent`).
- Storage for first-seen times, verdicts and batches (memory store vs new table).
- Model choice for the review call, prompt wording, and the default `min_age`.
- How Ansible creates the WUD `rw` user (WUD API with the bootstrap admin is the likely route).
- Email layout (reuse the report's HTML style).

