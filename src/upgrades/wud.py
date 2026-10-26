"""Client for What's Up Docker's API: detect pending updates and trigger a deploy."""

import logging
import re
import unicodedata
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ValidationError

from src.config import get_settings
from src.upgrades.config import AdvisedContainer

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 200
DOCKER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
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


def _clean(text: str) -> str:
    """Make text from a WUD response safe for an error message: no control characters, at most 200 chars."""
    return "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)[:MAX_DETAIL_CHARS]


def _describe_malformed(exc: KeyError | ValidationError) -> str:
    """Name what was wrong with a WUD record (field locations and error types only, never values)."""
    if isinstance(exc, KeyError):
        return f"missing key {_clean(str(exc.args[0]) if exc.args else '?')}"
    return _clean(", ".join(".".join(map(str, e["loc"])) + ": " + e["type"] for e in exc.errors()))


def _fail(message: str) -> WudError:
    """Log ``message`` and build the WudError (the caller raises it, chained)."""
    logger.error(message)
    return WudError(message)


async def get_container(c: AdvisedContainer) -> WudContainer:
    """Fetch WUD's record for ``c`` (matched on ``name``)."""
    url = f"{c.wud_url.rstrip('/')}/api/containers"
    try:
        async with httpx.AsyncClient(auth=_auth(), timeout=GET_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(url)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _fail(f"WUD request failed for {_where(c)}: {exc!r}") from exc
    if not response.is_success:
        raise _fail(f"WUD returned HTTP {response.status_code} listing containers for {_where(c)}")
    try:
        records = response.json()
    except ValueError as exc:
        raise _fail(f"WUD returned invalid JSON for {_where(c)}") from exc
    if not isinstance(records, list):
        raise _fail(f"WUD returned an unexpected payload for {_where(c)}")
    matches = [r for r in records if isinstance(r, dict) and r.get("name") == c.name]
    if len(matches) > 1:
        raise _fail(f"ambiguous: {len(matches)} WUD containers named {c.name} at {urlparse(c.wud_url).hostname}")
    if matches:
        record = matches[0]
        try:
            return WudContainer(
                id=str(record["id"]),
                name=c.name,
                running_digest=_dig(record, "image", "digest", "value"),
                candidate_digest=_dig(record, "result", "digest"),
                update_available=bool(record.get("updateAvailable", False)),
                update_kind=_dig(record, "updateKind", "kind"),
            )
        except (KeyError, ValidationError) as exc:
            raise _fail(f"WUD returned a malformed record for {_where(c)}: {_describe_malformed(exc)}") from exc
    raise _fail(f"WUD does not list {_where(c)}")


async def trigger_deploy(c: AdvisedContainer, wud_id: str) -> None:
    """Ask WUD to deploy the pending update; returns once WUD reports the deploy finished."""
    if not DOCKER_ID_RE.fullmatch(wud_id):
        raise _fail(f"refusing to deploy {_where(c)}: WUD id is not a Docker container id")
    url = f"{c.wud_url.rstrip('/')}/api/containers/{quote(wud_id, safe='')}/triggers/dockercompose/deploy"
    try:
        async with httpx.AsyncClient(auth=_auth(), timeout=TRIGGER_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _fail(f"WUD deploy request failed for {_where(c)}: {exc!r}") from exc
    if response.is_success:
        return
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = _clean(str(body.get("message") or body.get("error") or ""))
    except ValueError:
        pass
    raise _fail(f"WUD deploy failed for {_where(c)}: HTTP {response.status_code} {detail}".rstrip())
