# Incident explainer

> Purpose
>
> How a resolved Gatus outage turns into a second Pushover push that says why the service went down and how it came
> back. Covers the webhook contract, the incident store and its state machine, the background job, the verdict, the
> push, restart recovery, the dry-run command, the settings and the metrics.

## 1. Flow

1. **Gatus posts every alert transition.** Each alerting Gatus endpoint has a second alert of type `custom` with
   `send-on-resolved: true`. On TRIGGERED and on RESOLVED, Gatus posts a small JSON body to
   `POST /incidents/gatus` with a bearer token. Gatus's own "down" and "back up" pushes are unchanged; the
   explanation is a separate push from its own Pushover app.
2. **The receiver records the event and answers 202 at once.** TRIGGERED opens an incident row (or reuses the open
   one). RESOLVED closes it. A newly resolved incident schedules `explain(incident_id)` as a background task. A
   duplicate RESOLVED schedules nothing.
3. **The job waits, claims, then collects evidence.** `src/incidents/job.py:explain` waits
   `INCIDENT_SETTLE_SECONDS` so the last log lines and metric samples land, then claims the incident. Only the one
   caller whose claim succeeds goes on, so each incident gets at most one push; any other caller logs "already
   claimed" and stops. The job then builds an `EvidenceBundle` (`src/incidents/evidence.py`, see
   [section 5](#5-evidence-sources)) over the window from 10 minutes before the first failing Gatus result to the
   resolve. A source that fails is marked `unavailable` with the reason and never stops the job.
4. **Sonnet gives a verdict with no tools.** `src/incidents/verdict.py:judge` sends the bundle to `INCIDENT_MODEL`
   through the shared no-tools call `src/agent/llm.py:ask_text` and validates the JSON answer
   ([section 6](#6-verdict)).
5. **Code composes and sends the push.** `src/incidents/message.py` composes the title, body and link
   ([section 7](#7-the-push)); `src/incidents/pushover.py` sends it ([section 8](#8-pushover)). When the model fails
   (error, timeout or an answer that does not parse), the push still goes out with plain facts from the bundle and
   `Summary unavailable: <reason>`.
6. **The outcome is recorded and counted.** The incident moves to `explained` or `failed` with an outcome code and a
   detail line:

   | Outcome     | State       | When                                                    | Detail, for example                       |
   | ----------- | ----------- | ------------------------------------------------------- | ----------------------------------------- |
   | `explained` | `explained` | the verdict push was sent                               | `sent (request 5f2c…)`                    |
   | `fallback`  | `explained` | the model failed, then the fallback push was sent       | `fallback (<reason>); sent (request …)`   |
   | `not_sent`  | `failed`    | Pushover failed or is not configured                    | `not sent: Pushover not configured`       |
   | `error`     | `failed`    | an unexpected exception (logged with its traceback)     | `KeyError`                                |

   Restart recovery also records `error` and `not_sent` ([section 9](#9-restart-recovery)).

   Every SQLite call runs in a worker thread (`asyncio.to_thread`). The job never raises: an unexpected exception
   after the claim is logged with the traceback, recorded as `error` (best effort) and counted.

## 2. Endpoint contract

`POST /incidents/gatus`, router in `src/api/incidents.py`, mounted in `src/api/main.py`.

**Request.** Header `Authorization: Bearer <INCIDENT_WEBHOOK_TOKEN>`. Body (JSON, at most 16 KiB; each field at most
512 characters; unknown fields ignored):

```json
{"event": "RESOLVED", "group": "monitoring", "name": "Dozzle", "url": "http://dozzle.example.test",
 "description": "host=infra;container=dozzle"}
```

`event` is `TRIGGERED` or `RESOLVED`. `group` and `name` are required; `url` and `description` default to empty.
`description` is `host=<h>;container=<c>`: either part may be missing or empty, keys are case-insensitive, and unknown
keys are ignored (`src/incidents/gatus.py:parse_target`). The body carries only Ansible-controlled values: Gatus's
`[RESULT_ERRORS]` placeholder is not JSON-safe, so the job reads the failing results from the Gatus API instead.

**Endpoint key.** The store and the Gatus API address an endpoint by the key Gatus derives from group and name
(`src/incidents/gatus.py:endpoint_key`, a port of Gatus v5.37.0 `config/key/key.go`): lower-case, trim surrounding
whitespace, replace each of `/ _ . , space # + &` with `-`, then join group and name with `_`. Dashes are not
collapsed and parentheses are kept. Examples: `monitoring` / `Dozzle` gives `monitoring_dozzle`; `logging` /
`Alloy - Infra` gives `logging_alloy---infra`; `dns` / `AdGuard DNS (LAN 192.0.2.11)` gives
`dns_adguard-dns-(lan-192-0-2-11)`.

**Responses**, in the order they are checked:

| Code | Body                                              | When                                                    |
| ---- | ------------------------------------------------- | ------------------------------------------------------- |
| 503  | `{"detail": "incident explainer not configured"}` | `INCIDENT_WEBHOOK_TOKEN` or `INCIDENT_DB_PATH` is empty |
| 401  | `{"detail": "unauthorized"}`                      | missing header, other scheme, or wrong token            |
| 413  | `{"detail": "body over 16384 bytes"}`             | body larger than 16 KiB                                 |
| 422  | `{"detail": [{"loc", "msg", "type"}, ...]}`       | not JSON, unknown event, missing or oversized field     |
| 500  | `{"detail": "incident store error"}`              | the SQLite store failed (logged with the traceback)     |
| 202  | `{"incident_id": 7, "state": "resolved"}`         | accepted, including a duplicate                         |

The configuration check comes before authentication on purpose: Gatus logs the response, so an operator who has not
set the token yet reads "not configured" rather than a misleading "unauthorized". An unconfigured endpoint accepts
nothing, so the order reveals nothing useful. The token is compared with `hmac.compare_digest` on UTF-8 bytes; the
scheme `Bearer` is case-insensitive.

Gatus retries a TRIGGERED send that got a non-2xx answer on its next failing check, but sends a RESOLVED once and
drops it on error. So the endpoint never waits for the explanation: store work runs in a worker thread
(`asyncio.to_thread`), the job runs as a background task, and a failure of that task is logged with its traceback
(never lost) by the task's done-callback. A lost RESOLVED loses only the explanation: Gatus's own "back up" push
still arrives.

## 3. Incident store and state machine

`src/incidents/store.py`, one SQLite table `incidents` in `INCIDENT_DB_PATH` (WAL mode). Datetimes are stored as
ISO-8601 UTC strings and returned as timezone-aware UTC; naive datetimes are rejected. At most one `open` row exists
per endpoint key (a partial unique index), and the read-then-write steps run in one `BEGIN IMMEDIATE` transaction.

```text
TRIGGERED            RESOLVED                  claim_for_explanation      record_outcome
   │                    │                              │                       │
   ▼                    ▼                              ▼                       ▼
 open  ──────────▶  resolved  ─────────────────▶  explaining  ─────────▶  explained | failed
                        ▲
 (no open row) ─────────┘  RESOLVED alone: triggered_at = NULL
```

- **TRIGGERED** (`open_incident`): returns the endpoint's open row, or inserts one with `triggered_at=now`. A
  repeated TRIGGERED keeps the first trigger time.
- **RESOLVED** (`resolve_incident`): closes the open row (`resolved_at=now`) and schedules the job. With no open row,
  it inserts a `resolved` row with `triggered_at` empty (the start comes from Gatus) and schedules the job. When the
  endpoint's latest row was resolved within the last 60 seconds and no TRIGGERED came in between, the event is a
  duplicate: the answer names that incident and nothing is scheduled.
- **Claim** (`claim_for_explanation`): an atomic `UPDATE ... SET state='explaining' WHERE id=? AND
  state='resolved'`. Only the caller that moved the row goes on to send, so each incident gets at most one push.
- **Outcome** (`record_outcome`): `explained` or `failed`, with an outcome code and a detail line. Only an
  `explaining` incident accepts one.
- **Unfinished** (`list_unfinished`): the `resolved` and `explaining` rows, oldest first, read by the restart
  recovery ([section 9](#9-restart-recovery)).

## 4. Shared models

`src/incidents/models.py` holds the types the receiver, store, evidence collector and job share: `GatusEvent` (the
webhook body), `Target` (host and container from the description), `Incident` (one stored row, frozen), `Source`
(`name`, `status` of `ok`/`unavailable`/`skipped`, `detail` with the reason when not ok, `lines`) and
`EvidenceBundle` (the incident, the window, the first failure time and the sources).

## 5. Evidence sources

`evidence.collect(incident, now, window=None)` (`src/incidents/evidence.py`) returns seven sources, always in the
order below, each `ok`, `unavailable` (the reason in `detail`) or `skipped` (the reason in `detail`). It never
raises. Each source runs concurrently with a 10 s timeout and is capped at 20 lines of 300 characters. Times in the
lines are UTC, written `HH:MM:SSZ`; the push shows them in `USER_TIMEZONE`.

Gatus sets the window, so it is read first. The window runs from 10 min before the first failed check (the last
`UNHEALTHY` event at or before the resolve) to the resolve. When Gatus cannot be read or has no `UNHEALTHY` event,
the window starts 13 min before the TRIGGERED time, or else covers the 30 min before the resolve, and the `gatus`
detail names the fallback. The dry run passes its own window.

| Source           | What its lines hold                                                                                   |
| ---------------- | ----------------------------------------------------------------------------------------------------- |
| `gatus`          | first failed check, recovery, failed checks out of total in the window, up to 5 distinct errors and the failed conditions |
| `docker_events`  | the container's lifecycle events from Loki `service_name="docker-events"`: `kill signal=15`, `die exitCode=0`, `oom`, `stop`, `start`, `create`, `destroy`, health changes (exec events dropped) |
| `container_logs` | the last 15 lines before the first failure, then error, warn and fatal lines in the window          |
| `restarts`       | when the container was not running (gaps in its cAdvisor series), whether it was recreated (its start time is its creation; a stop and start keeps it), and whether the host rebooted |
| `proxmox_tasks`  | Proxmox tasks started in the window: type, guest, user, status                                       |
| `logins`         | `sudo` and `sshd` lines, only on hosts that ship them; otherwise skipped                              |
| `deploys`        | What's Up Docker lines naming the container, and Upgrade Advisor deploy rows read from its database read-only |

An endpoint with no host gets only `gatus` and `proxmox_tasks`; the other five are skipped. A host with no container
skips `docker_events` and `container_logs`, checks only the host reboot, and reads WUD lines for the whole host. An
unconfigured Loki, Prometheus, Gatus or Proxmox skips its sources. Loki and Prometheus name hosts the same way except
the Proxmox host: `pve` in Loki, `proxmox` in Prometheus.

## 6. Verdict

`src/incidents/verdict.py`. `build_prompt(bundle)` puts the incident (name, group, host, container, URL) and each
source in its own fenced block, `<<<UNTRUSTED SOURCE <name> <nonce>>>` … `<<<END UNTRUSTED SOURCE <name> <nonce>>>`,
with the source's status, detail and lines; the nonce is random per prompt, so a forged end marker inside a log line
stays inside its block. The window, the first failing check and the resolve time sit outside the blocks. A source is
cut at 6 000 characters and a line at 500.

The system text (`SYSTEM_TEXT`) tells the model to base every claim only on the evidence, to say "unknown" rather
than guess, to write times with the `Z`, and how to read the evidence:

- `kill signal=15` then `die` then `stop`: stopped on purpose (a person, a compose command or a deploy);
- `die` with a non-zero exit code and no `kill` before it: a crash; `oom`: out of memory;
- `die` then `start` with no `stop`: Docker's restart policy restarted it;
- `destroy` then `create`: recreated (a deploy or `docker compose up`);
- a What's Up Docker or Upgrade Advisor deploy line: a deploy;
- a Proxmox task by a user on the guest: a person acted on the VM or LXC;
- a host boot time change: the host rebooted;
- no restart, no action and only connection errors: a network or transient problem that cleared by itself.

The answer is one JSON object, validated by the `Verdict` model:

| Field        | Type                                       | Rule                                                        |
| ------------ | ------------------------------------------ | ----------------------------------------------------------- |
| `cause`      | string                                     | why it went down; clipped to 200 characters                 |
| `how`        | string                                     | how it came back; clipped to 200 characters                 |
| `resolution` | `self`, `manual`, `deploy` or `unknown`    | `manual`: a person or manual command; `deploy`: a deploy caused or ended it; `self`: no recorded action (restart policy included); `unknown`: the evidence cannot tell |
| `confidence` | `low`, `medium` or `high`                  |                                                             |
| `evidence`   | list of strings                            | first 3 kept, each clipped to 120 characters                |

`parse_verdict(text)` takes the first JSON object in the answer and ignores prose or a code fence around it.
Over-long text is clipped rather than rejected, so a wordy but correct answer still gives a verdict. It raises
`VerdictError` for an answer over 20 000 characters, an answer with no JSON object, a missing field, a wrong type or
a value outside the allowed ones. `judge(bundle)` calls `ask_text(SYSTEM_TEXT, prompt,
model_override=INCIDENT_MODEL, budget_s=60)`: rate limits, 5xx, network errors and timeouts are retried with
doubling backoff within the 60-second budget.

## 7. The push

`src/incidents/message.py`, pure functions. The title is `Why <name> went down` (at most 250 characters). The body is
plain text, at most 1024 characters (Pushover's limit):

1. `Down HH:MM–HH:MM <TZ> (<N> min)` in `USER_TIMEZONE`, from the first failing check to the resolve. Minutes are
   rounded; under a minute reads `under 1 min`. When the two ends fall on either side of a clock change, each end
   carries its own abbreviation (`Down 02:58 CEST–02:03 CET (5 min)`). With no known first failure the line reads
   `Back up at HH:MM <TZ>`.
2. `Cause: <cause>`
3. `How it came back: <how>`
4. `Confidence: <confidence>`
5. Up to three `• <evidence>` lines.
6. `Missing: <source> (<detail>), …` when any source was `unavailable`, so a degraded explanation says so.

UTC times inside the window (`10:06:12Z`) in the verdict and evidence lines are shown in `USER_TIMEZONE`
(`11:06:12`). When the body is too long, evidence lines are cut or dropped first; the other lines stay. The link is
`{GATUS_PUBLIC_URL}/endpoints/<endpoint key>` with the title `Gatus history`, left out when `GATUS_PUBLIC_URL` is
empty.

Example (a fake service):

```text
Why Wiki went down

Down 11:06–11:09 CET (3 min)
Cause: The wiki container was stopped on purpose (SIGTERM, exit code 0) after an SSH login.
How it came back: Someone started the container again at 11:09:30.
Confidence: high
• 11:06:12 kill signal=15 then stop
• 11:05:58 SSH login by admin
• 11:09:30 start
Missing: deploys (Loki timed out after 10 s)
```

**Fallback.** When the model fails, the body is the `Down` line, `Summary unavailable: <reason>` (for example `the
model did not answer in time`, `no JSON object in the answer`, `model call failed (AuthenticationError)`), then up to
four facts from `ok` sources in this order: `docker_events` lines, `restarts` lines, `proxmox_tasks` lines,
`deploys` lines, then the first Gatus error line; then the `Missing:` line.

```text
Why Wiki went down

Down 11:06–11:09 CET (3 min)
Summary unavailable: the model did not answer in time
• 11:06:12 kill signal=15
• 11:06:13 die exitCode=0
• 11:06:13 stop
• 11:09:30 start
Missing: deploys (Loki timed out after 10 s)
```

## 8. Pushover

`src/incidents/pushover.py:send` posts form data to `https://api.pushover.net/1/messages.json` with the
explainer's own application token (`PUSHOVER_APP_TOKEN`), the user key (`PUSHOVER_USER_KEY`), priority 0 (normal),
and a 10-second timeout. A network error or a 5xx is retried once after 5 seconds; a 4xx is not retried. A reply of
`{"status": 1, "request": "<id>"}` is a success; anything else is a failure whose detail names the HTTP status and
Pushover's `errors`. With either key empty, nothing is sent and the outcome is `not_sent` with `Pushover not
configured`. The token and user key never appear in a log line or a stored detail.

## 9. Restart recovery

A restart can leave incidents behind: a job killed mid-flight leaves `explaining`, and a RESOLVED whose job never
ran leaves `resolved`. At startup, when the webhook is configured, the lifespan in `src/api/main.py` starts
`src/api/incidents.py:start_incident_recovery` as a background task (startup never waits on it), which runs
`job.recover_unfinished`:

- an `explaining` incident is recorded as `failed`, `error`, `interrupted by a restart` (a WARNING);
- a `resolved` incident resolved within the last 30 minutes is explained now, through the same scheduling helper
  as the webhook (the task is referenced until done, and a failure is logged);
- an older `resolved` incident is claimed and recorded as `failed`, `not_sent`, `not explained before a restart`
  (a WARNING): its evidence window has passed, and a late push would confuse more than it helps.

## 10. Dry run

`python -m src.incidents --dry-run` explains a past outage and prints the result without sending or storing
anything:

```bash
uv run python -m src.incidents --dry-run --group monitoring --name Dozzle --host infra --container dozzle \
    --start 2026-10-27T10:06:00Z --end 2026-10-27T10:10:00Z
```

`--url`, `--host` and `--container` are optional; `--start` and `--end` are ISO-8601 with a timezone. It builds an
in-memory incident (id 0, `resolved`), collects the evidence over `start - 10 min` to `end`, asks the model, and
prints the bundle (each source's name, status, detail and lines), the verdict JSON (or why it is unavailable, then
the fallback) and the push's title, link and body. It exits with code 2 and names the missing settings when
`INCIDENT_MODEL` or `GATUS_URL` is empty. It still calls the model, so it costs one request.

## 11. Settings

| Setting                   | Default             | Purpose                                                          |
| ------------------------- | ------------------- | ---------------------------------------------------------------- |
| `INCIDENT_WEBHOOK_TOKEN`  | empty (disabled)    | Bearer token Gatus sends; empty makes the endpoint answer 503    |
| `INCIDENT_DB_PATH`        | empty (disabled)    | SQLite file for incidents; empty makes the endpoint answer 503   |
| `INCIDENT_MODEL`          | `claude-sonnet-5-5` | Model that writes the verdict (no tools); used only with `LLM_PROVIDER=anthropic` |
| `INCIDENT_SETTLE_SECONDS` | `30`                | Wait after RESOLVED before collecting evidence                   |
| `USER_TIMEZONE`           | `UTC`               | Timezone of the times in the push (shared with the agent)        |
| `GATUS_URL`               | empty               | Gatus API base URL, read for the failing results                 |
| `GATUS_PUBLIC_URL`        | empty               | Gatus URL the push links to                                      |
| `PUSHOVER_APP_TOKEN`      | empty               | The explainer's own Pushover application token                   |
| `PUSHOVER_USER_KEY`       | empty               | The Pushover user or group key to notify                         |

At startup the service logs one line: a warning naming the missing settings when the token or DB path is empty; a
warning naming any empty Gatus or Pushover setting when the endpoint is enabled but an explanation would lack what
those provide; otherwise `incident explainer enabled (model ...)`. `.env.example` has placeholders.

## 12. Metrics

- `sre_assistant_incident_events_total{event, outcome}` counts webhook calls. `event` is `triggered`, `resolved`,
  or `unknown` (the body was not read or not valid). `outcome` is `accepted`, `duplicate`, `unauthorized`,
  `not_configured`, `invalid` or `error`.
- `sre_assistant_incident_explanations_total{outcome}` counts finished explanations: `explained`, `fallback`,
  `not_sent` or `error` (the outcomes in [section 1](#1-flow), restart recovery included).
