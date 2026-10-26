# Upgrade Advisor, Part B: The Advisor (sre-agent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily job in `sre-agent` that reviews pending third-party image updates, emails a
recommendation with one batch-approval link, deploys approved items through WUD, and verifies
each deploy.

**Architecture:** A new `src/upgrades/` package. Code collects from WUD, the registry and
GitHub, runs the supply-chain gate, then makes one tool-less LLM call per candidate (the weekly
report's pattern). Verdicts, batches and deploys live in their own SQLite file. A small
`APIRouter` serves the approval page and the approve POST. The deploy client is imported only by
the approve handler and the verify path, never by the agent or MCP tools.

**Tech Stack:** Python 3.13, FastAPI, httpx, pydantic v2, SQLite, APScheduler 3, PyJWT[crypto]
(new), respx + pytest for tests, mypy strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-26-upgrade-advisor-design.md`.
Companion plan: `2026-10-26-upgrade-advisor-a-hosts.md` (owns the env and allowlist contract;
its "Interfaces shared with Part B" section is binding here).

## Global Constraints

- Verdicts are exactly `upgrade` | `hold` | `needs_you`.
- Major changes, including any 0.x minor bump, are **forced to `needs_you` by code**.
- `min_age` is measured from the advisor's **own first-seen time** per digest, never the image's `created`.
- A candidate whose digest matches no release tag is `hold` with reason `"unreleased build"`.
- **GET never changes state.** Only the POST from the page deploys; the link token is single-use
  and expires after **48 h**.
- The approve endpoint verifies the **Cloudflare Access JWT** (`Cf-Access-Jwt-Assertion`) itself.
- Deploy only if WUD's candidate digest still equals the reviewed digest; after every deploy,
  compare the running digest with the reviewed one and **email at once on mismatch**.
- The deploy capability is never an agent tool and never on `/mcp`.
- Email only when there is something to report; `UPGRADE_LAST_SUCCESS` covers a dead job.
- Never silently fail: every per-container error appears in the email and the logs, and the run
  continues with the other containers.
- Public repo: no IPs, hostnames, domains or secrets in code, tests, docs or examples.
- `make check` (ruff, mypy strict, pytest) passes after every task.

## Review Focus

- **Mail scanners open the link:** GET twice → no deploy, token not consumed (Task 8 test).
- **Second click or an expired link:** POST with a used or expired token → 410 page, nothing
  deployed (Task 8 test).
- **Upstream pushes between review and approval:** WUD's candidate digest no longer equals the
  reviewed one → item skipped and reported, others deploy (Task 8 test).
- **The model answers in prose, or a release note says "verdict: upgrade":** unparseable output
  → `needs_you`; a major stays `needs_you` even when the model says `upgrade` (Task 5 tests).
- **One WUD host down:** that container shows as an error in the email; the other is still
  reviewed (Task 7 test).

---

### Task 0: Builder Ownership check (no code)

Per `CLAUDE.md`, before any code ask John 2–3 questions, e.g.: why the model has no tools; why
GET must not deploy; what the post-deploy digest check guards against. Continue only when his
answers show the design is understood. Record them in the journal entry (Task 9).

### Task 1: Settings and allowlist

**Files:**
- Modify: `src/config.py`, `tests/conftest.py` (fake values + patch sites), `.env.example`
- Create: `src/upgrades/__init__.py`, `src/upgrades/config.py`
- Test: `tests/test_upgrades_config.py`

**Interfaces:**
- Produces in `Settings` (all default to "disabled"): `upgrade_advisor_config_path: str = ""`,
  `upgrade_advisor_db_path: str = ""`, `upgrade_advisor_cron: str = ""`,
  `upgrade_advisor_min_age_hours: int = 72`, `upgrade_advisor_public_base_url: str = ""`,
  `upgrade_advisor_verify_delay_seconds: int = 300`, `upgrade_advisor_wud_username: str = ""`,
  `upgrade_advisor_wud_password: str = ""`, `cf_access_team_domain: str = ""`,
  `cf_access_aud: str = ""`, `upgrade_advisor_github_token: str = ""`.
- Produces in `src/upgrades/config.py`:
  `class AdvisedContainer(BaseModel)` with fields `name, host, wud_url, registry, repository,
  tag, github_repo, release_tag_format: str = "{release}", readiness_url, pbs_guest,
  context_files: list[str], auto_deploy: bool = False`;
  `load_allowlist(path: str) -> list[AdvisedContainer]`;
  `is_advisor_configured() -> bool` (config path, db path, WUD credentials and public base URL all set).

- [ ] **Step 1: Write the failing tests**

```python
def test_load_allowlist_parses_contract_example(tmp_path): ...
    # the YAML from Part A's contract → one AdvisedContainer, tag == "v3", auto_deploy is False
def test_load_allowlist_rejects_missing_fields(tmp_path): ...
    # entry without wud_url → pydantic ValidationError
def test_auto_deploy_true_is_rejected_in_phase_1(tmp_path): ...
    # auto_deploy: true → ValueError("auto_deploy is not supported yet")
def test_advisor_disabled_by_default(mock_settings): ...
    # is_advisor_configured() is False with the fixture's empty values
```

- [ ] **Step 2:** `uv run pytest tests/test_upgrades_config.py -v` → FAIL (module missing).
- [ ] **Step 3:** Implement the settings, model and loader (`yaml.safe_load`, top-level key
  `containers`). Add every new field to `mock_settings` (empty/defaults) and
  `src.upgrades.config.get_settings` (plus each later module's site) to `patch_sites`. Document
  each variable in `.env.example` with placeholder values only.
- [ ] **Step 4:** `make check` → PASS.
- [ ] **Step 5:** Commit `feat(upgrades): settings and allowlist loader`.

### Task 2: Store

**Files:**
- Create: `src/upgrades/store.py`, `src/upgrades/models.py`
- Test: `tests/test_upgrades_store.py`

**Interfaces:**
- Produces in `models.py`: `Verdict = Literal["upgrade", "hold", "needs_you"]`;
  `class VerdictRecord(BaseModel)`: `id: int, container: str, running_digest: str,
  candidate_digest: str, running_version: str | None, candidate_version: str | None,
  verdict: Verdict, summary: str, details: dict[str, object], created_at: datetime,
  batch_id: str | None`; `class DeployRecord(BaseModel)`: `id: int, verdict_id: int,
  started_at: datetime, outcome: Literal["pending_verify", "ok", "failed", "digest_mismatch", "skipped"],
  running_digest: str | None, detail: str`; `class Batch(BaseModel)`: `id: str,
  verdict_ids: list[int], created_at: datetime, expires_at: datetime, used_at: datetime | None`.
- Produces in `store.py` (each takes `conn: sqlite3.Connection` first; schema auto-created as in
  `src/memory/store.py`): `connect(path: str) -> sqlite3.Connection`;
  `first_seen(conn, container: str, digest: str, now: datetime) -> datetime` (inserts on first call,
  returns the stored time after);
  `save_verdict(conn, ...) -> int`; `create_batch(conn, verdict_ids: list[int], token: str,
  now: datetime, ttl: timedelta) -> str` (stores **sha256 of the token**, returns batch id from
  `secrets.token_urlsafe(16)`); `get_batch(conn, batch_id) -> Batch | None`;
  `consume_batch(conn, batch_id, token, now) -> Literal["ok", "invalid", "used", "expired"]`
  (atomic `UPDATE ... WHERE used_at IS NULL`); `record_decision(conn, verdict_id, approved: bool, now)`;
  `save_deploy(conn, ...) -> int`; `update_deploy(conn, deploy_id, outcome, running_digest, detail)`;
  `pending_verifications(conn) -> list[DeployRecord]`; `latest_verdict(conn, container, digest) -> VerdictRecord | None`.

- [ ] **Step 1: Write the failing tests** (in-memory DB):

```python
def test_first_seen_is_sticky(): ...        # second call 5 days later returns the first time
def test_batch_token_is_single_use(): ...   # consume → "ok", again → "used"
def test_batch_token_expires_after_ttl(): ...  # now + 49h with ttl 48h → "expired"
def test_wrong_token_is_invalid(): ...      # → "invalid"; batch still unused afterwards
def test_token_stored_hashed(): ...         # raw token string not present in the batches table
def test_pending_verifications_lists_only_pending(): ...
```

- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `make check` → PASS.
- [ ] **Step 5:** Commit `feat(upgrades): sqlite store for verdicts, batches and deploys`.

### Task 3: WUD client (detect and deploy)

**Files:**
- Create: `src/upgrades/wud.py`
- Test: `tests/test_upgrades_wud_integration.py` (respx)

**Interfaces:**
- Produces: `class WudContainer(BaseModel)`: `id: str, name: str, running_digest: str | None`
  (from `image.digest.value`), `candidate_digest: str | None` (from `result.digest`),
  `update_available: bool`; `class WudError(Exception)`;
  `async get_container(c: AdvisedContainer) -> WudContainer` (`GET {wud_url}/api/containers`,
  basic auth from settings, match on `name`; `WudError` if absent, on non-2xx, or on a network error);
  `async trigger_deploy(c: AdvisedContainer, wud_id: str) -> None`
  (`POST {wud_url}/api/containers/{wud_id}/triggers/dockercompose/deploy`, timeout 600 s, since
  WUD answers only after the deploy; `WudError` with WUD's `message` on non-2xx).

- [ ] **Step 1: Write the failing tests:** parses a WUD 9.6.0-shaped record (fixture JSON with
  `image.digest.value`, `result.digest`, `updateAvailable`); 401 → `WudError("...401...")`;
  container missing → `WudError`; trigger posts to the exact path with basic auth; trigger 500
  surfaces WUD's `message`.
- [ ] **Step 2–4:** fail → implement → `make check`.
- [ ] **Step 5:** Commit `feat(upgrades): WUD client`.

### Task 4: Registry digests and GitHub releases

**Files:**
- Create: `src/upgrades/registry.py`, `src/upgrades/releases.py`
- Test: `tests/test_upgrades_registry_integration.py`, `tests/test_upgrades_releases.py`,
  `tests/test_upgrades_releases_integration.py`

**Interfaces:**
- Produces in `registry.py`: `async tag_digests(registry: str, repository: str, tag: str) -> set[str]`:
  the index digest (`Docker-Content-Digest` of `GET /v2/<repo>/manifests/<tag>` with the
  OCI index + Docker manifest-list `Accept` headers) **plus** every platform manifest digest in
  the index. Anonymous auth via the `WWW-Authenticate: Bearer realm=...,service=...,scope=...`
  challenge on 401 (works for Docker Hub and ghcr).
- Produces in `releases.py`: `class Release(BaseModel)`: `tag: str, version: tuple[int, ...],
  published_at: datetime, body: str, url: str`;
  `parse_version(tag: str) -> tuple[int, ...] | None` (`v3.15.1` and `3.15.1` → `(3, 15, 1)`;
  anything with a prerelease suffix → `None`);
  `is_major(running: tuple[int, ...], candidate: tuple[int, ...]) -> bool`
  (major differs, or major == 0 and minor differs);
  `async list_releases(github_repo: str, limit: int = 30) -> list[Release]` (skips drafts,
  prereleases and unparseable tags; `Authorization` header only when `upgrade_advisor_github_token` is set);
  `async release_for_digest(c: AdvisedContainer, digest: str, releases: list[Release]) -> Release | None`
  (newest first; image tag = `c.release_tag_format.format(release=r.tag)`; match when `digest in
  await tag_digests(...)`; stop after 10 lookups);
  `releases_between(releases, running: tuple[int, ...], candidate: tuple[int, ...]) -> list[Release]`
  (exclusive, inclusive);
  `async regression_issues(github_repo: str, release_tag: str, since: datetime) -> list[dict[str, str]]`
  (search API `repo:<r> is:issue created:>=<date> "<version without v>"`, max 10, title + url).

- [ ] **Step 1: Write the failing tests:**

```python
def test_parse_version_accepts_v_prefix_and_rejects_rc(): ...
    # parse_version("v3.15.1") == (3, 15, 1); parse_version("v3.15.0-rc.1") is None
def test_is_major_treats_zero_minor_as_major(): ...
    # is_major((0, 11, 3), (0, 12, 4)) is True; is_major((3, 15, 0), (3, 15, 1)) is False;
    # is_major((2, 55, 0), (3, 0, 0)) is True
def test_releases_between_is_exclusive_inclusive(): ...
# integration (respx):
def test_tag_digests_follows_bearer_challenge_and_returns_platform_digests(): ...
def test_release_for_digest_matches_platform_manifest(): ...
def test_release_for_digest_returns_none_for_unreleased_build(): ...
def test_list_releases_skips_prereleases_and_drafts(): ...
```

- [ ] **Step 2–4:** fail → implement → `make check`.
- [ ] **Step 5:** Commit `feat(upgrades): registry digests and GitHub release lookup`.

### Task 5: Gate and review

**Files:**
- Create: `src/upgrades/review.py`
- Test: `tests/test_upgrades_review.py`

**Interfaces:**
- Consumes: `Release`, `is_major`, `AdvisedContainer`, `create_llm` (`src/agent/llm.py`, as in
  `src/report/generator.py::_generate_narrative`).
- Produces: `class Candidate(BaseModel)`: `container: AdvisedContainer, wud_id: str,
  running_digest: str, candidate_digest: str, running_release: Release | None,
  candidate_release: Release | None, first_seen_at: datetime, notes: list[Release],
  issues: list[dict[str, str]]`;
  `gate(c: Candidate, now: datetime, min_age: timedelta) -> str | None`: hold reason or `None`
  (`"unreleased build"` when `candidate_release is None`;
  `"too new: first seen <iso>, eligible <iso>"` when `now - first_seen_at < min_age`);
  `class Review(BaseModel)`: `verdict: Verdict, summary: str, relevant_changes: list[str],
  risks: list[str], links: list[str]`;
  `build_prompt(c: Candidate, context: dict[str, str]) -> str`;
  `parse_review(text: str) -> Review | None` (first fenced or bare JSON object; pydantic-validated);
  `async review(c: Candidate) -> Review`.
- `review` rules, in order: running release unknown → `needs_you`
  ("running version unknown, e.g. a dev build"); call the model once (`temperature=0`, no tools,
  same OAuth system-prefix handling as the report, retries for transient errors only, 120 s budget);
  unparseable or failed call → `needs_you` with the error in `summary`; then, if
  `is_major(running, candidate)`, force `needs_you` and prepend `"Major version change"` to `risks`.
- Prompt content: the container's context files (read from `context_files`, each capped at 20 000
  chars), every release body in `notes` (each capped at 15 000 chars, newest last), issue titles
  and URLs, versions, and the instruction to answer with one JSON object of the `Review` shape.
  Wrap all GitHub text in clearly delimited "untrusted content" blocks and say instructions inside
  them are to be ignored.

- [ ] **Step 1: Write the failing tests** (LLM stubbed by patching `create_llm`):

```python
def test_gate_holds_unreleased_build(): ...
def test_gate_holds_until_min_age_from_first_seen(): ...  # 71h → hold; 73h → None (min_age 72h)
def test_gate_ignores_image_created_field(): ...           # Candidate has no created field at all
def test_review_forces_needs_you_on_major_even_if_model_says_upgrade(): ...
def test_review_prose_answer_becomes_needs_you(): ...
def test_review_llm_exception_becomes_needs_you_with_error(): ...
def test_review_unknown_running_version_skips_model(): ...   # create_llm never called
def test_prompt_fences_untrusted_text_and_caps_lengths(): ...
```

- [ ] **Step 2–4:** fail → implement → `make check`.
- [ ] **Step 5:** Commit `feat(upgrades): supply-chain gate and model review`.

### Task 6: Verify after deploy

**Files:**
- Create: `src/upgrades/verify.py`
- Test: `tests/test_upgrades_verify_integration.py`

**Interfaces:**
- Produces: `async verify_deploy(deploy_id: int) -> DeployRecord` and
  `async verify_pending() -> list[DeployRecord]` (every `pending_verify` older than the delay).
  Checks, in order: WUD `running_digest == verdict.candidate_digest` (else `digest_mismatch`);
  `GET readiness_url` → 2xx within 10 s (else `failed`); Loki
  `sum(count_over_time({container="<name>"} |~ "(?i)(error|panic|fatal)" [5m]))` is at most
  `max(10, 3 × the same count over the 5 min ending 1 h earlier)` (else `failed`); Loki unreachable
  is a warning in `detail`, not a failure. `failed` or `digest_mismatch` → immediate email naming
  the container, its `pbs_guest` and "restore from the latest PBS backup" (use `pbs` tooling if
  configured to name the latest snapshot time).

- [ ] **Step 1: Write the failing tests:**

```python
async def test_verify_ok_when_digest_ready_and_quiet(): ...
async def test_digest_mismatch_emails_at_once(): ...
async def test_readiness_failure_emails_with_pbs_guest(): ...
async def test_loki_down_is_not_a_failure(): ...
async def test_verify_pending_picks_up_deploys_lost_to_a_restart(): ...
```

- [ ] **Step 2–4:** fail → implement → `make check`.
- [ ] **Step 5:** Commit `feat(upgrades): post-deploy verification`.

### Task 7: Daily job, email, scheduler, metrics, CLI

**Files:**
- Create: `src/upgrades/job.py`, `src/upgrades/email.py`, `src/upgrades/__main__.py`
- Modify: `src/report/scheduler.py`, `src/observability/metrics.py`, `makefile` (`advisor` target)
- Test: `tests/test_upgrades_job.py`, `tests/test_upgrades_email.py`

**Interfaces:**
- Produces in `metrics.py` (exported names in brackets): `UPGRADE_RUNS_TOTAL` Counter
  [`sre_assistant_upgrade_runs_total`, label `status`: success|error], `UPGRADE_VERDICTS_TOTAL`
  Counter [`sre_assistant_upgrade_verdicts_total`, label `verdict`], `UPGRADE_DEPLOYS_TOTAL`
  Counter [`sre_assistant_upgrade_deploys_total`, label `outcome`], `UPGRADE_LAST_SUCCESS` Gauge
  [`sre_assistant_upgrade_last_success_timestamp`, unix ts, set at the end of every completed
  run, email or not]. Task 6 increments `UPGRADE_DEPLOYS_TOTAL` once it exists (add it there if
  this task has not run yet).
- Produces in `job.py`: `class ItemResult(BaseModel)`: `container: str, verdict: Verdict | None,
  review: Review | None, hold_reason: str | None, error: str | None, verdict_id: int | None`;
  `class RunSummary(BaseModel)`: `items: list[ItemResult], batch_id: str | None,
  approve_url: str | None, outcomes: list[DeployRecord]`;
  `async run_advisor(now: datetime | None = None, send_email: bool = True) -> RunSummary`.
  Order per container: `get_container` → no candidate (`not update_available` or digests equal) →
  skip; `first_seen`; releases + `release_for_digest` for running and candidate; `gate`; `review`;
  `save_verdict`. Any exception becomes `ItemResult.error` and the loop continues. Also re-run
  `verify_pending()` (Task 6) first, and include yesterday's deploy outcomes. Create one batch
  for all `upgrade` and `needs_you` items with a fresh `secrets.token_urlsafe(32)` token; approve
  URL = `f"{public_base_url}/upgrades/batches/{batch_id}?token={token}"`.
  Send email only if any item is not skipped or any outcome exists.
- Produces in `email.py`: `render(summary: RunSummary) -> tuple[str, str, str]` (subject,
  markdown, html), reusing the report's inline-CSS helpers where they fit; subject
  `"Upgrades: N recommended, M need you"`. Holds list their reason; errors their message.
  Sent with `send_report_email(markdown, html, subject)`.
- Scheduler: when `upgrade_advisor_cron` is set and `is_advisor_configured()`, add job
  `id="upgrade_advisor"` with `CronTrigger.from_crontab(cron, timezone=settings.user_timezone)`;
  the existing report job is unchanged and the scheduler starts if either cron is set.
- CLI: `uv run python -m src.upgrades --dry-run` prints the markdown and sends nothing;
  `make advisor` runs it.

- [ ] **Step 1: Write the failing tests** (`wud`, `releases`, `review` patched at module level):

```python
async def test_nothing_pending_sends_no_email_but_sets_last_success(): ...
async def test_one_wud_host_down_reports_error_and_reviews_the_other(): ...
async def test_batch_contains_upgrade_and_needs_you_but_not_hold(): ...
async def test_approve_url_uses_public_base_and_token(): ...
def test_email_subject_counts_and_lists_hold_reasons(): ...
def test_scheduler_registers_advisor_job_only_when_configured(): ...
```

- [ ] **Step 2–4:** fail → implement → `make check`.
- [ ] **Step 5:** Commit `feat(upgrades): daily advisor job, email and scheduler`.

### Task 8: Approval page and deploy

**Files:**
- Create: `src/upgrades/access.py`, `src/upgrades/deploy.py`, `src/api/upgrades.py`
- Modify: `src/api/main.py` (`app.include_router(upgrades_router)`), `pyproject.toml`
  (`pyjwt[crypto]>=2.9`, then `uv lock`)
- Test: `tests/test_upgrades_access.py`, `tests/test_upgrades_api_integration.py`

**Interfaces:**
- Produces in `access.py`: `class AccessDenied(Exception)`;
  `async verify_access_jwt(token: str | None) -> str` returns the email claim. RS256 against keys
  from `https://{cf_access_team_domain}/cdn-cgi/access/certs` (cached 1 h), `audience=cf_access_aud`,
  `issuer=f"https://{cf_access_team_domain}"`. Missing token, bad signature, wrong audience or
  expiry → `AccessDenied`.
- Produces in `deploy.py`: `async deploy_approved(batch_id: str, approved_ids: list[int]) -> list[DeployRecord]`.
  Per approved verdict: `get_container`; if `candidate_digest != verdict.candidate_digest` →
  outcome `skipped` ("upstream moved since review; re-reviewed next run"); else
  `trigger_deploy` → `pending_verify`, `WudError` → `failed`; schedule `verify_deploy(id)` after
  `upgrade_advisor_verify_delay_seconds` with `asyncio.create_task`. Records
  `record_decision(approved=False)` for every batch item not ticked.
- Routes (served behind the webapp's `/api/` prefix, so links and form actions are **relative**):
  - `GET /upgrades/batches/{batch_id}?token=...`: verify Access JWT (403 page on failure); batch
    unknown, used or expired → 410 page; else an HTML page: one row per item with a checkbox
    (`upgrade` ticked, `needs_you` unticked), summary, relevant changes, risks, links, and a form
    `method="post" action=""` carrying the token in a hidden field. **No state change.**
  - `POST /upgrades/batches/{batch_id}` (form: `token`, `verdict_id` repeated): verify JWT;
    `consume_batch` (`invalid` → 403, `used`/`expired` → 410); ids must belong to the batch
    (else 400); run `deploy_approved`; render the outcomes.
  - All HTML escaped (`html.escape`); `Cache-Control: no-store`.

- [ ] **Step 1: Write the failing tests** (JWT keys generated in-test; JWKS URL mocked with respx;
  WUD patched):

```python
async def test_jwt_valid_returns_email(): ...
async def test_jwt_wrong_audience_denied(): ...
async def test_get_without_access_header_is_403(): ...
async def test_get_twice_changes_nothing(): ...          # no trigger call; consume still "ok" after
async def test_post_deploys_ticked_items_only(): ...       # unticked recorded approved=False
async def test_post_second_time_is_410_and_deploys_nothing(): ...
async def test_post_expired_token_is_410(): ...
async def test_post_skips_item_when_upstream_moved(): ...
async def test_post_rejects_verdict_id_from_another_batch(): ...
async def test_page_escapes_model_text(): ...              # "<script>" in summary renders escaped
def test_deploy_is_not_an_agent_or_mcp_tool(): ...
    # no tool name in src/agent/tools, src/agent/mcp_tools.py or src/api/mcp_server.py
    # imports src.upgrades.deploy or src.upgrades.wud (static scan of the source text)
```

- [ ] **Step 2–4:** fail → implement → `make check`.
- [ ] **Step 5:** Commit `feat(upgrades): approval page and guarded deploy`.

### Task 9: Principle, docs and journal

**Files:**
- Modify: `CLAUDE.md` (Design Principles bullet, Source Layout `src/upgrades/`, Build Phases:
  "7. Upgrade Advisor"), `readme.md` (Build Order row), `docs/architecture.md` (new section),
  `docs/code-flow.md` (job and approve flows), `docs/dependencies.md` (PyJWT, WUD, GitHub API),
  spec status line → "implemented"
- Create: `journal/261026-upgrade-advisor.md` (create `journal/` if missing)

- [ ] **Step 1:** Replace the "Advisory only" bullet with, verbatim:
  "**Advisory by default.** The only automated action is a container upgrade through the Upgrade
  Advisor's approval path (and, once enabled per container, its allowlisted auto path). The deploy
  capability is never an agent tool or an MCP tool."
- [ ] **Step 2:** Write the docs sections (what runs when, the verdict rules, the approval
  security model, the env variables) and the journal entry (decisions, Task 0 answers).
- [ ] **Step 3:** `make check` → PASS. Commit `docs: Upgrade Advisor; amend the advisory principle`.

### Task 10: Rollout (operator, after Part A Tasks 1–3)

- [ ] **Step 1:** Push to `main`; CI publishes the image. Run Part A Task 4 (wiring).
- [ ] **Step 2:** In Cloudflare Zero Trust, read the AUD tag of the Access application covering
  the SRE hostname; put it and the team domain into the vault (Part A Task 4).
- [ ] **Step 3: Dry run:** `ssh infra 'docker exec sre-agent python -m src.upgrades --dry-run'`
  → markdown lists both containers (nothing pending, or a hold/recommendation with reasons).
- [ ] **Step 4: Grafana** (alerting lives in Grafana's DB; writes need the vault admin login,
  `documentation/grafana-alerting.md` in home-server):
  - New rule "Upgrade advisor not running": `time() - sre_assistant_upgrade_last_success_timestamp > 93600`
    (26 h), for 30 m, routed like the other rules.
  - Rule `ffrx10nu94ao0b` ("Container image stale"): add `container_name!~"prometheus|open-webui"`
    to every selector in its query. Verify with an instant query that those two series are gone.
- [ ] **Step 5: End-to-end:** wait for (or force with a lowered `UPGRADE_ADVISOR_MIN_AGE_HOURS`)
  a real recommendation; open the link on the phone, approve, confirm the deploy email and that
  `ssh prometheus 'docker inspect prometheus --format {{.Image}}'` changed.
