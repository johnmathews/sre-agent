"""Evidence collector: a fixed set of read-only queries that show why one Gatus outage happened and how it ended.

``collect(incident, now, window=None)`` returns an ``EvidenceBundle`` with exactly seven ``Source`` items, always in
the order of ``SOURCE_NAMES``. It never raises: a source that fails is ``unavailable`` with a short reason (no URLs, no
secrets), a source that does not apply is ``skipped`` with the reason, so the verdict can say what it could not see.
Gatus is read first, because it sets the window; the other six then run concurrently. Each has its own timeout
(``SOURCE_TIMEOUT_SECONDS``) and is capped at ``MAX_LINES_PER_SOURCE`` lines of at most ``MAX_LINE_CHARS`` characters.
Lines are plain text, one fact per line, with UTC times as ``HH:MM:SSZ``; the message layer converts them to local time.

The window
    Gatus is read first because it fixes the window. The first failed check is the last ``UNHEALTHY`` event at or
    before the resolve (or ``now`` while the outage is open); recovery is the next ``HEALTHY``. The window runs from
    10 minutes before the first failed check to the resolve. Without Gatus, or with no ``UNHEALTHY`` event, the
    window starts 13 minutes before the TRIGGERED time (Gatus triggers after about three failed 60 s checks), or
    else covers the 30 minutes before the resolve; the ``gatus`` source detail says which fallback was used. A
    caller-supplied ``window`` (a dry run over a past outage) replaces the derived bounds.

The sources
    ``gatus``
        The first failed check, the recovery, how many checks failed in the window, up to five distinct error
        strings and the failed conditions. This is what the user saw fail.
    ``docker_events``
        Container lifecycle events from Loki (``service_name="docker-events"``, the JSON of ``docker events``),
        filtered to this container: ``kill signal=15``, ``die exitCode=137``, ``oom``, ``stop``, ``start``, health
        changes. These tell a crash from a stop, an OOM kill or a restart.
    ``container_logs``
        The last lines the container wrote before the first failed check, and its error, warn and fatal lines in
        the window. These often name the cause directly.
    ``restarts``
        From cAdvisor and node exporter: when the container was not running (gaps in its cAdvisor series), whether
        it was recreated (its start time is its creation, which a stop and start keeps), and whether the host
        rebooted. This catches stops and restarts when Docker events are missing.
    ``proxmox_tasks``
        Proxmox tasks started in the window (backups, migrations, guest stops and starts). A guest-level action
        explains an outage of everything on that guest.
    ``logins``
        ``sudo`` and ``sshd`` journal lines on the host in the window. Skipped when the host shipped neither in the 7
        days before the window ends. A login just before the failure suggests a manual action.
    ``deploys``
        What's Up Docker log lines on the host naming the container, plus Upgrade Advisor deploy rows for the
        container (read-only). A deploy just before the failure is the usual suspect.

    An incident with no host gets ``gatus`` and ``proxmox_tasks``; the other five are skipped with "no host mapped
    for this check". A host with no container skips ``docker_events`` and ``container_logs``, checks only the host
    reboot in ``restarts`` and reads WUD lines for the whole host in ``deploys``. Loki, Prometheus, Gatus or Proxmox
    left unconfigured skips their sources with "<X> not configured".

Hostnames: Loki and Prometheus share the ``hostname`` label except on the Proxmox host (Loki ``pve``, Prometheus
``proxmox``); ``loki_hostname`` and ``prometheus_hostname`` map between them.
"""

import asyncio
import json
import logging
import math
import re
import sqlite3
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import quote

import httpx

from src.agent.tools.loki import _datetime_to_nanoseconds, _query_loki
from src.agent.tools.prometheus import _query_prometheus
from src.agent.tools.proxmox import PveTaskEntry, _pve_get
from src.config import get_settings
from src.incidents.models import EvidenceBundle, Incident, Source, SourceStatus

logger = logging.getLogger(__name__)

# Step of the cAdvisor range query that finds when a container was not running. Long windows widen it so the query
# stays at or under RESTART_MAX_POINTS samples (Prometheus rejects more than 11,000 points per series).
RESTART_STEP_SECONDS = 30
RESTART_MAX_POINTS = 2000
# The marker every WUD deploy log line carries, e.g. "[trigger.dockercompose.deploy]" or "[trigger.command.deploy]".
WUD_DEPLOY_MARKER = "[trigger."
SOURCE_NAMES: tuple[str, ...] = (
    "gatus",
    "docker_events",
    "container_logs",
    "restarts",
    "proxmox_tasks",
    "logins",
    "deploys",
)

SOURCE_TIMEOUT_SECONDS = 10.0
MAX_LINES_PER_SOURCE = 20
MAX_LINE_CHARS = 300

WINDOW_LEAD = timedelta(minutes=10)  # evidence starts this long before the first failed check
TRIGGER_DELAY = timedelta(minutes=3)  # Gatus triggers after about three failed 60 s checks
FALLBACK_SPAN = timedelta(minutes=30)  # window length when neither Gatus nor the TRIGGERED time is known

GATUS_PAGE_SIZE = 100
MAX_GATUS_ERRORS = 5
LOKI_LIMIT = 200  # raw lines fetched per Loki query, before filtering and capping
LOGS_BEFORE_FAILURE = 15  # container log lines read just before the first failed check
LOGS_BEFORE_LOOKBACK = timedelta(hours=1)  # how far back to look for those lines
LOGS_AFTER_PIVOT = timedelta(minutes=1)
PROXMOX_TASK_LIMIT = 50
LOGINS_LOOKBACK = timedelta(days=7)  # how far back to look for sshd or sudo lines before deciding a host ships none

NO_HOST = "no host mapped for this check"

_LOKI_TO_PROMETHEUS: dict[str, str] = {"pve": "proxmox"}
_PROMETHEUS_TO_LOKI: dict[str, str] = {prom: loki for loki, prom in _LOKI_TO_PROMETHEUS.items()}

# Container actions worth reporting; exec_* events (health checks, docker exec) are noise.
_DOCKER_ACTIONS = frozenset(
    {"create", "start", "restart", "stop", "kill", "die", "oom", "pause", "unpause", "destroy", "health_status"}
)
_LOGIN_SERVICES = frozenset({"sudo", "sshd"})

_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


class _SourceError(Exception):
    """A source answered, but not usefully; the message is safe to show."""


# Failures a source can hit at runtime; each becomes ``unavailable`` with ``_describe``.
_EXPECTED_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    TimeoutError,
    _SourceError,
    sqlite3.Error,
    OSError,
    ValueError,
    KeyError,
    IndexError,
    TypeError,
)


# --- pure helpers ---


def parse_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, including Go's nanosecond fractions, to an aware UTC datetime.

    The fraction is truncated to microseconds; a missing offset is taken as UTC. Raises ValueError.
    """
    match = _TIMESTAMP.match(value.strip())
    if match is None:
        raise ValueError(f"not an RFC 3339 timestamp: {value[:40]!r}")
    base, fraction, offset = match.groups()
    text = base
    if fraction:
        text += "." + fraction[:6].ljust(6, "0")
    if offset and offset != "Z":
        text += offset
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_hms(value: datetime) -> str:
    """``HH:MM:SSZ`` in UTC."""
    return value.astimezone(UTC).strftime("%H:%M:%SZ")


def _format_day(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def loki_hostname(host: str) -> str:
    """The Loki ``hostname`` for a host named either way (only the Proxmox host differs)."""
    return _PROMETHEUS_TO_LOKI.get(host, host)


def prometheus_hostname(host: str) -> str:
    """The Prometheus ``hostname`` for a host named either way (only the Proxmox host differs)."""
    return _LOKI_TO_PROMETHEUS.get(host, host)


def cap_lines(
    lines: Iterable[str], max_lines: int = MAX_LINES_PER_SOURCE, max_chars: int = MAX_LINE_CHARS
) -> tuple[list[str], int]:
    """Collapse each line to one line of at most ``max_chars`` (ending in "…" when cut); keep the first
    ``max_lines``. Returns the kept lines and how many were dropped."""
    flat = [" ".join(line.split()) for line in lines]
    kept = [line if len(line) <= max_chars else line[: max_chars - 1] + "…" for line in flat[:max_lines]]
    return kept, len(flat) - len(kept)


class StatusEvent(NamedTuple):
    """One Gatus endpoint event: ``START``, ``HEALTHY`` or ``UNHEALTHY``."""

    type: str
    at: datetime


def parse_status_events(raw: object) -> list[StatusEvent]:
    """Gatus ``events`` to ``StatusEvent`` oldest first; malformed entries are skipped."""
    events: list[StatusEvent] = []
    if not isinstance(raw, list):
        return events
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind, stamp = item.get("type"), item.get("timestamp")
        if not isinstance(kind, str) or not isinstance(stamp, str):
            continue
        try:
            events.append(StatusEvent(kind, parse_timestamp(stamp)))
        except ValueError:
            continue
    return sorted(events, key=lambda e: e.at)


@dataclass(frozen=True)
class Window:
    """The evidence window and the outage times it was derived from."""

    start: datetime
    end: datetime
    first_failure_at: datetime | None
    recovered_at: datetime | None
    # The first failed check, or its best estimate; container logs are read up to here.
    pivot: datetime
    # Which fallback set the window, or None when Gatus gave the first failed check.
    fallback: str | None
    overridden: bool = False


def derive_window(
    events: list[StatusEvent] | None,
    incident: Incident,
    now: datetime,
    override: tuple[datetime, datetime] | None = None,
) -> Window:
    """The evidence window for an incident from its Gatus events (None when Gatus could not be read)."""
    end = incident.resolved_at or now
    anchor = incident.resolved_at or (override[1] if override else now)
    first: datetime | None = None
    recovered: datetime | None = None
    unhealthy = [e.at for e in events or [] if e.type == "UNHEALTHY" and e.at <= anchor]
    if unhealthy:
        first = unhealthy[-1]
        recovered = next((e.at for e in events or [] if e.type == "HEALTHY" and e.at > first), None)

    fallback: str | None = None
    if first is not None:
        pivot = first
        start = first - WINDOW_LEAD
    elif incident.triggered_at is not None:
        pivot = incident.triggered_at - TRIGGER_DELAY
        start = pivot - WINDOW_LEAD
        minutes = int((TRIGGER_DELAY + WINDOW_LEAD).total_seconds() // 60)
        fallback = f"window from the TRIGGERED time minus {minutes} min"
    else:
        start = end - FALLBACK_SPAN
        pivot = start + WINDOW_LEAD
        reference = "the resolve" if incident.resolved_at else "now"
        fallback = f"window is the {int(FALLBACK_SPAN.total_seconds() // 60)} min before {reference}"

    if override is None:
        return Window(start, end, first, recovered, pivot, fallback)
    start, end = override
    return Window(start, end, first, recovered, min(max(pivot, start), end), fallback, overridden=True)


def parse_docker_events(lines: Iterable[str], container: str) -> tuple[list[str], int]:
    """``docker events`` JSON lines to ``HH:MM:SSZ <action> [signal=…] [exitCode=…]`` for one container, oldest
    first. Other containers, non-container objects and exec noise are dropped; returns the lines and how many
    input lines could not be parsed."""
    parsed: list[tuple[datetime, str]] = []
    junk = 0
    for line in lines:
        try:
            event: Any = json.loads(line)
        except ValueError:
            junk += 1
            continue
        if not isinstance(event, dict):
            junk += 1
            continue
        actor = event.get("Actor")
        action = event.get("Action")
        attributes = actor.get("Attributes") if isinstance(actor, dict) else None
        if not isinstance(attributes, dict) or not isinstance(action, str):
            junk += 1
            continue
        if event.get("Type") != "container" or attributes.get("name") != container:
            continue
        if action.split(":", 1)[0].strip() not in _DOCKER_ACTIONS:
            continue
        nano, seconds = event.get("timeNano"), event.get("time")
        if isinstance(nano, int) and not isinstance(nano, bool):
            at = _from_epoch(nano / 1_000_000_000)
        elif isinstance(seconds, int | float) and not isinstance(seconds, bool):
            at = _from_epoch(seconds)
        else:
            junk += 1
            continue
        text = f"{format_hms(at)} {action}"
        if action == "kill" and "signal" in attributes:
            text += f" signal={attributes['signal']}"
        if action == "die" and "exitCode" in attributes:
            text += f" exitCode={attributes['exitCode']}"
        parsed.append((at, text))
    parsed.sort(key=lambda item: item[0])
    return [text for _, text in parsed], junk


def _describe(exc: BaseException) -> str:
    """A short, safe reason for a failed source (no URLs, which may carry tokens or queries)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    if isinstance(exc, TimeoutError):
        return f"timed out after {SOURCE_TIMEOUT_SECONDS:g} s"
    if isinstance(exc, httpx.ConnectError):
        return "connection failed"
    if isinstance(exc, httpx.HTTPError | httpx.InvalidURL):
        return f"request failed ({type(exc).__name__})"
    if isinstance(exc, _SourceError):
        return str(exc)
    if isinstance(exc, sqlite3.Error):
        return f"advisor DB: {exc}"
    if isinstance(exc, OSError):
        return f"I/O error ({type(exc).__name__})"
    return f"unexpected response ({type(exc).__name__})"


# --- LogQL / PromQL builders ---


def _quote(value: str) -> str:
    """A LogQL/PromQL double-quoted string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _selector(*matchers: tuple[str, str, str]) -> str:
    """``{name op "value", ...}`` from ``(name, op, value)`` triples."""
    return "{" + ", ".join(f"{name}{op}{_quote(value)}" for name, op, value in matchers) + "}"


# --- low-level reads ---


class _LogEntry(NamedTuple):
    at: datetime
    text: str
    labels: dict[str, str]


async def _loki_lines(query: str, start: datetime, end: datetime, *, limit: int, direction: str) -> list[_LogEntry]:
    """Log lines for a LogQL query, in Loki's order across streams merged by time (oldest first for
    ``forward``, newest first for ``backward``)."""
    params = {
        "query": query,
        "start": _datetime_to_nanoseconds(start),
        "end": _datetime_to_nanoseconds(end),
        "limit": str(limit),
        "direction": direction,
    }
    body: Any = await _query_loki("/loki/api/v1/query_range", params)
    if body.get("status") != "success":
        raise _SourceError(f"Loki status {str(body.get('status'))[:40]}")
    entries: list[_LogEntry] = []
    for stream in body["data"]["result"]:
        labels = {str(k): str(v) for k, v in (stream.get("stream") or {}).items()}
        for value in stream.get("values") or []:
            entries.append(_LogEntry(_from_epoch(int(value[0]) / 1_000_000_000), str(value[1]), labels))
    entries.sort(key=lambda e: e.at, reverse=direction == "backward")
    return entries


async def _loki_label_values(label: str, selector: str, start: datetime, end: datetime) -> list[str]:
    params = {"query": selector, "start": _datetime_to_nanoseconds(start), "end": _datetime_to_nanoseconds(end)}
    body: Any = await _query_loki(f"/loki/api/v1/label/{label}/values", params)
    if body.get("status") != "success":
        raise _SourceError(f"Loki status {str(body.get('status'))[:40]}")
    return [str(v) for v in body.get("data") or []]


async def _prom_value(query: str, at: datetime) -> float | None:
    """The largest sample of an instant query at ``at``, or None when there is no series."""
    body = await _query_prometheus("/api/v1/query", {"query": query, "time": str(at.timestamp())})
    if body.get("status") != "success":
        raise _SourceError(f"Prometheus error: {str(body.get('error', 'unknown'))[:120]}")
    values = [float(series["value"][1]) for series in body.get("data", {}).get("result", [])]
    return max(values) if values else None


async def _fetch_gatus(base_url: str, key: str) -> dict[str, Any]:
    """The endpoint's latest results and events from the Gatus API."""
    url = f"{base_url.rstrip('/')}/api/v1/endpoints/{quote(key, safe='')}/statuses"
    params = {"page": "1", "pageSize": str(GATUS_PAGE_SIZE)}
    async with httpx.AsyncClient(timeout=SOURCE_TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = await client.get(url, params=params)
        _ = response.raise_for_status()
        body: Any = response.json()
    if not isinstance(body, dict):
        raise _SourceError("unexpected response from Gatus")
    return body


def _advisor_deploys(path: str, container: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """Upgrade Advisor deploy rows for a container started in the window, read-only (runs in a thread)."""
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT d.started_at, d.outcome, d.trigger_state, v.running_version, v.candidate_version"
            " FROM deploys d JOIN verdicts v ON v.id = d.verdict_id"
            " WHERE v.container = ? AND d.started_at >= ? AND d.started_at <= ? ORDER BY d.started_at, d.id",
            (container, _store_time(start), _store_time(end)),
        ).fetchall()
    finally:
        conn.close()
    found: list[tuple[datetime, str]] = []
    for started_at, outcome, trigger_state, running, candidate in rows:
        at = datetime.fromisoformat(str(started_at)).astimezone(UTC)
        text = (
            f"{format_hms(at)} advisor deploy {running or '?'} -> {candidate or '?'}"
            f" outcome={outcome} trigger={trigger_state or '-'}"
        )
        found.append((at, text))
    return found


def _store_time(value: datetime) -> str:
    """The advisor store's fixed-width timestamp format (``src/upgrades/store.py:_to_str``)."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


# --- sources ---


def _gatus_lines(body: dict[str, Any], window: Window) -> list[str]:
    lines: list[str] = []
    if window.first_failure_at is not None:
        lines.append(f"first failed check {format_hms(window.first_failure_at)}")
    if window.recovered_at is not None:
        lines.append(f"recovered {format_hms(window.recovered_at)}")
    results: list[tuple[datetime, dict[str, Any]]] = []
    raw_results = body.get("results")
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("timestamp"), str):
            continue
        try:
            at = parse_timestamp(item["timestamp"])
        except ValueError:
            continue
        if window.start <= at <= window.end:
            results.append((at, item))
    results.sort(key=lambda r: r[0])  # Gatus's result order is not relied on
    failed = [item for _, item in results if item.get("success") is False]
    if results:
        lines.append(f"failed checks: {len(failed)} of {len(results)} in the window")
    errors: list[str] = []
    conditions: list[str] = []
    for item in failed:
        raw_errors = item.get("errors")
        for error in raw_errors if isinstance(raw_errors, list) else []:
            if isinstance(error, str) and error not in errors:
                errors.append(error)
        raw_conditions = item.get("conditionResults")
        for condition in raw_conditions if isinstance(raw_conditions, list) else []:
            if isinstance(condition, dict) and condition.get("success") is False:
                name = str(condition.get("condition", "?"))
                if name not in conditions:
                    conditions.append(name)
    lines.extend(f"error: {error}" for error in errors[:MAX_GATUS_ERRORS])
    lines.extend(f"failed condition: {condition}" for condition in conditions)
    return lines


async def _docker_events(host: str, container: str, window: Window) -> Source:
    query = _selector(("service_name", "=", "docker-events"), ("hostname", "=", host)) + f" |= {_quote(container)}"
    entries = await _loki_lines(query, window.start, window.end, limit=LOKI_LIMIT, direction="forward")
    lines, junk = parse_docker_events((e.text for e in entries), container)
    notes: list[str] = []
    if junk:
        notes.append(f"{junk} unparseable line(s) skipped")
    if len(entries) >= LOKI_LIMIT:
        notes.append(f"Loki limit of {LOKI_LIMIT} lines reached")
    if not lines:
        notes.append("no lifecycle events for this container in the window")
    return Source(name="docker_events", status="ok", detail="; ".join(notes), lines=lines)


def _log_line(entry: _LogEntry) -> str:
    level = entry.labels.get("detected_level")
    return f"{format_hms(entry.at)} [{level}] {entry.text}" if level else f"{format_hms(entry.at)} {entry.text}"


async def _container_logs(host: str, container: str, window: Window) -> Source:
    base = _selector(("hostname", "=", host), ("container", "=", container))
    problems = _selector(
        ("hostname", "=", host), ("container", "=", container), ("detected_level", "=~", "error|warn|fatal")
    )
    before_end = window.pivot + LOGS_AFTER_PIVOT
    before_raw, problem_raw = await asyncio.gather(
        _loki_lines(
            base, before_end - LOGS_BEFORE_LOOKBACK, before_end, limit=LOGS_BEFORE_FAILURE, direction="backward"
        ),
        _loki_lines(problems, window.start, window.end, limit=LOKI_LIMIT, direction="forward"),
    )
    before = list(reversed(before_raw))
    seen = {(e.at, e.text) for e in before}
    extra = [e for e in problem_raw if (e.at, e.text) not in seen]
    room = max(0, MAX_LINES_PER_SOURCE - len(before))
    kept = sorted(before + extra[:room], key=lambda e: e.at)
    notes: list[str] = []
    if len(extra) > room:
        notes.append(f"{len(extra) - room} more error/warn lines not shown")
    if not kept:
        notes.append("no log lines for this container around the outage")
    return Source(name="container_logs", status="ok", detail="; ".join(notes), lines=[_log_line(e) for e in kept])


def find_gaps(
    timestamps: Iterable[float], start: datetime, end: datetime, step_s: int
) -> list[tuple[datetime, datetime]]:
    """The spans of ``[start, end]`` with no sample, given a series sampled every ``step_s`` seconds.

    One missing sample is jitter, not a gap: a span counts only when it is longer than two steps. A gap runs from the
    step after the last sample to the next sample (or from ``start``, or to ``end``).
    """
    step = timedelta(seconds=step_s)
    points = sorted(datetime.fromtimestamp(t, tz=UTC) for t in timestamps)
    if not points:
        return [(start, end)]
    gaps: list[tuple[datetime, datetime]] = []
    if points[0] - start > 2 * step:
        gaps.append((start, points[0]))
    for prev, nxt in zip(points, points[1:], strict=False):
        if nxt - prev > 2 * step:
            gaps.append((prev + step, nxt))
    if end - points[-1] > 2 * step:
        gaps.append((points[-1] + step, end))
    return gaps


async def _prom_sample_times(query: str, start: datetime, end: datetime, step_s: int) -> list[float]:
    """The timestamps at which a range query has a sample (any series)."""
    params = {"query": query, "start": str(start.timestamp()), "end": str(end.timestamp()), "step": f"{step_s}s"}
    body = await _query_prometheus("/api/v1/query_range", params)
    if body.get("status") != "success":
        raise _SourceError(f"Prometheus error: {str(body.get('error', 'unknown'))[:120]}")
    times: set[float] = set()
    for series in body.get("data", {}).get("result", []):
        times.update(float(t) for t, _ in series.get("values", []))
    return sorted(times)


async def _restarts(host: str, container: str | None, window: Window) -> Source:
    window_seconds = max(60, int((window.end - window.start).total_seconds()))
    step_s = max(RESTART_STEP_SECONDS, math.ceil(window_seconds / RESTART_MAX_POINTS))
    boot_query = f"max(node_boot_time_seconds{_selector(('hostname', '=', host))})"
    lines: list[str] = []
    detail = ""
    if container is not None:
        series = f"container_start_time_seconds{_selector(('name', '=', container), ('hostname', '=', host))}"
        started, changes, boot, samples = await asyncio.gather(
            _prom_value(f"max({series})", window.end),
            _prom_value(f"sum(changes({series}[{window_seconds}s]))", window.end),
            _prom_value(boot_query, window.end),
            _prom_sample_times(f"max({series})", window.start, window.end, step_s),
        )
        # cAdvisor drops a stopped container's series, so a gap means the container was not running. Its start time
        # is the container's creation: a recreate changes it, a stop and start of the same container does not.
        gaps = find_gaps(samples, window.start, window.end, step_s)
        if gaps == [(window.start, window.end)]:
            lines.append("cAdvisor saw no running container in the whole window")
        else:
            lines.extend(f"cAdvisor saw no running container from {format_hms(a)} to {format_hms(b)}" for a, b in gaps)
        if changes:
            lines.append(f"container start time changed {int(changes)} time(s) in the window")
        if started is None:
            lines.append(f"no cAdvisor series for {container} on {host}")
        elif window.start <= _from_epoch(started) <= window.end:
            lines.append(f"container created {format_hms(_from_epoch(started))}")
        else:
            lines.append(
                f"container start time unchanged since {_format_day(_from_epoch(started))}"
                " (a recreate changes it; a stop and start does not)"
            )
    else:
        boot = await _prom_value(boot_query, window.end)
        detail = "no container mapped; host reboot only"
    if boot is None:
        lines.append(f"no node_boot_time_seconds series for {host}")
    elif window.start <= _from_epoch(boot) <= window.end:
        lines.append(f"host rebooted {format_hms(_from_epoch(boot))}")
    else:
        lines.append(f"host not rebooted (up since {_format_day(_from_epoch(boot))})")
    return Source(name="restarts", status="ok", detail=detail, lines=lines)


async def _proxmox_tasks(node: str, window: Window) -> Source:
    params = {
        "since": str(int(window.start.timestamp())),
        "until": str(int(window.end.timestamp())),
        "limit": str(PROXMOX_TASK_LIMIT),
    }
    body = await _pve_get(f"/nodes/{node}/tasks", params=params)
    raw = body.get("data")
    if not isinstance(raw, list):
        raise _SourceError("unexpected response from Proxmox")
    tasks = [cast(PveTaskEntry, task) for task in raw if isinstance(task, dict)]
    tasks.sort(key=lambda task: task.get("starttime", 0))
    lines = [
        f"{format_hms(_from_epoch(task.get('starttime', 0)))} {task.get('type', '?')}"
        f" guest={task.get('id') or '-'} user={task.get('user', '?')} status={task.get('status') or 'running'}"
        for task in tasks
    ]
    return Source(name="proxmox_tasks", status="ok", detail="" if lines else "no tasks in the window", lines=lines)


async def _logins(host: str, window: Window) -> Source:
    # Login lines are rare (a few sudo lines a week on an LXC), so look back further than the window.
    host_selector = _selector(("hostname", "=", host))
    services = await _loki_label_values("service_name", host_selector, window.end - LOGINS_LOOKBACK, window.end)
    if not _LOGIN_SERVICES.intersection(services):
        detail = f"host shipped no sshd or sudo lines in the {LOGINS_LOOKBACK.days} days before the outage"
        return Source(name="logins", status="skipped", detail=detail)
    query = _selector(("hostname", "=", host), ("service_name", "=~", "sudo|sshd"))
    entries = await _loki_lines(query, window.start, window.end, limit=LOKI_LIMIT, direction="forward")
    lines = [f"{format_hms(e.at)} {e.labels.get('service_name', '?')} {e.text}" for e in entries]
    return Source(name="logins", status="ok", detail="" if lines else "no logins in the window", lines=lines)


async def _deploys(host: str, container: str | None, window: Window, loki: bool, db_path: str) -> Source:
    found: list[tuple[datetime, str]] = []
    notes: list[str] = []
    failed = False
    if container is None:
        notes.append("no container mapped; WUD lines for the whole host, advisor rows not read")
    if loki:
        # Only trigger lines are deploys ("[trigger.dockercompose.deploy] Container x recreated ..."). WUD's watcher
        # also logs a container's state change (`container: "local_dozzle"`) at every stop, which is not a deploy.
        query = (
            _selector(("hostname", "=", host), ("service_name", "=~", "wud|wud-advise"))
            + f" |= {_quote(WUD_DEPLOY_MARKER)}"
        )
        if container is not None:
            query += f" |= {_quote(container)}"
        try:
            entries = await _loki_lines(query, window.start, window.end, limit=LOKI_LIMIT, direction="forward")
            found.extend((e.at, f"{format_hms(e.at)} {e.labels.get('service_name', 'wud')} {e.text}") for e in entries)
        except _EXPECTED_ERRORS as exc:
            failed = True
            notes.append(f"Loki: {_describe(exc)}")
            logger.warning("Evidence source deploys: Loki part failed: %s", _describe(exc))
    else:
        notes.append("Loki not configured")
    if not db_path:
        notes.append("Upgrade Advisor not configured")
    elif container is not None:
        try:
            found.extend(await asyncio.to_thread(_advisor_deploys, db_path, container, window.start, window.end))
        except _EXPECTED_ERRORS as exc:
            failed = True
            notes.append(_describe(exc) if isinstance(exc, sqlite3.Error) else f"advisor DB: {_describe(exc)}")
            logger.warning("Evidence source deploys: advisor DB part failed: %s", _describe(exc))
    found.sort(key=lambda item: item[0])
    return Source(
        name="deploys",
        status="unavailable" if failed else "ok",
        detail="; ".join(notes),
        lines=[text for _, text in found],
    )


async def _guarded(name: str, work: Coroutine[Any, Any, Source]) -> Source:
    """Run one source under its timeout; an expected failure becomes ``unavailable``."""
    try:
        return await asyncio.wait_for(work, SOURCE_TIMEOUT_SECONDS)
    except _EXPECTED_ERRORS as exc:
        reason = _describe(exc)
        logger.warning("Evidence source %s unavailable: %s", name, reason)
        return Source(name=name, status="unavailable", detail=reason)


def _skipped(name: str, detail: str) -> Source:
    return Source(name=name, status="skipped", detail=detail)


async def _done(source: Source) -> Source:
    return source


def _finish(source: Source) -> Source:
    """Apply the per-source line cap and say how much was dropped."""
    lines, dropped = cap_lines(source.lines)
    detail = source.detail
    if dropped:
        detail = "; ".join(part for part in (detail, f"{dropped} more lines not shown") if part)
    return source.model_copy(update={"lines": lines, "detail": detail})


async def collect(incident: Incident, now: datetime, window: tuple[datetime, datetime] | None = None) -> EvidenceBundle:
    """Collect the seven evidence sources for an incident. Never raises; see the module docstring."""
    settings = get_settings()

    # Gatus first: it fixes the window every other source reads.
    gatus_body: dict[str, Any] | None = None
    gatus_problem: str | None = None
    if not settings.gatus_url:
        gatus_problem = "Gatus not configured"
    else:
        try:
            gatus_body = await asyncio.wait_for(
                _fetch_gatus(settings.gatus_url, incident.endpoint_key), SOURCE_TIMEOUT_SECONDS
            )
        except _EXPECTED_ERRORS as exc:
            gatus_problem = _describe(exc)
            logger.warning("Evidence source gatus unavailable: %s", gatus_problem)
    events = parse_status_events(gatus_body.get("events")) if gatus_body is not None else None
    win = derive_window(events, incident, now, override=window)

    notes: list[str] = []
    if gatus_problem:
        notes.append(gatus_problem)
    elif win.first_failure_at is None:
        notes.append("no UNHEALTHY event before the resolve")
    if win.fallback:
        notes.append(win.fallback)
    if win.overridden:
        notes.append("window set by the caller")
    if gatus_body is None:
        status: SourceStatus = "skipped" if not settings.gatus_url else "unavailable"
        gatus = Source(name="gatus", status=status, detail="; ".join(notes))
    else:
        gatus = Source(name="gatus", status="ok", detail="; ".join(notes), lines=_gatus_lines(gatus_body, win))

    proxmox: Coroutine[Any, Any, Source] = (
        _guarded("proxmox_tasks", _proxmox_tasks(settings.proxmox_node, win))
        if settings.proxmox_url
        else _done(_skipped("proxmox_tasks", "Proxmox not configured"))
    )

    others: list[Coroutine[Any, Any, Source]]
    if not incident.host:
        others = [_done(_skipped(name, NO_HOST)) for name in ("docker_events", "container_logs", "restarts")]
        others += [proxmox] + [_done(_skipped(name, NO_HOST)) for name in ("logins", "deploys")]
    else:
        loki_host = loki_hostname(incident.host)
        prom_host = prometheus_hostname(loki_host)
        container = incident.container or None
        no_loki = "Loki not configured"
        no_container = "no container mapped for this check"

        def loki_source(name: str, work: Coroutine[Any, Any, Source] | None) -> Coroutine[Any, Any, Source]:
            if not settings.loki_url:
                if work is not None:
                    work.close()
                return _done(_skipped(name, no_loki))
            if work is None:
                return _done(_skipped(name, no_container))
            return _guarded(name, work)

        others = [
            loki_source("docker_events", _docker_events(loki_host, container, win) if container else None),
            loki_source("container_logs", _container_logs(loki_host, container, win) if container else None),
            _guarded("restarts", _restarts(prom_host, container, win))
            if settings.prometheus_url
            else _done(_skipped("restarts", "Prometheus not configured")),
            proxmox,
            loki_source("logins", _logins(loki_host, win)),
            _guarded(
                "deploys",
                _deploys(loki_host, container, win, bool(settings.loki_url), settings.upgrade_advisor_db_path),
            )
            if settings.loki_url or settings.upgrade_advisor_db_path
            else _done(_skipped("deploys", no_loki)),
        ]

    results = await asyncio.gather(*others, return_exceptions=True)
    sources = [gatus]
    for name, result in zip(SOURCE_NAMES[1:], results, strict=True):
        if isinstance(result, Source):
            sources.append(result)
        elif isinstance(result, Exception):
            # A bug, not an upstream failure: still never fail the whole bundle, but log it loudly.
            logger.error("Evidence source %s crashed", name, exc_info=result)
            sources.append(Source(name=name, status="unavailable", detail=f"internal error ({type(result).__name__})"))
        else:
            raise result
    return EvidenceBundle(
        incident=incident,
        window_start=win.start,
        window_end=win.end,
        first_failure_at=win.first_failure_at,
        sources=[_finish(source) for source in sources],
    )


__all__ = [
    "FALLBACK_SPAN",
    "MAX_LINES_PER_SOURCE",
    "MAX_LINE_CHARS",
    "SOURCE_NAMES",
    "SOURCE_TIMEOUT_SECONDS",
    "TRIGGER_DELAY",
    "WINDOW_LEAD",
    "StatusEvent",
    "Window",
    "cap_lines",
    "collect",
    "derive_window",
    "format_hms",
    "loki_hostname",
    "parse_docker_events",
    "parse_status_events",
    "parse_timestamp",
    "prometheus_hostname",
]
