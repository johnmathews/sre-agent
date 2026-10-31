"""Send one push through the incident explainer's own Pushover application.

Priority 0 (normal). One retry after ``RETRY_DELAY_SECONDS`` on a 5xx or on a transport error that means the
request never reached Pushover (``ConnectError``, ``ConnectTimeout``, ``PoolTimeout``). Any other transport error
(a read or write timeout, a dropped connection) may come after Pushover took the request, so it is not retried: a
retry could send the push twice. A 4xx is not retried (the request itself is wrong). The application token and user
key never appear in a log line or a result detail.
"""

import asyncio
import logging

import httpx
from pydantic import BaseModel

from src.config import get_settings
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TIMEOUT_SECONDS = 10.0
RETRY_DELAY_SECONDS = 5.0
PRIORITY = "0"
# Transport errors raised before the request was sent: retrying them cannot deliver the push twice.
NOT_SENT_ERRORS: tuple[type[httpx.TransportError], ...] = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class PushoverResult(BaseModel):
    """How a send ended. ``request_id`` is Pushover's request id when it answered with one."""

    ok: bool
    request_id: str | None = None
    detail: str


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return clean_text(text)


def _parse(response: httpx.Response, secrets: tuple[str, ...]) -> PushoverResult:
    """Read Pushover's answer: ``{"status": 1, "request": "..."}`` on success, ``errors`` on failure."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        suffix = " without a JSON body" if response.is_success else ""
        return PushoverResult(ok=False, detail=f"Pushover answered HTTP {response.status_code}{suffix}")
    request = body.get("request")
    request_id = _redact(request, secrets) if isinstance(request, str) else None
    if response.is_success and body.get("status") == 1:
        return PushoverResult(ok=True, request_id=request_id, detail=f"sent (request {request_id})")
    errors = body.get("errors")
    reason = "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else ""
    if response.is_success:
        detail = f"Pushover answered status {body.get('status')}"
    else:
        detail = f"Pushover answered HTTP {response.status_code}"
    if reason:
        detail = f"{detail}: {_redact(reason, secrets)}"
    return PushoverResult(ok=False, request_id=request_id, detail=detail)


async def send(title: str, message: str, url: str | None, url_title: str | None) -> PushoverResult:
    """Send one push. Never raises: a failure comes back as ``ok=False`` with the reason in ``detail``."""
    settings = get_settings()
    token, user = settings.pushover_app_token, settings.pushover_user_key
    if not token or not user:
        logger.warning("Pushover not configured (PUSHOVER_APP_TOKEN or PUSHOVER_USER_KEY is empty); push not sent")
        return PushoverResult(ok=False, detail="Pushover not configured")
    secrets = (token, user)
    form = {"token": token, "user": user, "title": title, "message": message, "priority": PRIORITY}
    if url:
        form["url"] = url
        if url_title:
            form["url_title"] = url_title

    result = PushoverResult(ok=False, detail="not sent")
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for attempt in (1, 2):
            retryable = False
            try:
                response = await client.post(PUSHOVER_URL, data=form)
            except NOT_SENT_ERRORS as exc:
                result = PushoverResult(ok=False, detail=f"Pushover unreachable: {type(exc).__name__}")
                retryable = True
            except httpx.TransportError as exc:
                result = PushoverResult(ok=False, detail=f"delivery unknown: {type(exc).__name__}; not resent")
            else:
                result = _parse(response, secrets)
                retryable = response.status_code >= 500
            if result.ok:
                return result
            logger.warning("Pushover send failed (attempt %d): %s", attempt, result.detail)
            if not retryable or attempt == 2:
                break
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    return result


__all__ = ["PUSHOVER_URL", "RETRY_DELAY_SECONDS", "PushoverResult", "send"]
