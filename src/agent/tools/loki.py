"""LangChain tools for querying a Loki instance via its HTTP API.

Provides three tools:
- loki_query_logs: general-purpose LogQL query
- loki_list_label_values: discover available label values (hostnames, services, etc.)
- loki_correlate_changes: higher-level change correlation around a reference time
"""

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from urllib.parse import quote

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.agent.tools._common import ERROR_LEVELS, WARN_LEVELS, format_utc, parse_time, promql_string, regex_literal
from src.config import get_settings

logger = logging.getLogger(__name__)

# --- Constants ---

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_LOG_LINE_LENGTH = 500
MAX_LOG_OUTPUT_CHARS = 60_000
MAX_LABEL_VALUES = 200
_LABEL_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

CORRELATE_ERROR_LIMIT = 200
"""Newest error/warn lines ``loki_correlate_changes`` reads."""
CORRELATE_LIFECYCLE_LIMIT = 100
"""Newest docker-events lines ``loki_correlate_changes`` reads."""

# --- Input schemas ---


class LokiQueryInput(BaseModel):
    """Input for a general-purpose LogQL query."""

    query: str = Field(
        description=(
            "A LogQL query string. Must include a stream selector in curly braces. "
            'Examples: {hostname="media"}, '
            '{service_name="traefik"} |= "error", '
            '{detected_level=~"error|warn"}'
        ),
        min_length=1,
        max_length=2000,
    )
    start: str = Field(
        default="1h",
        description=(
            "Start of the time range. Either a relative duration like '1h', '30m', '2d' or 'now-1h' "
            "(meaning that long ago from now), or an ISO 8601 timestamp. Default: '1h'."
        ),
    )
    end: str = Field(
        default="now",
        description=(
            "End of the time range. Either 'now', a relative duration like '5m' or 'now-5m' "
            "(meaning that long ago from now), or an ISO 8601 timestamp. Default: 'now'."
        ),
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        description=f"Maximum number of log lines to return (1-{MAX_LIMIT}). Default: {DEFAULT_LIMIT}.",
        ge=1,
        le=MAX_LIMIT,
    )
    direction: str = Field(
        default="backward",
        description="Sort order: 'backward' (newest first) or 'forward' (oldest first). Default: 'backward'.",
    )


class LokiLabelValuesInput(BaseModel):
    """Input for listing Loki label values."""

    label: str = Field(
        description=(
            "The label name to list values for. Common labels: "
            "'hostname', 'service_name', 'container', 'detected_level'."
        ),
        min_length=1,
        max_length=200,
        pattern=_LABEL_NAME_PATTERN,
    )
    query: str | None = Field(
        default=None,
        description=(
            "Optional LogQL stream selector to scope results. "
            'Example: {hostname="media"} to list services on the media VM only.'
        ),
    )
    start: str = Field(
        default="6h",
        description=(
            "Start of the time range. Only values with log lines in the range are listed. Either a relative "
            "duration like '6h', '7d' or 'now-6h' (meaning that long ago from now), or an ISO 8601 timestamp. "
            "Default: '6h'."
        ),
    )
    end: str = Field(
        default="now",
        description=(
            "End of the time range. Either 'now', a relative duration like '1d' or 'now-1d' "
            "(meaning that long ago from now), or an ISO 8601 timestamp. Default: 'now'."
        ),
    )


class LokiMetricQueryInput(BaseModel):
    """Input for a LogQL metric query (aggregations that return numbers, not log lines)."""

    query: str = Field(
        description=(
            "A LogQL metric query that returns numeric results. Must use an aggregation "
            "function like count_over_time, rate, sum, topk, etc.\n"
            "Examples:\n"
            '- topk(5, sum by (hostname) (count_over_time({hostname=~".+"}[24h])))\n'
            '- sum by (service_name) (rate({detected_level="error"}[1h]))\n'
            '- count_over_time({hostname="media"}[6h])'
        ),
        min_length=1,
        max_length=2000,
    )
    start: str = Field(
        default="1h",
        description=(
            "Start of the time range (only used for range queries when step is provided). "
            "Relative duration like '1h', '6h', '24h' or ISO 8601 timestamp. Default: '1h'."
        ),
    )
    end: str = Field(
        default="now",
        description=(
            "End of the time range (only used for range queries when step is provided). "
            "'now' or a relative duration or ISO 8601 timestamp. Default: 'now'."
        ),
    )
    step: str | None = Field(
        default=None,
        description=(
            "Evaluation step interval for range queries (e.g. '5m', '1h'). "
            "If provided, uses the range query endpoint and returns a time series. "
            "If omitted, uses the instant query endpoint and returns a single value per series."
        ),
    )


class LokiCorrelateInput(BaseModel):
    """Input for change correlation around a reference time."""

    reference_time: str = Field(
        description=(
            "The point in time to investigate around. "
            "Either 'now' or an ISO 8601 timestamp (e.g. '2024-06-15T14:00:00Z')."
        ),
    )
    window_minutes: int = Field(
        default=30,
        description="How many minutes before the reference time to search. Default: 30.",
        ge=1,
        le=1440,
    )
    hostname: str | None = Field(
        default=None,
        description="Optional hostname filter (e.g. 'infra', 'media').",
    )
    service_name: str | None = Field(
        default=None,
        description="Optional service name filter (e.g. 'traefik', 'jellyfin').",
    )


# --- Loki API response types ---


class LokiStreamValues(TypedDict, total=False):
    """A single stream from a Loki query response."""

    stream: dict[str, str]
    values: list[list[str]]


class LokiQueryData(TypedDict, total=False):
    """The data portion of a Loki query_range response."""

    resultType: str
    result: list[LokiStreamValues]


class LokiQueryResponse(TypedDict, total=False):
    """Top-level Loki query_range response."""

    status: str
    data: LokiQueryData


class LokiVectorSample(TypedDict, total=False):
    """A single instant vector sample from a Loki metric query."""

    metric: dict[str, str]
    value: list[str]  # [timestamp_epoch_str, value_str]


class LokiMatrixSeries(TypedDict, total=False):
    """A single time series from a Loki range metric query."""

    metric: dict[str, str]
    values: list[list[str]]  # [[timestamp_epoch_str, value_str], ...]


class LokiMetricData(TypedDict, total=False):
    """The data portion of a Loki metric query response (vector or matrix)."""

    resultType: str
    result: list[LokiVectorSample] | list[LokiMatrixSeries]


class LokiMetricResponse(TypedDict, total=False):
    """Top-level Loki metric query response."""

    status: str
    data: LokiMetricData


class LokiLabelValuesResponse(TypedDict, total=False):
    """Loki label values response."""

    status: str
    data: list[str]


# --- Time parsing helpers ---

_RELATIVE_TIME_PATTERN = re.compile(r"^(?:now)?-?(?:\d+(?:ms|[smhdw]))+$", re.IGNORECASE)


def _parse_relative_time(time_str: str) -> datetime:
    """Parse 'now', a duration ('1h', '-1h', 'now-1h'), an ISO 8601 timestamp or epoch seconds into a UTC datetime.

    Delegates to ``_common.parse_time``; durations count back from now.
    """
    return parse_time(time_str, datetime.now(tz=UTC))


def _datetime_to_nanoseconds(dt: datetime) -> str:
    """Convert a datetime to Loki's nanosecond epoch string."""
    return str(int(dt.timestamp() * 1_000_000_000))


# --- HTTP helper ---


async def _query_loki(
    endpoint: str,
    params: dict[str, str],
) -> dict[str, object]:
    """Make an HTTP request to the Loki API."""
    url = f"{get_settings().loki_url}{endpoint}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params)
        _ = response.raise_for_status()
        data: dict[str, object] = response.json()  # pyright: ignore[reportAny]
        return data


# --- Formatting helpers ---


def _stream_header(stream_labels: dict[str, str]) -> str:
    """Name a stream once: the well-known labels in a fixed order, else the whole label set."""
    parts = [
        f"{key}={stream_labels[key]}"
        for key in ("hostname", "service_name", "container", "detected_level")
        if key in stream_labels
    ]
    return ", ".join(parts) if parts else str(stream_labels)


def _format_log_lines(data: LokiQueryResponse, limit: int, window: str = "") -> str:
    """Format Loki query results into readable log lines for the LLM.

    Each stream's labels print once as a ``# ...`` header, followed by ``[YYYY-MM-DD HH:MM:SSZ] text`` lines.
    Output stops at ``limit`` lines or ``MAX_LOG_OUTPUT_CHARS`` characters, whichever comes first, and says
    "showing N of M lines".
    ``window`` names the queried range (e.g. 'in the last 1h') for the empty-result text.
    """
    status = data.get("status", "unknown")
    if status != "success":
        return f"Loki query failed with status: {status}"

    results = [stream for stream in data.get("data", {}).get("result", []) if stream.get("values")]
    where = f" {window}" if window else ""

    if not results:
        return (
            f"No log lines found{where}. A valid selector can still be quiet: it matches no log lines in this range. "
            "Widen start (e.g. '24h' or '7d'), or check the label values with loki_list_label_values."
        )

    total_available = sum(len(stream.get("values", [])) for stream in results)
    body: list[str] = []
    shown = 0
    used = 0
    budget_hit = False
    for stream in results:
        if shown >= limit or budget_hit:
            break
        stream_lines: list[str] = []
        header = "# " + _stream_header(stream.get("stream", {}))
        for entry in stream.get("values", []):
            if shown >= limit:
                break
            if len(entry) < 2:
                continue
            ts_ns, log_text = entry[0], entry[1]
            try:
                dt_str = datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")
            except (ValueError, OSError):
                dt_str = ts_ns
            if len(log_text) > MAX_LOG_LINE_LENGTH:
                log_text = log_text[:MAX_LOG_LINE_LENGTH] + "..."
            line = f"[{dt_str}] {log_text}"
            cost = len(line) + 1 + (0 if stream_lines else len(header) + 1)
            if used + cost > MAX_LOG_OUTPUT_CHARS - 400:  # leave room for the summary line
                budget_hit = True
                break
            if not stream_lines:
                stream_lines.append(header)
            stream_lines.append(line)
            used += cost
            shown += 1
        body.extend(stream_lines)

    if budget_hit:
        summary = (
            f"Found {total_available} log lines, showing {shown} of {total_available} lines "
            f"(output capped at {MAX_LOG_OUTPUT_CHARS} characters; use a narrower time range or filter):"
        )
    elif shown >= limit and total_available > shown:
        summary = (
            f"Found {shown} log lines, showing {shown} of {total_available} lines "
            f"(limited to {limit} — use a narrower time range or filter for more specific results):"
        )
    elif shown >= limit:
        summary = (
            f"Found {shown} log lines "
            f"(limited to {limit} — use a narrower time range or filter for more specific results):"
        )
    else:
        summary = f"Found {shown} log lines:"

    return summary + "\n" + "\n".join(body)


def _describe_window(start: str, end: str) -> str:
    """Name a start/end pair as given, e.g. 'in the last 6h' or 'from 7d ago to 1d ago'."""
    if end == "now" and _RELATIVE_TIME_PATTERN.match(start):
        return f"in the last {start.removeprefix('now-').removeprefix('-')}"

    def point(value: str) -> str:
        if value != "now" and _RELATIVE_TIME_PATTERN.match(value):
            return f"{value.removeprefix('now-').removeprefix('-')} ago"
        return value

    return f"from {point(start)} to {point(end)}"


def _format_label_values(values: list[str], label: str, window: str) -> str:
    """Format label values into a readable list. ``window`` names the time range, e.g. 'in the last 6h'."""
    if not values:
        return (
            f"No values found for label '{label}' {window}. "
            "Available labels include: hostname, service_name, container, detected_level. "
            "A value with no log lines in the range is absent: widen start (e.g. '7d') before concluding it ships "
            "no logs."
        )

    sorted_values = sorted(values)
    shown_values = sorted_values[:MAX_LABEL_VALUES]
    if len(sorted_values) > MAX_LABEL_VALUES:
        lines = [
            f"Found {len(sorted_values)} values for label '{label}' {window}, "
            f"showing {MAX_LABEL_VALUES} of {len(sorted_values)} (narrow with the query selector):"
        ]
    else:
        lines = [f"Found {len(sorted_values)} values for label '{label}' {window}:"]
    for val in shown_values:
        lines.append(f"  {val}")

    return "\n".join(lines)


# --- Metric query formatting helpers ---

MAX_METRIC_OUTPUT_LINES = 200


def _format_metric_labels(metric: dict[str, str]) -> str:
    """Format a metric label set into a readable string."""
    if not metric:
        return "{}"
    parts = [f'{k}="{v}"' for k, v in sorted(metric.items())]
    return "{" + ", ".join(parts) + "}"


def _format_vector_results(data: LokiMetricData) -> str:
    """Format instant vector results (label-set + single value)."""
    results: list[LokiVectorSample] = data.get("result", [])  # type: ignore[assignment]

    if not results:
        return (
            "Query returned no results. Check that the metric query and label filters "
            "are correct. Use loki_list_label_values to discover available labels."
        )

    # Sort by value descending (highest first)
    def sort_key(sample: LokiVectorSample) -> float:
        value = sample.get("value", ["0", "0"])
        try:
            return float(value[1]) if len(value) >= 2 else 0.0
        except (ValueError, IndexError):
            return 0.0

    sorted_results = sorted(results, key=sort_key, reverse=True)

    lines: list[str] = [f"Found {len(sorted_results)} series:"]
    lines.append("")

    for sample in sorted_results:
        if len(lines) >= MAX_METRIC_OUTPUT_LINES:
            lines.append(f"... truncated ({len(sorted_results)} total series)")
            break
        metric = sample.get("metric", {})
        value = sample.get("value", ["0", "0"])
        val_str = value[1] if len(value) >= 2 else "?"
        # Format large numbers with commas
        try:
            val_float = float(val_str)
            if val_float == int(val_float) and abs(val_float) < 1e15:
                val_str = f"{int(val_float):,}"
            else:
                val_str = f"{val_float:,.2f}"
        except (ValueError, OverflowError):
            pass
        label_str = _format_metric_labels(metric)
        lines.append(f"  {label_str}: {val_str}")

    return "\n".join(lines)


def _format_matrix_results(data: LokiMetricData) -> str:
    """Format range matrix results (label-set + time series of values)."""
    results: list[LokiMatrixSeries] = data.get("result", [])  # type: ignore[assignment]

    if not results:
        return (
            "Query returned no results. Check that the metric query and label filters "
            "are correct. Use loki_list_label_values to discover available labels."
        )

    lines: list[str] = [f"Found {len(results)} series:"]
    line_count = 1

    for series in results:
        if line_count >= MAX_METRIC_OUTPUT_LINES:
            lines.append(f"... truncated ({len(results)} total series, output limit reached)")
            break

        metric = series.get("metric", {})
        values = series.get("values", [])
        label_str = _format_metric_labels(metric)

        lines.append("")
        lines.append(f"## {label_str} ({len(values)} data points)")
        line_count += 2

        for point in values:
            if line_count >= MAX_METRIC_OUTPUT_LINES:
                remaining = len(values) - values.index(point)
                lines.append(f"  ... {remaining} more data points truncated")
                line_count += 1
                break
            if len(point) >= 2:
                try:
                    ts = float(point[0])
                    dt_str = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OSError):
                    dt_str = point[0]
                val_str = point[1]
                try:
                    val_float = float(val_str)
                    if val_float == int(val_float) and abs(val_float) < 1e15:
                        val_str = f"{int(val_float):,}"
                    else:
                        val_str = f"{val_float:,.2f}"
                except (ValueError, OverflowError):
                    pass
                lines.append(f"  [{dt_str}] {val_str}")
                line_count += 1

    return "\n".join(lines)


def _format_metric_response(data: LokiMetricResponse) -> str:
    """Format a Loki metric query response (auto-detects vector vs matrix)."""
    status = data.get("status", "unknown")
    if status != "success":
        return f"Loki query failed with status: {status}"

    query_data = data.get("data", {})
    result_type = query_data.get("resultType", "unknown")

    if result_type == "vector":
        return _format_vector_results(query_data)
    elif result_type == "matrix":
        return _format_matrix_results(query_data)
    else:
        return f"Unexpected result type '{result_type}'. Expected 'vector' or 'matrix'."


# --- Correlation helpers ---


class _TimelineEvent:
    """A single event in a correlation timeline."""

    __slots__ = ("timestamp", "service", "hostname", "level", "message")

    def __init__(
        self,
        timestamp: datetime,
        service: str,
        hostname: str,
        level: str,
        message: str,
    ) -> None:
        self.timestamp = timestamp
        self.service = service
        self.hostname = hostname
        self.level = level
        self.message = message


_ERROR_AND_WARN_LEVELS = f"{ERROR_LEVELS}|{WARN_LEVELS}"
_DOCKER_EVENTS_SERVICE = "docker-events"
# Container actions that count as lifecycle events; exec_* (health checks, docker exec), create and
# "health_status: healthy" are noise.
_LIFECYCLE_ACTIONS = ("start", "stop", "restart", "die", "kill", "oom", "health_status: unhealthy")
_LIFECYCLE_DETAIL_ATTRIBUTES = ("exitCode", "signal")
_COMPOSE_SERVICE_ATTRIBUTE = "com.docker.compose.service"


class _LokiResponseError(Exception):
    """Loki answered, but not with a usable result. The message is safe to show."""


# Failures a correlation query can hit at runtime. ValueError covers a body that is not JSON.
_EXPECTED_QUERY_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, ValueError, _LokiResponseError)


def _correlation_error_selector(hostname: str | None, service_name: str | None) -> str:
    """The stream selector for error and warning lines, with the filters LogQL-quoted.

    Without a ``service_name`` filter the ``docker-events`` stream is left out: its JSON lines are
    lifecycle events, read by their own query.
    """
    matchers: list[str] = []
    if hostname:
        matchers.append(f"hostname={promql_string(hostname)}")
    if service_name:
        matchers.append(f"service_name={promql_string(service_name)}")
    matchers.append(f"detected_level=~{promql_string(_ERROR_AND_WARN_LEVELS)}")
    if not service_name:
        matchers.append(f"service_name!={promql_string(_DOCKER_EVENTS_SERVICE)}")
    return "{" + ", ".join(matchers) + "}"


def _docker_events_query(hostname: str | None, service_name: str | None) -> str:
    """The LogQL query for container lifecycle events on the ``docker-events`` stream.

    The stream holds ``docker events`` JSON. Loki extracts only ``Type`` and ``Action`` and keeps
    container events with a lifecycle action, so health-check exec events do not use up the line
    limit. A ``service_name`` filter becomes a line filter here;
    :func:`_lifecycle_events_from_response` checks it exactly.
    """
    matchers = [f"service_name={promql_string(_DOCKER_EVENTS_SERVICE)}"]
    if hostname:
        matchers.append(f"hostname={promql_string(hostname)}")
    query = "{" + ", ".join(matchers) + "}"
    if service_name:
        query += f" |= {promql_string(service_name)}"
    actions = "|".join(regex_literal(action) for action in _LIFECYCLE_ACTIONS)
    return query + f' | json Type="Type", Action="Action" | Type="container" | Action=~{promql_string(actions)}'


def _parse_docker_event(text: str) -> tuple[object, str, dict[str, object], datetime | None] | None:
    """``(Type, Action, Actor.Attributes, time)`` from one ``docker events`` JSON line, or None if it is not one.

    The time comes from ``timeNano``, else ``time`` (seconds), else None.
    """
    try:
        event: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    actor: object = event.get("Actor")  # pyright: ignore[reportUnknownMemberType]
    action: object = event.get("Action")  # pyright: ignore[reportUnknownMemberType]
    attributes: object = actor.get("Attributes") if isinstance(actor, dict) else None  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(attributes, dict) or not isinstance(action, str):
        return None
    nano: object = event.get("timeNano")  # pyright: ignore[reportUnknownMemberType]
    seconds: object = event.get("time")  # pyright: ignore[reportUnknownMemberType]
    at: datetime | None = None
    try:
        if isinstance(nano, int) and not isinstance(nano, bool):
            at = datetime.fromtimestamp(nano / 1_000_000_000, tz=UTC)
        elif isinstance(seconds, int | float) and not isinstance(seconds, bool):
            at = datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        at = None  # out of range: the caller falls back to Loki's timestamp
    event_type: object = event.get("Type")  # pyright: ignore[reportUnknownMemberType]
    return event_type, action, {str(k): v for k, v in attributes.items()}, at  # pyright: ignore[reportUnknownVariableType]


def _lifecycle_events_from_response(
    data: LokiQueryResponse, service_name: str | None
) -> tuple[list[_TimelineEvent], int]:
    """Container lifecycle events from a ``docker-events`` query response, and how many lines were not events JSON.

    Keeps ``Type=container`` events whose action is in ``_LIFECYCLE_ACTIONS``, e.g. ``die exitCode=137`` or
    ``kill signal=15``. With ``service_name``, keeps events whose container name or Compose service equals it.
    """
    events: list[_TimelineEvent] = []
    unparseable = 0
    for stream in data.get("data", {}).get("result", []):
        hostname = stream.get("stream", {}).get("hostname", "unknown")
        for entry in stream.get("values", []):
            parsed = _parse_docker_event(entry[1]) if len(entry) >= 2 else None
            if parsed is None:
                unparseable += 1
                continue
            event_type, action, attributes, at = parsed
            if event_type != "container" or action not in _LIFECYCLE_ACTIONS:
                continue
            name = str(attributes.get("name", "unknown"))
            if service_name and service_name not in (name, attributes.get(_COMPOSE_SERVICE_ATTRIBUTE)):
                continue
            if at is None:
                try:
                    at = datetime.fromtimestamp(int(entry[0]) / 1_000_000_000, tz=UTC)
                except (ValueError, OSError):
                    unparseable += 1
                    continue
            message = action
            for key in _LIFECYCLE_DETAIL_ATTRIBUTES:
                if key in attributes:
                    message += f" {key}={attributes[key]}"
            events.append(
                _TimelineEvent(timestamp=at, service=name, hostname=hostname, level="lifecycle", message=message)
            )
    return events, unparseable


def _parse_line_count(data: LokiMetricResponse) -> int:
    """The single value of a ``sum(count_over_time(...))`` instant query; an empty result is 0."""
    status = data.get("status")
    if status != "success":
        raise _LokiResponseError(f"Loki returned status {status!r}")
    try:
        result: list[LokiVectorSample] = data["data"]["result"]  # type: ignore[assignment]
        return int(float(result[0]["value"][1])) if result else 0
    except (KeyError, IndexError, TypeError, ValueError):
        raise _LokiResponseError("Loki returned an unexpected count response") from None


def _failure_reason(exc: BaseException) -> str:
    """A one-line reason for a failed Loki query."""
    if isinstance(exc, httpx.ConnectError):
        return f"Cannot connect to Loki at {get_settings().loki_url}: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Loki query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        body = " ".join(exc.response.text.split())[:300]
        return f"HTTP {exc.response.status_code} - {body}"
    if isinstance(exc, httpx.HTTPError):
        return f"request failed ({type(exc).__name__}: {exc})"
    if isinstance(exc, _LokiResponseError):
        return str(exc)
    if isinstance(exc, ValueError):
        return "Loki returned a body that is not JSON"
    return f"{type(exc).__name__}: {exc}"


def _ensure_success(data: LokiQueryResponse) -> LokiQueryResponse:
    """Return a streams response, or raise ``_LokiResponseError`` when Loki says it failed."""
    status = data.get("status")
    if status != "success":
        raise _LokiResponseError(f"Loki returned status {status!r}")
    return data


def _extract_events_from_response(
    data: LokiQueryResponse,
    max_events: int = 200,
) -> list[_TimelineEvent]:
    """Extract timeline events from a Loki query response."""
    events: list[_TimelineEvent] = []

    query_data = data.get("data", {})
    results = list(query_data.get("result", []))

    for stream in results:
        stream_labels = stream.get("stream", {})
        service = stream_labels.get("service_name", stream_labels.get("container", "unknown"))
        hostname = stream_labels.get("hostname", "unknown")
        level = stream_labels.get("detected_level", "unknown")

        values = stream.get("values", [])
        for entry in values:
            if len(events) >= max_events:
                break
            if len(entry) >= 2:
                ts_ns = entry[0]
                log_text = entry[1]
                try:
                    ts_seconds = int(ts_ns) / 1_000_000_000
                    timestamp = datetime.fromtimestamp(ts_seconds, tz=UTC)
                except (ValueError, OSError):
                    continue

                if len(log_text) > MAX_LOG_LINE_LENGTH:
                    log_text = log_text[:MAX_LOG_LINE_LENGTH] + "..."

                events.append(
                    _TimelineEvent(
                        timestamp=timestamp,
                        service=service,
                        hostname=hostname,
                        level=level,
                        message=log_text,
                    )
                )

    return events


def _build_timeline(events: list[_TimelineEvent]) -> str:
    """Build a chronological timeline grouped by service from timeline events."""
    if not events:
        return "No significant events found in the specified time window."

    # Sort chronologically
    events.sort(key=lambda e: e.timestamp)

    # Group by service
    by_service: dict[str, list[_TimelineEvent]] = defaultdict(list)
    for event in events:
        key = f"{event.hostname}/{event.service}"
        by_service[key].append(event)

    lines: list[str] = [f"Found {len(events)} significant events across {len(by_service)} services:"]
    lines.append("")

    # Chronological timeline
    lines.append("## Chronological Timeline")
    for event in events:
        dt_str = format_utc(event.timestamp)
        lines.append(f"  [{dt_str}] [{event.level.upper():5s}] {event.hostname}/{event.service}: {event.message}")

    lines.append("")

    # Summary by service
    lines.append("## Summary by Service")
    for service_key in sorted(by_service.keys()):
        service_events = by_service[service_key]
        level_counts: dict[str, int] = defaultdict(int)
        for evt in service_events:
            level_counts[evt.level] += 1
        count_str = ", ".join(f"{count} {level}" for level, count in sorted(level_counts.items()))
        lines.append(f"  {service_key}: {len(service_events)} events ({count_str})")

    return "\n".join(lines)


# --- Tool descriptions ---

TOOL_DESCRIPTION_QUERY_LOGS = HOMELAB_CONTEXT + (
    "Query Loki for log lines using LogQL. Use this for general log searches like "
    "'show me recent logs from traefik' or 'what errors occurred on the media VM'.\n\n"
    "This homelab's Loki setup:\n"
    "- Logs are collected by Alloy from Docker containers and some systemd units\n"
    "- Available labels: `hostname`, `service_name`, `container`, `detected_level`\n"
    "- `detected_level` values: debug, info, notice, verbose, trace; warnings are "
    + f"`{WARN_LEVELS}` and errors `{ERROR_LEVELS}`; services spell levels differently, so match the whole set\n"
    "- `hostname` identifies the VM/LXC (e.g. 'media', 'infra', 'jellyfin')\n"
    "- `service_name` identifies the Docker service or systemd unit\n\n"
    "LogQL examples:\n"
    '- `{hostname="media"}` — all logs from the media VM\n'
    '- `{service_name="traefik"}` — all traefik logs\n'
    + f'- `{{detected_level=~"{ERROR_LEVELS}|{WARN_LEVELS}"}}` — all errors and warnings\n'
    '- `{hostname="infra"} |= "connection refused"` — search for a string\n'
    '- `{service_name="jellyfin"} |~ "(?i)transcode"` — case-insensitive regex\n\n'
    "IMPORTANT: Always include at least one label filter in the stream selector. "
    "Do not query `{}` with no labels — this returns all logs and is very slow."
)

TOOL_DESCRIPTION_LABEL_VALUES = HOMELAB_CONTEXT + (
    "List available values for a Loki label. Use this to discover what hostnames, "
    "services, containers, or log levels exist before querying logs.\n\n"
    "Common label lookups:\n"
    "- `hostname` — which VMs/LXCs send logs\n"
    "- `service_name` — which services are logging\n"
    "- `container` — Docker container names\n"
    "- `detected_level` — available log levels\n\n"
    "You can optionally scope results with a stream selector, e.g. "
    'list service_name values where {hostname="media"} to see services on the media VM.\n\n'
    "Only values with log lines in the time range are listed. The range defaults to the last 6h, so a "
    "container that logged nothing in that time is absent. Widen `start` (e.g. '7d') before concluding "
    "that something ships no logs."
)

TOOL_DESCRIPTION_METRIC_QUERY = HOMELAB_CONTEXT + (
    "Run a LogQL metric query against Loki to count, rate, or aggregate logs. "
    "Returns numeric results, NOT log lines. Use this for questions like 'which host has "
    "the most logs?', 'what is the error rate?', 'how many warnings today?'.\n\n"
    "IMPORTANT: count_over_time, rate, sum by, topk are LogQL functions — they run against "
    "Loki, NOT Prometheus. Never send these to prometheus_instant_query.\n\n"
    "LogQL metric query examples:\n"
    '- `topk(5, sum by (hostname) (count_over_time({hostname=~".+"}[24h])))` — '
    "top 5 hosts by log volume in last 24h\n"
    '- `sum by (service_name) (count_over_time({detected_level="error"}[1h]))` — '
    "error count per service in last hour\n"
    '- `sum(rate({hostname="media"}[5m]))` — current log rate for media VM\n'
    '- `sum by (detected_level) (count_over_time({hostname="infra"}[24h]))` — '
    "log volume by level for infra\n\n"
    "If step is omitted, runs an instant query (single value per series). "
    "If step is provided (e.g. '5m', '1h'), runs a range query returning a time series."
)

TOOL_DESCRIPTION_CORRELATE = HOMELAB_CONTEXT + (
    "Search for significant log events in the window_minutes before a reference time. Use this for change "
    "correlation — 'what changed before this alert fired?' or 'what happened around 2pm?'.\n\n"
    "This tool searches for:\n"
    f"- Error and warning lines (detected_level {ERROR_LEVELS}|{WARN_LEVELS}), the newest "
    f"{CORRELATE_ERROR_LIMIT}. The header gives the total, e.g. 'showing newest 200 of 775'.\n"
    "- Container lifecycle events from Docker's event stream: start, stop, restart, die (with exit code), "
    "kill (with signal), oom and unhealthy.\n\n"
    "Returns a chronological UTC timeline and a summary by service. A header line starting 'WARNING:' means "
    "one of the two searches failed and the timeline is incomplete. Use hostname and service_name filters to "
    "narrow results when investigating a specific host or service."
)


# --- Tool functions ---


@tool("loki_query_logs", args_schema=LokiQueryInput)  # pyright: ignore[reportUnknownParameterType]
async def loki_query_logs(
    query: str,
    start: str = "1h",
    end: str = "now",
    limit: int = DEFAULT_LIMIT,
    direction: str = "backward",
) -> str:
    """Query Loki for log lines. See TOOL_DESCRIPTION_QUERY_LOGS."""
    # Validate direction
    if direction not in ("forward", "backward"):
        raise ToolException(f"Invalid direction '{direction}'. Must be 'forward' or 'backward'.")

    # Parse time range
    try:
        start_dt = _parse_relative_time(start)
        end_dt = _parse_relative_time(end)
    except ValueError as e:
        raise ToolException(str(e)) from e

    if end_dt <= start_dt:
        raise ToolException(
            "End time must be after start time. "
            "Note: relative times like '1h' mean '1 hour ago', so start='1h' end='now' is correct."
        )

    params: dict[str, str] = {
        "query": query,
        "start": _datetime_to_nanoseconds(start_dt),
        "end": _datetime_to_nanoseconds(end_dt),
        "limit": str(limit),
        "direction": direction,
    }

    logger.info("Loki query: %s (start=%s, end=%s, limit=%d)", query, start, end, limit)
    try:
        raw_data = await _query_loki("/loki/api/v1/query_range", params)
        data: LokiQueryResponse = raw_data  # type: ignore[assignment]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Loki at {get_settings().loki_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Loki query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Loki API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_log_lines(data, limit, _describe_window(start, end))


loki_query_logs.description = TOOL_DESCRIPTION_QUERY_LOGS
loki_query_logs.handle_tool_error = True


@tool("loki_metric_query", args_schema=LokiMetricQueryInput)  # pyright: ignore[reportUnknownParameterType]
async def loki_metric_query(
    query: str,
    start: str = "1h",
    end: str = "now",
    step: str | None = None,
) -> str:
    """Run a LogQL metric query. See TOOL_DESCRIPTION_METRIC_QUERY."""
    settings = get_settings()

    if step is not None:
        # Range query: evaluate at each step across [start, end]
        try:
            start_dt = _parse_relative_time(start)
            end_dt = _parse_relative_time(end)
        except ValueError as e:
            raise ToolException(str(e)) from e

        if end_dt <= start_dt:
            raise ToolException(
                "End time must be after start time. "
                "Note: relative times like '1h' mean '1 hour ago', "
                "so start='1h' end='now' is correct."
            )

        params: dict[str, str] = {
            "query": query,
            "start": _datetime_to_nanoseconds(start_dt),
            "end": _datetime_to_nanoseconds(end_dt),
            "step": step,
        }
        endpoint = "/loki/api/v1/query_range"
        logger.info(
            "Loki metric range query: %s (start=%s, end=%s, step=%s)",
            query,
            start,
            end,
            step,
        )
    else:
        # Instant query: evaluate at a single point (now)
        params = {"query": query}
        endpoint = "/loki/api/v1/query"
        logger.info("Loki metric instant query: %s", query)

    try:
        raw_data = await _query_loki(endpoint, params)
        data: LokiMetricResponse = raw_data  # type: ignore[assignment]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Loki at {settings.loki_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Loki query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Loki API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    return _format_metric_response(data)


loki_metric_query.description = TOOL_DESCRIPTION_METRIC_QUERY
loki_metric_query.handle_tool_error = True


@tool("loki_list_label_values", args_schema=LokiLabelValuesInput)  # pyright: ignore[reportUnknownParameterType]
async def loki_list_label_values(
    label: str,
    query: str | None = None,
    start: str = "6h",
    end: str = "now",
) -> str:
    """List available values for a Loki label. See TOOL_DESCRIPTION_LABEL_VALUES."""
    if not re.fullmatch(_LABEL_NAME_PATTERN, label):
        raise ToolException(
            f"Invalid label name {label!r}: use letters, digits and underscores only, not starting with a digit "
            "(e.g. 'hostname', 'service_name')."
        )
    try:
        start_dt = _parse_relative_time(start)
        end_dt = _parse_relative_time(end)
    except ValueError as e:
        raise ToolException(str(e)) from e

    if end_dt <= start_dt:
        raise ToolException(
            "End time must be after start time. "
            "Note: relative times like '1h' mean '1 hour ago', so start='1h' end='now' is correct."
        )

    params: dict[str, str] = {
        "start": _datetime_to_nanoseconds(start_dt),
        "end": _datetime_to_nanoseconds(end_dt),
    }
    if query is not None:
        params["query"] = query

    logger.info("Loki label values: label=%s, query=%s (start=%s, end=%s)", label, query, start, end)
    try:
        raw_data = await _query_loki(f"/loki/api/v1/label/{quote(label, safe='')}/values", params)
        data: LokiLabelValuesResponse = raw_data  # type: ignore[assignment]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Loki at {get_settings().loki_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Loki label query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Loki API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    values: list[str] = data.get("data", [])
    return _format_label_values(values, label, _describe_window(start, end))


loki_list_label_values.description = TOOL_DESCRIPTION_LABEL_VALUES
loki_list_label_values.handle_tool_error = True


@tool("loki_correlate_changes", args_schema=LokiCorrelateInput)  # pyright: ignore[reportUnknownParameterType]
async def loki_correlate_changes(
    reference_time: str,
    window_minutes: int = 30,
    hostname: str | None = None,
    service_name: str | None = None,
) -> str:
    """Correlate log events around a reference time. See TOOL_DESCRIPTION_CORRELATE."""
    try:
        ref_dt = _parse_relative_time(reference_time)
    except ValueError as e:
        raise ToolException(str(e)) from e

    window_start = ref_dt - timedelta(minutes=window_minutes)
    start_ns = _datetime_to_nanoseconds(window_start)
    end_ns = _datetime_to_nanoseconds(ref_dt)

    error_selector = _correlation_error_selector(hostname, service_name)
    lifecycle_query = _docker_events_query(hostname, service_name)
    count_query = f"sum(count_over_time({error_selector}[{window_minutes * 60}s]))"

    logger.info(
        "Loki correlation: ref=%s, window=%dm, errors=%s, lifecycle=%s",
        reference_time,
        window_minutes,
        error_selector,
        lifecycle_query,
    )

    def newest_lines(query: str, limit: int) -> dict[str, str]:
        return {"query": query, "start": start_ns, "end": end_ns, "limit": str(limit), "direction": "backward"}

    # Independent queries, run concurrently. Each may fail on its own; unexpected errors still raise.
    results = await asyncio.gather(
        _query_loki("/loki/api/v1/query_range", newest_lines(error_selector, CORRELATE_ERROR_LIMIT)),
        _query_loki("/loki/api/v1/query_range", newest_lines(lifecycle_query, CORRELATE_LIFECYCLE_LIMIT)),
        _query_loki("/loki/api/v1/query", {"query": count_query, "time": end_ns}),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, _EXPECTED_QUERY_ERRORS):
            raise result
    error_raw, lifecycle_raw, count_raw = results

    error_events: list[_TimelineEvent] = []
    error_failure: BaseException | None = None
    try:
        if isinstance(error_raw, BaseException):
            raise error_raw
        error_response = _ensure_success(error_raw)  # type: ignore[arg-type]
        error_events = _extract_events_from_response(error_response, max_events=CORRELATE_ERROR_LIMIT)
    except _EXPECTED_QUERY_ERRORS as e:
        error_failure = e

    lifecycle_events: list[_TimelineEvent] = []
    lifecycle_failure: BaseException | None = None
    lifecycle_lines = 0
    unparseable = 0
    try:
        if isinstance(lifecycle_raw, BaseException):
            raise lifecycle_raw
        lifecycle_response = _ensure_success(lifecycle_raw)  # type: ignore[arg-type]
        lifecycle_lines = sum(len(s.get("values", [])) for s in lifecycle_response.get("data", {}).get("result", []))
        lifecycle_events, unparseable = _lifecycle_events_from_response(lifecycle_response, service_name)
    except _EXPECTED_QUERY_ERRORS as e:
        lifecycle_failure = e

    if error_failure is not None and lifecycle_failure is not None:
        raise ToolException(
            "Both correlation queries failed. "
            + f"error/warn query failed: {_failure_reason(error_failure)}; "
            + f"lifecycle query failed: {_failure_reason(lifecycle_failure)}"
        ) from error_failure

    total_errors: int | None = None
    count_failure = ""
    try:
        if isinstance(count_raw, BaseException):
            raise count_raw
        total_errors = _parse_line_count(count_raw)  # type: ignore[arg-type]
    except _EXPECTED_QUERY_ERRORS as e:
        count_failure = _failure_reason(e)
        logger.warning("Loki correlation line count failed: %s", count_failure)

    filters: list[str] = []
    if hostname:
        filters.append(f"hostname={hostname}")
    if service_name:
        filters.append(f"service_name={service_name}")
    header = [
        f"Time window: {format_utc(window_start)} to {format_utc(ref_dt)} ({window_minutes} minutes)",
        f"Filters: {', '.join(filters)}" if filters else "Filters: none (all hosts/services)",
    ]

    if error_failure is None:
        shown = len(error_events)
        if total_errors is None:
            header.append(
                f"Error/warn lines: {shown}, newest first (limit {CORRELATE_ERROR_LIMIT}); "
                + f"the total count failed: {count_failure}"
            )
        elif shown < total_errors:
            header.append(
                f"Error/warn lines: showing newest {shown} of {total_errors} error/warn lines in the window. "
                + "Narrow with hostname or service_name, or shorten window_minutes, to see older ones."
            )
        else:
            header.append(f"Error/warn lines: {shown} in the window")
    else:
        reason = _failure_reason(error_failure)
        logger.warning("Loki correlation error/warn query failed: %s", reason)
        header.append(f"WARNING: error/warn query failed: {reason}; the timeline is incomplete")

    if lifecycle_failure is None:
        line = f"Container lifecycle events: {len(lifecycle_events)} from docker-events"
        if lifecycle_lines >= CORRELATE_LIFECYCLE_LIMIT:
            line += f" (newest {CORRELATE_LIFECYCLE_LIMIT} lines read; older ones not shown)"
        if unparseable:
            line += f"; {unparseable} docker-events line(s) were not docker events JSON and were skipped"
        header.append(line)
    else:
        reason = _failure_reason(lifecycle_failure)
        logger.warning("Loki correlation lifecycle query failed: %s", reason)
        header.append(f"WARNING: lifecycle query failed: {reason}; the timeline is incomplete")

    timeline = _build_timeline(error_events + lifecycle_events)
    return "\n".join(header) + "\n\n" + timeline


loki_correlate_changes.description = TOOL_DESCRIPTION_CORRELATE
loki_correlate_changes.handle_tool_error = True
