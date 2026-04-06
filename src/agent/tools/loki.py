"""LangChain tools for querying a Loki instance via its HTTP API.

Provides three tools:
- loki_query_logs: general-purpose LogQL query
- loki_list_label_values: discover available label values (hostnames, services, etc.)
- loki_correlate_changes: higher-level change correlation around a reference time
"""

import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.config import get_settings

logger = logging.getLogger(__name__)

# --- Constants ---

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_LOG_LINE_LENGTH = 500

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
            "Start of the time range. Either a relative duration like '1h', '30m', '2d' "
            "(meaning that long ago from now), or an ISO 8601 timestamp. Default: '1h'."
        ),
    )
    end: str = Field(
        default="now",
        description=(
            "End of the time range. Either 'now', a relative duration like '5m' "
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
    )
    query: str | None = Field(
        default=None,
        description=(
            "Optional LogQL stream selector to scope results. "
            'Example: {hostname="media"} to list services on the media VM only.'
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

_RELATIVE_TIME_PATTERN = re.compile(r"^(\d+)([smhdw])$")

_DURATION_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_relative_time(time_str: str) -> datetime:
    """Parse a relative time string like '1h', '30m', '2d' into a datetime.

    Returns a datetime that many seconds in the past from now.
    Also handles 'now' and ISO 8601 timestamps.
    """
    if time_str == "now":
        return datetime.now(tz=UTC)

    match = _RELATIVE_TIME_PATTERN.match(time_str)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        seconds = amount * _DURATION_MULTIPLIERS[unit]
        return datetime.now(tz=UTC) - timedelta(seconds=seconds)

    # Try ISO 8601
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return dt.replace(tzinfo=UTC if dt.tzinfo is None else dt.tzinfo)
    except ValueError:
        raise ValueError(
            f"Cannot parse time '{time_str}'. "
            "Use a relative duration (e.g. '1h', '30m', '2d'), 'now', "
            "or an ISO 8601 timestamp (e.g. '2024-06-15T14:00:00Z')."
        ) from None


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


def _format_log_lines(data: LokiQueryResponse, limit: int) -> str:
    """Format Loki query results into readable log lines for the LLM."""
    status = data.get("status", "unknown")
    if status != "success":
        return f"Loki query failed with status: {status}"

    query_data = data.get("data", {})
    results = list(query_data.get("result", []))

    if not results:
        return (
            "No log lines found. Check that the stream selector labels are correct. "
            "Use loki_list_label_values to discover available hostnames, services, and containers."
        )

    lines: list[str] = []
    total_count = 0

    for stream in results:
        stream_labels = stream.get("stream", {})
        label_parts: list[str] = []
        for key in ("hostname", "service_name", "container", "detected_level"):
            if key in stream_labels:
                label_parts.append(f"{key}={stream_labels[key]}")
        label_str = ", ".join(label_parts) if label_parts else str(stream_labels)

        values = stream.get("values", [])
        for entry in values:
            if total_count >= limit:
                break
            if len(entry) >= 2:
                ts_ns = entry[0]
                log_text = entry[1]
                try:
                    ts_seconds = int(ts_ns) / 1_000_000_000
                    dt_str = datetime.fromtimestamp(ts_seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OSError):
                    dt_str = ts_ns

                # Truncate very long log lines
                if len(log_text) > MAX_LOG_LINE_LENGTH:
                    log_text = log_text[:MAX_LOG_LINE_LENGTH] + "..."

                lines.append(f"[{dt_str}] ({label_str}) {log_text}")
                total_count += 1

        if total_count >= limit:
            break

    header = f"Found {total_count} log lines"
    if total_count >= limit:
        header += f" (limited to {limit} — use a narrower time range or filter for more specific results)"
    header += ":"

    return header + "\n" + "\n".join(lines)


def _format_label_values(values: list[str], label: str) -> str:
    """Format label values into a readable list."""
    if not values:
        return (
            f"No values found for label '{label}'. "
            "Available labels include: hostname, service_name, container, detected_level."
        )

    sorted_values = sorted(values)
    lines = [f"Found {len(sorted_values)} values for label '{label}':"]
    for val in sorted_values:
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


_LIFECYCLE_KEYWORDS = re.compile(
    r"\b(started|stopped|exited|restarting|signal|killed|oom|healthcheck|unhealthy|crashed)\b",
    re.IGNORECASE,
)


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
        dt_str = event.timestamp.strftime("%H:%M:%S")
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
    "- `detected_level` values: debug, info, notice, warn, error, fatal, verbose, trace\n"
    "- `hostname` identifies the VM/LXC (e.g. 'media', 'infra', 'jellyfin')\n"
    "- `service_name` identifies the Docker service or systemd unit\n\n"
    "LogQL examples:\n"
    '- `{hostname="media"}` — all logs from the media VM\n'
    '- `{service_name="traefik"}` — all traefik logs\n'
    '- `{detected_level=~"error|warn"}` — all errors and warnings\n'
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
    'list service_name values where {hostname="media"} to see services on the media VM.'
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
    "Search for significant log events around a reference time. Use this for change "
    "correlation — 'what changed before this alert fired?' or 'what happened around 2pm?'.\n\n"
    "This tool automatically searches for:\n"
    "- Error/warn/fatal log entries in the time window\n"
    "- Container lifecycle events (started, stopped, exited, restarting, crashed, OOM)\n\n"
    "Returns a chronological timeline grouped by service. Use hostname and service_name "
    "filters to narrow results when investigating a specific host or service."
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

    return _format_log_lines(data, limit)


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
) -> str:
    """List available values for a Loki label. See TOOL_DESCRIPTION_LABEL_VALUES."""
    params: dict[str, str] = {}
    if query is not None:
        params["query"] = query

    logger.info("Loki label values: label=%s, query=%s", label, query)
    try:
        raw_data = await _query_loki(f"/loki/api/v1/label/{label}/values", params)
        data: LokiLabelValuesResponse = raw_data  # type: ignore[assignment]
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Loki at {get_settings().loki_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Loki label query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Loki API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    values: list[str] = data.get("data", [])
    return _format_label_values(values, label)


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
    # Parse reference time
    try:
        ref_dt = _parse_relative_time(reference_time)
    except ValueError as e:
        raise ToolException(str(e)) from e

    window_start = ref_dt - timedelta(minutes=window_minutes)
    start_ns = _datetime_to_nanoseconds(window_start)
    end_ns = _datetime_to_nanoseconds(ref_dt)

    # Build label selector — Loki requires at least one label matcher in
    # the stream selector.  When no filters are provided, match all hosts.
    label_filters: list[str] = []
    if hostname:
        label_filters.append(f'hostname="{hostname}"')
    if service_name:
        label_filters.append(f'service_name="{service_name}"')

    base_selector = "{" + ", ".join(label_filters) + "}" if label_filters else '{hostname=~".+"}'

    all_events: list[_TimelineEvent] = []

    # Query 1: Error/warn/fatal logs
    error_selector = base_selector.rstrip("}")
    if label_filters:
        error_selector += ', detected_level=~"error|warn|fatal"}'
    else:
        error_selector = '{detected_level=~"error|warn|fatal"}'

    logger.info(
        "Loki correlation: ref=%s, window=%dm, selector=%s",
        reference_time,
        window_minutes,
        error_selector,
    )

    try:
        error_data = await _query_loki(
            "/loki/api/v1/query_range",
            {
                "query": error_selector,
                "start": start_ns,
                "end": end_ns,
                "limit": "200",
                "direction": "forward",
            },
        )
        error_response: LokiQueryResponse = error_data  # type: ignore[assignment]
        all_events.extend(_extract_events_from_response(error_response))
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
        logger.warning("Loki correlation error query failed: %s", e)

    # Query 2: Container lifecycle events (search all levels for lifecycle keywords)
    lifecycle_filter = ' |~ "(?i)(started|stopped|exited|restarting|signal|killed|oom|healthcheck|unhealthy|crashed)"'
    lifecycle_query = base_selector + lifecycle_filter

    try:
        lifecycle_data = await _query_loki(
            "/loki/api/v1/query_range",
            {
                "query": lifecycle_query,
                "start": start_ns,
                "end": end_ns,
                "limit": "100",
                "direction": "forward",
            },
        )
        lifecycle_response: LokiQueryResponse = lifecycle_data  # type: ignore[assignment]
        lifecycle_events = _extract_events_from_response(lifecycle_response)

        # Only add lifecycle events that aren't already in error events (deduplicate)
        existing_keys = {(e.timestamp, e.service, e.message) for e in all_events}
        for evt in lifecycle_events:
            key = (evt.timestamp, evt.service, evt.message)
            if key not in existing_keys:
                all_events.append(evt)
                existing_keys.add(key)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
        logger.warning("Loki correlation lifecycle query failed: %s", e)

    # If both queries failed, raise an error
    if not all_events and len(all_events) == 0:
        # Check if we got here because queries failed
        try:
            # Test connectivity with a simple query
            await _query_loki("/ready", {})
        except httpx.ConnectError as e:
            raise ToolException(f"Cannot connect to Loki at {get_settings().loki_url}: {e}") from e
        except httpx.TimeoutException as e:
            raise ToolException(f"Loki query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
        except (httpx.HTTPStatusError, Exception):
            pass  # Loki is reachable but /ready may not exist — that's fine

    window_info = (
        f"Time window: {window_start.strftime('%Y-%m-%d %H:%M:%S')} "
        f"to {ref_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"({window_minutes} minutes)"
    )
    filters = []
    if hostname:
        filters.append(f"hostname={hostname}")
    if service_name:
        filters.append(f"service_name={service_name}")
    filter_info = f"Filters: {', '.join(filters)}" if filters else "Filters: none (all hosts/services)"

    timeline = _build_timeline(all_events)
    return f"{window_info}\n{filter_info}\n\n{timeline}"


loki_correlate_changes.description = TOOL_DESCRIPTION_CORRELATE
loki_correlate_changes.handle_tool_error = True
