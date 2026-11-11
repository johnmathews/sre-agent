"""LangChain tools for the agent memory store.

Four tools conditionally registered when MEMORY_DB_PATH is set:
- memory_search_incidents: Search past incidents
- memory_record_incident: Record a new incident
- memory_get_previous_report: Retrieve archived report(s)
- memory_check_baseline: Check if a metric value is normal
"""

import logging

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field

from src.memory.store import (
    get_baseline,
    get_initialized_connection,
    get_latest_report,
    get_reports,
    save_incident,
    search_incidents,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class SearchIncidentsInput(BaseModel):
    query: str | None = Field(
        None, description="Free-text keyword to search across title, description, root cause, and resolution"
    )
    alert_name: str | None = Field(None, description="Exact alert name to filter by (e.g. 'HighCPU')")
    service: str | None = Field(None, description="Service name substring to filter by (e.g. 'traefik')")
    limit: int = Field(10, description="Maximum number of results to return", ge=1, le=50)


class RecordIncidentInput(BaseModel):
    title: str = Field(..., description="One-line summary of the incident")
    description: str = Field(..., description="Detailed description of what happened")
    alert_name: str | None = Field(None, description="Name of the triggering alert (if applicable)")
    root_cause: str | None = Field(None, description="Identified root cause (if known)")
    resolution: str | None = Field(None, description="What was done to fix it (if resolved)")
    severity: str = Field("info", description="Severity level: info, warning, or critical")
    services: str = Field("", description="Comma-separated list of affected service names")


class GetPreviousReportInput(BaseModel):
    count: int = Field(1, description="Number of recent reports to retrieve", ge=1, le=10)


class CheckBaselineInput(BaseModel):
    metric_name: str = Field(..., description="Prometheus metric name (e.g. 'node_cpu_usage_ratio')")
    current_value: float = Field(..., description="Current metric value to compare against baseline")
    labels: str | None = Field(None, description='JSON label set to match (e.g. \'{"hostname": "media"}\')')


# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

_SEARCH_INCIDENTS_DESC = (
    "Search past incidents recorded in the agent's memory store. "
    "Returns matching incidents with their root causes and resolutions. "
    "Use this to check if a similar alert has fired before or find known patterns."
)

_RECORD_INCIDENT_DESC = (
    "Record a new incident in the agent's memory store. "
    "Use this after identifying a root cause or resolution during an investigation, "
    "so the knowledge is preserved for future sessions."
)

_GET_PREVIOUS_REPORT_DESC = (
    "Retrieve the most recent archived weekly reliability report(s). "
    "Use this to compare current state with past reports or answer questions about trends."
)

_CHECK_BASELINE_DESC = (
    "Check whether a metric value is within the normal range based on computed baselines. "
    "Returns baseline statistics (avg, p95, min, max) and an assessment of the current value."
)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@tool(args_schema=SearchIncidentsInput)  # pyright: ignore[reportUnknownParameterType]
def memory_search_incidents(
    query: str | None = None,
    alert_name: str | None = None,
    service: str | None = None,
    limit: int = 10,
) -> str:
    """Search past incidents recorded in the agent's memory store."""
    try:
        conn = get_initialized_connection()
    except ValueError:
        raise ToolException("Memory store not configured — set MEMORY_DB_PATH to enable") from None
    try:
        incidents = search_incidents(conn, query=query, alert_name=alert_name, service=service, limit=limit)
        if not incidents:
            return "No matching incidents found in memory."

        lines: list[str] = [f"Found {len(incidents)} incident(s):\n"]
        for inc in incidents:
            lines.append(f"--- Incident #{inc['id']} ({inc['severity']}) ---")
            lines.append(f"Title: {inc['title']}")
            lines.append(f"Date: {inc['created_at']}")
            if inc["alert_name"]:
                lines.append(f"Alert: {inc['alert_name']}")
            if inc["services"]:
                lines.append(f"Services: {inc['services']}")
            lines.append(f"Description: {inc['description']}")
            if inc["root_cause"]:
                lines.append(f"Root cause: {inc['root_cause']}")
            if inc["resolution"]:
                lines.append(f"Resolution: {inc['resolution']}")
            status = "Resolved" if inc["resolved_at"] else "Open"
            lines.append(f"Status: {status}")
            lines.append("")
        return "\n".join(lines)
    finally:
        conn.close()


memory_search_incidents.description = _SEARCH_INCIDENTS_DESC  # pyright: ignore[reportAttributeAccessIssue]
memory_search_incidents.handle_tool_error = True  # pyright: ignore[reportAttributeAccessIssue]


@tool(args_schema=RecordIncidentInput)  # pyright: ignore[reportUnknownParameterType]
def memory_record_incident(
    title: str,
    description: str,
    alert_name: str | None = None,
    root_cause: str | None = None,
    resolution: str | None = None,
    severity: str = "info",
    services: str = "",
) -> str:
    """Record a new incident in the agent's memory store."""
    try:
        conn = get_initialized_connection()
    except ValueError:
        raise ToolException("Memory store not configured — set MEMORY_DB_PATH to enable") from None
    try:
        incident_id = save_incident(
            conn,
            title=title,
            description=description,
            alert_name=alert_name,
            root_cause=root_cause,
            resolution=resolution,
            severity=severity,
            services=services,
        )
        return f"Incident #{incident_id} recorded successfully: {title}"
    finally:
        conn.close()


memory_record_incident.description = _RECORD_INCIDENT_DESC  # pyright: ignore[reportAttributeAccessIssue]
memory_record_incident.handle_tool_error = True  # pyright: ignore[reportAttributeAccessIssue]


@tool(args_schema=GetPreviousReportInput)  # pyright: ignore[reportUnknownParameterType]
def memory_get_previous_report(count: int = 1) -> str:
    """Retrieve the most recent archived weekly reliability report(s)."""
    try:
        conn = get_initialized_connection()
    except ValueError:
        raise ToolException("Memory store not configured — set MEMORY_DB_PATH to enable") from None
    try:
        if count == 1:
            report = get_latest_report(conn)
            if report is None:
                return "No previous reports found in memory."
            return f"Previous report (generated {report['generated_at']}):\n\n{report['report_markdown']}"
        else:
            reports = get_reports(conn, limit=count)
            if not reports:
                return "No previous reports found in memory."
            lines: list[str] = [f"Found {len(reports)} report(s):\n"]
            for r in reports:
                lines.append(f"--- Report from {r['generated_at']} ---")
                lines.append(f"Lookback: {r['lookback_days']} days")
                lines.append(f"Active alerts: {r['active_alerts']}, SLO failures: {r['slo_failures']}")
                lines.append(f"Log errors: {r['total_log_errors']}, Cost: ${r['estimated_cost']:.4f}")
                lines.append("")
            return "\n".join(lines)
    finally:
        conn.close()


memory_get_previous_report.description = _GET_PREVIOUS_REPORT_DESC  # pyright: ignore[reportAttributeAccessIssue]
memory_get_previous_report.handle_tool_error = True  # pyright: ignore[reportAttributeAccessIssue]


@tool(args_schema=CheckBaselineInput)  # pyright: ignore[reportUnknownParameterType]
def memory_check_baseline(
    metric_name: str,
    current_value: float,
    labels: str | None = None,
) -> str:
    """Check whether a metric value is within the normal range based on computed baselines."""
    try:
        conn = get_initialized_connection()
    except ValueError:
        raise ToolException("Memory store not configured — set MEMORY_DB_PATH to enable") from None
    try:
        baseline = get_baseline(conn, metric_name, labels)
        if baseline is None:
            return f"No baseline found for metric '{metric_name}'. Cannot assess normality."

        lines: list[str] = [
            f"Baseline for '{metric_name}' (computed {baseline['computed_at']}, "
            f"{baseline['window_days']}d window, {baseline['sample_count']} samples):",
            f"  Average: {baseline['avg_value']:.4f}",
        ]
        if baseline["p95_value"] is not None:
            lines.append(f"  P95:     {baseline['p95_value']:.4f}")
        if baseline["min_value"] is not None:
            lines.append(f"  Min:     {baseline['min_value']:.4f}")
        if baseline["max_value"] is not None:
            lines.append(f"  Max:     {baseline['max_value']:.4f}")

        lines.append(f"\nCurrent value: {current_value:.4f}")

        # Assessment
        if baseline["p95_value"] is not None and current_value > baseline["p95_value"]:
            lines.append("Assessment: ABOVE P95 — this value exceeds the 95th percentile baseline.")
        elif baseline["max_value"] is not None and current_value > baseline["max_value"]:
            lines.append("Assessment: ABOVE MAX — this value exceeds the historical maximum.")
        elif baseline["min_value"] is not None and current_value < baseline["min_value"]:
            lines.append("Assessment: BELOW MIN — this value is below the historical minimum.")
        else:
            lines.append("Assessment: WITHIN NORMAL RANGE.")

        return "\n".join(lines)
    finally:
        conn.close()


memory_check_baseline.description = _CHECK_BASELINE_DESC  # pyright: ignore[reportAttributeAccessIssue]
memory_check_baseline.handle_tool_error = True  # pyright: ignore[reportAttributeAccessIssue]


def get_memory_tools() -> list[BaseTool]:
    """Return the list of memory tools for agent registration.

    Returns an empty list if memory is not configured.
    """
    from src.memory.store import is_memory_configured

    if not is_memory_configured():
        return []

    # Cast needed because @tool decorator has incomplete stubs
    from typing import cast

    return cast(
        list[BaseTool],
        [
            memory_search_incidents,
            memory_record_incident,
            memory_get_previous_report,
            memory_check_baseline,
        ],
    )
