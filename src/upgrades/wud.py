"""Client for What's Up Docker's API: detect pending updates and trigger a deploy."""

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from src.config import get_settings
from src.upgrades.config import AdvisedContainer

logger = logging.getLogger(__name__)

GET_TIMEOUT_SECONDS = 30.0
# WUD answers the trigger POST only after the deploy has finished.
TRIGGER_TIMEOUT_SECONDS = 600.0


class WudError(Exception):
    """WUD was unreachable, refused the request, or does not know the container."""


class WudContainer(BaseModel):
    """The parts of a WUD container record the advisor uses."""

    id: str
    name: str
    running_digest: str | None
    candidate_digest: str | None
    update_available: bool
    update_kind: str | None = None  # "tag" | "digest" | "unknown"; None when WUD omits it


def _auth() -> httpx.BasicAuth:
    s = get_settings()
    return httpx.BasicAuth(s.upgrade_advisor_wud_username, s.upgrade_advisor_wud_password)


def _where(c: AdvisedContainer) -> str:
    return f"container {c.name} (WUD at {urlparse(c.wud_url).hostname})"


def _dig(record: dict[str, Any], *keys: str) -> Any:
    """Follow nested dict keys, returning None as soon as one is missing or not a dict."""
    value: Any = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


async def get_container(c: AdvisedContainer) -> WudContainer:
    """Fetch WUD's record for ``c`` (matched on ``name``)."""
    url = f"{c.wud_url.rstrip('/')}/api/containers"
    try:
        async with httpx.AsyncClient(auth=_auth(), timeout=GET_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.error("WUD request failed for %s: %r", c.name, exc)
        raise WudError(f"WUD request failed for {_where(c)}: {exc!r}") from exc
    if not response.is_success:
        raise WudError(f"WUD returned HTTP {response.status_code} listing containers for {_where(c)}")
    try:
        records = response.json()
    except ValueError as exc:
        raise WudError(f"WUD returned invalid JSON for {_where(c)}") from exc
    if not isinstance(records, list):
        raise WudError(f"WUD returned an unexpected payload for {_where(c)}")
    for record in records:
        if isinstance(record, dict) and record.get("name") == c.name:
            return WudContainer(
                id=str(record["id"]),
                name=c.name,
                running_digest=_dig(record, "image", "digest", "value"),
                candidate_digest=_dig(record, "result", "digest"),
                update_available=bool(record.get("updateAvailable", False)),
                update_kind=_dig(record, "updateKind", "kind"),
            )
    raise WudError(f"WUD does not list {_where(c)}")


async def trigger_deploy(c: AdvisedContainer, wud_id: str) -> None:
    """Ask WUD to deploy the pending update; returns once WUD reports the deploy finished."""
    url = f"{c.wud_url.rstrip('/')}/api/containers/{wud_id}/triggers/dockercompose/deploy"
    try:
        async with httpx.AsyncClient(auth=_auth(), timeout=TRIGGER_TIMEOUT_SECONDS) as client:
            response = await client.post(url)
    except httpx.HTTPError as exc:
        logger.error("WUD deploy request failed for %s: %r", c.name, exc)
        raise WudError(f"WUD deploy request failed for {_where(c)}: {exc!r}") from exc
    if response.is_success:
        return
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")
    except ValueError:
        pass
    logger.error("WUD deploy failed for %s: HTTP %s %s", c.name, response.status_code, detail)
    raise WudError(f"WUD deploy failed for {_where(c)}: HTTP {response.status_code} {detail}".rstrip())
