"""Prometheus metric definitions for SRE assistant self-instrumentation.

All metrics are module-level singletons registered with the default
prometheus_client registry.  Import them wherever instrumentation is needed.
"""

from prometheus_client import Counter, Gauge, Histogram, Info

# ---------------------------------------------------------------------------
# Histogram bucket definitions
# ---------------------------------------------------------------------------

REQUEST_DURATION_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0)
TOOL_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0)

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
# Tool-call metrics (populated by callback handler)
# ---------------------------------------------------------------------------

TOOL_CALL_DURATION = Histogram(
    "sre_assistant_tool_call_duration_seconds",
    "Duration of individual tool calls in seconds",
    labelnames=["tool_name"],
    buckets=TOOL_DURATION_BUCKETS,
)

TOOL_CALLS_TOTAL = Counter(
    "sre_assistant_tool_calls_total",
    "Total number of tool calls",
    labelnames=["tool_name", "status"],
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
