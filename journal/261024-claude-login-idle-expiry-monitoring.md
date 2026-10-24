# Claude login: idle expiry, background refresh, credentials gauge (261024)

## What broke

At 06:32 CEST the webapp answered every question with `llm_auth_failed`. The
refresh endpoint returned `400 invalid_grant`, `"Refresh token expired"`.

- Last successful refresh: 2026-08-23 07:02 CEST. The access token lapsed at
  15:02 CEST that day.
- No LLM call between then and today. The daily ~07:00 CEST status questions
  stop after 2026-08-23, and their source is outside this repo.
- Refresh ran only before a query, so the refresh token sat unspent for two
  months and expired.
- `/health` reported `oauth_token` healthy throughout: "access token expired
  …h ago, will refresh on next call". It cannot know a refresh token is
  dead until something spends it.

Recovery was `claude login` on infra, then a container restart. Afterwards
`sre_assistant_oauth_token_remaining_seconds` read ~8.0h.

This is the third Claude-login outage here (260621, 260722, now). Both earlier
entries specced an alert. Neither was ever created in Grafana, so the
user's failed question was the first signal each time. Library Ask hit the
same idle expiry on 2026-10-22 (library PR #186), and this change follows its
design.

## Change

- `oauth_refresh.keep_fresh(interval_s)`: runs `ensure_valid_token()` every
  `CLAUDE_CREDENTIALS_CHECK_INTERVAL_S` (default 900 s) from the FastAPI
  lifespan, on the anthropic path. A dead refresh token now flips health within
  one access-token lifetime, without a question. Each iteration is guarded so
  one bad read cannot kill the loop. It must run in the API process, because
  the rejection flag is process memory.
- `sre_assistant_claude_credentials_healthy`: 1/0 gauge computed on scrape by
  `credentials_healthy()` from `get_token_health()`, the same check as
  `/health`. Registered lazily and only on the anthropic path: an unlabelled
  gauge exports 0 at import, which on OpenAI would read as "re-login needed".
- The existing `sre_assistant_component_healthy{component="oauth_token"}` was
  not reused. It only updates when `/health` is called, so between calls it is
  stale.

## Decisions

- **Alert on the gauge, not on `oauth_token_remaining_seconds < 0`.** Access
  token expiry is routine every ~8h and self-heals. Only a dead refresh token
  needs a human.
- `degraded` (valid access token, no refresh token) counts as 0: it needs a
  human before the access token runs out.
- No credentials file reports 1, matching `/health`'s "not using OAuth".

## Grafana side

The alert rule and a Logins row on the Home Server dashboard (SRE agent,
Library Ask, Seerr → Jellyfin) live in Grafana's DB and are recorded in the
home-server repo: `documentation/grafana-alerting.md` and its 261024 journal
entry.
