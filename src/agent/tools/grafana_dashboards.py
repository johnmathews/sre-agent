"""LangChain tools for querying Grafana dashboard configuration."""

import logging
import re
from typing import Any

import httpx
from langchain_core.tools import ToolException, tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.tools import HOMELAB_CONTEXT
from src.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15


# --- Grafana dashboard response types ---


# Using dict[str, Any] for deeply nested Grafana JSON structures where
# TypedDict would be impractical (fieldConfig, overrides, transformations).
# The formatting functions extract only the fields we care about.


# --- Input schemas ---


class GetDashboardInput(BaseModel):
    """Input for fetching a Grafana dashboard."""

    dashboard: str = Field(
        description=(
            "Dashboard UID (e.g. 'dekkfibh9454wb') or dashboard name to search for. "
            "If the value contains spaces or looks like a name, the tool searches by name first."
        ),
    )
    panel: str | None = Field(
        default=None,
        description=(
            "Optional panel title to extract. If provided, returns only the matching panel's "
            "details (title, type, datasource, queries, thresholds, units, overrides). "
            "Case-insensitive partial match."
        ),
    )


class SearchDashboardsInput(BaseModel):
    """Input for searching Grafana dashboards."""

    query: str = Field(description="Search query for dashboard name", min_length=1)


# --- HTTP helpers ---


def _grafana_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.grafana_service_account_token}",
        "Accept": "application/json",
    }


async def _grafana_get_json(
    path: str,
    params: dict[str, str] | None = None,
) -> Any:
    """Make an authenticated GET request to the Grafana API."""
    url = f"{get_settings().grafana_url}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=_grafana_headers(), params=params)
        _ = response.raise_for_status()
        result: Any = response.json()
        return result


async def _search_dashboards(query: str) -> list[dict[str, Any]]:
    """Search Grafana dashboards by name."""
    results: list[dict[str, Any]] = await _grafana_get_json("/api/search", params={"query": query, "type": "dash-db"})
    return results


async def _get_dashboard_by_uid(uid: str) -> dict[str, Any]:
    """Fetch a dashboard by UID."""
    result: dict[str, Any] = await _grafana_get_json(f"/api/dashboards/uid/{uid}")
    return result


# --- Panel helpers ---


def _is_likely_uid(value: str) -> bool:
    """Heuristic: Grafana UIDs are short alphanumeric strings without spaces."""
    return bool(re.match(r"^[a-zA-Z0-9_-]+$", value)) and len(value) <= 40


def _flatten_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively flatten row panels that contain nested sub-panels."""
    flat: list[dict[str, Any]] = []
    for panel in panels:
        if panel.get("type") == "row":
            # Row panels may have nested panels
            nested = panel.get("panels", [])
            if nested:
                flat.extend(_flatten_panels(nested))
            else:
                flat.append(panel)
        else:
            flat.append(panel)
    return flat


def _find_panel(panels: list[dict[str, Any]], title: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Find a panel by title (case-insensitive partial match).

    Returns (matched_panel, other_partial_matches) so we can suggest alternatives.
    """
    flat = _flatten_panels(panels)
    title_lower = title.lower()

    # Try exact match first
    for panel in flat:
        panel_title = panel.get("title", "")
        if panel_title.lower() == title_lower:
            return panel, []

    # Partial match
    matches: list[dict[str, Any]] = []
    for panel in flat:
        panel_title = panel.get("title", "")
        if title_lower in panel_title.lower():
            matches.append(panel)

    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return matches[0], [m.get("title", "") for m in matches[1:]]
    return None, []


# --- Formatting ---


def _format_target(target: dict[str, Any], idx: int) -> list[str]:
    """Format a single panel target/query."""
    lines: list[str] = []
    ref_id = target.get("refId", chr(65 + idx))
    expr = target.get("expr", "")
    loki_expr = target.get("expression", "")
    legend = target.get("legendFormat", "")
    ds = target.get("datasource", {})
    ds_type = ds.get("type", "") if isinstance(ds, dict) else ""

    query = expr or loki_expr
    if query:
        lines.append(f"    [{ref_id}] {query}")
        if legend:
            lines.append(f"        Legend: {legend}")
        if ds_type:
            lines.append(f"        Datasource type: {ds_type}")
    return lines


def _format_field_config(field_config: dict[str, Any]) -> list[str]:
    """Format fieldConfig (thresholds, units, overrides)."""
    lines: list[str] = []
    defaults = field_config.get("defaults", {})

    unit = defaults.get("unit", "")
    if unit:
        lines.append(f"  Unit: {unit}")

    decimals = defaults.get("decimals")
    if decimals is not None:
        lines.append(f"  Decimals: {decimals}")

    # Thresholds
    thresholds = defaults.get("thresholds", {})
    steps = thresholds.get("steps", [])
    if steps:
        threshold_parts: list[str] = []
        for step in steps:
            value = step.get("value")
            color = step.get("color", "")
            if value is None:
                threshold_parts.append(f"base({color})")
            else:
                threshold_parts.append(f"{value}({color})")
        lines.append(f"  Thresholds: {' → '.join(threshold_parts)}")

    # Overrides
    overrides = field_config.get("overrides", [])
    if overrides:
        lines.append(f"  Overrides ({len(overrides)}):")
        for override in overrides[:10]:  # Cap at 10 to avoid overwhelming output
            matcher = override.get("matcher", {})
            match_id = matcher.get("id", "")
            match_opts = matcher.get("options", "")
            props = override.get("properties", [])
            prop_summary = ", ".join(p.get("id", "") for p in props[:5])
            lines.append(f"    {match_id}={match_opts}: {prop_summary}")

    return lines


def _format_panel_detail(
    panel: dict[str, Any],
    templating: dict[str, Any] | None = None,
) -> str:
    """Format a single panel with full details."""
    lines: list[str] = []
    title = panel.get("title", "untitled")
    panel_type = panel.get("type", "unknown")
    panel_id = panel.get("id", "?")
    ds = panel.get("datasource", {})

    lines.append(f"Panel: {title}")
    lines.append(f"  Type: {panel_type}")
    lines.append(f"  ID: {panel_id}")

    if isinstance(ds, dict) and ds:
        ds_type = ds.get("type", "")
        ds_uid = ds.get("uid", "")
        lines.append(f"  Datasource: {ds_type} (uid={ds_uid})")

    # Targets / queries
    targets = panel.get("targets", [])
    if targets:
        lines.append(f"  Queries ({len(targets)}):")
        for i, target in enumerate(targets):
            lines.extend(_format_target(target, i))

    # Field config
    field_config = panel.get("fieldConfig", {})
    if field_config:
        fc_lines = _format_field_config(field_config)
        if fc_lines:
            lines.extend(fc_lines)

    # Transformations
    transformations = panel.get("transformations", [])
    if transformations:
        lines.append(f"  Transformations ({len(transformations)}):")
        for t in transformations:
            lines.append(f"    - {t.get('id', 'unknown')}")

    # Options (visualization-specific)
    options = panel.get("options", {})
    if options:
        # Show a few key options without dumping everything
        tooltip = options.get("tooltip", {})
        if tooltip:
            lines.append(f"  Tooltip mode: {tooltip.get('mode', 'unknown')}")
        legend = options.get("legend", {})
        if legend:
            lines.append(f"  Legend placement: {legend.get('placement', 'unknown')}")

    # Template variables referenced in queries
    if templating and targets:
        all_queries = " ".join(t.get("expr", "") + t.get("expression", "") for t in targets)
        template_list = templating.get("list", [])
        referenced_vars: list[str] = []
        for var in template_list:
            var_name = var.get("name", "")
            if f"${var_name}" in all_queries or "${" + var_name + "}" in all_queries:
                referenced_vars.append(var_name)
        if referenced_vars:
            lines.append(f"  Template variables used: {', '.join(referenced_vars)}")

    return "\n".join(lines)


def _format_dashboard_summary(data: dict[str, Any]) -> str:
    """Format a full dashboard with all panels summarized."""
    dashboard = data.get("dashboard", {})
    meta = data.get("meta", {})

    title = dashboard.get("title", "untitled")
    uid = dashboard.get("uid", "")
    tags = dashboard.get("tags", [])
    panels = dashboard.get("panels", [])
    templating = dashboard.get("templating", {})
    annotations = dashboard.get("annotations", {})
    links = dashboard.get("links", [])
    refresh = dashboard.get("refresh", "")

    flat_panels = _flatten_panels(panels)

    lines: list[str] = []
    lines.append(f"Dashboard: {title}")
    lines.append(f"  UID: {uid}")
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    lines.append(f"  Panels: {len(flat_panels)}")
    if refresh:
        lines.append(f"  Auto-refresh: {refresh}")

    folder = meta.get("folderTitle", "")
    if folder:
        lines.append(f"  Folder: {folder}")

    # Template variables
    template_list = templating.get("list", [])
    if template_list:
        lines.append(f"\nTemplate Variables ({len(template_list)}):")
        for var in template_list:
            var_name = var.get("name", "")
            var_type = var.get("type", "")
            query = var.get("query", "")
            current = var.get("current", {})
            current_text = current.get("text", "") if isinstance(current, dict) else ""
            query_str = query if isinstance(query, str) else str(query)
            lines.append(f"  ${var_name} (type={var_type})")
            if query_str:
                lines.append(f"    Query: {query_str}")
            if current_text:
                lines.append(f"    Current: {current_text}")

    # Datasources used
    datasources: set[str] = set()
    for panel in flat_panels:
        ds = panel.get("datasource", {})
        if isinstance(ds, dict) and ds.get("type"):
            ds_label = f"{ds.get('type', '')} (uid={ds.get('uid', '')})"
            datasources.add(ds_label)
        for target in panel.get("targets", []):
            tds = target.get("datasource", {})
            if isinstance(tds, dict) and tds.get("type"):
                ds_label = f"{tds.get('type', '')} (uid={tds.get('uid', '')})"
                datasources.add(ds_label)
    if datasources:
        lines.append(f"\nDatasources ({len(datasources)}):")
        for ds_label in sorted(datasources):
            lines.append(f"  - {ds_label}")

    # Annotations
    annotation_list = annotations.get("list", [])
    if annotation_list:
        lines.append(f"\nAnnotations ({len(annotation_list)}):")
        for ann in annotation_list:
            ann_name = ann.get("name", "")
            ann_ds = ann.get("datasource", {})
            ann_type = ann_ds.get("type", "") if isinstance(ann_ds, dict) else ""
            lines.append(f"  - {ann_name} (datasource={ann_type})")

    # Links
    if links:
        lines.append(f"\nLinks ({len(links)}):")
        for link in links:
            lines.append(f"  - {link.get('title', 'untitled')}: {link.get('url', '')}")

    # Panel list with types and queries
    lines.append(f"\nPanels ({len(flat_panels)}):")
    for panel in flat_panels:
        panel_title = panel.get("title", "untitled")
        panel_type = panel.get("type", "unknown")
        targets = panel.get("targets", [])
        query_count = len(targets)

        lines.append(f"  - [{panel_type}] {panel_title} ({query_count} queries)")
        # Show first query for each panel
        if targets:
            first_expr = targets[0].get("expr", "") or targets[0].get("expression", "")
            if first_expr:
                # Truncate long queries
                if len(first_expr) > 120:
                    first_expr = first_expr[:117] + "..."
                lines.append(f"      Query: {first_expr}")

    return "\n".join(lines)


def _format_search_results(results: list[dict[str, Any]]) -> str:
    """Format dashboard search results."""
    if not results:
        return "No dashboards found matching the search query."

    lines: list[str] = [f"Found {len(results)} dashboard(s):\n"]
    for result in results:
        title = result.get("title", "untitled")
        uid = result.get("uid", "")
        folder = result.get("folderTitle", "General")
        url = result.get("url", "")
        tags = result.get("tags", [])

        lines.append(f"- {title} (uid={uid})")
        lines.append(f"  Folder: {folder}")
        if url:
            lines.append(f"  URL: {url}")
        if tags:
            lines.append(f"  Tags: {', '.join(tags)}")
        lines.append("")

    return "\n".join(lines)


# --- UID resolution ---


async def _resolve_dashboard(dashboard: str) -> dict[str, Any]:
    """Resolve a dashboard identifier to the full dashboard response.

    Tries UID-based fetch first (if it looks like a UID), falls back to search.
    """
    if _is_likely_uid(dashboard):
        try:
            return await _get_dashboard_by_uid(dashboard)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Fall through to search
                pass
            else:
                raise

    # Search by name
    results = await _search_dashboards(dashboard)
    if not results:
        raise ToolException(
            f"No dashboard found matching '{dashboard}'. "
            "Use grafana_search_dashboards to discover available dashboards."
        )

    # Use first result
    uid = results[0].get("uid", "")
    if not uid:
        raise ToolException(f"Search returned a result for '{dashboard}' but it has no UID.")

    return await _get_dashboard_by_uid(uid)


# --- Tool descriptions ---

TOOL_DESCRIPTION_DASHBOARD = HOMELAB_CONTEXT + (
    "Fetch a Grafana dashboard by UID or name, and inspect its panels, queries, "
    "thresholds, template variables, and configuration. "
    "Use this to answer questions like 'what query does the CPU panel use?', "
    "'what thresholds are configured?', 'why isn't the hostname filter working on this panel?', "
    "'what panels does the Home Server dashboard have?', or when debugging dashboard issues.\n\n"
    "Input: `dashboard` — a dashboard UID (e.g. 'dekkfibh9454wb') or a name to search for. "
    "Optional: `panel` — a panel title to extract just that panel's details (case-insensitive partial match).\n\n"
    "When no `panel` is specified, returns a summary of all panels with their types, queries, "
    "template variables, and datasources. When `panel` is specified, returns full details for "
    "that panel including queries, thresholds, units, overrides, and transformations."
)

TOOL_DESCRIPTION_SEARCH = HOMELAB_CONTEXT + (
    "Search for Grafana dashboards by name. "
    "Use this to discover available dashboards and their UIDs before fetching details. "
    "Example questions: 'what dashboards exist?', 'find the network dashboard'.\n\n"
    "Returns matching dashboards with title, UID, folder, and URL."
)


@tool("grafana_get_dashboard", args_schema=GetDashboardInput)  # pyright: ignore[reportUnknownParameterType]
async def grafana_get_dashboard(dashboard: str, panel: str | None = None) -> str:
    """Fetch a Grafana dashboard by UID or name. See TOOL_DESCRIPTION_DASHBOARD."""
    logger.info("Fetching Grafana dashboard (dashboard=%s, panel=%s)", dashboard, panel)

    try:
        data = await _resolve_dashboard(dashboard)
    except ToolException:
        raise
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Grafana API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Grafana at {get_settings().grafana_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Grafana request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e

    dashboard_data = data.get("dashboard", {})

    if panel:
        panels = dashboard_data.get("panels", [])
        templating = dashboard_data.get("templating", {})
        found, other_matches = _find_panel(panels, panel)
        if found is None:
            flat = _flatten_panels(panels)
            all_titles = [p.get("title", "") for p in flat if p.get("title")]
            return (
                f"No panel matching '{panel}' found in dashboard "
                f"'{dashboard_data.get('title', 'unknown')}'.\n\n"
                f"Available panels ({len(all_titles)}):\n" + "\n".join(f"  - {t}" for t in all_titles)
            )

        result = _format_panel_detail(found, templating)
        if other_matches:
            result += f"\n\nNote: '{panel}' also partially matches: " + ", ".join(f"'{t}'" for t in other_matches)
        return result

    return _format_dashboard_summary(data)


grafana_get_dashboard.description = TOOL_DESCRIPTION_DASHBOARD
grafana_get_dashboard.handle_tool_error = True


@tool("grafana_search_dashboards", args_schema=SearchDashboardsInput)  # pyright: ignore[reportUnknownParameterType]
async def grafana_search_dashboards(query: str) -> str:
    """Search for Grafana dashboards by name. See TOOL_DESCRIPTION_SEARCH."""
    logger.info("Searching Grafana dashboards (query=%s)", query)

    try:
        results = await _search_dashboards(query)
    except httpx.HTTPStatusError as e:
        raise ToolException(f"Grafana API error: HTTP {e.response.status_code} - {e.response.text[:500]}") from e
    except httpx.ConnectError as e:
        raise ToolException(f"Cannot connect to Grafana at {get_settings().grafana_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise ToolException(f"Grafana request timed out after {DEFAULT_TIMEOUT_SECONDS}s: {e}") from e

    return _format_search_results(results)


grafana_search_dashboards.description = TOOL_DESCRIPTION_SEARCH
grafana_search_dashboards.handle_tool_error = True
