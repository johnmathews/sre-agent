"""Current-time tool and prompt-time renderer.

Two responsibilities:

1. ``render_prompt_time_fields`` — produce the dict of placeholders the
   system prompt expects (current_time_full, current_weekday, current_date,
   current_local_time, user_timezone, retention_cutoff). Centralised so the
   LangChain and SDK agent paths render identical strings.
2. ``get_current_time`` — a LangChain tool that returns the same time
   information at runtime, so the agent can re-anchor 'now' mid-conversation
   if a duration claim looks suspect.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel

from src.agent.tools import HOMELAB_CONTEXT
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90


class GetCurrentTimeInput(BaseModel):
    """No inputs — returns the current time in multiple formats."""


def _resolve_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone, falling back to UTC if invalid."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %r — falling back to UTC", name)
        return ZoneInfo("UTC")


def _format_now(settings: Settings, now: datetime) -> dict[str, Any]:
    """Build the time fields, raw values included for tool output."""
    tz = _resolve_timezone(settings.user_timezone)
    local = now.astimezone(tz)
    return {
        "utc_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_epoch": int(now.timestamp()),
        "weekday": now.strftime("%A"),
        "date": now.strftime("%Y-%m-%d"),
        "user_timezone": settings.user_timezone,
        "user_local_iso": local.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "user_local_human": local.strftime("%A %Y-%m-%d %H:%M %Z"),
    }


def render_prompt_time_fields(settings: Settings | None = None) -> dict[str, str]:
    """Return the dict of {placeholder: value} for the system prompt.

    Computes a fresh 'now' on every call so each agent invocation gets
    up-to-date time fields.
    """
    if settings is None:
        settings = get_settings()
    now = datetime.now(UTC)
    raw = _format_now(settings, now)
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
    "today's date, the user's IANA timezone, and the user's local time. Always prefer "
    "subtracting epoch seconds over deriving durations from day-of-week reasoning — the "
    "latter is a known failure mode that produces ±24h errors."
)


@tool("get_current_time", args_schema=GetCurrentTimeInput)
def get_current_time() -> str:
    """Return the current date and time. See TOOL_DESCRIPTION."""
    settings = get_settings()
    now = datetime.now(UTC)
    raw = _format_now(settings, now)
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
