"""Docker container state across hosts, from the container-status exporter and cadvisor.

The exporter runs on one host and reports every Portainer-managed container. Prometheus keeps
the exporter's own host in ``hostname`` (``"infra"`` on every series) and moves the container's
host to ``exported_hostname`` (``"media vm"``, ``"jellyfin lxc"``). So this module never filters on
``hostname`` for exporter series: it keys each container on
``(host_key(exported_hostname or hostname), container_name)`` and joins cadvisor's
``(host_key(hostname), name)`` to that key.

``collect_containers`` returns structured records for other tools (``service_health``);
``container_status`` formats them for clients.
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT, prometheus
from src.agent.tools._common import format_age, format_utc, host_key

logger = logging.getLogger(__name__)

MAX_CONTAINERS_SHOWN = 150
STALE_AFTER_SECONDS = 300  # the exporter collects every 30s; 5 minutes without one means it is stuck
DEFAULT_RESTART_WINDOW_SECONDS = 86400  # container_status always reports restarts over 24h

_EXPORTER = '{job="container-status"}'


def queries(restart_window_seconds: int = DEFAULT_RESTART_WINDOW_SECONDS) -> dict[str, str]:
    """The instant queries ``collect_containers`` runs concurrently, by source key.

    ``restarts_24h`` counts restarts over ``restart_window_seconds`` (24h unless a caller such as
    ``service_health`` passes its own window).
    """
    return {
        "state": f"container_state{_EXPORTER}",
        "health": f"container_health{_EXPORTER}",
        "restarts": f"container_restart_count{_EXPORTER}",
        "restarts_24h": f"increase(container_restart_count{_EXPORTER}[{restart_window_seconds}s])",
        "outdated": f"container_image_outdated{_EXPORTER}",
        "image_info": f"container_image_info{_EXPORTER}",
        "start_time": 'container_start_time_seconds{name!=""}',
        "exporter_up": f"up{_EXPORTER}",
        "last_collection": f"portainer_exporter_last_scrape_timestamp{_EXPORTER}",
    }


QUERIES: dict[str, str] = queries()
"""The default queries, with the 24h restart window ``container_status`` uses."""

SOURCE_LABELS: dict[str, str] = {
    "state": "container_state",
    "health": "container_health",
    "restarts": "container_restart_count",
    "restarts_24h": "restarts in 24h",
    "outdated": "container_image_outdated",
    "image_info": "container_image_info",
    "start_time": "start time (cadvisor)",
    "exporter_up": "exporter up",
    "last_collection": "exporter last collection",
}
"""How each source is named in output, e.g. ``start time (cadvisor): unavailable: HTTP 500``."""

_STATES: dict[float, str] = {
    0: "exited",
    1: "running",
    2: "paused",
    3: "created",
    4: "restarting",
    5: "dead",
    6: "unknown",
}
_HEALTH: dict[float, str | None] = {0: None, 1: "healthy", 2: "unhealthy", 3: "starting"}

type Sample = tuple[dict[str, str], float]
"""One instant-vector sample: its labels and value."""

type _Key = tuple[str, str]


class ContainerStatusInput(BaseModel):
    """Input for container_status."""

    host: str | None = Field(
        default=None,
        description=(
            "Only containers on this host, e.g. 'media', 'media vm' or 'jellyfin'. Matched loosely: case, "
            "punctuation and a trailing ' vm', ' lxc' or ' ve' are ignored. An unknown host lists the known ones."
        ),
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9 ._-]+$",
    )
    name: str | None = Field(
        default=None,
        description="Only containers whose name contains this text (case-insensitive), e.g. 'jellyfin'.",
        min_length=1,
        max_length=128,
    )
    only_problems: bool = Field(
        default=False,
        description=(
            "List only containers with a problem: not running, unhealthy, restarted in the last 24h, "
            "or running an outdated image."
        ),
    )


@dataclass
class ContainerRecord:
    """One container as the exporter reports it, with its cadvisor start time when known.

    ``host`` is the exporter's spelling (``"media vm"``). A field is ``None`` when its source has no
    series for the container or failed (see ``ContainerReport.unavailable``). ``health`` is ``None``
    for a container without a health check.
    """

    host: str
    name: str
    image: str
    state: str
    health: str | None = None
    restart_count: int | None = None
    restarts_24h: int | None = None  # over the report's restart_window_seconds (24h by default)
    outdated: bool | None = None
    image_status: str | None = None
    current_version: str | None = None
    available_version: str | None = None
    started_at: float | None = None

    @property
    def problems(self) -> list[str]:
        """Why this container needs attention; empty when it is running, not unhealthy, not restarted, current."""
        found: list[str] = []
        if self.state != "running":
            found.append("not running")
        if self.health == "unhealthy":
            found.append("unhealthy")
        if self.restarts_24h:
            found.append(f"{self.restarts_24h} restart{'s' if self.restarts_24h != 1 else ''} in 24h")
        if self.outdated:
            found.append("outdated image")
        return found


@dataclass
class ExporterStatus:
    """Whether the container-status exporter's data is current.

    ``up`` is the lowest ``up{job="container-status"}`` value (``None`` without a series);
    ``last_collection`` is the exporter's last successful collection as Unix seconds. ``stale`` is
    true when ``up`` is 0 or has no series, or the last collection is missing or over 5 minutes old;
    ``reasons`` says which.
    """

    up: float | None
    last_collection: float | None
    stale: bool
    reasons: list[str]


@dataclass
class ContainerReport:
    """What ``collect_containers`` found.

    - ``containers``: the containers matching the filters, problems first, then by host and name.
    - ``known_hosts``: every host the exporter reports, in its spelling, sorted.
    - ``unknown_host``: a host filter was given and matches no known host.
    - ``unavailable``: failed sources, ``{source key: reason}``; keys are those of ``QUERIES``.
    - ``exporter``: whether the data is current.
    - ``restart_window_seconds``: the window ``ContainerRecord.restarts_24h`` counts over.
    """

    containers: list[ContainerRecord]
    known_hosts: list[str]
    unknown_host: bool
    unavailable: dict[str, str]
    exporter: ExporterStatus
    host_filter: str | None = None
    name_filter: str | None = None
    restart_window_seconds: int = DEFAULT_RESTART_WINDOW_SECONDS
    problem_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.problem_count = sum(1 for record in self.containers if record.problems)

    @property
    def state_available(self) -> bool:
        """False when ``container_state`` failed, so ``containers`` is empty for that reason, not by filter."""
        return "state" not in self.unavailable


class _PrometheusResultError(Exception):
    """Prometheus answered, but not with a usable instant vector. The message is safe to show."""


# Failures one source can hit at runtime; anything else is a bug and propagates. ValueError: a body that is not JSON.
_EXPECTED_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, ValueError, _PrometheusResultError)


def _samples(data: object) -> list[Sample]:
    """The samples of an instant-query response; raises ``_PrometheusResultError`` for anything else."""
    if not isinstance(data, dict):
        raise _PrometheusResultError("Prometheus returned an unexpected response")
    if data.get("status") != "success":
        raise _PrometheusResultError(f"Prometheus error: {data.get('error', 'unknown error')}")
    try:
        result = data["data"]["result"]
        return [({str(k): str(v) for k, v in item["metric"].items()}, float(item["value"][1])) for item in result]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise _PrometheusResultError("Prometheus returned an unexpected instant-vector shape") from None


async def _fetch(promql: str) -> list[Sample]:
    data = await prometheus._query_prometheus("/api/v1/query", {"query": promql})  # pyright: ignore[reportPrivateUsage]
    return _samples(data)


def _failure_reason(exc: BaseException) -> str:
    """A one-line reason for a failed source."""
    if isinstance(exc, httpx.ConnectError):
        return f"cannot connect to Prometheus: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out after {prometheus.DEFAULT_TIMEOUT_SECONDS}s"
    if isinstance(exc, httpx.HTTPStatusError):
        body = " ".join(exc.response.text.split())[:300]
        return f"HTTP {exc.response.status_code} - {body}"
    if isinstance(exc, httpx.HTTPError):
        return f"request failed ({type(exc).__name__}: {exc})"
    if isinstance(exc, _PrometheusResultError):
        return str(exc)
    if isinstance(exc, ValueError):
        return "Prometheus returned a body that is not JSON"
    return f"{type(exc).__name__}: {exc}"  # pragma: no cover - unexpected types propagate before this


def _host_label(labels: dict[str, str]) -> str:
    """The container's host: ``exported_hostname`` when Prometheus moved it there, else ``hostname``."""
    return labels.get("exported_hostname") or labels.get("hostname", "")


def _key(labels: dict[str, str], name_label: str) -> _Key | None:
    name = labels.get(name_label, "")
    return (host_key(_host_label(labels)), name) if name else None


def _by_key(samples: list[Sample], name_label: str = "container_name") -> dict[_Key, tuple[dict[str, str], float]]:
    """Index finite samples by container key; the larger value wins on a duplicate key."""
    out: dict[_Key, tuple[dict[str, str], float]] = {}
    for labels, value in samples:
        key = _key(labels, name_label)
        if key is None or not math.isfinite(value):
            continue
        if key not in out or value > out[key][1]:
            out[key] = (labels, value)
    return out


def _exporter_status(
    results: dict[str, list[Sample] | BaseException], unavailable: dict[str, str], now: datetime
) -> ExporterStatus:
    up_samples = _ok(results, "exporter_up")
    up = min((value for _, value in up_samples), default=None)
    collected = [value for _, value in _ok(results, "last_collection") if math.isfinite(value) and value > 0]
    last = max(collected, default=None)

    reasons: list[str] = []
    if up is not None and up < 1:
        reasons.append("up=0")
    elif up is None and "exporter_up" not in unavailable:
        reasons.append('no up series for job="container-status"')
    if last is not None:
        if now.timestamp() - last > STALE_AFTER_SECONDS:
            reasons.append(f"last collection {format_utc(last)} ({format_age(last, now)})")
    elif "last_collection" not in unavailable:
        reasons.append("no last-collection timestamp")
    return ExporterStatus(up=up, last_collection=last, stale=bool(reasons), reasons=reasons)


def _ok(results: dict[str, list[Sample] | BaseException], key: str) -> list[Sample]:
    value = results.get(key, [])
    return [] if isinstance(value, BaseException) else value


def build_report(
    results: dict[str, list[Sample] | BaseException], host: str | None, name: str | None, now: datetime
) -> ContainerReport:
    """Join the per-source samples into records and apply the host and name filters. Pure: no IO.

    ``results`` maps each ``QUERIES`` key to its samples, or to the exception that source raised.
    """
    unavailable = {key: _failure_reason(value) for key, value in results.items() if isinstance(value, BaseException)}

    records: dict[_Key, ContainerRecord] = {}
    for labels, value in _ok(results, "state"):
        key = _key(labels, "container_name")
        if key is None:
            continue
        state = _STATES.get(value, "unknown") if math.isfinite(value) else "unknown"
        if key in records and records[key].state == "running":
            continue  # a stale duplicate (e.g. the old image's series) never hides the running one
        records[key] = ContainerRecord(
            host=_host_label(labels), name=key[1], image=labels.get("image", ""), state=state
        )

    for key, (_, value) in _by_key(_ok(results, "health")).items():
        if key in records:
            records[key].health = _HEALTH.get(value)
    for key, (_, value) in _by_key(_ok(results, "restarts")).items():
        if key in records:
            records[key].restart_count = round(value)
    for key, (_, value) in _by_key(_ok(results, "restarts_24h")).items():
        if key in records:
            records[key].restarts_24h = max(0, round(value))  # increase() extrapolates: 2.04 means 2
    for key, (_, value) in _by_key(_ok(results, "outdated")).items():
        if key in records:
            records[key].outdated = value >= 1
    for key, (labels, _) in _by_key(_ok(results, "image_info")).items():
        if key in records:
            records[key].image_status = labels.get("status") or None
            records[key].current_version = labels.get("current_version") or None
            records[key].available_version = labels.get("available_version") or None
    for key, (_, value) in _by_key(_ok(results, "start_time"), name_label="name").items():
        if key in records and value > 0:
            records[key].started_at = value

    known_hosts = sorted({record.host for record in records.values()})
    wanted_host = host_key(host) if host else None
    unknown_host = wanted_host is not None and bool(records) and all(key[0] != wanted_host for key in records)
    needle = name.lower() if name else None
    matching = [
        record
        for key, record in records.items()
        if (wanted_host is None or key[0] == wanted_host) and (needle is None or needle in record.name.lower())
    ]
    matching.sort(key=lambda r: (not r.problems, r.host, r.name))

    return ContainerReport(
        containers=matching,
        known_hosts=known_hosts,
        unknown_host=unknown_host,
        unavailable=unavailable,
        exporter=_exporter_status(results, unavailable, now),
        host_filter=host,
        name_filter=name,
    )


async def collect_containers(
    host: str | None = None,
    name: str | None = None,
    *,
    now: datetime | None = None,
    restart_window_seconds: int = DEFAULT_RESTART_WINDOW_SECONDS,
) -> ContainerReport:
    """Container state for every host, or for ``host`` and/or containers whose name contains ``name``.

    Runs the ``QUERIES`` instant queries concurrently. A source that fails with an HTTP or response
    error is listed in ``report.unavailable`` (and logged at WARNING) and the others still fill the
    records; any other exception type propagates. Check ``report.state_available``: when
    ``container_state`` failed, ``containers`` is empty because nothing could be listed.

    ``host`` matches through ``host_key`` (``"media"`` finds ``"media vm"``); ``name`` is a
    case-insensitive substring of the container name. ``now`` defaults to the current UTC time.
    ``restart_window_seconds`` sets the window ``restarts_24h`` counts over (default 24h).
    """
    if restart_window_seconds < 1:
        raise ValueError(f"restart_window_seconds must be at least 1, got {restart_window_seconds}")
    promql = queries(restart_window_seconds)
    keys = list(promql)
    raw = await asyncio.gather(*(_fetch(promql[key]) for key in keys), return_exceptions=True)
    for result in raw:
        if isinstance(result, BaseException) and not isinstance(result, _EXPECTED_ERRORS):
            raise result
    report = build_report(dict(zip(keys, raw, strict=True)), host, name, now or datetime.now(UTC))
    report.restart_window_seconds = restart_window_seconds
    for key, reason in report.unavailable.items():
        logger.warning("container_status source %s unavailable: %s", SOURCE_LABELS[key], reason)
    return report


def _container_line(record: ContainerRecord, now: datetime) -> str:
    parts = [record.state + (f", {record.health}" if record.health else "")]
    if record.restart_count is not None:
        restarts = f"restarts {record.restart_count}"
        if record.restarts_24h:
            restarts += f" (+{record.restarts_24h} in 24h)"
        parts.append(restarts)
    if record.started_at is not None:
        parts.append(f"started {format_utc(record.started_at)} ({format_age(record.started_at, now)})")
    image = f"image {record.image or 'unknown'}"
    if record.outdated and record.current_version and record.available_version:
        image += f" ({record.current_version} -> {record.available_version} available)"
    elif record.outdated:
        image += " (newer image available)"
    elif record.current_version:
        image += f" ({record.current_version})"
    parts.append(image)
    problems = record.problems
    prefix = f"PROBLEM ({', '.join(problems)}) " if problems else ""
    return f"{prefix}{record.host}/{record.name}: " + ", ".join(parts)


def _exporter_line(exporter: ExporterStatus, now: datetime) -> str:
    if exporter.stale:
        return f"Exporter: STALE ({'; '.join(exporter.reasons)}); container states may be out of date."
    text = "Exporter: up" if exporter.up is not None else "Exporter: up unknown"
    if exporter.last_collection is not None:
        last = exporter.last_collection
        text += f", last collection {format_utc(last)} ({format_age(last, now)})"
    return text + "."


def _containers(count: int) -> str:
    return f"{count} container{'s' if count != 1 else ''}"


def _scope(report: ContainerReport) -> str:
    parts: list[str] = []
    if report.host_filter:
        parts.append(f"host '{report.host_filter}'")
    if report.name_filter:
        parts.append(f"name '{report.name_filter}'")
    return " and ".join(parts)


def format_report(report: ContainerReport, only_problems: bool, now: datetime) -> str:
    """Render a report: summary, exporter state, failed sources, then one line per container, problems first."""
    scope = _scope(report)
    total = len(report.containers)
    shown = [r for r in report.containers if r.problems] if only_problems else report.containers

    if report.unknown_host:
        summary = f"Unknown host '{report.host_filter}'. Known hosts: {', '.join(report.known_hosts)}."
    elif not report.known_hosts:
        summary = "No container-status series found."
    elif total == 0:
        summary = f"No containers match {scope}."
    elif only_problems and not shown:
        summary = f"No container problems among {_containers(total)}" + (f" ({scope})." if scope else ".")
    elif only_problems:
        summary = f"{len(shown)} of {_containers(total)} with problems" + (f" ({scope})." if scope else ".")
    else:
        summary = _containers(total) + (f" ({scope})" if scope else "") + f", {report.problem_count} with problems."

    lines = [summary, _exporter_line(report.exporter, now)]
    lines.extend(f"{SOURCE_LABELS[key]}: unavailable: {reason}" for key, reason in report.unavailable.items())
    if report.unknown_host:
        return "\n".join(lines)
    if len(shown) > MAX_CONTAINERS_SHOWN:
        lines.append(
            f"(showing {MAX_CONTAINERS_SHOWN} of {len(shown)} containers; filter by host or name to see the rest)"
        )
    lines.extend(_container_line(record, now) for record in shown[:MAX_CONTAINERS_SHOWN])
    return "\n".join(lines)


TOOL_DESCRIPTION = HOMELAB_CONTEXT + (
    "Report Docker container state across hosts: running or not, health, restarts (total and in the last 24h), "
    "start time, image and whether a newer image is available. Use this instead of ssh 'docker ps' or a raw "
    "container_state query. Problems print first.\n\n"
    "Filter with host (e.g. 'media', 'jellyfin'; 'media vm' works too) and/or name (substring of the container "
    "name). only_problems=true lists only containers that are not running, unhealthy, restarted in 24h or "
    "outdated. An unknown host lists the known hosts. If a data source fails its line says 'unavailable' and the "
    "rest still prints; a stale exporter is flagged."
)


@tool("container_status", args_schema=ContainerStatusInput)
async def container_status(host: str | None = None, name: str | None = None, only_problems: bool = False) -> str:
    """Report Docker container state across hosts. See TOOL_DESCRIPTION."""
    logger.info("Container status: host=%s name=%s only_problems=%s", host, name, only_problems)
    now = datetime.now(UTC)
    report = await collect_containers(host=host, name=name, now=now)
    if not report.state_available:
        failures = "; ".join(
            f"{SOURCE_LABELS[key]}: unavailable: {reason}" for key, reason in report.unavailable.items()
        )
        raise ToolException(f"Cannot list containers. {failures}")
    return format_report(report, only_problems, now)


container_status.description = TOOL_DESCRIPTION
container_status.handle_tool_error = True
