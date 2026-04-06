"""LangChain tool for querying Grafana's alerting API."""

import logging
from typing import TypedDict

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15


# --- Grafana alert response types ---


class GrafanaAlertStatus(TypedDict, total=False):
    state: str
    silencedBy: list[str]
    inhibitedBy: list[str]


class GrafanaAlertAnnotations(TypedDict, total=False):
    summary: str
    description: str
    runbook_url: str
    __dashboardUid__: str
    __panelId__: str


class GrafanaAlert(TypedDict, total=False):
    labels: dict[str, str]
    annotations: GrafanaAlertAnnotations
    startsAt: str
    endsAt: str
    generatorURL: str
    fingerprint: str
    status: GrafanaAlertStatus


class GrafanaAlertGroup(TypedDict, total=False):
    labels: dict[str, str]
    receiver: dict[str, str]
    alerts: list[GrafanaAlert]


class GrafanaAlertRule(TypedDict, total=False):
    uid: str
    title: str
    condition: str
    folderUID: str
    ruleGroup: str
    for_: str
    labels: dict[str, str]
    annotations: GrafanaAlertAnnotations


# --- Input schemas ---


class GetAlertsInput(BaseModel):
    """Input for fetching active Grafana alerts."""

    state: str | None = Field(
        default=None,
        description=(
            "Filter by alert state. Options: 'active', 'suppressed', 'unprocessed'. Omit to return all alerts."
        ),
    )


class GetAlertRulesInput(BaseModel):
    """Input for fetching Grafana alert rule definitions."""

    # No required fields — fetches all rules
    pass


# --- HTTP helpers ---


def _grafana_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.grafana_service_account_token}",
        "Accept": "application/json",
    }


async def _grafana_get(
    path: str,
    params: dict[str, str] | None = None,
) -> list[GrafanaAlertGroup] | list[GrafanaAlertRule]:
    """Make an authenticated GET request to the Grafana API."""
    url = f"{get_settings().grafana_url}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=_grafana_headers(), params=params)
        _ = response.raise_for_status()
        data: list[GrafanaAlertGroup] | list[GrafanaAlertRule] = response.json()  # pyright: ignore[reportAny]
        return data


# --- Result formatting ---


def _format_alerts(groups: list[GrafanaAlertGroup], state_filter: str | None) -> str:
    """Format Grafana alert groups into a readable string for the LLM."""
    all_alerts: list[tuple[str, GrafanaAlert]] = []
    for group in groups:
        group_labels = group.get("labels", {})
        group_name = group_labels.get("grafana_folder", "unknown")
        for alert in group.get("alerts", []):
            alert_state = alert.get("status", {}).get("state", "unknown")
            if state_filter and alert_state != state_filter:
                continue
            all_alerts.append((group_name, alert))

    if not all_alerts:
        if state_filter:
            return f"No alerts with state '{state_filter}' found."
        return "No alerts found."

    lines: list[str] = [f"Found {len(all_alerts)} alert(s):\n"]

    for group_name, alert in all_alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        status = alert.get("status", {})

        alertname = labels.get("alertname", "unnamed")
        severity = labels.get("severity", "none")
        state = status.get("state", "unknown")
        summary = annotations.get("summary", "")
        description = annotations.get("description", "")
        starts_at = alert.get("startsAt", "unknown")

        lines.append(f"- [{state}] {alertname} (severity={severity}, folder={group_name})")
        lines.append(f"  Started: {starts_at}")
        if summary:
            lines.append(f"  Summary: {summary}")
        if description:
            lines.append(f"  Description: {description}")

        # Show relevant labels (skip internal ones)
        extra_labels = {
            k: v
            for k, v in labels.items()
            if k not in ("alertname", "severity", "grafana_folder", "__alert_rule_uid__")
        }
        if extra_labels:
            label_str = ", ".join(f'{k}="{v}"' for k, v in extra_labels.items())
            lines.append(f"  Labels: {label_str}")
        lines.append("")

    return "\n".join(lines)


def _format_alert_rules(rules: list[GrafanaAlertRule]) -> str:
    """Format Grafana alert rule definitions into a readable string for the LLM."""
    if not rules:
        return "No alert rules found."

    lines: list[str] = [f"Found {len(rules)} alert rule(s):\n"]

    for rule in rules:
        title = rule.get("title", "untitled")
        uid = rule.get("uid", "")
        folder = rule.get("folderUID", "unknown")
        group = rule.get("ruleGroup", "unknown")
        labels = rule.get("labels", {})
        annotations = rule.get("annotations", {})
        severity = labels.get("severity", "none")
        summary = annotations.get("summary", "")

        lines.append(f"- {title} (uid={uid}, severity={severity})")
        lines.append(f"  Folder: {folder}, Group: {group}")
        if summary:
            lines.append(f"  Summary: {summary}")
        lines.append("")

    return "\n".join(lines)


def _get_incident_history_enrichment(
    formatted_alerts: str,
    groups: list[GrafanaAlertGroup],
    state_filter: str | None,
) -> str:
    """Extract alert names from groups and enrich with incident history from memory."""
    try:
        from src.memory.context import enrich_alerts_with_incident_history

        alert_names: list[str] = []
        for group in groups:
            for alert in group.get("alerts", []):
                alert_state = alert.get("status", {}).get("state", "unknown")
                if state_filter and alert_state != state_filter:
                    continue
                name = alert.get("labels", {}).get("alertname", "")
                if name:
                    alert_names.append(name)

        if not alert_names:
            return formatted_alerts

        return enrich_alerts_with_incident_history(formatted_alerts, alert_names)
    except Exception:
        return formatted_alerts


# --- Tool descriptions ---

TOOL_DESCRIPTION_ALERTS = HOMELAB_CONTEXT + (
    "Fetch active alerts from Grafana's alerting system. "
    "Use this to answer questions like 'what alerts are firing?', 'are there any active alerts?', "
    "or 'summarize current alerts'.\n\n"
    "Returns alert name, severity, state (active/suppressed/unprocessed), labels, "
    "annotations (summary, description), and start time.\n\n"
    "Optionally filter by state: 'active' (firing), 'suppressed' (silenced), 'unprocessed'."
)

TOOL_DESCRIPTION_RULES = HOMELAB_CONTEXT + (
    "Fetch alert rule definitions from Grafana. "
    "Use this to answer questions like 'what alerts are configured?', "
    "'what conditions trigger the high CPU alert?', or 'list all alert rules'.\n\n"
    "Returns rule name, UID, folder, group, severity, and summary. "
    "This shows the alert DEFINITIONS, not whether they are currently firing — "
    "use the alerts tool for that."
)


@tool("grafana_get_alerts", args_schema=GetAlertsInput)
async def grafana_get_alerts(state: str | None = None) -> str:
    """Fetch active alerts from Grafana. See TOOL_DESCRIPTION_ALERTS."""
    logger.info("Fetching Grafana alerts (state=%s)", state)

    params: dict[str, str] = {}
    if state:
        params["filter"] = state

    try:
        groups = await _grafana_get("/api/alertmanager/grafana/api/v2/alerts/groups", params)
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Grafana API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Grafana at {get_settings().grafana_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Grafana request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e

    formatted = _format_alerts(groups, state)  # type: ignore[arg-type]
    return _get_incident_history_enrichment(formatted, groups, state)  # type: ignore[arg-type]


grafana_get_alerts.description = TOOL_DESCRIPTION_ALERTS
grafana_get_alerts.handle_tool_error = True


@tool("grafana_get_alert_rules", args_schema=GetAlertRulesInput)
async def grafana_get_alert_rules() -> str:
    """Fetch alert rule definitions from Grafana. See TOOL_DESCRIPTION_RULES."""
    logger.info("Fetching Grafana alert rules")

    try:
        rules = await _grafana_get("/api/v1/provisioning/alert-rules")
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Grafana API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Grafana at {get_settings().grafana_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Grafana request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e

    return _format_alert_rules(rules)  # type: ignore[arg-type]


grafana_get_alert_rules.description = TOOL_DESCRIPTION_RULES
grafana_get_alert_rules.handle_tool_error = True
