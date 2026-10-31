# Incident explainer: a second push that says why a service went down

> Purpose: record what was built on 2026-10-31 so that a resolved Gatus outage produces a Pushover message with its
> cause and how the service came back, and what was observed while building it.

## 1. Decisions

The user made these in the design discussion before the build.

- **Two pushes.** Gatus keeps its own "down" and "back up" pushes. The agent sends a separate follow-up from its own
  Pushover app at priority 0. A lost webhook costs the explanation, never the notification.
- **Gatus only.** Grafana alerts are out of scope; they add no information here.
- **Option B: fixed evidence, then a no-tools model.** Code collects seven sources over the outage window, and
  `INCIDENT_MODEL` (`claude-sonnet-5-5`) answers one JSON verdict. This is the Upgrade Advisor's pattern. It was
  chosen over a tool-calling agent for bounded cost, predictability and testability.
- **Docker events now.** They ship from a `docker-events` unit per host in proxmox-setup (same day,
  `journal/261031-incident-explainer-gatus-docker-events.md` there).

## 2. What was built

- `POST /incidents/gatus` (`src/api/incidents.py`): bearer token, answers 202 immediately, and stores each incident
  in SQLite (`src/incidents/store.py`). The answer is immediate because Gatus sends a RESOLVED once and drops it on
  error (Gatus v5.37.0 `watchdog/alerting.go`).
- The body carries only Ansible-controlled fields. Gatus escapes only `"` in `[RESULT_ERRORS]`
  (`alerting/provider/custom/custom.go:111`), so errors are read from the Gatus API instead.
- `src/incidents/evidence.py` collects seven sources: Gatus, Docker events, container logs, restarts, Proxmox tasks,
  logins and deploys. Each source runs under its own timeout and line caps.
- `verdict.py`, `message.py`, `pushover.py` and `job.py` turn the evidence into one push per incident.
  - The one-push guarantee is an atomic claim in the store.
  - A model failure still sends a fallback push with the plain facts.
  - Startup recovery handles incidents a restart left behind.
- `python -m src.incidents --dry-run …` prints the bundle, verdict and push for a past outage without sending.
- The no-tools model call moved from `src/upgrades/review.py` into `src/agent/llm.py:ask_text`. The review tests pass
  unchanged apart from patch targets.

## 3. Observed during the build

- **Dry run on the real Dozzle outage of 2026-10-27, 10:06:44–10:10:44 UTC.** All seven sources were collected
  against the live services.
  - The model call returned 401 locally, because the local `.env` has no Anthropic key, so the fallback push was
    exercised instead. The verdict path is unproven against the real model until the live test.
  - `restarts` said "container not restarted" while Dozzle's own logs showed it starting at 10:06:51.
  - A Prometheus query showed the cause (confirmed): `container_start_time_seconds` stayed at 09:26:15 for the whole
    outage. It is the container's creation time, which a stop and start keeps. The cAdvisor series instead had gaps
    exactly while the container was down: 09:53:00–10:07:30 and 10:07:30–10:10:00.
  - `restarts` now reports those gaps. A re-run printed "no running container from 09:56:44Z to 10:07:14Z" and "from
    10:07:44Z to 10:09:44Z". Gatus recorded the recovery at 10:09:44.
- **Code review** (independent agent) found five issues, each fixed test-first:
  - a stale open incident widening the window after a lost RESOLVED (now retired after 24 h);
  - a Pushover retry after a read timeout that could double-send (now retries only when the request never left);
  - a model failure logged without its cause;
  - a sent push recorded as failed when the outcome write failed;
  - URLs from model text reaching the push (now `[link removed]`).
- **Doc audit** (independent agent) found 23 doc problems and one code bug: the fallback push picked the Gatus
  "failed checks" count line instead of the error line. All were fixed.
- Tests: 1428 on `origin/main`, 1668 on the branch; coverage 96% overall and 97% on the new code.

## 4. What is deliberately not done

- **No retry for a lost resolve webhook.** Gatus's own push still arrives, so only the explanation is lost.
- **Logins mostly skipped.** Only TrueNAS ships `sudo`/`sshd` lines to Loki today, so `logins` is skipped on every
  other host. Shipping them would make "someone fixed it" clearer; that is a separate proxmox-setup change.
- **No dashboard panels** for the two new counters. The Upgrade Advisor's counters have none either.
- **Not yet proven live:** the verdict from the real model, the token check end to end, and Pushover delivery. W8 of
  the plan (a stop by hand of Dozzle after deploy) settles them.

## 5. Live test, same day (appended after deploy)

- Dozzle stopped by hand at 15:15:13 UTC. Gatus triggered at 15:18:04 and sent both the Pushover push and the webhook,
  which sre-agent accepted (202 from 192.168.2.115). Dozzle started by hand at 15:18:30, and Gatus resolved at
  15:20:05. Incident 1 was `explained` / `fallback`, and the push was sent (Pushover request 2c366335…). Confirmed.
- The verdict fell back. The log shows `anthropic.BadRequestError: 400 … '`temperature` is deprecated for this model'`
  (request req_011CgaYMG5k97ceCKEsppho5). `create_anthropic_chat` always sent `temperature=0`. The Upgrade Advisor's
  `claude-opus-4-6` accepts it; `claude-sonnet-5-5` rejects it. The local dry run never reached the model (no key), so
  this surfaced only live.
- Fix: send `temperature` only to models that accept it (Claude 3.x and Claude 4 up to 4.6). The main agent's model
  (`claude-opus-4-6`) keeps `temperature=0`, so its behaviour is unchanged.
