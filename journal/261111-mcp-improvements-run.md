# MCP improvements run: 37 units in seven PRs (W1–W37)

> Purpose: record what the 2026-11-11 engineering-team run changed, what was seen live after each deploy, what went
> wrong on the way, and what it left undone. The owner's question was whether the logs show the MCP server performing
> well and serving the right tools. The run's answer: before it, they could not. Unit detail lives in the PR bodies
> and the four earlier entries for this day.

## 1. What shipped

Seven PRs, each squash-merged once `check` passed, smoke-tested and deployed by WUD, then checked through the
deployed MCP tools:

| PR | Units | What it changed |
| --- | --- | --- |
| #43 | W1–W3 | Dependency caps, an image smoke test before push, a coverage floor, hooks that work in worktrees |
| #44 | W4–W7 | A real-HTTP `/mcp` test, each log line once, shared tool helpers, `truenas_snapshots` that answers (F1) |
| #45 | W8–W14 | One tool registry for `/mcp` and the agent, one `tool_call` line and metric per call, the Access JWT check (`log` mode), SLIs that can fail, a loud memory-store-off |
| #46 | W15–W21 | Grafana, Prometheus, Loki, Proxmox, PBS and runbook-chunk fixes |
| #47 | W22–W26 | New tools `container_status`, `scrape_targets`, `upgrade_status`, `service_health`; server instructions |
| #48 | W27–W32 | SDK retry errors, report cron in `USER_TIMEZONE`, session-ID validation, an untrusted-data prompt rule, LAN addresses scrubbed with a guard test, an e2e tier; plus two fixes from the live checks (below) |
| #49 | W33–W36 | ADR log (0001–0005), `docs/mcp-clients.md`, the readme MCP-first, design docs, a docs-parity test |

W37 was the live verification after every deploy. Its full log stays in the run directory (`w37-log.md`).

## 2. What was seen live (confirmed)

- After each deploy, `max(process_start_time_seconds{job="sre-agent"})` passed the merge time within 199–208 s, and
  `/mcp` returned Cloudflare 502 for about 20 s during the swap.
- The full pass on the final build (`79b1cde`, 21:43Z) made 11 calls, every one `outcome=success`, and Loki shows one
  `tool_call` line each.
  - `truenas_snapshots`: 1294 / 711 / 1475 snapshots per pool and both tasks `FINISHED`, in 3969 ms. Before W7 it
    reported 0 or timed out.
  - `sre_assistant_mcp_http_requests_total` reads `{access="unconfigured", client="unknown"} 11`, as designed until
    home-server#422 sets the Access AUD.
- Startup logs `MCP: 32 tools registered` (36 with every integration on, memory off here) and two expected warnings:
  the access check is inactive and the memory store is disabled.
- `upgrade_status` reads the advisor's verdicts. The suspected read-only WAL failure did not happen.
- The weekly report now logs `cron: 0 20 * * 1 (Europe/Amsterdam)`. Before W29 the same cron ran in UTC (21:00 CET).

## 3. What went wrong on the way

- **The runbook re-ingest emptied production `runbook_search` (confirmed).**
  - The owner ran `sre-ingest` on infra for W21. It cleared the vector store, then failed its settings check:
    `ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic`. The `sre-ingest` service never gets that key
    (home-server#428).
  - A retry with a placeholder key rebuilt 278 chunks. The running `sre-agent` still answered "No relevant runbook
    content found" until the PR 6 deploy restarted it, because chromadb keeps its client per path in the process.
  - Fix in #48: `scripts/ingest_runbooks.py` loads the runbooks and makes one embedding call before clearing. The docs
    now say to restart `sre-agent` after an ingest.
  - The lead had asked for the re-ingest without first reading how `sre-ingest` is configured on infra. That check
    would have caught the missing key.
- **`service_health` called healthy services DEGRADED (confirmed).**
  - All seven active alerts were image-update notices, and none of the 31 Grafana rules sets a `severity` label,
    outages included.
  - A first fix treated a missing severity as informational. Reading the rules live showed that would silence
    "NanoClaw down" too, so it was narrowed: only an explicit `info` or `none` is informational. The live verdict waits
    on home-server#429.
- **GitHub Actions had a major outage.** PR 6's CI sat queued for about 40 minutes. Nothing merged without `check`.
- **The independent reviewer caught real defects in every PR it saw.**
  - PR 4: PromQL quoting gave HTTP 400 on any `.` or `-`.
  - PR 5: the Gatus key match.
  - PR 6: e2e inputs not read from `.env`, the domain guard missing escaped forms, recovery session IDs over 64
    characters.
  - PR 7: five doc claims the code did not back.
  - Each was settled with a failing test or a live call before it was fixed.

## 4. Decisions

- Every brief banned TrueNAS calls, ssh and login calls by name, after a planning agent revoked the production
  TrueNAS key. All live checks went through the read-only `mcp__sre-agent__*` tools.
- Each PR got its own branch in one worktree, because the auto-mode classifier blocks `git reset --hard` and force
  pushes.
- The coverage floor stays at 91: the final total is 92.89%, so floor(total) − 1 = 91.
- `CLAUDE.md` gains a `## Shipping` section, confirmed by the owner in this wrap-up.

## 5. What is deliberately not done

- No home-server changes (owner decision). Its items are issues: #421, #422, #423, #424, #425, #426, #428, #429.
- The Access check stays in `log` mode until home-server#422 sets the AUD. Enforcing it now would refuse every client.
- The memory store stays off (ADR 0005).
- The evals were not run: they cost money, and sre-agent#51 shows the runner cannot run a case anyway.
- Owner-only checks not done:
  - connect Claude Code from `docs/mcp-clients.md` alone with a fresh service token;
  - run `tests/test_e2e_live.py` with a token.
- Not observed live: `loki_correlate_changes`' "showing newest N of M" header. The live windows never hit the
  200-line cap (supported by unit tests only).
- Follow-ups filed: sre-agent#50 (flaky test, investigation), #51 (eval runner), #52 (five Low defects), #53 (three
  clean-ups).
