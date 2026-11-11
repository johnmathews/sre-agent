"""LangChain tools for querying Grafana's alerting API."""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict, get_args

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools._common import format_utc
from src.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15

ALERT_GROUPS_PATH = "/api/alertmanager/grafana/api/v2/alerts/groups"
RULES_PATH = "/api/prometheus/grafana/api/v1/rules"

AlertState = Literal["active", "suppressed", "unprocessed", "firing"]
RuleState = Literal["firing", "pending", "inactive", "recovering"]
RuleHealth = Literal["ok", "error", "nodata"]

ALERT_STATES: tuple[str, ...] = get_args(AlertState)
RULE_STATES: tuple[str, ...] = get_args(RuleState)
RULE_HEALTHS: tuple[str, ...] = get_args(RuleHealth)

MAX_NAME_FILTER_LENGTH = 100
MAX_LAST_ERROR_CHARS = 200
MAX_INSTANCES_PER_RULE = 5
NOT_REPORTED = "not reported by Grafana"

# Ruler-style state names some Grafana versions use, mapped to the Prometheus-style names.
_STATE_ALIASES = {"alerting": "firing", "normal": "inactive"}
# Labels repeated elsewhere in each alert's output line.
_SHOWN_ELSEWHERE = frozenset({"alertname", "severity", "grafana_folder"})


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


@dataclass(frozen=True)
class AlertInstance:
    """One alert instance of a rule, from the rules API's ``alerts[]``."""

    state: str
    active_at: str | None
    labels: dict[str, str]


@dataclass(frozen=True)
class AlertRuleView:
    """An alerting rule from the Prometheus-compatible rules API, normalised.

    ``state`` and ``health`` are lower case, or ``None`` when Grafana left them out.
    """

    name: str
    uid: str | None
    folder: str
    group: str
    state: str | None
    health: str | None
    severity: str
    summary: str
    last_evaluation: str | None
    last_error: str | None
    instances: list[AlertInstance] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class ParsedRules:
    """The alerting rules of one rules API response, and how many recording rules it held."""

    rules: list[AlertRuleView]
    recording_count: int = 0


# --- Input schemas ---


class GetAlertsInput(BaseModel):
    """Input for fetching current Grafana alerts."""

    state: AlertState | None = Field(
        default=None,
        description=(
            "Only alerts in this state: 'active' (firing), 'suppressed' (silenced or inhibited), 'unprocessed'. "
            "'firing' is an alias for 'active'. Omit to return all alerts."
        ),
    )


class GetAlertRulesInput(BaseModel):
    """Input for listing Grafana alert rules with their state and health."""

    state: RuleState | None = Field(
        default=None,
        description="Only rules in this evaluation state: 'firing', 'pending', 'inactive', 'recovering'.",
    )
    health: RuleHealth | None = Field(
        default=None,
        description="Only rules with this health: 'ok', 'error' (evaluation failed), 'nodata'.",
    )
    name_contains: str | None = Field(
        default=None,
        max_length=MAX_NAME_FILTER_LENGTH,
        description="Only rules whose name contains this text (case-insensitive).",
    )


# --- Validation ---


def _check_choice(name: str, value: str | None, accepted: tuple[str, ...]) -> None:
    """Refuse a value outside ``accepted``, as the MCP schema does, for direct and LangGraph calls."""
    if value is not None and value not in accepted:
        raise ToolException(f"Invalid {name} {value!r}. Accepted values: {', '.join(accepted)}.")


# --- HTTP helpers ---


def _grafana_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.grafana_service_account_token}",
        "Accept": "application/json",
    }


async def _grafana_get(path: str) -> object:
    """Make an authenticated GET request to the Grafana API; failures become ``ToolException``."""
    url = f"{get_settings().grafana_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_grafana_headers())
            _ = response.raise_for_status()
            data: object = response.json()
            return data
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Grafana API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Grafana at {get_settings().grafana_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Grafana request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e


# --- Alerts ---


def _select_alerts(groups: list[GrafanaAlertGroup], state_filter: str | None) -> list[tuple[str, GrafanaAlert]]:
    """(folder, alert) pairs whose state matches ``state_filter`` (all of them when it is ``None``)."""
    selected: list[tuple[str, GrafanaAlert]] = []
    for group in groups:
        group_name = group.get("labels", {}).get("grafana_folder", "unknown")
        for alert in group.get("alerts", []):
            alert_state = alert.get("status", {}).get("state", "unknown")
            if state_filter and alert_state != state_filter:
                continue
            selected.append((group_name, alert))
    return selected


def _format_alerts(groups: list[GrafanaAlertGroup], state_filter: str | None) -> str:
    """Format Grafana alert groups into a readable string for the LLM."""
    all_alerts = _select_alerts(groups, state_filter)

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

        label_str = _label_text(labels)
        if label_str:
            lines.append(f"  Labels: {label_str}")
        lines.append("")

    return "\n".join(lines)


def _label_text(labels: dict[str, str]) -> str:
    """``k="v"`` pairs, without Grafana's ``__``-prefixed internals and labels shown elsewhere."""
    return ", ".join(f'{k}="{v}"' for k, v in labels.items() if not k.startswith("__") and k not in _SHOWN_ELSEWHERE)


def _get_incident_history_enrichment(
    formatted_alerts: str,
    groups: list[GrafanaAlertGroup],
    state_filter: str | None,
) -> str:
    """Append incident history from memory for the listed alert names.

    A failed lookup logs a WARNING and returns the alerts unchanged.
    """
    try:
        from src.memory.context import enrich_alerts_with_incident_history

        selected = _select_alerts(groups, state_filter)
        alert_names = [name for _, alert in selected if (name := alert.get("labels", {}).get("alertname"))]
        if not alert_names:
            return formatted_alerts

        return enrich_alerts_with_incident_history(formatted_alerts, alert_names)
    except Exception:
        logger.warning("Grafana alerts: incident history lookup failed; returning alerts without it", exc_info=True)
        return formatted_alerts


# --- Alert rules ---


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


def _as_str(value: object) -> str | None:
    """A non-empty stripped string, else ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_labels(value: object) -> dict[str, str]:
    return {k: str(v) for k, v in _as_dict(value).items()}


def _normalise_state(value: object) -> str | None:
    text = _as_str(value)
    if text is None:
        return None
    lowered = text.lower()
    return _STATE_ALIASES.get(lowered, lowered)


def _normalise_health(value: object) -> str | None:
    text = _as_str(value)
    if text is None:
        return None
    return re.sub(r"[^a-z]", "", text.lower()) or None


def _parse_rules(payload: object) -> ParsedRules:
    """Parse a ``/api/prometheus/grafana/api/v1/rules`` response.

    Expects ``data.groups[].rules[]``. Missing rule fields become ``None`` and print as
    "not reported by Grafana"; only a response without ``data.groups`` is refused.
    """
    data = _as_dict(_as_dict(payload).get("data"))
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise ToolException(
            f"Unexpected response from Grafana rules API ({RULES_PATH}): expected data.groups, "
            + f"got {type(payload).__name__}"
        )

    rules: list[AlertRuleView] = []
    recording = 0
    for raw_group in _as_list(groups):  # pyright: ignore[reportUnknownArgumentType]
        group = _as_dict(raw_group)
        folder = _as_str(group.get("file")) or _as_str(group.get("folderUid")) or "unknown"
        group_name = _as_str(group.get("name")) or "unknown"
        for raw_rule in _as_list(group.get("rules")):
            rule = _as_dict(raw_rule)
            if not rule:
                continue
            if (_as_str(rule.get("type")) or "").lower() == "recording":
                recording += 1
                continue
            labels = _str_labels(rule.get("labels"))
            annotations = _str_labels(rule.get("annotations"))
            instances = [
                AlertInstance(
                    state=_as_str(inst.get("state")) or "unknown",
                    active_at=_as_str(inst.get("activeAt")),
                    labels=_str_labels(inst.get("labels")),
                )
                for inst in map(_as_dict, _as_list(rule.get("alerts")))
                if (_as_str(inst.get("state")) or "").lower() not in ("normal", "inactive")
            ]
            rules.append(
                AlertRuleView(
                    name=_as_str(rule.get("name")) or "unnamed",
                    uid=_as_str(rule.get("uid")),
                    folder=folder,
                    group=group_name,
                    state=_normalise_state(rule.get("state")),
                    health=_normalise_health(rule.get("health")),
                    severity=labels.get("severity", "none"),
                    summary=annotations.get("summary", ""),
                    last_evaluation=_as_str(rule.get("lastEvaluation")),
                    last_error=_as_str(rule.get("lastError")),
                    instances=instances,
                )
            )
    return ParsedRules(rules=rules, recording_count=recording)


def _format_time(raw: str | None) -> str:
    """A Grafana timestamp in UTC; "never" for Go's zero time, the raw text if it does not parse."""
    if raw is None:
        return NOT_REPORTED
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:40]
    if parsed.year <= 1:
        return "never"
    return format_utc(parsed)


def _counts(values: list[str | None], order: tuple[str, ...]) -> str:
    """``"firing 1, inactive 3"`` in ``order``, then other values, then those not reported."""
    counter = Counter(v if v is not None else NOT_REPORTED for v in values)
    keys = [k for k in order if counter[k]]
    keys += sorted(k for k in counter if k not in order and k != NOT_REPORTED)
    if counter[NOT_REPORTED]:
        keys.append(NOT_REPORTED)
    return ", ".join(f"{k} {counter[k]}" for k in keys)


def _needs_detail(rule: AlertRuleView) -> bool:
    """Everything except an inactive rule with health ok is printed in full."""
    return not (rule.state == "inactive" and rule.health == "ok")


def _detail_order(rule: AlertRuleView) -> tuple[int, str]:
    rank = {"firing": 0, "pending": 1, "recovering": 2}.get(rule.state or "", 3)
    return (rank, rule.name.lower())


def _rule_place(rule: AlertRuleView) -> str:
    uid = f", uid={rule.uid}" if rule.uid else ""
    return f"folder={rule.folder}, group={rule.group}, severity={rule.severity}{uid}"


def _format_rule_detail(rule: AlertRuleView) -> list[str]:
    state = rule.state or f"state {NOT_REPORTED}"
    health = f"health {rule.health}" if rule.health else f"health {NOT_REPORTED}"
    lines = [
        f"- [{state}, {health}] {rule.name}",
        f"  {_rule_place(rule)}",
        f"  Last evaluation: {_format_time(rule.last_evaluation)}",
    ]
    if rule.last_error:
        lines.append(f"  Last error: {rule.last_error[:MAX_LAST_ERROR_CHARS]}")
    elif rule.health == "error":
        lines.append(f"  Last error: {NOT_REPORTED}")
    if rule.summary:
        lines.append(f"  Summary: {rule.summary}")
    if rule.instances:
        lines.append(f"  Active alerts: {len(rule.instances)}")
        for inst in rule.instances[:MAX_INSTANCES_PER_RULE]:
            since = f" since {_format_time(inst.active_at)}" if inst.active_at else ""
            labels = _label_text(inst.labels) or "no labels"
            lines.append(f"    - {inst.state}{since}: {labels}")
        hidden = len(rule.instances) - MAX_INSTANCES_PER_RULE
        if hidden > 0:
            lines.append(f"    ... and {hidden} more")
    return lines


def _format_alert_rules(
    parsed: ParsedRules,
    *,
    state: str | None = None,
    health: str | None = None,
    name_contains: str | None = None,
) -> str:
    """Summarise alert rules: counts by state and health, then the matching rules.

    Firing, pending, recovering and unhealthy rules print in full; inactive rules with health
    ok print on one line each.
    """
    rules = parsed.rules
    if not rules:
        note = f" ({parsed.recording_count} recording rule(s) not shown)" if parsed.recording_count else ""
        return f"No alert rules found{note}."

    needle = name_contains.lower() if name_contains else None
    matching = [
        r
        for r in rules
        if (state is None or r.state == state)
        and (health is None or r.health == health)
        and (needle is None or needle in r.name.lower())
    ]

    filters = [f"{k}={v}" for k, v in (("state", state), ("health", health), ("name_contains", name_contains)) if v]
    filter_note = f" (filters: {', '.join(filters)})" if filters else ""
    lines = [
        f"{len(rules)} alert rule(s) in Grafana.",
        f"By state: {_counts([r.state for r in rules], RULE_STATES)}",
        f"By health: {_counts([r.health for r in rules], RULE_HEALTHS)}",
    ]
    if parsed.recording_count:
        lines.append(f"{parsed.recording_count} recording rule(s) not shown.")
    lines.append(f"Showing {len(matching)} of {len(rules)} alert rules{filter_note}.")

    detailed = sorted((r for r in matching if _needs_detail(r)), key=_detail_order)
    quiet = sorted((r for r in matching if not _needs_detail(r)), key=lambda r: r.name.lower())
    if detailed:
        lines += ["", "Firing, pending, recovering or unhealthy:"]
        for rule in detailed:
            lines += _format_rule_detail(rule)
    if quiet:
        lines += ["", "Inactive, health ok:"]
        lines += [f"- {r.name} ({_rule_place(r)})" for r in quiet]
    return "\n".join(lines)


# --- Tool descriptions ---

TOOL_DESCRIPTION_ALERTS = (
    "Fetch current alerts from Grafana's alerting system. "
    "Use this to answer questions like 'what alerts are firing?', 'are there any active alerts?', "
    "or 'summarize current alerts'.\n\n"
    "Returns alert name, severity, state (active/suppressed/unprocessed), labels, "
    "annotations (summary, description), and start time.\n\n"
    "Optionally filter by state: 'active' (firing; 'firing' is accepted as an alias), "
    "'suppressed' (silenced), 'unprocessed'."
)

TOOL_DESCRIPTION_RULES = (
    "List Grafana alert rules with their evaluation state (firing, pending, inactive, recovering) "
    "and health (ok, error, nodata). "
    "Use this to answer questions like 'which alert rules are firing or pending?', "
    "'are any alert rules failing to evaluate?', or 'what alerts are configured?'.\n\n"
    "Returns counts by state and health, then firing, pending and unhealthy rules in full "
    "(folder, group, severity, last evaluation, last error, active alert instances) and "
    "inactive healthy rules one per line. Filter with state, health and name_contains."
)


@tool("grafana_get_alerts", args_schema=GetAlertsInput)
async def grafana_get_alerts(state: str | None = None) -> str:
    """Fetch current alerts from Grafana. See TOOL_DESCRIPTION_ALERTS."""
    logger.info("Fetching Grafana alerts (state=%s)", state)
    _check_choice("state", state, ALERT_STATES)
    upstream_state = "active" if state == "firing" else state

    # The endpoint's ``filter`` parameter takes label matchers, not states: filter locally.
    payload = await _grafana_get(ALERT_GROUPS_PATH)
    if not isinstance(payload, list):
        raise ToolException(
            f"Unexpected response from Grafana alerts API: expected a list, got {type(payload).__name__}"
        )
    groups: list[GrafanaAlertGroup] = payload  # pyright: ignore[reportUnknownVariableType]

    formatted = _format_alerts(groups, upstream_state)
    return _get_incident_history_enrichment(formatted, groups, upstream_state)


grafana_get_alerts.description = TOOL_DESCRIPTION_ALERTS
grafana_get_alerts.handle_tool_error = True


@tool("grafana_get_alert_rules", args_schema=GetAlertRulesInput)
async def grafana_get_alert_rules(
    state: str | None = None,
    health: str | None = None,
    name_contains: str | None = None,
) -> str:
    """List Grafana alert rules with state and health. See TOOL_DESCRIPTION_RULES."""
    logger.info("Fetching Grafana alert rules (state=%s, health=%s, name_contains=%s)", state, health, name_contains)
    _check_choice("state", state, RULE_STATES)
    _check_choice("health", health, RULE_HEALTHS)
    if name_contains is not None and len(name_contains) > MAX_NAME_FILTER_LENGTH:
        raise ToolException(f"name_contains is {len(name_contains)} characters; at most {MAX_NAME_FILTER_LENGTH}.")

    payload = await _grafana_get(RULES_PATH)
    return _format_alert_rules(_parse_rules(payload), state=state, health=health, name_contains=name_contains)


grafana_get_alert_rules.description = TOOL_DESCRIPTION_RULES
grafana_get_alert_rules.handle_tool_error = True
