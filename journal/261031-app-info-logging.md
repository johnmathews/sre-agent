# App INFO logs show in docker logs

> Purpose: record why `src.*` INFO lines were missing from `docker logs sre-agent` and what changed on 2026-10-31.

## 1. Problem

- During the incident explainer's live test, a successful explanation left no log line. Only WARNING and above
  appeared, so the outcome could only be read from `incidents.db` or the metric. Confirmed.
- Cause: nothing configured Python logging. uvicorn sets up only its own loggers, so `src.*` records fell through to
  Python's last-resort handler, which prints WARNING and above only.

## 2. Change

- `src/observability/logs.py:configure_app_logging(level)` gives the `src` logger one stderr handler at
  `LOG_LEVEL` (new setting, default `INFO`), formatted `LEVEL:module: message`. Calling it twice adds no second
  handler.
- The lifespan calls it first, so startup lines ("Building SRE assistant agent...", "incident explainer enabled")
  appear too.
- Library loggers (`httpx`, `anthropic`, `langchain`) are left alone, so their per-request INFO lines stay hidden.
  Propagation stays on, so pytest's caplog still sees app records.

## 3. What is deliberately not done

- No root-logger configuration (`basicConfig`): it would turn on library INFO lines and duplicate uvicorn's own.
- No JSON log format. Loki stores the lines as they are, and the existing WARNING lines keep the same text.
