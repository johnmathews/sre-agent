"""Current-time tool and prompt-time renderer.

Three responsibilities:

1. ``render_prompt_time_fields`` — produce the dict of placeholders the
   system prompt expects (current_time_full, current_weekday, current_date,
   current_local_time, user_timezone, retention_cutoff). Centralised so the
   LangChain and SDK agent paths render identical strings.
2. ``get_current_time`` — a LangChain tool that returns the same time
   information at runtime, so the agent can re-anchor 'now' mid-conversation
   if a duration claim looks suspect.
3. ``request_user_timezone`` — context manager that sets the per-request
   timezone override (sent by the webapp from the user's browser/device),
   so a travelling user gets answers in the timezone they're actually in
   without having to redeploy with a new ``USER_TIMEZONE`` env var.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel

from src.agent.tools import HOMELAB_CONTEXT
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90

# Per-request timezone override. The API handler sets this before invoking
# the agent; render_prompt_time_fields and get_current_time both read it.
# When unset (None), we fall back to settings.user_timezone.
_REQUEST_TZ: ContextVar[str | None] = ContextVar("request_user_timezone", default=None)


class GetCurrentTimeInput(BaseModel):
    """No inputs — returns the current time in multiple formats."""


def _resolve_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone, falling back to UTC if invalid."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %r — falling back to UTC", name)
        return ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    """Return True iff *name* is a known IANA timezone (no fallback)."""
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


@contextmanager
def request_user_timezone(tz: str | None) -> Iterator[None]:
    """Set the per-request timezone for the duration of the with-block.

    Pass ``None`` to leave the contextvar untouched (e.g. when no timezone
    was supplied on the request — fall back to ``settings.user_timezone``).
    Invalid IANA names are rejected by the caller (the API request validator);
    we don't re-validate here so misuse during testing fails loudly.
    """
    if tz is None:
        yield
        return
    token = _REQUEST_TZ.set(tz)
    try:
        yield
    finally:
        _REQUEST_TZ.reset(token)


def effective_timezone(settings: Settings | None = None) -> str:
    """Return the timezone to use for this request: override if set, else settings."""
    override = _REQUEST_TZ.get()
    if override:
        return override
    if settings is None:
        settings = get_settings()
    return settings.user_timezone


def _format_now(tz_name: str, now: datetime) -> dict[str, Any]:
    """Build the time fields, raw values included for tool output."""
    tz = _resolve_timezone(tz_name)
    local = now.astimezone(tz)
    return {
        "utc_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_epoch": int(now.timestamp()),
        "weekday": now.strftime("%A"),
        "date": now.strftime("%Y-%m-%d"),
        "user_timezone": tz_name,
        "user_local_iso": local.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "user_local_human": local.strftime("%A %Y-%m-%d %H:%M %Z"),
    }


def render_prompt_time_fields(settings: Settings | None = None) -> dict[str, str]:
    """Return the dict of {placeholder: value} for the system prompt.

    Computes a fresh 'now' on every call so each agent invocation gets
    up-to-date time fields, and reads the per-request timezone override
    (set by the API handler) if present.
    """
    tz_name = effective_timezone(settings)
    now = datetime.now(UTC)
    raw = _format_now(tz_name, now)
    return {
        # New, explicit fields
        "current_time_full": f"{raw['weekday']}, {raw['date']} {now.strftime('%H:%M:%S')} UTC",
        "current_weekday": raw["weekday"],
        "current_date": raw["date"],
        "current_local_time": raw["user_local_human"],
        "user_timezone": raw["user_timezone"],
        "retention_cutoff": (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d"),
        # Legacy fields kept for any prompt fragment that still uses them
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


TOOL_DESCRIPTION = HOMELAB_CONTEXT + (
    "Return the current date, time, weekday, and the user's local time. Use this when "
    "you need to ground a duration or 'how long ago' claim, when computing elapsed time "
    "from a Unix timestamp returned by another tool, or when you want to confirm the "
    "weekday for today before reasoning about past events.\n\n"
    "Returns: UTC ISO timestamp, UTC epoch seconds, weekday name (e.g. 'Tuesday'), "
    "today's date, the user's IANA timezone, and the user's local time. The user's "
    "timezone reflects the device they made this request from (so a travelling user "
    "gets answers in the zone they're actually in), falling back to the deployment "
    "default when the client did not send one. Always prefer subtracting epoch seconds "
    "over deriving durations from day-of-week reasoning — the latter is a known failure "
    "mode that produces ±24h errors."
)


@tool("get_current_time", args_schema=GetCurrentTimeInput)
def get_current_time() -> str:
    """Return the current date and time. See TOOL_DESCRIPTION."""
    tz_name = effective_timezone()
    now = datetime.now(UTC)
    raw = _format_now(tz_name, now)
    lines = [
        f"UTC ISO:        {raw['utc_iso']}",
        f"UTC epoch:      {raw['utc_epoch']}",
        f"UTC weekday:    {raw['weekday']}",
        f"UTC date:       {raw['date']}",
        f"User timezone:  {raw['user_timezone']}",
        f"User local ISO: {raw['user_local_iso']}",
        f"User local:     {raw['user_local_human']}",
    ]
    return "\n".join(lines)


get_current_time.description = TOOL_DESCRIPTION
