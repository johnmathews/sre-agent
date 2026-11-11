"""Build-time and per-request context from the memory store.

Functions here are called during agent build or per-request to enrich the
agent's context with persistent memory data. All functions return empty strings
when memory is not configured or on any error — they must never crash the agent.
"""

import logging

from src.memory.store import (
    get_initialized_connection,
    get_open_incidents,
    get_recent_query_patterns,
    is_memory_configured,
)

logger = logging.getLogger(__name__)

MAX_OPEN_INCIDENTS = 5
MAX_RECENT_PATTERNS = 10


def get_open_incidents_context() -> str:
    """Format open incidents for system prompt injection.

    Returns a formatted string section or empty string if no incidents/not configured.
    """
    if not is_memory_configured():
        return ""

    try:
        conn = get_initialized_connection()
        try:
            incidents = get_open_incidents(conn)
            if not incidents:
                return ""

            display = incidents[:MAX_OPEN_INCIDENTS]
            lines: list[str] = [
                "\n## Active Incidents (from memory store)",
                "These were open as of agent startup. Use memory_search_incidents for the latest.\n",
            ]
            for inc in display:
                severity = inc["severity"]
                services = f" [{inc['services']}]" if inc["services"] else ""
                lines.append(f"- #{inc['id']}: {inc['title']} ({severity}){services} — since {inc['created_at'][:10]}")

            if len(incidents) > MAX_OPEN_INCIDENTS:
                lines.append(f"  ... and {len(incidents) - MAX_OPEN_INCIDENTS} more open incidents")

            return "\n".join(lines)
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to load open incidents for context", exc_info=True)
        return ""


def get_recent_patterns_context() -> str:
    """Format recent query patterns for system prompt injection.

    Returns a formatted string section or empty string if no patterns/not configured.
    """
    if not is_memory_configured():
        return ""

    try:
        conn = get_initialized_connection()
        try:
            patterns = get_recent_query_patterns(conn, limit=MAX_RECENT_PATTERNS)
            if not patterns:
                return ""

            lines: list[str] = [
                "\n## Recent User Questions",
                "These are recent topics the user has asked about:\n",
            ]
            for p in patterns:
                tools = f" (tools: {p['tool_names']})" if p["tool_names"] else ""
                lines.append(f"- {p['question']}{tools}")

            return "\n".join(lines)
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to load query patterns for context", exc_info=True)
        return ""


def enrich_alerts_with_incident_history(
    formatted_alerts: str,
    alert_names: list[str],
) -> str:
    """Append past incident history for active alert names.

    Called by grafana_get_alerts after formatting. Returns the original string
    plus an incident history section, or the original string unchanged if no
    history is found or memory is not configured.
    """
    if not is_memory_configured() or not alert_names:
        return formatted_alerts

    try:
        from src.memory.store import search_incidents

        conn = get_initialized_connection()
        try:
            history_lines: list[str] = []
            seen_alert_names: set[str] = set()

            for name in alert_names:
                if name in seen_alert_names or name == "unnamed":
                    continue
                seen_alert_names.add(name)
                incidents = search_incidents(conn, alert_name=name, limit=3)
                if incidents:
                    history_lines.append(f"\nPast incidents for '{name}':")
                    for inc in incidents:
                        status = "resolved" if inc["resolved_at"] else "open"
                        cause = f" — {inc['root_cause']}" if inc["root_cause"] else ""
                        line = f"  - #{inc['id']} ({status}, {inc['created_at'][:10]}): {inc['title']}{cause}"
                        history_lines.append(line)

            if not history_lines:
                return formatted_alerts

            return formatted_alerts + "\n--- Incident History from Memory ---" + "\n".join(history_lines)
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to enrich alerts with incident history", exc_info=True)
        return formatted_alerts


def enrich_with_baseline_context(
    formatted_result: str,
    metric_names: list[str],
) -> str:
    """Append baseline context for metrics returned by a Prometheus query.

    Called by prometheus_instant_query after formatting. Returns the original
    string plus baseline info, or unchanged if no baselines found.
    """
    if not is_memory_configured() or not metric_names:
        return formatted_result

    try:
        from src.memory.store import get_baseline

        conn = get_initialized_connection()
        try:
            baseline_lines: list[str] = []
            seen: set[str] = set()

            for name in metric_names:
                if name in seen:
                    continue
                seen.add(name)
                baseline = get_baseline(conn, name)
                if baseline:
                    parts = [f"avg={baseline['avg_value']:.4g}"]
                    if baseline["p95_value"] is not None:
                        parts.append(f"p95={baseline['p95_value']:.4g}")
                    if baseline["min_value"] is not None:
                        parts.append(f"min={baseline['min_value']:.4g}")
                    if baseline["max_value"] is not None:
                        parts.append(f"max={baseline['max_value']:.4g}")
                    baseline_lines.append(f"  {name}: {', '.join(parts)} ({baseline['window_days']}d window)")

            if not baseline_lines:
                return formatted_result

            header = "\n\nBaseline context (from memory store):\n"
            return formatted_result + header + "\n".join(baseline_lines)
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to enrich with baseline context", exc_info=True)
        return formatted_result


def detect_incident_suggestion(tool_names: list[str], response_text: str) -> str:
    """Check if a conversation looks like an alert investigation and suggest recording.

    Returns a suggestion string to append, or empty string if not applicable.
    """
    if not is_memory_configured():
        return ""

    # Must have used investigation-related tools
    investigation_tools = {
        "grafana_get_alerts",
        "loki_correlate_changes",
        "loki_query_logs",
        "memory_search_incidents",
    }
    if not investigation_tools.intersection(tool_names):
        return ""

    # Response must contain investigation-outcome language
    outcome_keywords = [
        "root cause",
        "caused by",
        "the issue was",
        "the problem was",
        "fixed by",
        "resolved by",
        "restarting",
        "the fix",
        "identified the",
    ]
    response_lower = response_text.lower()
    if not any(kw in response_lower for kw in outcome_keywords):
        return ""

    return (
        "\n\n---\n"
        "This investigation identified potential findings. "
        "Consider recording them with `memory_record_incident` "
        "so they can be referenced in future investigations."
    )
