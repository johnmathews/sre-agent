"""The Gatus incident webhook: ``POST /incidents/gatus``.

Gatus's custom alert provider posts ``{"event", "group", "name", "url", "description"}`` with
``Authorization: Bearer <INCIDENT_WEBHOOK_TOKEN>`` on every alert transition. Gatus retries a TRIGGERED send that got
a non-2xx answer on its next failing check, but sends a RESOLVED once and drops it on error. So the endpoint records
the event, answers 202 at once and runs the explanation in the background: downstream work never fails a valid
request.

Answers, in the order they are checked:

- 503 ``incident explainer not configured`` when ``INCIDENT_WEBHOOK_TOKEN`` or ``INCIDENT_DB_PATH`` is empty;
- 401 ``unauthorized`` for a missing or wrong bearer token;
- 413 for a body over ``MAX_BODY_BYTES``, 422 for a body that is not a valid Gatus event;
- 500 ``incident store error`` when the SQLite store fails (logged with the traceback and counted);
- 202 ``{"incident_id", "state"}`` otherwise. A newly resolved incident schedules ``job.explain(incident_id)``.
"""

import asyncio
import functools
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.config import get_settings
from src.incidents import job, store
from src.incidents.gatus import endpoint_key, parse_target
from src.incidents.models import GatusEvent, Incident
from src.observability.metrics import INCIDENT_EVENTS_TOTAL
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

router = APIRouter()

# Gatus bodies are a few hundred bytes; every field is capped at 512 characters by the model.
MAX_BODY_BYTES = 16 * 1024

# Strong references to running explanation tasks, so none is garbage-collected mid-flight; each removes itself
# when done.
_explain_tasks: set[asyncio.Task[None]] = set()

_REQUIRED = (("incident_webhook_token", "INCIDENT_WEBHOOK_TOKEN"), ("incident_db_path", "INCIDENT_DB_PATH"))
_DELIVERY = (
    ("gatus_url", "GATUS_URL"),
    ("gatus_public_url", "GATUS_PUBLIC_URL"),
    ("pushover_app_token", "PUSHOVER_APP_TOKEN"),
    ("pushover_user_key", "PUSHOVER_USER_KEY"),
)


def _missing(pairs: tuple[tuple[str, str], ...]) -> list[str]:
    settings = get_settings()
    return [env for attr, env in pairs if not getattr(settings, attr)]


def missing_incident_settings() -> list[str]:
    """Names of the settings the webhook needs that are empty (names only, never values)."""
    return _missing(_REQUIRED)


def log_incident_explainer_status() -> None:
    """At startup, log one line: a warning naming the missing settings, or an info line naming the model."""
    missing = missing_incident_settings()
    if missing:
        logger.warning(
            "incident explainer is not configured (missing: %s); POST /incidents/gatus answers 503",
            ", ".join(missing),
        )
        return
    model = get_settings().incident_model
    degraded = _missing(_DELIVERY)
    if degraded:
        logger.warning(
            "incident explainer enabled (model %s) but missing: %s; explanations lack what those provide",
            model,
            ", ".join(degraded),
        )
        return
    logger.info("incident explainer enabled (model %s)", model)


def _answer(status: int, body: dict[str, Any], event: str, outcome: str) -> JSONResponse:
    INCIDENT_EVENTS_TOTAL.labels(event=event, outcome=outcome).inc()
    return JSONResponse(body, status_code=status)


def _authorised(header: str | None, token: str) -> bool:
    """True when the Authorization header is ``Bearer <token>`` (scheme case-insensitive). Compared as UTF-8 bytes:
    ``hmac.compare_digest`` raises on non-ASCII ``str``, which would turn a junk header into a 500."""
    if not header:
        return False
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


async def _read_body(request: Request) -> bytes | None:
    """The request body, or None when it exceeds ``MAX_BODY_BYTES`` (read no further than that)."""
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            return None
    return bytes(raw)


def _record(db_path: str, event: GatusEvent, now: datetime) -> tuple[Incident, bool]:
    """Write the event to the store; returns the incident and whether an explanation should be scheduled."""
    fields: dict[str, Any] = {
        "endpoint_key": endpoint_key(event.group, event.name),
        "group": event.group,
        "name": event.name,
        "url": event.url,
        "target": parse_target(event.description),
        "now": now,
    }
    conn = store.connect(db_path)
    try:
        if event.event == "TRIGGERED":
            return store.open_incident(conn, **fields), False
        return store.resolve_incident(conn, **fields)
    finally:
        conn.close()


def _explanation_done(incident_id: int, task: asyncio.Task[None]) -> None:
    _explain_tasks.discard(task)
    if task.cancelled():
        logger.warning("incident %d explanation was cancelled before it finished", incident_id)
        return
    exc = task.exception()
    if exc is not None:
        # logger.exception needs an active except block; exc_info carries the task's traceback instead.
        logger.error("incident %d explanation failed", incident_id, exc_info=exc)


def _schedule_explanation(incident_id: int) -> None:
    task = asyncio.create_task(job.explain(incident_id), name=f"incident-explain-{incident_id}")
    _explain_tasks.add(task)
    task.add_done_callback(functools.partial(_explanation_done, incident_id))


@router.post("/incidents/gatus", status_code=202)
async def gatus_webhook(request: Request) -> JSONResponse:
    """Record a Gatus alert transition; on a newly resolved incident, schedule its explanation."""
    settings = get_settings()
    # Configuration is checked before auth on purpose: Gatus logs the response body, so an operator who has not set
    # the token yet sees "not configured" rather than a misleading "unauthorized". It reveals nothing a caller
    # could use: an unconfigured endpoint accepts nothing.
    if not settings.incident_webhook_token or not settings.incident_db_path:
        return _answer(503, {"detail": "incident explainer not configured"}, "unknown", "not_configured")
    if not _authorised(request.headers.get("Authorization"), settings.incident_webhook_token):
        logger.warning("POST /incidents/gatus: missing or wrong bearer token")
        return _answer(401, {"detail": "unauthorized"}, "unknown", "unauthorized")

    raw = await _read_body(request)
    if raw is None:
        return _answer(413, {"detail": f"body over {MAX_BODY_BYTES} bytes"}, "unknown", "invalid")
    try:
        event = GatusEvent.model_validate_json(raw)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        logger.warning("POST /incidents/gatus: invalid body (%d errors)", len(errors))
        return _answer(422, {"detail": errors}, "unknown", "invalid")

    label = event.event.lower()
    try:
        incident, schedule = await asyncio.to_thread(_record, settings.incident_db_path, event, datetime.now(UTC))
    except Exception:
        logger.exception("POST /incidents/gatus: storing %s for %r failed", event.event, clean_text(event.name))
        return _answer(500, {"detail": "incident store error"}, label, "error")

    body = {"incident_id": incident.id, "state": incident.state}
    if event.event == "RESOLVED" and not schedule:
        logger.info("incident %d (%s): duplicate RESOLVED ignored", incident.id, clean_text(incident.endpoint_key))
        return _answer(202, body, label, "duplicate")
    logger.info("incident %d (%s): %s", incident.id, clean_text(incident.endpoint_key), event.event)
    if schedule:
        _schedule_explanation(incident.id)
    return _answer(202, body, label, "accepted")


__all__ = [
    "MAX_BODY_BYTES",
    "gatus_webhook",
    "log_incident_explainer_status",
    "missing_incident_settings",
    "router",
]
