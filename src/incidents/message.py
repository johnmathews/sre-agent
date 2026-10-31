"""Compose the explanation push from an evidence bundle and a verdict (or from the bundle alone when the model failed).

Pure functions: the caller passes the user's timezone and the public Gatus URL. Limits follow Pushover's API: a
title of at most 250 characters and a message of at most 1024. When the body is too long, evidence lines go first;
the time, cause, how, confidence and ``Missing:`` lines stay. Times in evidence (``HH:MM:SSZ``, UTC) are shown in the
user's timezone.
"""

import math
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from src.incidents.models import EvidenceBundle, Source
from src.incidents.verdict import Verdict

MAX_TITLE_CHARS = 250
MAX_MESSAGE_CHARS = 1024
MAX_REASON_CHARS = 120
MAX_FACT_CHARS = 200
MAX_MISSING_DETAIL_CHARS = 40
MAX_MISSING_CHARS = 200
MAX_FALLBACK_FACTS = 4
# A cut evidence line shorter than this is dropped instead.
MIN_CLIPPED_LINE = 20
URL_TITLE = "Gatus history"
BULLET = "• "
# Sources whose lines become fallback facts, in this order; the Gatus error line comes after them.
FALLBACK_SOURCES = ("docker_events", "restarts", "proxmox_tasks", "deploys")
# The Gatus line used as a fallback fact: the first error line, else the first failed condition.
_GATUS_FACT_PREFIXES = ("error:", "failed condition:")
# A UTC time in an evidence line: HH:MMZ or HH:MM:SSZ, not part of a longer number.
_UTC_TIME = re.compile(r"(?<![\d:])(\d{2}):(\d{2})(?::(\d{2}))?Z(?!\w)")
# A time in a line counts as inside the window with this much slack on each side.
_WINDOW_SLACK = timedelta(hours=1)


class Push(BaseModel):
    """One Pushover message."""

    title: str
    message: str
    url: str | None
    url_title: str | None


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _localize(text: str, bundle: EvidenceBundle, tz: ZoneInfo) -> str:
    """Rewrite UTC times inside the bundle's window (``10:06:12Z``) as local times (``11:06:12``). A time that
    falls outside the window, or is not a valid time, is left as it is."""
    start, end = bundle.window_start.astimezone(UTC), bundle.window_end.astimezone(UTC)
    days = sorted({start.date(), end.date()})

    def replace(match: re.Match[str]) -> str:
        hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
        for day in days:
            try:
                moment = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)
            except ValueError:
                return match[0]
            if start - _WINDOW_SLACK <= moment <= end + _WINDOW_SLACK:
                return moment.astimezone(tz).strftime("%H:%M:%S" if match[3] else "%H:%M")
        return match[0]

    return _UTC_TIME.sub(replace, text)


def down_line(bundle: EvidenceBundle, timezone: str) -> str:
    """``Down 11:06–11:09 CET (3 min)``, or ``Back up at 11:09 CET`` when the first failure is unknown."""
    tz = ZoneInfo(timezone)
    resolved = bundle.incident.resolved_at or bundle.window_end
    end = resolved.astimezone(tz)
    end_zone = end.strftime("%Z")
    if bundle.first_failure_at is None:
        return f"Back up at {end:%H:%M} {end_zone}"
    start = bundle.first_failure_at.astimezone(tz)
    start_zone = start.strftime("%Z")
    seconds = (resolved - bundle.first_failure_at).total_seconds()
    duration = "under 1 min" if seconds < 60 else f"{math.floor(seconds / 60 + 0.5)} min"
    if start_zone == end_zone:
        return f"Down {start:%H:%M}–{end:%H:%M} {end_zone} ({duration})"
    return f"Down {start:%H:%M} {start_zone}–{end:%H:%M} {end_zone} ({duration})"


def missing_line(sources: list[Source]) -> str | None:
    """``Missing: <name> (<detail>), …`` for the unavailable sources, or None when there are none."""
    parts = [
        f"{s.name} ({_clip(s.detail, MAX_MISSING_DETAIL_CHARS)})" if s.detail else s.name
        for s in sources
        if s.status == "unavailable"
    ]
    if not parts:
        return None
    return _clip("Missing: " + ", ".join(parts), MAX_MISSING_CHARS)


def _title(name: str) -> str:
    room = MAX_TITLE_CHARS - len("Why  went down")
    return f"Why {_clip(name, room)} went down"


def _url(bundle: EvidenceBundle, gatus_public_url: str) -> tuple[str | None, str | None]:
    if not gatus_public_url:
        return None, None
    key = quote(bundle.incident.endpoint_key, safe="")
    return f"{gatus_public_url.rstrip('/')}/endpoints/{key}", URL_TITLE


def _assemble(head: list[str], middle: list[str], tail: list[str]) -> str:
    """Join the lines, dropping (or cutting) ``middle`` lines from the end until the text fits the limit."""
    fixed = "\n".join(head + tail)
    room = MAX_MESSAGE_CHARS - len(fixed)
    kept: list[str] = []
    for line in middle:
        cost = len(line) + 1
        if cost <= room:
            kept.append(line)
            room -= cost
            continue
        if room - 1 >= MIN_CLIPPED_LINE:
            kept.append(_clip(line, room - 1))
        break
    return "\n".join(head + kept + tail)[:MAX_MESSAGE_CHARS]


def compose(bundle: EvidenceBundle, verdict: Verdict, *, timezone: str, gatus_public_url: str) -> Push:
    """The push for a verdict: time, cause, how it came back, confidence, evidence, then what was missing."""
    tz = ZoneInfo(timezone)
    head = [
        down_line(bundle, timezone),
        "Cause: " + _localize(verdict.cause, bundle, tz),
        "How it came back: " + _localize(verdict.how, bundle, tz),
        f"Confidence: {verdict.confidence}",
    ]
    evidence = [BULLET + _localize(item, bundle, tz) for item in verdict.evidence]
    missing = missing_line(bundle.sources)
    url, url_title = _url(bundle, gatus_public_url)
    return Push(
        title=_title(bundle.incident.name),
        message=_assemble(head, evidence, [missing] if missing else []),
        url=url,
        url_title=url_title,
    )


def fallback_facts(bundle: EvidenceBundle) -> list[str]:
    """Up to four plain facts from the bundle: Docker events, restarts, Proxmox tasks, deploys, then the Gatus
    ``error:`` line (or its first ``failed condition:`` line when there is no error). Only sources with status
    ``ok`` count."""
    ok = {s.name: s for s in bundle.sources if s.status == "ok"}
    facts: list[str] = []
    for name in FALLBACK_SOURCES:
        if name in ok:
            facts += ok[name].lines
    if "gatus" in ok:
        for prefix in _GATUS_FACT_PREFIXES:
            matches = [line for line in ok["gatus"].lines if line.startswith(prefix)]
            if matches:
                facts.append(matches[0])
                break
    return facts[:MAX_FALLBACK_FACTS]


def compose_fallback(bundle: EvidenceBundle, reason: str, *, timezone: str, gatus_public_url: str) -> Push:
    """The push when the model gave no usable verdict: time, why the summary is missing, plain facts, missing
    sources."""
    tz = ZoneInfo(timezone)
    head = [down_line(bundle, timezone), "Summary unavailable: " + _clip(reason, MAX_REASON_CHARS)]
    facts = [BULLET + _clip(_localize(fact, bundle, tz), MAX_FACT_CHARS) for fact in fallback_facts(bundle)]
    missing = missing_line(bundle.sources)
    url, url_title = _url(bundle, gatus_public_url)
    return Push(
        title=_title(bundle.incident.name),
        message=_assemble(head, facts, [missing] if missing else []),
        url=url,
        url_title=url_title,
    )


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_TITLE_CHARS",
    "Push",
    "compose",
    "compose_fallback",
    "down_line",
    "fallback_facts",
    "missing_line",
]
