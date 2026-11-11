"""Shared, pure helpers for the SRE agent tools.

Formatting (bytes, UTC timestamps, ages), time and duration parsing, PromQL/LogQL
literal quoting, host-name normalisation and log-level vocabularies.

This module must stay free of IO and must not import ``get_settings``: every tool
module imports it, and tests use it without the ``mock_settings`` fixture.
"""

import math
import re
from datetime import UTC, datetime, timedelta

ERROR_LEVELS = "error|err|fatal|critical|crit"
"""Log levels counted as errors, as a regex alternation (Loki ``level=~``)."""

WARN_LEVELS = "warn|warning"
"""Log levels counted as warnings, as a regex alternation (Loki ``level=~``)."""

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(n: float) -> str:
    """Format a byte count with binary units and one decimal, e.g. ``"7.9 GiB"``.

    Uses real division, so 1.99 TiB prints as ``"2.0 TiB"`` rather than being
    truncated to ``"1.0 TiB"``. A value that rounds up to 1024 moves to the next unit.
    """
    value = float(n)
    for unit in _BYTE_UNITS[:-1]:
        if abs(round(value, 1)) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {_BYTE_UNITS[-1]}"


def _as_utc(value: datetime | float) -> datetime:
    """A timezone-aware UTC datetime from a datetime (naive means UTC) or epoch seconds."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.fromtimestamp(value, tz=UTC)


def format_utc(value: datetime | float) -> str:
    """Format a datetime or Unix epoch seconds as ``YYYY-MM-DD HH:MM:SSZ`` in UTC.

    A naive datetime is taken to be UTC already.
    """
    return _as_utc(value).strftime("%Y-%m-%d %H:%M:%SZ")


def format_age(value: datetime | float, now: datetime) -> str:
    """How long before ``now`` the moment ``value`` was, e.g. ``"8h ago"``.

    Uses the largest whole unit (s, m, h, d). A moment after ``now`` gives ``"in 5m"``.
    """
    seconds = (_as_utc(now) - _as_utc(value)).total_seconds()
    magnitude = int(abs(seconds))
    if magnitude < 60:
        text = f"{magnitude}s"
    elif magnitude < 3600:
        text = f"{magnitude // 60}m"
    elif magnitude < 86400:
        text = f"{magnitude // 3600}h"
    else:
        text = f"{magnitude // 86400}d"
    return f"in {text}" if seconds < 0 else f"{text} ago"


_DURATION_UNITS: dict[str, float] = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")
_DURATION_FULL = re.compile(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h|d|w))+")
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def parse_duration(value: str) -> float:
    """Parse a duration to seconds.

    Accepts plain seconds (``"90"``), one unit (``"5m"``, ``"24h"``) and compound
    forms (``"1h30m"``). Units: ms, s, m, h, d, w. Negative, infinite and
    malformed values raise ``ValueError``.
    """
    text = value.strip()
    if _PLAIN_NUMBER.fullmatch(text):
        return float(text) if "." in text else int(text)
    if not text or not _DURATION_FULL.fullmatch(text):
        raise ValueError(
            f"Invalid duration {value!r}: use seconds ('90') or number+unit, compound allowed "
            + "('30s', '5m', '24h', '7d', '1h30m'; units ms, s, m, h, d, w)"
        )
    total = sum(float(number) * _DURATION_UNITS[unit] for number, unit in _DURATION_PART.findall(text))
    return int(total) if total.is_integer() else total


_TIME_FORMS = (
    "accepted forms: 'now'; a duration before now as '24h', '-24h' or 'now-24h' "
    + "(units ms, s, m, h, d, w; compound like '1h30m'); "
    + "RFC3339/ISO 8601 like '2026-11-11T13:00:00Z' (no zone means UTC); "
    + "or Unix epoch seconds like '1794369609'"
)


def parse_time(value: str, now: datetime) -> datetime:
    """Parse a time expression to a timezone-aware UTC datetime.

    Relative forms (``"24h"``, ``"-24h"``, ``"now-24h"``) count back from ``now``;
    a naive ``now`` is taken to be UTC. Absolute forms are RFC3339/ISO 8601 (a
    timestamp without a zone is UTC) and Unix epoch seconds. Anything else raises a
    ``ValueError`` that lists every accepted form.
    """
    text = value.strip()
    lowered = text.lower()
    now_utc = _as_utc(now)

    if lowered == "now":
        return now_utc

    if _PLAIN_NUMBER.fullmatch(text):
        seconds = float(text)
        if math.isfinite(seconds):
            try:
                return datetime.fromtimestamp(seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                pass
        raise ValueError(f"Invalid time {value!r}: epoch out of range; {_TIME_FORMS}")

    relative: str | None = None
    if lowered.startswith("now-"):
        relative = lowered.removeprefix("now-")
    elif lowered.startswith("-"):
        relative = lowered.removeprefix("-")
    elif _DURATION_FULL.fullmatch(lowered):
        relative = lowered
    if relative is not None:
        try:
            return now_utc - timedelta(seconds=parse_duration(relative))
        except (ValueError, OverflowError):
            raise ValueError(f"Invalid time {value!r}: {_TIME_FORMS}") from None

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Invalid time {value!r}: {_TIME_FORMS}") from None
    return _as_utc(parsed)


_REGEX_META = re.compile(r"([\\.+*?()|\[\]{}^$])")


def regex_literal(value: str) -> str:
    """Escape RE2/Python regex metacharacters so ``value`` matches only itself.

    For a PromQL/LogQL ``=~`` matcher, wrap the result in :func:`promql_string`.
    """
    return _REGEX_META.sub(r"\\\1", value)


def promql_string(value: str) -> str:
    """A PromQL/LogQL double-quoted string literal.

    Backslashes are escaped before quotes (same rule as ``src.incidents.evidence._quote``),
    so a value cannot close the literal or un-escape a quote.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_HOST_SUFFIX = re.compile(r"\s+(?:vm|lxc|ve)$")
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_HOST_ALIASES = {"pve": "proxmox"}


def host_key(name: str) -> str:
    """Normalise a host name so exporter and Prometheus spellings compare equal.

    Lowercases, strips a trailing `` vm``, `` lxc`` or `` ve`` word, keeps only
    alphanumerics and maps ``pve`` to ``proxmox``: ``"media vm"`` and ``"media"``,
    ``"tubearchivist lxc"`` and ``"tube-archivist"``, ``"proxmox ve"`` and ``"pve"``
    each give the same key.
    """
    key = _NON_ALNUM.sub("", _HOST_SUFFIX.sub("", name.strip().lower()))
    return _HOST_ALIASES.get(key, key)
