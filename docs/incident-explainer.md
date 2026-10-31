# Incident explainer

> Purpose
>
> How a resolved Gatus outage turns into a second Pushover push that says why the service went down and how it came
> back. Covers the webhook contract, the incident store and its state machine, the background job, and the settings.

## 1. Flow

1. **Gatus posts every alert transition.** Each alerting Gatus endpoint has a second alert of type `custom` with
   `send-on-resolved: true`. On TRIGGERED and on RESOLVED, Gatus posts a small JSON body to
   `POST /incidents/gatus` with a bearer token. Gatus's own "down" and "back up" pushes are unchanged; the
   explanation is a separate push from its own Pushover app.
2. **The receiver records the event and answers 202 at once.** TRIGGERED opens an incident row (or reuses the open
   one). RESOLVED closes it. A newly resolved incident schedules `explain(incident_id)` as a background task. A
   duplicate RESOLVED schedules nothing.
3. **The job waits, then collects evidence.** `src/incidents/job.py:explain` waits `INCIDENT_SETTLE_SECONDS` so the
   last log lines and metric samples land, claims the incident (at most one job ever does), and builds an
   `EvidenceBundle` (`src/incidents/evidence.py`). The window runs from 10 minutes before the first failing Gatus
   result to the resolve. Each source is a fixed query and reports `ok`, `unavailable` (with the reason) or
   `skipped`, so a missing source shows up in the verdict and never stops the job:
   1. Gatus results for the endpoint (`GET {GATUS_URL}/api/v1/endpoints/{key}/statuses`): the first failure's time
      and errors. This also gives the start when the TRIGGERED event was never received.
   2. Docker lifecycle events for the container from Loki (`service_name="docker-events"`): `kill`, `die` (with the
      exit code), `oom`, `stop`, `start` and so on.
   3. The container's last error and warning lines, and its last lines before the first failure.
   4. Restarts: cAdvisor's container start time and the host's boot time from Prometheus.
   5. Proxmox tasks in the window.
   6. Logins (`sudo`, `sshd`) on the host.
   7. Deploys: What's Up Docker log lines naming the container, and Upgrade Advisor deploy rows.

   An endpoint whose description names no host gets the Gatus and Proxmox sources only, and the bundle says so.
4. **Sonnet gives a verdict with no tools.** `src/incidents/verdict.py` sends the bundle, each source in a fenced
   block marked untrusted, to `INCIDENT_MODEL` (default `claude-sonnet-5-5`) through the shared no-tools call in
   `src/agent/llm.py`. The answer is one JSON object: `cause`, `resolution` (`self`, `manual`, `deploy` or
   `unknown`), `confidence` (`low`, `medium` or `high`) and `evidence` (at most three short strings), validated with
   pydantic. A reply that does not parse counts as a model failure.
5. **Code composes and sends the push.** `src/incidents/pushover.py` posts to the Pushover API at priority 0 with
   the title `Why <name> went down`, a body of at most 1024 characters (cause, how it came back, outage duration,
   confidence) and a link to the endpoint's page under `GATUS_PUBLIC_URL`. When the model fails, the push still goes
   out with the deterministic facts and `Summary unavailable: <reason>`.
6. **The outcome is recorded.** The incident moves to `explained` or `failed`, with a short outcome code and a detail
   line (for example the Pushover request id or the error).

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

## 4. Shared models

`src/incidents/models.py` holds the types the receiver, store, evidence collector and job share: `GatusEvent` (the
webhook body), `Target` (host and container from the description), `Incident` (one stored row, frozen), `Source`
(`name`, `status` of `ok`/`unavailable`/`skipped`, `detail` with the reason when not ok, `lines`) and
`EvidenceBundle` (the incident, the window, the first failure time and the sources).

## 5. Settings

| Setting                   | Default             | Purpose                                                          |
| ------------------------- | ------------------- | ---------------------------------------------------------------- |
| `INCIDENT_WEBHOOK_TOKEN`  | empty (disabled)    | Bearer token Gatus sends; empty makes the endpoint answer 503    |
| `INCIDENT_DB_PATH`        | empty (disabled)    | SQLite file for incidents; empty makes the endpoint answer 503   |
| `INCIDENT_MODEL`          | `claude-sonnet-5-5` | Model that writes the verdict (no tools)                         |
| `INCIDENT_SETTLE_SECONDS` | `30`                | Wait after RESOLVED before collecting evidence                   |
| `GATUS_URL`               | empty               | Gatus API base URL, read for the failing results                 |
| `GATUS_PUBLIC_URL`        | empty               | Gatus URL the push links to                                      |
| `PUSHOVER_APP_TOKEN`      | empty               | The explainer's own Pushover application token                   |
| `PUSHOVER_USER_KEY`       | empty               | The Pushover user or group key to notify                         |

At startup the service logs one line: a warning naming the missing settings when the token or DB path is empty; a
warning naming any empty Gatus or Pushover setting when the endpoint is enabled but an explanation would lack what
those provide; otherwise `incident explainer enabled (model ...)`. `.env.example` has placeholders.

## 6. Metrics

`sre_incident_events_total{event, outcome}` counts webhook calls. `event` is `triggered`, `resolved`, or `unknown`
(the body was not read or not valid). `outcome` is `accepted`, `duplicate`, `unauthorized`, `not_configured`,
`invalid` or `error`. `sre_incident_explanations_total{outcome}` counts finished explanations.
