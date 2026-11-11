"""Metric baseline computation — queries Prometheus and stores summary statistics.

Computed automatically after weekly report generation. Baselines provide the
agent with a sense of "normal" so it can distinguish anomalies from routine values.
"""

import logging
from datetime import UTC, datetime

import httpx

from src.config import get_settings
from src.memory.models import BaselineRecord
from src.memory.store import get_initialized_connection, is_memory_configured, save_baselines

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15

# Metrics to baseline and their PromQL queries.
# Each entry: (metric_name, labels_json, promql_template)
# The template is a str.format template: {window} is the lookback duration and {{ }} are literal braces.
_DEFAULT_BASELINE_METRICS: list[tuple[str, str, str]] = [
    # Per-host CPU usage (average across all modes, all hosts)
    (
        "node_cpu_usage_ratio",
        "{}",
        '1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}[{window}]))',
    ),
    # Memory usage ratio (average across hosts)
    (
        "node_memory_usage_ratio",
        "{}",
        "avg(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
    ),
    # Agent p95 latency
    (
        "sre_assistant_p95_latency",
        "{}",
        "histogram_quantile(0.95, rate(sre_assistant_request_duration_seconds_bucket[{window}]))",
    ),
    # Agent tool success rate: successes over the calls that were not the client's mistake
    (
        "sre_assistant_tool_success_rate",
        "{}",
        (
            'sum(increase(sre_assistant_tool_calls_total{{status="success"}}[{window}]))'
            ' / sum(increase(sre_assistant_tool_calls_total{{status!~"invalid_args|unknown_tool"}}[{window}]))'
        ),
    ),
    # LLM error rate
    (
        "sre_assistant_llm_error_rate",
        "{}",
        (
            'sum(increase(sre_assistant_llm_calls_total{{status="error"}}[{window}]))'
            " / sum(increase(sre_assistant_llm_calls_total[{window}]))"
        ),
    ),
]


async def _prom_query_value(
    client: httpx.AsyncClient,
    prometheus_url: str,
    query: str,
) -> float | None:
    """Run a Prometheus instant query and return a single float value."""
    try:
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
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict):
                    value = first.get("value")
                    if isinstance(value, list) and len(value) >= 2:
                        return float(str(value[1]))
    except Exception:
        logger.debug("Baseline Prometheus query failed: %s", query)
    return None


async def compute_baselines(lookback_days: int) -> list[BaselineRecord]:
    """Compute metric baselines from Prometheus for the given lookback window.

    Queries avg, min, and max for each configured metric. P95 is computed
    only for histogram metrics (where it makes sense).

    Returns a list of BaselineRecord dicts ready for storage.
    """
    settings = get_settings()
    window = f"{lookback_days}d"
    now = datetime.now(UTC).isoformat()
    baselines: list[BaselineRecord] = []

    async with httpx.AsyncClient() as client:
        url = settings.prometheus_url

        for metric_name, labels_json, promql_template in _DEFAULT_BASELINE_METRICS:
            promql = promql_template.format(window=window)

            avg_val = await _prom_query_value(client, url, promql)
            if avg_val is None:
                logger.debug("Skipping baseline for %s — no data", metric_name)
                continue

            baselines.append(
                BaselineRecord(
                    id=0,  # Will be set by SQLite
                    metric_name=metric_name,
                    labels=labels_json,
                    avg_value=avg_val,
                    p95_value=None,  # Could be extended with quantile queries
                    min_value=None,
                    max_value=None,
                    sample_count=1,  # Single aggregate query per metric
                    window_days=lookback_days,
                    computed_at=now,
                )
            )

    logger.info("Computed %d baselines for %dd window", len(baselines), lookback_days)
    return baselines


async def compute_and_store_baselines(lookback_days: int) -> int:
    """Compute baselines and save them to the memory store.

    Returns the number of baselines stored. Returns 0 if memory is not configured.
    """
    if not is_memory_configured():
        return 0

    baselines = await compute_baselines(lookback_days)
    if not baselines:
        return 0

    conn = get_initialized_connection()
    try:
        save_baselines(conn, baselines)
    finally:
        conn.close()

    return len(baselines)
