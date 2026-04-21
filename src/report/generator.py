"""Weekly reliability report generator.

Queries Prometheus, Grafana, and Loki APIs directly (not through the LangChain
agent) to collect structured data, then makes a single LLM call for a narrative
summary.  Each collector is independent and wrapped in try/except so a partial
report is always produced.
"""

import asyncio
import html as html_mod
import json
import logging
import ssl
import time
from datetime import UTC, datetime
from typing import NamedTuple, NotRequired, TypedDict

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from src.agent.llm import _is_oauth_token, create_llm
from src.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------------------
# Retry helpers for LLM narrative generation
# ---------------------------------------------------------------------------

# Initial delay before the first retry (seconds).
_RETRY_INITIAL_DELAY = 30.0
# Multiply delay by this factor after each retry.
_RETRY_BACKOFF_FACTOR = 2.0
# Cap individual retry delays at 30 minutes.
_RETRY_MAX_DELAY = 1800.0


def _is_retryable_llm_error(exc: Exception) -> bool:
    """Return True if *exc* is a transient LLM error worth retrying.

    Retryable errors:
      - HTTP 429 (rate limit) from Anthropic or OpenAI SDKs
      - HTTP 5xx (server-side) from Anthropic or OpenAI SDKs
      - Network / connection errors (httpx, OSError)

    Non-retryable: auth errors (401/403), validation errors (400/422), etc.
    """
    # Both anthropic and openai SDKs expose a .status_code on their API errors.
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or status_code >= 500

    # Network-level failures (connection refused, DNS, timeout, etc.)
    return isinstance(exc, (httpx.TransportError, OSError, ConnectionError, TimeoutError))


# ---------------------------------------------------------------------------
# Structured data types
# ---------------------------------------------------------------------------


class AlertSummaryData(TypedDict):
    total_rules: int
    active_alerts: int
    alerts_by_severity: dict[str, int]
    active_alert_names: list[str]


class SLOStatusData(TypedDict):
    p95_latency_seconds: float | None
    tool_success_rate: float | None
    llm_error_rate: float | None
    availability: float | None
    component_availability: NotRequired[dict[str, float]]


class ToolUsageData(TypedDict):
    tool_calls: dict[str, int]
    tool_errors: dict[str, int]


class CostData(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float


class LokiErrorSummary(TypedDict):
    errors_by_service: dict[str, int]
    total_errors: int
    previous_total_errors: NotRequired[int | None]
    previous_errors_by_service: NotRequired[dict[str, int]]
    error_samples: NotRequired[dict[str, str]]


class BackupGroupHealth(TypedDict):
    backup_type: str
    backup_id: str
    last_backup_ts: int
    backup_count: int
    stale: bool


class DatastoreHealth(TypedDict):
    store: str
    total_bytes: int
    used_bytes: int
    usage_percent: float


class BackupHealthData(TypedDict):
    datastores: list[DatastoreHealth]
    backups: list[BackupGroupHealth]
    stale_count: int
    total_count: int


class ReportData(TypedDict):
    generated_at: str
    lookback_days: int
    alerts: AlertSummaryData | None
    slo_status: SLOStatusData | None
    tool_usage: ToolUsageData | None
    cost: CostData | None
    loki_errors: LokiErrorSummary | None
    backup_health: NotRequired[BackupHealthData | None]
    narrative: str


class GeneratedReport(NamedTuple):
    """Both markdown and HTML renderings of a report."""

    markdown: str
    html: str


# ---------------------------------------------------------------------------
# Prometheus query helper
# ---------------------------------------------------------------------------


async def _prom_query(
    client: httpx.AsyncClient,
    prometheus_url: str,
    query: str,
) -> list[dict[str, object]]:
    """Run a Prometheus instant query, return the result list."""
    resp = await client.get(
        f"{prometheus_url}/api/v1/query",
        params={"query": query},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    _ = resp.raise_for_status()
    body: dict[str, object] = resp.json()
    data = body.get("data")
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, list):
            return result
    return []


def _scalar_value(results: list[dict[str, object]]) -> float | None:
    """Extract a single scalar float from a Prometheus instant query result."""
    if not results:
        return None
    first = results[0]
    value = first.get("value")
    if isinstance(value, list) and len(value) >= 2:
        try:
            return float(str(value[1]))
        except (ValueError, TypeError):
            return None
    return None


def _format_plain_table(
    headers: list[str],
    rows: list[list[str]],
    right_align: set[int] | None = None,
) -> str:
    """Format a plain-text table with aligned columns (no pipe characters).

    Args:
        headers: Column header strings.
        rows: List of rows, each a list of cell strings.
        right_align: Set of column indices (0-based) to right-align.

    Returns:
        Multi-line string with padded columns separated by two spaces.
    """
    right_align = right_align or set()
    if not rows:
        return ""
    all_data = [headers, *rows]
    col_widths = [max(len(row[i]) for row in all_data) for i in range(len(headers))]

    def fmt_row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell in enumerate(cells):
            width = col_widths[i]
            parts.append(cell.rjust(width) if i in right_align else cell.ljust(width))
        return "  ".join(parts)

    lines = [fmt_row(headers)]
    lines.append("  ".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


async def _collect_alert_summary(lookback_days: int) -> AlertSummaryData:
    """Collect alert rule count and currently active alerts from Grafana."""
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.grafana_service_account_token}"}
    _ = lookback_days  # Alert summary shows current state

    async with httpx.AsyncClient() as client:
        # Get alert rules
        rules_resp = await client.get(
            f"{settings.grafana_url}/api/v1/provisioning/alert-rules",
            headers=headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        _ = rules_resp.raise_for_status()
        rules: list[object] = rules_resp.json()
        total_rules = len(rules)

        # Get active alerts
        alerts_resp = await client.get(
            f"{settings.grafana_url}/api/alertmanager/grafana/api/v2/alerts/groups",
            headers=headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        _ = alerts_resp.raise_for_status()
        groups: list[dict[str, object]] = alerts_resp.json()

        active_alerts: list[str] = []
        severity_counts: dict[str, int] = {}
        for group in groups:
            group_alerts = group.get("alerts", [])
            if not isinstance(group_alerts, list):
                continue
            for alert in group_alerts:
                if not isinstance(alert, dict):
                    continue
                status = alert.get("status", {})
                if isinstance(status, dict) and status.get("state") == "active":
                    labels = alert.get("labels", {})
                    if isinstance(labels, dict):
                        name = str(labels.get("alertname", "unknown"))
                        active_alerts.append(name)
                        severity = str(labels.get("severity", "unknown"))
                        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    return AlertSummaryData(
        total_rules=total_rules,
        active_alerts=len(active_alerts),
        alerts_by_severity=severity_counts,
        active_alert_names=active_alerts,
    )


async def _collect_slo_status(lookback_days: int) -> SLOStatusData:
    """Collect SLO metrics from Prometheus over the lookback window."""
    settings = get_settings()
    window = f"{lookback_days}d"

    async with httpx.AsyncClient() as client:
        url = settings.prometheus_url

        p95_results = await _prom_query(
            client, url, f"histogram_quantile(0.95, rate(sre_assistant_request_duration_seconds_bucket[{window}]))"
        )
        p95 = _scalar_value(p95_results)

        # Tool success rate: 1 - (errors / total)
        tool_total = await _prom_query(client, url, f"sum(increase(sre_assistant_tool_calls_total[{window}]))")
        tool_errors = await _prom_query(
            client, url, f'sum(increase(sre_assistant_tool_calls_total{{status="error"}}[{window}]))'
        )
        total_val = _scalar_value(tool_total)
        error_val = _scalar_value(tool_errors)
        tool_success: float | None = None
        if total_val is not None and total_val > 0:
            tool_success = 1.0 - ((error_val or 0.0) / total_val)

        # LLM error rate
        llm_total = await _prom_query(client, url, f"sum(increase(sre_assistant_llm_calls_total[{window}]))")
        llm_errors = await _prom_query(
            client, url, f'sum(increase(sre_assistant_llm_calls_total{{status="error"}}[{window}]))'
        )
        llm_total_val = _scalar_value(llm_total)
        llm_error_val = _scalar_value(llm_errors)
        llm_error_rate: float | None = None
        if llm_total_val is not None and llm_total_val > 0:
            llm_error_rate = (llm_error_val or 0.0) / llm_total_val

        # Availability: per-component and overall average
        avail_results = await _prom_query(client, url, f"avg_over_time(sre_assistant_component_healthy[{window}])")
        availability: float | None = None
        component_availability: dict[str, float] = {}
        if avail_results:
            for r in avail_results:
                if isinstance(r, dict):
                    metric = r.get("metric", {})
                    val = _scalar_value([r])
                    if val is not None and isinstance(metric, dict):
                        component = str(metric.get("component", "unknown"))
                        component_availability[component] = val
            if component_availability:
                availability = sum(component_availability.values()) / len(component_availability)

    return SLOStatusData(
        p95_latency_seconds=p95,
        tool_success_rate=tool_success,
        llm_error_rate=llm_error_rate,
        availability=availability,
        component_availability=component_availability,
    )


async def _collect_tool_usage(lookback_days: int) -> ToolUsageData:
    """Collect per-tool call counts and error counts from Prometheus."""
    settings = get_settings()
    window = f"{lookback_days}d"

    async with httpx.AsyncClient() as client:
        url = settings.prometheus_url

        total_results = await _prom_query(
            client, url, f"sum by (tool_name) (increase(sre_assistant_tool_calls_total[{window}]))"
        )
        error_results = await _prom_query(
            client,
            url,
            f'sum by (tool_name) (increase(sre_assistant_tool_calls_total{{status="error"}}[{window}]))',
        )

    tool_calls: dict[str, int] = {}
    for r in total_results:
        if isinstance(r, dict):
            metric = r.get("metric", {})
            if isinstance(metric, dict):
                name = str(metric.get("tool_name", "unknown"))
                val = _scalar_value([r])
                if val is not None:
                    tool_calls[name] = int(val)

    tool_errors: dict[str, int] = {}
    for r in error_results:
        if isinstance(r, dict):
            metric = r.get("metric", {})
            if isinstance(metric, dict):
                name = str(metric.get("tool_name", "unknown"))
                val = _scalar_value([r])
                if val is not None and val > 0:
                    tool_errors[name] = int(val)

    return ToolUsageData(tool_calls=tool_calls, tool_errors=tool_errors)


async def _collect_cost_data(lookback_days: int) -> CostData:
    """Collect token usage and cost from Prometheus."""
    settings = get_settings()
    window = f"{lookback_days}d"

    async with httpx.AsyncClient() as client:
        url = settings.prometheus_url

        prompt_results = await _prom_query(
            client, url, f'increase(sre_assistant_llm_token_usage_total{{type="prompt"}}[{window}])'
        )
        completion_results = await _prom_query(
            client, url, f'increase(sre_assistant_llm_token_usage_total{{type="completion"}}[{window}])'
        )
        cost_results = await _prom_query(
            client, url, f"increase(sre_assistant_llm_estimated_cost_dollars_total[{window}])"
        )

    prompt_tokens = int(_scalar_value(prompt_results) or 0)
    completion_tokens = int(_scalar_value(completion_results) or 0)
    cost = _scalar_value(cost_results) or 0.0

    return CostData(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        estimated_cost_usd=round(cost, 4),
    )


def _normalize_service_name(name: str) -> str:
    """Normalize a service name so that e.g. 'node-exporter' and 'node_exporter' merge."""
    return name.replace("-", "_")


def _aggregate_by_normalized_name(raw: dict[str, int]) -> dict[str, int]:
    """Merge service counts whose names differ only by hyphens vs underscores.

    Keeps the variant with the highest count as the canonical name.
    """
    # Group by normalized key
    groups: dict[str, list[tuple[str, int]]] = {}
    for name, count in raw.items():
        key = _normalize_service_name(name)
        groups.setdefault(key, []).append((name, count))

    merged: dict[str, int] = {}
    for variants in groups.values():
        # Pick the name with the highest count as canonical
        canonical = max(variants, key=lambda x: x[1])[0]
        merged[canonical] = sum(c for _, c in variants)
    return merged


def _parse_loki_service_counts(body: dict[str, object]) -> dict[str, int]:
    """Extract per-service error counts from a Loki instant-query response."""
    raw: dict[str, int] = {}
    data = body.get("data")
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, list):
            for r in result:
                if isinstance(r, dict):
                    metric = r.get("metric", {})
                    if isinstance(metric, dict):
                        service = str(metric.get("service_name", "unknown"))
                        val = _scalar_value([r])
                        if val is not None:
                            raw[service] = int(val)
    return _aggregate_by_normalized_name(raw)


async def _collect_loki_errors(lookback_days: int) -> LokiErrorSummary | None:
    """Collect error log counts by service from Loki with previous-period comparison.

    Also fetches one representative error line per top-5 service.
    Returns None if Loki is not configured.
    """
    settings = get_settings()
    if not settings.loki_url:
        return None

    end = datetime.now(UTC)
    end_ns = int(end.timestamp() * 1e9)
    current_logql = f'sum by (service_name) (count_over_time({{detected_level=~"error|critical"}}[{lookback_days}d]))'
    previous_logql = (
        f'sum by (service_name) (count_over_time({{detected_level=~"error|critical"}}[{lookback_days}d]'
        f" offset {lookback_days}d))"
    )

    async with httpx.AsyncClient() as client:
        current_resp, previous_resp = await asyncio.gather(
            client.get(
                f"{settings.loki_url}/loki/api/v1/query",
                params={"query": current_logql, "time": str(end_ns)},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            ),
            client.get(
                f"{settings.loki_url}/loki/api/v1/query",
                params={"query": previous_logql, "time": str(end_ns)},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            ),
        )
        _ = current_resp.raise_for_status()
        current_body: dict[str, object] = current_resp.json()

        previous_by_service: dict[str, int] | None = None
        previous_total: int | None = None
        try:
            _ = previous_resp.raise_for_status()
            prev_body: dict[str, object] = previous_resp.json()
            previous_by_service = _parse_loki_service_counts(prev_body)
            previous_total = sum(previous_by_service.values())
        except Exception:
            logger.debug("Previous-period Loki query failed; omitting comparison")

    errors_by_service = _parse_loki_service_counts(current_body)
    total = sum(errors_by_service.values())

    # Fetch one representative error line per top-5 service
    error_samples = await _collect_loki_error_samples(settings.loki_url, errors_by_service, lookback_days)

    result = LokiErrorSummary(
        errors_by_service=errors_by_service,
        total_errors=total,
        error_samples=error_samples,
    )
    if previous_total is not None:
        result["previous_total_errors"] = previous_total
    if previous_by_service is not None:
        result["previous_errors_by_service"] = previous_by_service
    return result


async def _collect_loki_error_samples(
    loki_url: str,
    errors_by_service: dict[str, int],
    lookback_days: int,
) -> dict[str, str]:
    """Fetch one recent error log line per top-N service from Loki."""
    max_sample_services = 5
    top_services = sorted(errors_by_service, key=errors_by_service.get, reverse=True)[:max_sample_services]  # type: ignore[arg-type]
    if not top_services:
        return {}

    end = datetime.now(UTC)
    start_ns = str(int((end.timestamp() - lookback_days * 86400) * 1e9))
    end_ns = str(int(end.timestamp() * 1e9))

    samples: dict[str, str] = {}

    async def _fetch_sample(service: str) -> tuple[str, str]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{loki_url}/loki/api/v1/query_range",
                params={
                    "query": f'{{service_name="{service}", detected_level=~"error|critical"}}',
                    "start": start_ns,
                    "end": end_ns,
                    "limit": "1",
                    "direction": "backward",
                },
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            _ = resp.raise_for_status()
            body: dict[str, object] = resp.json()
            data = body.get("data")
            if isinstance(data, dict):
                result_list = data.get("result")
                if isinstance(result_list, list):
                    for stream in result_list:
                        if isinstance(stream, dict):
                            values = stream.get("values")
                            if isinstance(values, list) and values:
                                first_entry = values[0]
                                if isinstance(first_entry, list) and len(first_entry) >= 2:
                                    line = str(first_entry[1])[:200]
                                    return service, line
        return service, ""

    results = await asyncio.gather(*[_fetch_sample(s) for s in top_services], return_exceptions=True)
    for r in results:
        if isinstance(r, tuple):
            svc, line = r
            if line:
                samples[svc] = line
        elif isinstance(r, BaseException):
            logger.debug("Error sample fetch failed: %s", r)
    return samples


async def _collect_backup_health(lookback_days: int) -> BackupHealthData | None:
    """Collect backup health from PBS. Returns None if PBS not configured."""
    settings = get_settings()
    if not settings.pbs_url:
        return None

    _ = lookback_days  # backup health shows current state
    headers = {
        "Authorization": f"PBSAPIToken={settings.pbs_api_token}",
        "Accept": "application/json",
    }
    verify: ssl.SSLContext | bool = False
    if settings.pbs_verify_ssl:
        verify = ssl.create_default_context(cafile=settings.pbs_ca_cert) if settings.pbs_ca_cert else True

    base = f"{settings.pbs_url}/api2/json"
    stale_threshold = 86400  # 24 hours in seconds

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, verify=verify) as client:
        ds_resp = await client.get(f"{base}/status/datastore-usage", headers=headers)
        _ = ds_resp.raise_for_status()
        ds_raw: dict[str, object] = ds_resp.json()  # pyright: ignore[reportAny]
        ds_list: list[object] = ds_raw.get("data", [])  # type: ignore[assignment]

        datastores: list[DatastoreHealth] = []
        for ds in ds_list:
            if isinstance(ds, dict):
                total = int(ds.get("total", 0))
                used = int(ds.get("used", 0))
                pct = (used / total * 100) if total > 0 else 0.0
                datastores.append(
                    DatastoreHealth(
                        store=str(ds.get("store", "unknown")),
                        total_bytes=total,
                        used_bytes=used,
                        usage_percent=round(pct, 1),
                    )
                )

        # Fetch backup groups from each datastore
        all_backups: list[BackupGroupHealth] = []
        for ds in datastores:
            try:
                groups_resp = await client.get(f"{base}/admin/datastore/{ds['store']}/groups", headers=headers)
                _ = groups_resp.raise_for_status()
                groups_raw: dict[str, object] = groups_resp.json()  # pyright: ignore[reportAny]
                groups_list: list[object] = groups_raw.get("data", [])  # type: ignore[assignment]

                now_ts = int(datetime.now(UTC).timestamp())
                for g in groups_list:
                    if isinstance(g, dict):
                        # PBS API uses hyphenated keys
                        last_ts = int(
                            g.get("last-backup", g.get("last_backup", 0))  # type: ignore[arg-type]
                        )
                        all_backups.append(
                            BackupGroupHealth(
                                backup_type=str(g.get("backup-type", g.get("backup_type", "?"))),
                                backup_id=str(g.get("backup-id", g.get("backup_id", "?"))),
                                last_backup_ts=last_ts,
                                backup_count=int(
                                    g.get("backup-count", g.get("backup_count", 0))  # type: ignore[arg-type]
                                ),
                                stale=(now_ts - last_ts) > stale_threshold if last_ts > 0 else True,
                            )
                        )
            except Exception:
                logger.debug("Failed to fetch backup groups for datastore %s", ds["store"])

    stale_count = sum(1 for b in all_backups if b["stale"])
    return BackupHealthData(
        datastores=datastores,
        backups=all_backups,
        stale_count=stale_count,
        total_count=len(all_backups),
    )


# ---------------------------------------------------------------------------
# Collect all data
# ---------------------------------------------------------------------------


async def collect_report_data(lookback_days: int) -> dict[str, object]:
    """Run all collectors concurrently, returning partial data on failures."""
    collectors = {
        "alerts": _collect_alert_summary(lookback_days),
        "slo_status": _collect_slo_status(lookback_days),
        "tool_usage": _collect_tool_usage(lookback_days),
        "cost": _collect_cost_data(lookback_days),
        "loki_errors": _collect_loki_errors(lookback_days),
        "backup_health": _collect_backup_health(lookback_days),
    }

    results: dict[str, object] = {}
    gathered = await asyncio.gather(*collectors.values(), return_exceptions=True)

    for key, result in zip(collectors.keys(), gathered, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Collector %s failed: %s", key, result)
            results[key] = None
        else:
            results[key] = result

    return results


# ---------------------------------------------------------------------------
# LLM narrative
# ---------------------------------------------------------------------------


async def _generate_narrative(
    collected_data: dict[str, object],
    previous_report: str | None = None,
    max_retry_seconds: float = 0,
) -> str:
    """Generate a 2-3 paragraph executive summary via a single LLM call.

    Args:
        collected_data: Structured report data from collectors.
        previous_report: Previous report text for trend comparison.
        max_retry_seconds: Maximum wall-clock time (seconds) to spend
            retrying on transient errors (429, 5xx, network).  ``0`` means
            no retries — fail immediately (backwards-compatible default).
            The scheduled weekly report passes a large budget (e.g. 21 600 s
            = 6 hours) so the report is delayed rather than sent incomplete.
    """
    settings = get_settings()

    user_prompt = (
        "Given the following infrastructure data as JSON, write 3-5 concise bullet "
        "points (one line each, starting with '- '). Cover: alert status, any SLO "
        "violations (mention which components if per-component data is available), "
        "notable error trends (mention if counts are up/down vs previous period), "
        "backup health (any stale backups or storage concerns), and one actionable "
        "recommendation. Be specific with numbers. Do not use markdown bold/italic "
        "formatting. If data is missing (null), note the data source was unavailable.\n\n"
        f"Data:\n```json\n{json.dumps(collected_data, indent=2, default=str)}\n```"
    )
    if previous_report:
        truncated = previous_report[:3000]
        user_prompt += f"\n\nPrevious report for context (compare and note changes/trends):\n```\n{truncated}\n```"

    system_text = "You are an SRE assistant writing a weekly reliability report summary."
    if settings.llm_provider == "anthropic" and _is_oauth_token(settings.anthropic_api_key):
        system_text = "You are Claude Code, Anthropic's official CLI for Claude.\n\n" + system_text

    messages = [SystemMessage(content=system_text), HumanMessage(content=user_prompt)]

    deadline = time.monotonic() + max_retry_seconds
    delay = _RETRY_INITIAL_DELAY
    attempt = 0

    while True:
        attempt += 1
        try:
            llm = create_llm(settings, temperature=0.3)
            response = await llm.ainvoke(messages)
            return str(response.content)
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if _is_retryable_llm_error(exc) and remaining > 0:
                sleep_for = min(delay, remaining, _RETRY_MAX_DELAY)
                logger.warning(
                    "Narrative LLM call failed (attempt %d, %s), retrying in %.0fs (%.0fs remaining in budget)",
                    attempt,
                    type(exc).__name__,
                    sleep_for,
                    remaining,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * _RETRY_BACKOFF_FACTOR, _RETRY_MAX_DELAY)
                continue

            logger.exception("Failed to generate narrative (attempt %d)", attempt)
            return f"Narrative unavailable — LLM call failed: {exc}"


# ---------------------------------------------------------------------------
# Markdown formatter
# ---------------------------------------------------------------------------


def _format_slo_row(name: str, target: str, actual: float | None, higher_is_better: bool = True) -> list[str]:
    """Build a list of cell values for one SLO row: [name, target, actual, status]."""
    if actual is None:
        return [name, target, "N/A", "-"]
    # Format actual to match target's unit for readability
    if "%" in target:
        actual_str = f"{actual * 100:.2f}%"
    elif "s" in target:
        actual_str = f"{actual:.2f}s"
    else:
        actual_str = f"{actual:.4f}" if actual < 1 else f"{actual:.2f}"
    try:
        target_val = float(target.rstrip("%s").replace(">", "").replace("<", "").strip())
        # Normalize: if target is percentage like "99%", compare actual*100
        compare_actual = actual * 100 if "%" in target else actual
        passed = compare_actual >= target_val if higher_is_better else compare_actual <= target_val
        status = "PASS" if passed else "FAIL"
    except (ValueError, TypeError):
        status = "-"
    return [name, target, actual_str, status]


def format_report_markdown(data: ReportData) -> str:
    """Convert structured ReportData into a readable markdown report."""
    lines: list[str] = []
    lines.append("# Weekly Reliability Report")
    lines.append("")
    lines.append(f"**Generated:** {data['generated_at']}")
    lines.append(f"**Lookback:** {data['lookback_days']} days")
    lines.append("")

    # 1. Executive Summary
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(data["narrative"])
    lines.append("")

    # 2. Alert Summary
    lines.append("## Alert Summary")
    lines.append("")
    alerts = data.get("alerts")
    if alerts is None:
        lines.append("*Alert data unavailable.*")
    else:
        lines.append(f"- **Total alert rules:** {alerts['total_rules']}")
        lines.append(f"- **Currently active:** {alerts['active_alerts']}")
        if alerts["alerts_by_severity"]:
            severity_parts = [f"{sev}: {count}" for sev, count in sorted(alerts["alerts_by_severity"].items())]
            lines.append(f"- **By severity:** {', '.join(severity_parts)}")
        if alerts["active_alert_names"]:
            lines.append(f"- **Active alerts:** {', '.join(alerts['active_alert_names'])}")
    lines.append("")

    # 3. SLO Status
    lines.append("## SLO Status")
    lines.append("")
    slo = data.get("slo_status")
    if slo is None:
        lines.append("*SLO data unavailable.*")
    else:
        slo_rows = [
            _format_slo_row("P95 Latency", "< 15s", slo["p95_latency_seconds"], higher_is_better=False),
            _format_slo_row("Tool Success Rate", "> 99%", slo["tool_success_rate"]),
            _format_slo_row("LLM Error Rate", "< 1%", slo["llm_error_rate"], higher_is_better=False),
            _format_slo_row("Availability", "> 99.5%", slo["availability"]),
        ]
        lines.append(
            _format_plain_table(
                ["Metric", "Target", "Actual", "Status"],
                slo_rows,
                right_align={2},
            )
        )
        # Per-component availability breakdown
        comp_avail = slo.get("component_availability", {})
        if comp_avail:
            lines.append("")
            degraded = {k: v for k, v in comp_avail.items() if v < 1.0}
            if degraded:
                lines.append("Components with degraded availability:")
                for comp, val in sorted(degraded.items(), key=lambda x: x[1]):
                    lines.append(f"  - {comp}: {val * 100:.2f}%")
            else:
                lines.append("All components at 100% availability.")
    lines.append("")

    # 4. Tool Usage
    lines.append("## Tool Usage")
    lines.append("")
    usage = data.get("tool_usage")
    if usage is None:
        lines.append("*Tool usage data unavailable.*")
    else:
        if usage["tool_calls"]:
            active = {k: v for k, v in usage["tool_calls"].items() if v > 0}
            inactive_count = len(usage["tool_calls"]) - len(active)
            if active:
                tool_rows: list[list[str]] = []
                for tool_name, calls in sorted(active.items(), key=lambda x: x[1], reverse=True):
                    errors = usage["tool_errors"].get(tool_name, 0)
                    err_rate = f"{errors / calls * 100:.1f}%" if calls > 0 else "0.0%"
                    tool_rows.append([tool_name, str(calls), str(errors), err_rate])
                lines.append(
                    _format_plain_table(
                        ["Tool", "Calls", "Errors", "Error Rate"],
                        tool_rows,
                        right_align={1, 2, 3},
                    )
                )
                if inactive_count > 0:
                    lines.append("")
                    lines.append(f"{inactive_count} registered tools had no calls this period.")
            else:
                lines.append("*No tool calls recorded in this period.*")
        else:
            lines.append("*No tool calls recorded in this period.*")
    lines.append("")

    # 5. Cost & Token Usage
    lines.append("## Cost & Token Usage")
    lines.append("")
    cost = data.get("cost")
    if cost is None:
        lines.append("*Cost data unavailable.*")
    else:
        lines.append(f"- **Prompt tokens:** {cost['prompt_tokens']:,}")
        lines.append(f"- **Completion tokens:** {cost['completion_tokens']:,}")
        lines.append(f"- **Total tokens:** {cost['total_tokens']:,}")
        lines.append(f"- **Estimated cost:** ${cost['estimated_cost_usd']:.4f}")
    lines.append("")

    # 6. Log Error Summary (if Loki configured)
    loki = data.get("loki_errors")
    if loki is not None:
        lines.append("## Log Error Summary")
        lines.append("")
        if loki["errors_by_service"]:
            # Total with week-over-week delta
            total_str = f"**Total errors/critical logs:** {loki['total_errors']}"
            prev_total = loki.get("previous_total_errors")
            if prev_total is not None:
                delta = loki["total_errors"] - prev_total
                if delta > 0:
                    pct = (delta / prev_total * 100) if prev_total > 0 else 0
                    total_str += f" (up {delta:,} / {pct:.0f}% from previous period)"
                elif delta < 0:
                    pct = (abs(delta) / prev_total * 100) if prev_total > 0 else 0
                    total_str += f" (down {abs(delta):,} / {pct:.0f}% from previous period)"
                else:
                    total_str += " (unchanged from previous period)"
            lines.append(total_str)
            lines.append("")

            # Per-service table with delta column if previous data available
            max_loki_rows = 10
            sorted_services = sorted(loki["errors_by_service"].items(), key=lambda x: x[1], reverse=True)
            shown = sorted_services[:max_loki_rows]
            remaining = sorted_services[max_loki_rows:]

            prev_by_service = loki.get("previous_errors_by_service")
            if prev_by_service is not None:
                loki_rows = []
                for service, count in shown:
                    prev_count = prev_by_service.get(service, 0)
                    delta = count - prev_count
                    delta_str = f"+{delta}" if delta > 0 else str(delta)
                    if prev_count == 0 and count > 0:
                        delta_str = "new"
                    loki_rows.append([service, str(count), delta_str])
                lines.append(
                    _format_plain_table(
                        ["Service", "Errors", "vs Prev"],
                        loki_rows,
                        right_align={1, 2},
                    )
                )
            else:
                loki_rows_simple = [[service, str(count)] for service, count in shown]
                lines.append(
                    _format_plain_table(
                        ["Service", "Errors"],
                        loki_rows_simple,
                        right_align={1},
                    )
                )
            if remaining:
                remaining_total = sum(c for _, c in remaining)
                lines.append(f"+ {len(remaining)} more services ({remaining_total} errors)")

            # Error samples — one representative line per top service
            samples = loki.get("error_samples", {})
            if samples:
                lines.append("")
                lines.append("Top error samples:")
                for service, sample in samples.items():
                    lines.append(f"  {service}: {sample}")
        else:
            lines.append("*No error/critical logs recorded in this period.*")
        lines.append("")

    # 7. Backup Health (if PBS configured)
    backup = data.get("backup_health")
    if backup is not None:
        lines.append("## Backup Health")
        lines.append("")
        # Datastore usage
        if backup["datastores"]:
            for ds in backup["datastores"]:
                total_tib = ds["total_bytes"] / (1024**4)
                used_tib = ds["used_bytes"] / (1024**4)
                lines.append(
                    f"- **{ds['store']}:** {used_tib:.1f} / {total_tib:.1f} TiB ({ds['usage_percent']:.1f}% used)"
                )
        # Backup freshness
        if backup["backups"]:
            lines.append(f"- **Backup groups:** {backup['total_count']} total, {backup['stale_count']} stale (>24h)")
            stale = [b for b in backup["backups"] if b["stale"]]
            if stale:
                lines.append("")
                lines.append("Stale backups (last backup >24h ago):")
                type_labels = {"vm": "VM", "ct": "CT", "host": "Host"}
                for b in sorted(stale, key=lambda x: x["last_backup_ts"]):
                    label = type_labels.get(b["backup_type"], b["backup_type"])
                    age_h = (int(datetime.now(UTC).timestamp()) - b["last_backup_ts"]) / 3600
                    lines.append(f"  - {label}/{b['backup_id']}: {age_h:.0f}h ago ({b['backup_count']} snapshots)")
            else:
                lines.append("- All backups are fresh (<24h).")
        else:
            lines.append("*No backup groups found.*")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML formatter
# ---------------------------------------------------------------------------

# Inline styles for email-safe HTML (email clients ignore <style> blocks)
_BODY_STYLE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
    "color: #1a1a2e; background-color: #f8f9fa; margin: 0; padding: 0;"
)
_CONTAINER_STYLE = "max-width: 680px; margin: 0 auto; padding: 24px 20px;"
_HEADER_STYLE = (
    "font-size: 22px; font-weight: 700; color: #1a1a2e; "
    "border-bottom: 3px solid #4361ee; padding-bottom: 12px; margin-bottom: 4px;"
)
_META_STYLE = "font-size: 13px; color: #6c757d; margin-bottom: 24px;"
_SECTION_STYLE = (
    "background: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 16px;"
)
_H2_STYLE = "font-size: 16px; font-weight: 600; color: #1a1a2e; margin: 0 0 12px 0;"
_TABLE_STYLE = (
    "width: 100%; border-collapse: collapse; font-size: 13px; font-family: "
    "'SF Mono', 'Fira Code', 'Consolas', monospace;"
)
_TH_STYLE = (
    "text-align: left; padding: 8px 12px; border-bottom: 2px solid #dee2e6; "
    "color: #495057; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;"
)
_TD_STYLE = "padding: 7px 12px; border-bottom: 1px solid #f0f0f0;"
_TD_RIGHT_STYLE = _TD_STYLE + " text-align: right;"
_PASS_STYLE = (
    "display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; "
    "font-weight: 600; background-color: #d4edda; color: #155724;"
)
_FAIL_STYLE = (
    "display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; "
    "font-weight: 600; background-color: #f8d7da; color: #721c24;"
)
_WARN_STYLE = (
    "display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; "
    "font-weight: 600; background-color: #fff3cd; color: #856404;"
)
_BULLET_STYLE = "margin: 4px 0; padding: 0; line-height: 1.6;"
_CODE_STYLE = (
    "font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; font-size: 12px; "
    "background: #f6f8fa; padding: 8px 12px; border-radius: 4px; display: block; "
    "overflow-x: auto; white-space: pre-wrap; word-break: break-all; color: #d63384; "
    "border: 1px solid #e9ecef; margin: 4px 0;"
)
_STALE_STYLE = "color: #dc3545; font-weight: 600;"
_DELTA_UP_STYLE = "color: #dc3545;"
_DELTA_DOWN_STYLE = "color: #28a745;"
_DELTA_NEW_STYLE = "color: #dc3545; font-weight: 600;"
_FOOTER_STYLE = "font-size: 11px; color: #adb5bd; text-align: center; margin-top: 24px;"


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return html_mod.escape(str(text))


def _html_table(
    headers: list[str],
    rows: list[list[str]],
    right_align: set[int] | None = None,
    raw_html_cols: set[int] | None = None,
) -> str:
    """Build an HTML table with inline styles.

    Args:
        headers: Column header strings.
        rows: List of rows, each a list of cell strings.
        right_align: Set of column indices to right-align.
        raw_html_cols: Column indices whose cell content is already HTML (not escaped).
    """
    right_align = right_align or set()
    raw_html_cols = raw_html_cols or set()
    parts = [f'<table style="{_TABLE_STYLE}"><thead><tr>']
    for i, h in enumerate(headers):
        align = " text-align: right;" if i in right_align else ""
        parts.append(f'<th style="{_TH_STYLE}{align}">{_esc(h)}</th>')
    parts.append("</tr></thead><tbody>")
    for ri, row in enumerate(rows):
        bg = " background-color: #f8f9fa;" if ri % 2 == 1 else ""
        parts.append(f'<tr style="{bg}">')
        for i, cell in enumerate(row):
            style = _TD_RIGHT_STYLE if i in right_align else _TD_STYLE
            content = cell if i in raw_html_cols else _esc(cell)
            parts.append(f'<td style="{style}">{content}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _status_badge(status: str) -> str:
    """Return an inline HTML badge for PASS/FAIL/WARN."""
    styles = {"PASS": _PASS_STYLE, "FAIL": _FAIL_STYLE, "WARN": _WARN_STYLE}
    style = styles.get(status)
    if style:
        return f'<span style="{style}">{_esc(status)}</span>'
    return _esc(status)


def _delta_html(delta_str: str) -> str:
    """Wrap a delta string in colored HTML."""
    if delta_str == "new":
        return f'<span style="{_DELTA_NEW_STYLE}">new</span>'
    if delta_str.startswith("+"):
        return f'<span style="{_DELTA_UP_STYLE}">{_esc(delta_str)}</span>'
    if delta_str.startswith("-"):
        return f'<span style="{_DELTA_DOWN_STYLE}">{_esc(delta_str)}</span>'
    return _esc(delta_str)


def format_report_html(data: ReportData) -> str:
    """Convert structured ReportData into an email-safe HTML document."""
    sections: list[str] = []

    # --- Executive Summary ---
    narrative = data["narrative"]
    narrative_html_lines: list[str] = []
    for line in narrative.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            narrative_html_lines.append(f'<p style="{_BULLET_STYLE}">&bull; {_esc(stripped[2:])}</p>')
        elif stripped:
            narrative_html_lines.append(f'<p style="{_BULLET_STYLE}">{_esc(stripped)}</p>')
    sections.append(
        f'<div style="{_SECTION_STYLE}">'
        f'<h2 style="{_H2_STYLE}">Executive Summary</h2>'
        f"{''.join(narrative_html_lines)}"
        f"</div>"
    )

    # --- Alert Summary ---
    alerts = data.get("alerts")
    alert_body = ""
    if alerts is None:
        alert_body = '<p style="color: #6c757d; font-style: italic;">Alert data unavailable.</p>'
    else:
        parts = [
            f'<p style="{_BULLET_STYLE}">Total alert rules: <strong>{alerts["total_rules"]}</strong></p>',
            f'<p style="{_BULLET_STYLE}">Currently active: <strong>{alerts["active_alerts"]}</strong></p>',
        ]
        if alerts["alerts_by_severity"]:
            sev = ", ".join(f"{s}: {c}" for s, c in sorted(alerts["alerts_by_severity"].items()))
            parts.append(f'<p style="{_BULLET_STYLE}">By severity: {_esc(sev)}</p>')
        if alerts["active_alert_names"]:
            names = ", ".join(alerts["active_alert_names"])
            parts.append(f'<p style="{_BULLET_STYLE}">Active alerts: {_esc(names)}</p>')
        alert_body = "".join(parts)
    sections.append(f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">Alert Summary</h2>{alert_body}</div>')

    # --- SLO Status ---
    slo = data.get("slo_status")
    if slo is None:
        slo_body = '<p style="color: #6c757d; font-style: italic;">SLO data unavailable.</p>'
    else:
        slo_rows_raw = [
            _format_slo_row("P95 Latency", "< 15s", slo["p95_latency_seconds"], higher_is_better=False),
            _format_slo_row("Tool Success Rate", "> 99%", slo["tool_success_rate"]),
            _format_slo_row("LLM Error Rate", "< 1%", slo["llm_error_rate"], higher_is_better=False),
            _format_slo_row("Availability", "> 99.5%", slo["availability"]),
        ]
        # Replace status text with badges
        slo_rows_html = [[r[0], r[1], r[2], _status_badge(r[3])] for r in slo_rows_raw]
        slo_body = _html_table(
            ["Metric", "Target", "Actual", "Status"],
            slo_rows_html,
            right_align={2},
            raw_html_cols={3},
        )
        # Component availability
        comp_avail = slo.get("component_availability", {})
        if comp_avail:
            degraded = {k: v for k, v in comp_avail.items() if v < 1.0}
            if degraded:
                comp_lines = [
                    '<p style="margin-top: 12px; font-size: 13px; color: #495057;">'
                    "Components with degraded availability:</p>"
                ]
                for comp, val in sorted(degraded.items(), key=lambda x: x[1]):
                    pct_str = f"{val * 100:.2f}%"
                    style = _WARN_STYLE if val >= 0.995 else _FAIL_STYLE
                    comp_lines.append(
                        f'<p style="{_BULLET_STYLE} font-size: 13px;">'
                        f'&bull; {_esc(comp)}: <span style="{style}">{pct_str}</span></p>'
                    )
                slo_body += "".join(comp_lines)
    sections.append(f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">SLO Status</h2>{slo_body}</div>')

    # --- Tool Usage ---
    usage = data.get("tool_usage")
    if usage is None:
        tool_body = '<p style="color: #6c757d; font-style: italic;">Tool usage data unavailable.</p>'
    elif not usage["tool_calls"] or not any(v > 0 for v in usage["tool_calls"].values()):
        tool_body = '<p style="color: #6c757d; font-style: italic;">No tool calls recorded in this period.</p>'
    else:
        active = {k: v for k, v in usage["tool_calls"].items() if v > 0}
        inactive_count = len(usage["tool_calls"]) - len(active)
        tool_rows: list[list[str]] = []
        for tool_name, calls in sorted(active.items(), key=lambda x: x[1], reverse=True):
            errors = usage["tool_errors"].get(tool_name, 0)
            err_rate = f"{errors / calls * 100:.1f}%" if calls > 0 else "0.0%"
            tool_rows.append([tool_name, str(calls), str(errors), err_rate])
        tool_body = _html_table(
            ["Tool", "Calls", "Errors", "Error Rate"],
            tool_rows,
            right_align={1, 2, 3},
        )
        if inactive_count > 0:
            tool_body += (
                f'<p style="font-size: 12px; color: #6c757d; margin-top: 8px;">'
                f"{inactive_count} registered tools had no calls this period.</p>"
            )
    sections.append(f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">Tool Usage</h2>{tool_body}</div>')

    # --- Cost & Token Usage ---
    cost = data.get("cost")
    if cost is None:
        cost_body = '<p style="color: #6c757d; font-style: italic;">Cost data unavailable.</p>'
    else:
        cost_body = (
            f'<p style="{_BULLET_STYLE}">Prompt tokens: <strong>{cost["prompt_tokens"]:,}</strong></p>'
            f'<p style="{_BULLET_STYLE}">Completion tokens: <strong>{cost["completion_tokens"]:,}</strong></p>'
            f'<p style="{_BULLET_STYLE}">Total tokens: <strong>{cost["total_tokens"]:,}</strong></p>'
            f'<p style="{_BULLET_STYLE}">Estimated cost: '
            f"<strong>${cost['estimated_cost_usd']:.4f}</strong></p>"
        )
    sections.append(
        f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">Cost &amp; Token Usage</h2>{cost_body}</div>'
    )

    # --- Log Error Summary ---
    loki = data.get("loki_errors")
    if loki is not None:
        if loki["errors_by_service"]:
            # Total with delta
            total_str = f"Total errors/critical logs: <strong>{loki['total_errors']:,}</strong>"
            prev_total = loki.get("previous_total_errors")
            if prev_total is not None:
                delta = loki["total_errors"] - prev_total
                if delta > 0:
                    pct = (delta / prev_total * 100) if prev_total > 0 else 0.0
                    total_str += (
                        f' <span style="{_DELTA_UP_STYLE}">(up {delta:,} / {pct:.0f}% from previous period)</span>'
                    )
                elif delta < 0:
                    pct = (abs(delta) / prev_total * 100) if prev_total > 0 else 0.0
                    total_str += (
                        f' <span style="{_DELTA_DOWN_STYLE}">'
                        f"(down {abs(delta):,} / {pct:.0f}% from previous period)</span>"
                    )
                else:
                    total_str += " (unchanged from previous period)"

            # Per-service table
            max_loki_rows = 10
            sorted_services = sorted(loki["errors_by_service"].items(), key=lambda x: x[1], reverse=True)
            shown = sorted_services[:max_loki_rows]
            remaining = sorted_services[max_loki_rows:]

            prev_by_service = loki.get("previous_errors_by_service")
            if prev_by_service is not None:
                loki_rows: list[list[str]] = []
                for service, count in shown:
                    prev_count = prev_by_service.get(service, 0)
                    d = count - prev_count
                    delta_str = f"+{d}" if d > 0 else str(d)
                    if prev_count == 0 and count > 0:
                        delta_str = "new"
                    loki_rows.append([service, str(count), _delta_html(delta_str)])
                loki_table = _html_table(
                    ["Service", "Errors", "vs Prev"],
                    loki_rows,
                    right_align={1, 2},
                    raw_html_cols={2},
                )
            else:
                loki_rows_simple = [[service, str(count)] for service, count in shown]
                loki_table = _html_table(["Service", "Errors"], loki_rows_simple, right_align={1})

            remaining_html = ""
            if remaining:
                remaining_total = sum(c for _, c in remaining)
                remaining_html = (
                    f'<p style="font-size: 12px; color: #6c757d; margin-top: 4px;">'
                    f"+ {len(remaining)} more services ({remaining_total:,} errors)</p>"
                )

            # Error samples
            samples = loki.get("error_samples", {})
            samples_html = ""
            if samples:
                sample_parts = ['<p style="margin-top: 12px; font-size: 13px; color: #495057;">Top error samples:</p>']
                for service, sample in samples.items():
                    sample_parts.append(
                        f'<p style="margin: 2px 0; font-size: 12px;">'
                        f"<strong>{_esc(service)}:</strong></p>"
                        f'<code style="{_CODE_STYLE}">{_esc(sample)}</code>'
                    )
                samples_html = "".join(sample_parts)

            loki_body = f'<p style="{_BULLET_STYLE}">{total_str}</p>{loki_table}{remaining_html}{samples_html}'
        else:
            loki_body = (
                '<p style="color: #6c757d; font-style: italic;">No error/critical logs recorded in this period.</p>'
            )
        sections.append(
            f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">Log Error Summary</h2>{loki_body}</div>'
        )

    # --- Backup Health ---
    backup = data.get("backup_health")
    if backup is not None:
        backup_parts: list[str] = []
        if backup["datastores"]:
            for ds in backup["datastores"]:
                total_tib = ds["total_bytes"] / (1024**4)
                used_tib = ds["used_bytes"] / (1024**4)
                backup_parts.append(
                    f'<p style="{_BULLET_STYLE}">'
                    f"<strong>{_esc(ds['store'])}:</strong> "
                    f"{used_tib:.1f} / {total_tib:.1f} TiB ({ds['usage_percent']:.1f}% used)</p>"
                )
        if backup["backups"]:
            backup_parts.append(
                f'<p style="{_BULLET_STYLE}">Backup groups: '
                f"<strong>{backup['total_count']}</strong> total, "
                f"<strong>{backup['stale_count']}</strong> stale (&gt;24h)</p>"
            )
            stale = [b for b in backup["backups"] if b["stale"]]
            if stale:
                backup_parts.append(
                    '<p style="margin-top: 8px; font-size: 13px; color: #495057;">'
                    "Stale backups (last backup &gt;24h ago):</p>"
                )
                type_labels = {"vm": "VM", "ct": "CT", "host": "Host"}
                for b in sorted(stale, key=lambda x: x["last_backup_ts"]):
                    label = type_labels.get(b["backup_type"], b["backup_type"])
                    age_h = (int(datetime.now(UTC).timestamp()) - b["last_backup_ts"]) / 3600
                    backup_parts.append(
                        f'<p style="{_BULLET_STYLE} font-size: 13px;">'
                        f'&bull; <span style="{_STALE_STYLE}">'
                        f"{_esc(label)}/{_esc(b['backup_id'])}</span>: "
                        f"{age_h:.0f}h ago ({b['backup_count']} snapshots)</p>"
                    )
            else:
                backup_parts.append(f'<p style="{_BULLET_STYLE}">All backups are fresh (&lt;24h).</p>')
        else:
            backup_parts.append('<p style="color: #6c757d; font-style: italic;">No backup groups found.</p>')
        sections.append(
            f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">Backup Health</h2>{"".join(backup_parts)}</div>'
        )

    # --- Assemble full HTML document ---
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        "<title>Weekly Reliability Report</title></head>"
        f'<body style="{_BODY_STYLE}">'
        f'<div style="{_CONTAINER_STYLE}">'
        f'<h1 style="{_HEADER_STYLE}">Weekly Reliability Report</h1>'
        f'<p style="{_META_STYLE}">'
        f"Generated: {_esc(data['generated_at'])} &middot; "
        f"Lookback: {data['lookback_days']} days</p>"
        f"{''.join(sections)}"
        f'<p style="{_FOOTER_STYLE}">Generated by SRE Assistant</p>'
        f"</div></body></html>"
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def generate_report(
    lookback_days: int | None = None,
    max_narrative_retry_seconds: float = 0,
) -> GeneratedReport:
    """Generate a full weekly reliability report in both markdown and HTML.

    After generation, archives the report to the memory store (if configured)
    and triggers baseline computation. Loads the previous report to provide
    context for the LLM narrative.

    Args:
        lookback_days: Number of days to look back. Defaults to settings value.
        max_narrative_retry_seconds: Wall-clock budget for retrying the LLM
            narrative call on transient errors.  ``0`` = no retries.

    Returns:
        A GeneratedReport with .markdown and .html fields.
    """
    settings = get_settings()
    days = lookback_days if lookback_days is not None else settings.report_lookback_days

    collected = await collect_report_data(days)

    # Load previous report for narrative context (if memory configured)
    previous_report = _load_previous_report()

    narrative = await _generate_narrative(
        collected,
        previous_report=previous_report,
        max_retry_seconds=max_narrative_retry_seconds,
    )

    report_data = ReportData(
        generated_at=datetime.now(UTC).isoformat(),
        lookback_days=days,
        alerts=collected.get("alerts"),  # type: ignore[typeddict-item]
        slo_status=collected.get("slo_status"),  # type: ignore[typeddict-item]
        tool_usage=collected.get("tool_usage"),  # type: ignore[typeddict-item]
        cost=collected.get("cost"),  # type: ignore[typeddict-item]
        loki_errors=collected.get("loki_errors"),  # type: ignore[typeddict-item]
        backup_health=collected.get("backup_health"),  # type: ignore[typeddict-item]
        narrative=narrative,
    )

    markdown = format_report_markdown(report_data)
    html = format_report_html(report_data)

    # Archive report and compute baselines (non-blocking, best-effort)
    _archive_report(report_data, markdown)
    await _compute_post_report_baselines(days)

    return GeneratedReport(markdown=markdown, html=html)


def _load_previous_report() -> str | None:
    """Load the most recent archived report for narrative context.

    Returns None if memory is not configured or no previous report exists.
    """
    try:
        from src.memory.store import get_initialized_connection, get_latest_report, is_memory_configured

        if not is_memory_configured():
            return None
        conn = get_initialized_connection()
        try:
            report = get_latest_report(conn)
            if report is None:
                return None
            return report["report_markdown"]
        finally:
            conn.close()
    except Exception:
        logger.debug("Could not load previous report from memory store")
        return None


def _archive_report(report_data: ReportData, markdown: str) -> None:
    """Save the report to the memory store (best-effort, never raises)."""
    try:
        from src.memory.store import (
            _extract_report_metrics,
            get_initialized_connection,
            is_memory_configured,
            save_report,
        )

        if not is_memory_configured():
            return
        data_json = json.dumps(dict(report_data), default=str)
        metrics = _extract_report_metrics(data_json)
        conn = get_initialized_connection()
        try:
            save_report(
                conn,
                generated_at=report_data["generated_at"],
                lookback_days=report_data["lookback_days"],
                report_markdown=markdown,
                report_data=data_json,
                active_alerts=int(metrics.get("active_alerts", 0)),
                slo_failures=int(metrics.get("slo_failures", 0)),
                total_log_errors=int(metrics.get("total_log_errors", 0)),
                estimated_cost=float(metrics.get("estimated_cost", 0.0)),
            )
            logger.info("Report archived to memory store")
        finally:
            conn.close()
    except Exception:
        logger.debug("Failed to archive report to memory store")


async def _compute_post_report_baselines(lookback_days: int) -> None:
    """Compute and store metric baselines after report generation (best-effort)."""
    try:
        from src.memory.baselines import compute_and_store_baselines

        count = await compute_and_store_baselines(lookback_days)
        if count > 0:
            logger.info("Stored %d metric baselines", count)
    except Exception:
        logger.debug("Failed to compute/store baselines")
