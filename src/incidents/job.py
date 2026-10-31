"""The background job that explains one resolved incident, and the startup recovery of unfinished ones.

``POST /incidents/gatus`` schedules ``explain(incident_id)`` once per newly resolved incident. The job waits
``INCIDENT_SETTLE_SECONDS``, claims the incident (at most one job ever does), collects the evidence bundle, asks the
model for a verdict, sends one Pushover push and records the outcome. A model failure still sends a push, with plain
facts and the reason the summary is missing. Every SQLite call runs in a worker thread.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from src.config import get_settings
from src.incidents import evidence, message, pushover, store
from src.incidents import verdict as verdict_mod
from src.incidents.models import Incident, OutcomeState
from src.observability.metrics import INCIDENT_EXPLANATIONS_TOTAL
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

# At startup, a resolved incident newer than this is still explained; an older one is recorded as not sent.
RECOVERY_WINDOW = timedelta(minutes=30)


async def _in_store[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run one store call on its own connection in a worker thread."""
    db_path = get_settings().incident_db_path

    def run() -> T:
        conn = store.connect(db_path)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    return await asyncio.to_thread(run)


def no_verdict_reason(exc: Exception) -> str:
    """Why there is no verdict, in words for the push."""
    if isinstance(exc, verdict_mod.VerdictError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "the model did not answer in time"
    return f"model call failed ({type(exc).__name__})"


async def _record(incident_id: int, *, state: OutcomeState, outcome: str, detail: str) -> None:
    await _in_store(
        store.record_outcome,
        incident_id,
        state=state,
        outcome=outcome,
        detail=clean_text(detail),
        now=datetime.now(UTC),
    )
    INCIDENT_EXPLANATIONS_TOTAL.labels(outcome=outcome).inc()


async def _explain_claimed(incident_id: int) -> None:
    settings = get_settings()
    incident = await _in_store(store.get_incident, incident_id)
    if incident is None:
        msg = f"incident {incident_id} not found after claiming it"
        raise LookupError(msg)
    bundle = await evidence.collect(incident, datetime.now(UTC))

    reason: str | None = None
    try:
        verdict = await verdict_mod.judge(bundle)
    except Exception as exc:
        reason = no_verdict_reason(exc)
        # An unexpected model failure keeps its traceback: the SDK's message carries the provider's error body.
        expected = isinstance(exc, verdict_mod.VerdictError | TimeoutError)
        logger.warning(
            "incident %d: no verdict (%s); sending the fallback push",
            incident_id,
            reason,
            exc_info=None if expected else exc,
        )
        push = message.compose_fallback(
            bundle, reason, timezone=settings.user_timezone, gatus_public_url=settings.gatus_public_url
        )
    else:
        push = message.compose(
            bundle, verdict, timezone=settings.user_timezone, gatus_public_url=settings.gatus_public_url
        )

    result = await pushover.send(push.title, push.message, push.url, push.url_title)
    if not result.ok:
        # "delivery unknown" means the push may have arrived (a read timeout), so it is not called "not sent".
        unknown = result.detail.startswith("delivery unknown")
        detail = result.detail if unknown else f"not sent: {result.detail}"
        logger.warning("incident %d: %s", incident_id, detail)
        await _record(incident_id, state="failed", outcome="not_sent", detail=detail)
        return
    if reason is None:
        outcome, detail = "explained", result.detail
    else:
        outcome, detail = "fallback", f"fallback ({reason}); {result.detail}"
    try:
        await _record(incident_id, state="explained", outcome=outcome, detail=detail)
    except Exception:
        # The push went out: never record this incident as failed, and count what really happened.
        logger.exception("incident %d: push sent, but recording the outcome failed", incident_id)
        INCIDENT_EXPLANATIONS_TOTAL.labels(outcome=outcome).inc()
        return
    logger.info("incident %d: %s push %s", incident_id, outcome, result.detail)


async def explain(incident_id: int) -> None:
    """Explain incident ``incident_id`` with one push. Never raises: failures are logged, counted and recorded."""
    await asyncio.sleep(get_settings().incident_settle_seconds)
    try:
        claimed = await _in_store(store.claim_for_explanation, incident_id)
    except Exception:
        logger.exception("incident %d: claiming failed; not explained", incident_id)
        INCIDENT_EXPLANATIONS_TOTAL.labels(outcome="error").inc()
        return
    if not claimed:
        logger.info("incident %d: already claimed by another task; nothing to do", incident_id)
        return
    try:
        await _explain_claimed(incident_id)
    except Exception as exc:
        logger.exception("incident %d: explanation failed", incident_id)
        try:
            await _record(incident_id, state="failed", outcome="error", detail=type(exc).__name__)
        except Exception:
            logger.exception("incident %d: recording the error outcome failed", incident_id)
            INCIDENT_EXPLANATIONS_TOTAL.labels(outcome="error").inc()


async def _recover_one(incident: Incident, now: datetime, schedule: Callable[[int], None]) -> None:
    if incident.state == "explaining":
        logger.warning(
            "incident %d (%s): interrupted by a restart while being explained; recorded as failed",
            incident.id,
            clean_text(incident.endpoint_key),
        )
        await _record(incident.id, state="failed", outcome="error", detail="interrupted by a restart")
        return
    if incident.resolved_at is not None and now - incident.resolved_at <= RECOVERY_WINDOW:
        logger.info("incident %d: resolved before the restart; explaining it now", incident.id)
        schedule(incident.id)
        return
    if not await _in_store(store.claim_for_explanation, incident.id):
        return  # a job claimed it meanwhile
    logger.warning(
        "incident %d (%s): not explained before a restart and older than %d min; no push sent",
        incident.id,
        clean_text(incident.endpoint_key),
        int(RECOVERY_WINDOW.total_seconds() // 60),
    )
    await _record(incident.id, state="failed", outcome="not_sent", detail="not explained before a restart")


async def recover_unfinished(now: datetime, schedule: Callable[[int], None]) -> None:
    """Finish what a restart left behind. An ``explaining`` incident is recorded as failed; a ``resolved`` one is
    passed to ``schedule`` when resolved within ``RECOVERY_WINDOW`` of ``now``, otherwise recorded as not sent.
    Never raises: each failure is logged."""
    try:
        unfinished = await _in_store(store.list_unfinished)
    except Exception:
        logger.exception("incident recovery: reading unfinished incidents failed")
        return
    for incident in unfinished:
        try:
            await _recover_one(incident, now, schedule)
        except Exception:
            logger.exception("incident recovery: recovering incident %d failed", incident.id)


__all__ = ["RECOVERY_WINDOW", "explain", "no_verdict_reason", "recover_unfinished"]
