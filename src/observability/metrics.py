"""Prometheus metric definitions for SRE assistant self-instrumentation.

All metrics are module-level singletons registered with the default
prometheus_client registry.  Import them wherever instrumentation is needed.
"""

import logging
import math
import time
from collections.abc import Callable
from typing import Any, Protocol

from prometheus_client import Counter, Gauge, Histogram, Info

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Histogram bucket definitions
# ---------------------------------------------------------------------------

REQUEST_DURATION_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0)
TOOL_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0)
TOOL_RESULT_CHARS_BUCKETS = (500, 2_000, 8_000, 32_000, 128_000, 512_000)

# ---------------------------------------------------------------------------
# Request-level metrics
# ---------------------------------------------------------------------------

REQUEST_DURATION = Histogram(
    "sre_assistant_request_duration_seconds",
    "End-to-end request duration in seconds",
    labelnames=["endpoint"],
    buckets=REQUEST_DURATION_BUCKETS,
)

REQUESTS_TOTAL = Counter(
    "sre_assistant_requests_total",
    "Total number of requests",
    labelnames=["endpoint", "status"],
)

REQUESTS_IN_PROGRESS = Gauge(
    "sre_assistant_requests_in_progress",
    "Number of requests currently being processed",
    labelnames=["endpoint"],
)

# ---------------------------------------------------------------------------
# Tool-call metrics (recorded by src.observability.tool_calls.observe_tool_call)
# ---------------------------------------------------------------------------

TOOL_CALL_DURATION = Histogram(
    "sre_assistant_tool_call_duration_seconds",
    "Duration of individual tool calls in seconds, by source (mcp, agent)",
    labelnames=["tool_name", "source"],
    buckets=TOOL_DURATION_BUCKETS,
)

TOOL_CALLS_TOTAL = Counter(
    "sre_assistant_tool_calls_total",
    "Tool calls by outcome (status), source (mcp, agent) and client",
    labelnames=["tool_name", "status", "source", "client"],
)

TOOL_RESULT_CHARS = Histogram(
    "sre_assistant_tool_result_chars",
    "Characters of text returned by successful tool calls, by source (mcp, agent)",
    labelnames=["tool_name", "source"],
    buckets=TOOL_RESULT_CHARS_BUCKETS,
)

MCP_HTTP_REQUESTS_TOTAL = Counter(
    "sre_assistant_mcp_http_requests_total",
    "HTTP requests to /mcp by client label and Access check result (verified, missing, invalid, unconfigured, off)",
    labelnames=["client", "access"],
)

# ---------------------------------------------------------------------------
# LLM metrics (populated by callback handler)
# ---------------------------------------------------------------------------

LLM_CALLS_TOTAL = Counter(
    "sre_assistant_llm_calls_total",
    "Total number of LLM calls",
    labelnames=["status"],
)

LLM_TOKEN_USAGE = Counter(
    "sre_assistant_llm_token_usage",
    "Total LLM token usage",
    labelnames=["type"],
)

LLM_ESTIMATED_COST = Counter(
    "sre_assistant_llm_estimated_cost_dollars",
    "Estimated cumulative LLM cost in USD",
)

# ---------------------------------------------------------------------------
# Health / info metrics
# ---------------------------------------------------------------------------

COMPONENT_HEALTHY = Gauge(
    "sre_assistant_component_healthy",
    "Whether a dependency component is healthy (1=healthy, 0=unhealthy)",
    labelnames=["component"],
)

# 1 when MEMORY_DB_PATH is set and the database opens at startup; 0 when it is unset (the owner's choice)
# or the open fails (a directory, a missing parent, a read-only mount). Set once by log_memory_status().
MEMORY_STORE_ENABLED = Gauge(
    "sre_assistant_memory_store_enabled",
    "Whether the memory store is configured and usable (1=usable, 0=unset or broken)",
)

APP_INFO = Info(
    "sre_assistant",
    "SRE assistant build information",
)

# ---------------------------------------------------------------------------
# Report metrics
# ---------------------------------------------------------------------------

REPORT_DURATION_BUCKETS = (5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 180.0)

REPORTS_TOTAL = Counter(
    "sre_assistant_reports_total",
    "Total number of generated reports",
    labelnames=["trigger", "status"],
)

REPORT_DURATION = Histogram(
    "sre_assistant_report_duration_seconds",
    "Time taken to generate a report in seconds",
    buckets=REPORT_DURATION_BUCKETS,
)

# ---------------------------------------------------------------------------
# OAuth token metrics (SDK / Anthropic path)
# ---------------------------------------------------------------------------

OAUTH_TOKEN_EXPIRY = Gauge(
    "sre_assistant_oauth_token_expiry_seconds",
    "Unix timestamp when the current OAuth access token expires (0 = no token)",
)

OAUTH_TOKEN_REMAINING = Gauge(
    "sre_assistant_oauth_token_remaining_seconds",
    "Seconds until the OAuth access token expires (negative = expired)",
)

OAUTH_REFRESH_TOTAL = Counter(
    "sre_assistant_oauth_refresh_total",
    "Total OAuth token refresh attempts",
    labelnames=["status"],
)

# Registered on demand by observe_claude_credentials(), not at import: an
# unlabelled Gauge exports 0 from the moment it exists, which on the OpenAI path
# would read as "login needs renewing" for a login that is not in use.
_claude_credentials_gauge: Gauge | None = None


def observe_claude_credentials(read_healthy: Callable[[], float]) -> None:
    """Export ``sre_assistant_claude_credentials_healthy``, read on every scrape.

    1 while the Claude OAuth login is usable, 0 when a human must re-authenticate
    on the host. Called from the FastAPI lifespan only when
    ``LLM_PROVIDER=anthropic``, so the series is absent otherwise. Grafana's
    "SRE agent: Claude login needs renewing" rule reads it.
    """
    global _claude_credentials_gauge  # noqa: PLW0603

    if _claude_credentials_gauge is None:
        _claude_credentials_gauge = Gauge(
            "sre_assistant_claude_credentials_healthy",
            "1 while the Claude OAuth login is usable, 0 when a human must re-authenticate",
        )
    _claude_credentials_gauge.set_function(read_healthy)


AGENT_ERRORS_TOTAL = Counter(
    "sre_assistant_agent_errors_total",
    "Total classified agent failures by reason",
    labelnames=["reason"],
)

# ---------------------------------------------------------------------------
# Upgrade Advisor metrics
# ---------------------------------------------------------------------------

UPGRADE_RUNS_TOTAL = Counter(
    "sre_assistant_upgrade_runs_total",
    "Daily Upgrade Advisor runs by status (success, error)",
    labelnames=["status"],
)

UPGRADE_VERDICTS_TOTAL = Counter(
    "sre_assistant_upgrade_verdicts_total",
    "Upgrade Advisor verdicts saved, by verdict (upgrade, hold, needs_you)",
    labelnames=["verdict"],
)

UPGRADE_LAST_SUCCESS = Gauge(
    "sre_assistant_upgrade_last_success_timestamp",
    "Unix timestamp of the last completed Upgrade Advisor run (email or not)",
)

UPGRADE_DEPLOYS_TOTAL = Counter(
    "sre_assistant_upgrade_deploys_total",
    "Final outcome of each approved deploy: verified (ok, failed, digest_mismatch) or never verified (skipped, failed)",
    labelnames=["outcome"],
)

# ---------------------------------------------------------------------------
# Runbook index metrics
# ---------------------------------------------------------------------------

RUNBOOK_INDEX_REBUILDS_TOTAL = Counter(
    "sre_assistant_runbook_index_rebuilds_total",
    "Runbook index rebuilds started by the server's check loop, by status (success, error)",
    labelnames=["status"],
)
for _status in ("success", "error"):
    RUNBOOK_INDEX_REBUILDS_TOTAL.labels(status=_status)


class RunbookIndexReading(Protocol):
    """What the runbook index gauges read: src.agent.retrieval.index.IndexState satisfies it."""

    @property
    def in_sync(self) -> bool: ...

    @property
    def chunks(self) -> int: ...

    @property
    def built_at(self) -> float: ...


_runbook_index_gauges: tuple[Gauge, Gauge, Gauge] | None = None


def observe_runbook_index(read: Callable[[], RunbookIndexReading], max_age_s: float = 5.0) -> None:
    """Export the runbook index gauges, read from the store on each scrape.

    ``sre_assistant_runbook_index_in_sync`` is 1 when the live collection was built from the runbooks present now
    and holds every chunk, 0 otherwise. Grafana's "SRE agent: runbook index out of date" rule reads it. One reading
    serves all three gauges for ``max_age_s``, so a scrape reads the store once. A failed read exports 0s.
    """
    global _runbook_index_gauges  # noqa: PLW0603

    if _runbook_index_gauges is None:
        _runbook_index_gauges = (
            Gauge(
                "sre_assistant_runbook_index_in_sync",
                "1 when the runbook index matches the runbooks in this image and holds every chunk, else 0",
            ),
            Gauge("sre_assistant_runbook_index_chunks", "Chunks in the live runbook index collection"),
            Gauge(
                "sre_assistant_runbook_index_built_timestamp_seconds",
                "Unix time the live runbook index was built (0 = no manifest)",
            ),
        )
    in_sync, chunks, built_at = _runbook_index_gauges
    cache: dict[str, Any] = {"at": -math.inf, "reading": None, "error": None}

    def reading() -> RunbookIndexReading | None:
        now = time.monotonic()
        if now - cache["at"] >= max_age_s:
            try:
                cache["reading"] = read()
                cache["error"] = None
            except Exception as exc:
                if cache["error"] != str(exc):
                    logger.warning("Reading the runbook index for its metrics failed", exc_info=True)
                cache["reading"], cache["error"] = None, str(exc)
            cache["at"] = now
        current: RunbookIndexReading | None = cache["reading"]
        return current

    def _in_sync() -> float:
        current = reading()
        return 1.0 if current is not None and current.in_sync else 0.0

    def _chunks() -> float:
        current = reading()
        return float(current.chunks) if current is not None else 0.0

    def _built_at() -> float:
        current = reading()
        return current.built_at if current is not None else 0.0

    in_sync.set_function(_in_sync)
    chunks.set_function(_chunks)
    built_at.set_function(_built_at)


# ---------------------------------------------------------------------------
# Incident explainer metrics
# ---------------------------------------------------------------------------

INCIDENT_EVENTS_TOTAL = Counter(
    "sre_assistant_incident_events_total",
    "Gatus webhook events received by POST /incidents/gatus",
    labelnames=["event", "outcome"],
)

INCIDENT_EXPLANATIONS_TOTAL = Counter(
    "sre_assistant_incident_explanations_total",
    "Finished incident explanations by outcome (explained, fallback, not_sent, error)",
    labelnames=["outcome"],
)

# ---------------------------------------------------------------------------
# Cost pricing (USD per token) — GPT-4o-mini as default
# ---------------------------------------------------------------------------

# Prices per token for cost estimation.  Keys are model name prefixes;
# the callback handler picks the best match.
COST_PER_TOKEN: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"prompt": 0.15 / 1_000_000, "completion": 0.60 / 1_000_000},
    "gpt-4o": {"prompt": 2.50 / 1_000_000, "completion": 10.00 / 1_000_000},
    "gpt-4-turbo": {"prompt": 10.00 / 1_000_000, "completion": 30.00 / 1_000_000},
    "claude-sonnet-4": {"prompt": 3.00 / 1_000_000, "completion": 15.00 / 1_000_000},
    "claude-opus-4": {"prompt": 15.00 / 1_000_000, "completion": 75.00 / 1_000_000},
    "claude-haiku-4": {"prompt": 0.80 / 1_000_000, "completion": 4.00 / 1_000_000},
}
DEFAULT_COST_PER_TOKEN: dict[str, float] = {"prompt": 2.50 / 1_000_000, "completion": 10.00 / 1_000_000}
