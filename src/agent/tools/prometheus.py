"""LangChain tool for querying a Prometheus instance via its HTTP API."""

import contextlib
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools._common import format_utc, parse_duration, parse_time, promql_string, regex_literal
from src.config import get_settings

logger = logging.getLogger(__name__)

# --- Input validation ---

MAX_QUERY_LENGTH = 2000
MAX_RANGE_STEP_SECONDS = 86400  # 1 day
MAX_RANGE_DURATION_SECONDS = 90 * 86400  # 90 days, the Prometheus retention
MAX_RANGE_POINTS = 11000  # Prometheus rejects (end - start) / step above this
AUTO_STEP_TARGET_POINTS = 250
AUTO_STEP_LADDER_SECONDS = (15, 60, 300, 900, 3600, 21600, 86400)  # 15s 1m 5m 15m 1h 6h 1d
MAX_SERIES_SHOWN = 50
MAX_SEARCH_RESULTS = 50
DEFAULT_TIMEOUT_SECONDS = 15

_STEP_AND_LIMIT_FORMS = (
    "step: a duration like '30s', '5m' or '1h30m', or seconds, at most 1d; omit it to choose one "
    + "automatically. Range limit: 90 days (Prometheus retention)."
)
ACCEPTED_FORMS = (
    "Accepted time forms: 'now'; a duration before now as '24h', '-24h' or 'now-24h' "
    + "(units s, m, h, d, w; compound like '1h30m'); RFC3339 like '2026-11-11T13:00:00Z'; "
    + f"or Unix seconds like '1794369609'. {_STEP_AND_LIMIT_FORMS}"
)
"""Appended to every range validation error, so a client can correct the call in one retry."""

_TIME_FIELD_HELP = "'now', a duration before now ('24h', '-24h', 'now-24h'), RFC3339 or Unix seconds"


class PrometheusInstantInput(BaseModel):
    """Input for an instant PromQL query (single point in time)."""

    query: str = Field(
        description=(
            "A valid PromQL query string. Always include label filters "
            "(e.g. {hostname='jellyfin'}) to avoid returning thousands of time series."
        ),
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
    )
    time: str | None = Field(
        default=None,
        description=f"Optional evaluation time: {_TIME_FIELD_HELP}. Defaults to now.",
    )


class PrometheusSearchInput(BaseModel):
    """Input for searching available Prometheus metric names."""

    search_term: str = Field(
        description=(
            "A substring to search for in metric names. "
            "For example 'mktxp' to find MikroTik metrics, "
            "'node_cpu' to find CPU metrics, or 'container_memory' for container memory metrics."
        ),
        min_length=1,
        max_length=200,
    )


class PrometheusRangeInput(BaseModel):
    """Input for a range PromQL query (values over a time window)."""

    query: str = Field(
        description=(
            "A valid PromQL query string. Always include label filters "
            "(e.g. {hostname='jellyfin'}) to avoid returning thousands of time series."
        ),
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
    )
    start: str = Field(
        default="1h",
        description=f"Start time: {_TIME_FIELD_HELP}. Defaults to '1h' (one hour ago). At most 90 days before end.",
    )
    end: str = Field(
        default="now",
        description=f"End time, after start: {_TIME_FIELD_HELP}. Defaults to 'now'.",
    )
    step: str | None = Field(
        default=None,
        description=(
            "Resolution step as a duration ('15s', '5m', '1h30m') or seconds, at most 1d. "
            "Omit it to get about 250 points per series (15s, 1m, 5m, 15m, 1h, 6h or 1d)."
        ),
    )


@dataclass(frozen=True)
class RangeWindow:
    """A validated range query window, all values in seconds (start/end are Unix epoch)."""

    start: float
    end: float
    step: float


def _parse_timestamp(ts: str, now: datetime) -> float:
    """Parse any accepted time form to Unix epoch seconds; relative forms count back from ``now``.

    Raises ``ValueError`` listing every accepted form.
    """
    return parse_time(ts, now).timestamp()


def _parse_duration(step: str) -> float | None:
    """Parse a Prometheus duration ('60s', '5m', '1h30m' or plain seconds) to seconds; None if unparseable."""
    try:
        seconds = float(parse_duration(step))
    except (ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) else None


def _format_seconds(seconds: float) -> str:
    """Seconds as a compact duration that Prometheus and ``parse_duration`` accept: 55 -> '55s', 5400 -> '1h30m'."""
    remaining = int(math.ceil(seconds))
    parts: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


def _auto_step(duration: float) -> int:
    """Step for about ``AUTO_STEP_TARGET_POINTS`` points: ``max(15s, duration/250)`` rounded up the ladder."""
    target = max(AUTO_STEP_LADDER_SECONDS[0], duration / AUTO_STEP_TARGET_POINTS)
    for step in AUTO_STEP_LADDER_SECONDS:
        if step >= target:
            return step
    return AUTO_STEP_LADDER_SECONDS[-1]


def _number(value: float) -> str:
    """A float as a query parameter: '1700000000' for whole seconds, else millisecond precision."""
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"


def _resolve_range(start: str, end: str, step: str | None, now: datetime) -> RangeWindow:
    """Validate range parameters and resolve them to epoch seconds and a step in seconds.

    Raises ``ValueError`` naming every problem found, followed by the accepted forms.
    """
    errors: list[str] = []
    try:
        start_ts = _parse_timestamp(start, now)
        end_ts = _parse_timestamp(end, now)
    except ValueError as e:  # parse_time's message already lists the time forms
        raise ValueError(f"{e}. {_STEP_AND_LIMIT_FORMS}") from None

    duration = end_ts - start_ts
    if duration <= 0:
        errors.append(f"end must be after start (start={format_utc(start_ts)}, end={format_utc(end_ts)})")
    if duration > MAX_RANGE_DURATION_SECONDS:
        errors.append(
            f"Time range too large: {duration / 86400:.1f} days exceeds the maximum of 90 days "
            + "(Prometheus retention)"
        )

    step_seconds: float | None
    if step is None:
        step_seconds = float(_auto_step(max(duration, 0)))
    else:
        step_seconds = _parse_duration(step)
        if step_seconds is None or step_seconds <= 0:
            errors.append(f"Invalid step {step!r}")
            step_seconds = None
        elif step_seconds > MAX_RANGE_STEP_SECONDS:
            errors.append(f"Step too large: {_format_seconds(step_seconds)} exceeds the maximum of 1d")

    if step_seconds is not None and duration > 0 and duration / step_seconds > MAX_RANGE_POINTS:
        points = int(duration / step_seconds)
        smallest = _format_seconds(duration / MAX_RANGE_POINTS)
        errors.append(
            f"Too many data points: a {_format_seconds(duration)} range at a {_format_seconds(step_seconds)} step "
            + f"gives {points} points, over the {MAX_RANGE_POINTS} limit. Use step='{smallest}' or larger, "
            + "or omit step to choose one automatically"
        )

    if errors or step_seconds is None:
        raise ValueError(f"{'; '.join(errors)}. {ACCEPTED_FORMS}")
    return RangeWindow(start=start_ts, end=end_ts, step=step_seconds)


# --- Prometheus response types ---


class PrometheusSeries(TypedDict, total=False):
    metric: dict[str, str]
    value: list[float | str]
    values: list[list[float | str]]


class PrometheusData(TypedDict, total=False):
    resultType: str
    result: list[PrometheusSeries]


class PrometheusResponse(TypedDict, total=False):
    status: str
    error: str
    data: PrometheusData


class PrometheusLabelValuesResponse(TypedDict, total=False):
    status: str
    data: list[str]


class PrometheusMetadataEntry(TypedDict, total=False):
    type: str
    help: str
    unit: str


class PrometheusMetadataResponse(TypedDict, total=False):
    status: str
    data: dict[str, list[PrometheusMetadataEntry]]


_CADVISOR_LABEL_PREFIX = "container_label_"


def _format_labels(metric: dict[str, str], hide_cadvisor_labels: bool, hidden: set[str]) -> str:
    """Render a label set as ``k="v", ...``, dropping cadvisor ``container_label_*`` labels when asked."""
    shown: list[str] = []
    for key, value in metric.items():
        if hide_cadvisor_labels and key.startswith(_CADVISOR_LABEL_PREFIX):
            hidden.add(key)
            continue
        shown.append(f'{key}="{value}"')
    return ", ".join(shown)


def _sample_line(point: list[float | str]) -> str:
    return f"    [{format_utc(float(point[0]))}] {point[1]}"


def _format_result(data: PrometheusResponse, query: str = "") -> str:
    """Format a Prometheus API response into a readable string for the LLM.

    cadvisor's ``container_label_*`` labels (Compose metadata, about 1.4k characters per series) are
    hidden unless ``query`` names one; a single note says how many were hidden.
    """
    status = data.get("status", "unknown")
    if status != "success":
        error_msg = data.get("error", "unknown error")
        return f"Prometheus query failed: {error_msg}"

    result_data = data.get("data", {})
    result_type = result_data.get("resultType", "unknown")
    results = list(result_data.get("result", []))

    if not results:
        return (
            "Query returned no results. Common causes:\n"
            "1. Wrong label filters — not all metrics share the same labels. "
            "Try the query without label filters to see what labels exist.\n"
            "2. Time range outside Prometheus retention (90 days). "
            "Check that start/end are within the last 90 days.\n"
            "3. For 'peak/max/min/average' questions, use prometheus_instant_query "
            "with *_over_time functions instead of prometheus_range_query.\n"
            "Use prometheus_search_metrics to verify the metric name exists."
        )

    lines: list[str] = [f"Result type: {result_type}, series count: {len(results)}"]

    if len(results) > MAX_SERIES_SHOWN:
        lines.append(f"WARNING: {len(results)} series returned. Consider adding label filters to narrow results.")
        lines.append(f"(showing {MAX_SERIES_SHOWN} of {len(results)} series)")
        results = results[:MAX_SERIES_SHOWN]

    hide_cadvisor_labels = _CADVISOR_LABEL_PREFIX not in query
    hidden: set[str] = set()

    for series in results:
        label_str = _format_labels(series.get("metric", {}), hide_cadvisor_labels, hidden)

        if result_type == "vector":
            value_pair = series.get("value", [0, ""])
            val = str(value_pair[1]) if len(value_pair) > 1 else ""
            lines.append(f"  {{{label_str}}} => {val}")
        elif result_type == "matrix":
            values = series.get("values", [])
            # Compute summary stats so the agent sees the full data range
            # even when only a few samples are displayed
            numeric_vals: list[float] = []
            for point in values:
                with contextlib.suppress(ValueError, IndexError):
                    numeric_vals.append(float(point[1]))
            if numeric_vals:
                min_val = min(numeric_vals)
                max_val = max(numeric_vals)
                avg_val = sum(numeric_vals) / len(numeric_vals)
                summary = f" (min: {min_val:.4g}, max: {max_val:.4g}, avg: {avg_val:.4g})"
            else:
                summary = ""
            lines.append(f"  {{{label_str}}} => {len(values)} samples{summary}")
            if len(values) <= 6:
                lines.extend(_sample_line(point) for point in values)
            else:
                lines.extend(_sample_line(point) for point in values[:3])
                lines.append(f"    ... ({len(values) - 6} more samples)")
                lines.extend(_sample_line(point) for point in values[-3:])
        elif result_type == "scalar":
            lines.append(f"  scalar => {series}")
        else:
            lines.append(f"  {series}")

    if hidden:
        lines.append(
            f"({len(hidden)} cadvisor container_label_* labels hidden; " + "name one in the query to show them)"
        )

    return "\n".join(lines)


def _check_negative_max_over_time(query: str, data: PrometheusResponse) -> str:
    """Return a warning if max_over_time was likely misused on a negative metric.

    Catches two failure modes:
    1. max_over_time returns a negative value — obvious sign the metric is negative
    2. abs(max_over_time(...)) — hides the negative sign but still returns the
       smallest magnitude (closest to zero), not the peak. Detected by checking
       for abs() wrapping max_over_time in the query string.
    """
    q = query.lower()
    if "max_over_time" not in q:
        return ""

    warning = (
        "\n\nWARNING: max_over_time on a negative metric returns the value closest "
        "to zero (SMALLEST magnitude), not the peak. Wrapping in abs() does not fix "
        "this — it just makes the wrong answer positive. Use "
        "abs(min_over_time(...)) instead to get the largest magnitude (fastest speed)."
    )

    # Case 1: abs(max_over_time(...)) — wrong query pattern regardless of result sign
    if "abs(" in q and "max_over_time" in q:
        return warning

    # Case 2: max_over_time returned a negative value
    results = data.get("data", {}).get("result", [])
    for series in results:
        value_pair = series.get("value", [])
        if len(value_pair) > 1:
            with contextlib.suppress(ValueError, IndexError):
                if float(value_pair[1]) < 0:
                    return warning
    return ""


def _get_baseline_enrichment(data: PrometheusResponse) -> str:
    """Extract metric names from Prometheus results and enrich with baseline context.

    Returns baseline context string or empty string if not available.
    """
    try:
        from src.memory.context import enrich_with_baseline_context

        results = data.get("data", {}).get("result", [])
        metric_names: list[str] = []
        for series in results:
            name = series.get("metric", {}).get("__name__", "")
            if name:
                metric_names.append(name)
        if not metric_names:
            return ""
        return enrich_with_baseline_context("", metric_names)
    except Exception as e:
        logger.warning("Baseline enrichment failed, returning the result without it: %s: %s", type(e).__name__, e)
        return ""


async def _query_prometheus(endpoint: str, params: dict[str, str]) -> PrometheusResponse:
    """Make an HTTP request to the Prometheus API."""
    url = f"{get_settings().prometheus_url}{endpoint}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params)
        _ = response.raise_for_status()
        data: PrometheusResponse = response.json()  # pyright: ignore[reportAny]
        return data


async def _get_prometheus_raw(endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """Make an HTTP request to a Prometheus API endpoint and return the raw JSON.

    Unlike _query_prometheus, this does not assume the query/query_range response shape.
    Used for label values (/api/v1/label/*/values) and metadata (/api/v1/metadata).
    """
    url = f"{get_settings().prometheus_url}{endpoint}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params or {})
        _ = response.raise_for_status()
        data: dict[str, Any] = response.json()  # pyright: ignore[reportAny]
        return data


def _format_search_results(
    matched_names: list[str],
    metadata: dict[str, list[PrometheusMetadataEntry]],
    search_term: str,
) -> str:
    """Format metric search results with optional type/help metadata for the LLM."""
    if not matched_names:
        return (
            f'No metrics found matching "{search_term}". '
            "Try a broader search term, or check the exporter prefix "
            "(e.g. 'node_' for node_exporter, 'container_' for cadvisor, 'mktxp_' for MikroTik)."
        )

    sorted_names = sorted(matched_names)
    truncated = len(sorted_names) > MAX_SEARCH_RESULTS
    display_names = sorted_names[:MAX_SEARCH_RESULTS]

    lines: list[str] = [f'Found {len(sorted_names)} metrics matching "{search_term}":\n']

    for name in display_names:
        entries = metadata.get(name, [])
        if entries:
            entry = entries[0]
            metric_type = entry.get("type", "")
            help_text = entry.get("help", "")
            if metric_type and help_text:
                lines.append(f"  {name} ({metric_type}): {help_text}")
            elif metric_type:
                lines.append(f"  {name} ({metric_type})")
            else:
                lines.append(f"  {name}")
        else:
            lines.append(f"  {name}")

    if truncated:
        lines.append(f"\n(showing first {MAX_SEARCH_RESULTS} of {len(sorted_names)} matches)")

    lines.append("\nUse prometheus_instant_query or prometheus_range_query to fetch values.")
    return "\n".join(lines)


TOOL_DESCRIPTION_SEARCH = (
    "Search for available Prometheus metric names matching a substring. "
    "Use this BEFORE querying when you're unsure of the exact metric name. "
    "Returns matching metric names with their type and description.\n\n"
    "Examples:\n"
    "- Search 'mktxp' to find MikroTik router metrics\n"
    "- Search 'node_cpu' to find CPU-related node_exporter metrics\n"
    "- Search 'container_memory' to find container memory metrics\n"
    "- Search 'dhcp' to find DHCP-related metrics\n"
    "- Search 'pve' to find Proxmox VM/LXC inventory and resource metrics\n\n"
    "After finding the right metric name, use prometheus_instant_query or "
    "prometheus_range_query to fetch actual values."
)


TOOL_DESCRIPTION_INSTANT = (
    "Query Prometheus for the current value of a metric (instant query). "
    "Use this for point-in-time questions like 'what is the current CPU usage?', "
    "'how much memory is free right now?', or 'how many VMs are running?' "
    "(infrastructure inventory is available via pve_exporter metrics).\n\n"
    "This homelab's Prometheus setup:\n"
    "- The `hostname` label identifies each host "
    "(e.g. hostname='jellyfin', hostname='proxmox', hostname='immich')\n"
    "- Managed hosts (node_exporter + cadvisor): proxmox, truenas, media, infra, "
    "jellyfin, immich, prometheus, tube-archivist, documents, open-webui\n"
    "- Network/services (dedicated exporters): mikrotik, home-assistant, unbound, "
    "key-server, traefik, cloudflared\n"
    "- Exporters: node_exporter (node_*), cadvisor (container_*), pve_exporter (pve_*), "
    "adguard, NUT (network_ups_tools_*), smartctl, IPMI, MikroTik (mktxp_*)\n"
    "- Recording rules: share drive probes, disk spindown, network traffic (signed bytes/sec), "
    "UPS mains status\n"
    "- Container-status exporter series (container_state, container_health, container_restart_count, "
    'container_image_outdated, container_image_info) all carry hostname="infra", the exporter\'s own host; '
    'the container\'s host is in `exported_hostname`, so a hostname="media" filter returns nothing. '
    "For container state, use the container_status tool.\n\n"
    "IMPORTANT: Always include label filters to avoid returning too many time series. "
    "For example, use {hostname='jellyfin'} not just the bare metric name."
)

TOOL_DESCRIPTION_RANGE = (
    "Query Prometheus for metric values over a time range (range query). "
    "Use this for trend questions like 'how has CPU changed over the last hour?' "
    "or 'show memory usage for the past day'.\n\n"
    "Same homelab setup as the instant query tool. Always include label filters.\n\n"
    "Time window: start defaults to '1h' (one hour ago) and end to 'now'. Both accept 'now', a duration "
    "before now ('24h', '-24h', 'now-24h', '7d'), RFC3339 or Unix seconds. The range may span up to "
    "90 days (Prometheus retention). Omit step to get about 250 points per series; an explicit step "
    "may be at most 1d and must keep the range under 11,000 points.\n\n"
    "IMPORTANT: Always include label filters to avoid returning too many time series."
)


@tool("prometheus_search_metrics", args_schema=PrometheusSearchInput)  # pyright: ignore[reportUnknownParameterType]
async def prometheus_search_metrics(search_term: str) -> str:
    """Search for available Prometheus metric names. See TOOL_DESCRIPTION_SEARCH."""
    # The term matches as a literal substring: regex-escaped, then quoted as a PromQL string. Python's re.escape
    # output ('\\.', '\\-') is not valid inside a PromQL string, which Prometheus rejects with HTTP 400.
    match_param = "{__name__=~" + promql_string(".*" + regex_literal(search_term) + ".*") + "}"

    logger.info("Prometheus metric search: %s", search_term)
    try:
        label_data = cast(
            PrometheusLabelValuesResponse,
            await _get_prometheus_raw(
                "/api/v1/label/__name__/values",
                params={"match[]": match_param},
            ),
        )
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Prometheus at {get_settings().prometheus_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Prometheus search timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Prometheus API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e

    matched_names: list[str] = label_data.get("data", [])

    # Fetch metadata for type + help text (best-effort)
    metadata: dict[str, list[PrometheusMetadataEntry]] = {}
    try:
        meta_data = cast(PrometheusMetadataResponse, await _get_prometheus_raw("/api/v1/metadata"))
        metadata = meta_data.get("data", {})
    except Exception:
        logger.warning("Failed to fetch metric metadata — returning names only")

    return _format_search_results(matched_names, metadata, search_term)


prometheus_search_metrics.description = TOOL_DESCRIPTION_SEARCH
prometheus_search_metrics.handle_tool_error = True


@tool("prometheus_instant_query", args_schema=PrometheusInstantInput)
async def prometheus_instant_query(query: str, time: str | None = None) -> str:
    """Query Prometheus for current metric value. See TOOL_DESCRIPTION_INSTANT."""
    params: dict[str, str] = {"query": query}
    if time is not None:
        try:
            params["time"] = _number(_parse_timestamp(time, datetime.now(UTC)))
        except ValueError as e:
            raise ToolException(f"Invalid instant query time: {e}") from e

    logger.info("Prometheus instant query: %s", query)
    try:
        data = await _query_prometheus("/api/v1/query", params)
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Prometheus API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Prometheus at {get_settings().prometheus_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Prometheus query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e

    result = _format_result(data, query) + _check_negative_max_over_time(query, data)
    return result + _get_baseline_enrichment(data)


prometheus_instant_query.description = TOOL_DESCRIPTION_INSTANT
prometheus_instant_query.handle_tool_error = True


@tool("prometheus_range_query", args_schema=PrometheusRangeInput)
async def prometheus_range_query(query: str, start: str = "1h", end: str = "now", step: str | None = None) -> str:
    """Query Prometheus for metric values over a time range. See TOOL_DESCRIPTION_RANGE."""
    try:
        window = _resolve_range(start, end, step, datetime.now(UTC))
    except ValueError as e:
        raise ToolException(f"Invalid range query parameters: {e}") from e

    params: dict[str, str] = {
        "query": query,
        "start": _number(window.start),
        "end": _number(window.end),
        "step": _number(window.step),
    }

    logger.info(
        "Prometheus range query: %s (start=%s, end=%s, step=%s)",
        query,
        format_utc(window.start),
        format_utc(window.end),
        _format_seconds(window.step),
    )
    try:
        data = await _query_prometheus("/api/v1/query_range", params)
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Prometheus API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Prometheus at {get_settings().prometheus_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Prometheus query timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e

    return _format_result(data, query)


prometheus_range_query.description = TOOL_DESCRIPTION_RANGE
prometheus_range_query.handle_tool_error = True
