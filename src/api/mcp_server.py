"""Streamable HTTP MCP server exposing SRE tools.

Wraps the same LangChain tool functions used by the agent as FastMCP tools,
served over Streamable HTTP so MCP clients (Claude Code, Claude Desktop, etc.)
can call them directly without going through the agent loop.

Auth is handled externally by Cloudflare Access.
"""

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_sync(tool_obj: Any, **kwargs: Any) -> str:
    """Call a sync LangChain @tool and return its string result."""
    result: str = tool_obj.func(**kwargs)
    return result


async def _call_async(tool_obj: Any, **kwargs: Any) -> str:
    """Call an async LangChain @tool and return its string result."""
    result: str = await tool_obj.coroutine(**kwargs)
    return result


# ---------------------------------------------------------------------------
# Tool registration factories
# ---------------------------------------------------------------------------


def _register_prometheus_tools(mcp: FastMCP) -> None:
    from src.agent.tools.prometheus import (
        prometheus_instant_query,
        prometheus_range_query,
        prometheus_search_metrics,
    )

    @mcp.tool(name="prometheus_search_metrics", description=prometheus_search_metrics.description)
    async def prometheus_search_metrics_mcp(search_term: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(prometheus_search_metrics, search_term=search_term)

    @mcp.tool(name="prometheus_instant_query", description=prometheus_instant_query.description)
    async def prometheus_instant_query_mcp(query: str, time: str | None = None) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(prometheus_instant_query, query=query, time=time)

    @mcp.tool(name="prometheus_range_query", description=prometheus_range_query.description)
    async def prometheus_range_query_mcp(  # pyright: ignore[reportUnusedFunction]
        query: str, start: str, end: str, step: str = "60s"
    ) -> str:
        return await _call_async(prometheus_range_query, query=query, start=start, end=end, step=step)


def _register_grafana_alert_tools(mcp: FastMCP) -> None:
    from src.agent.tools.grafana_alerts import grafana_get_alert_rules, grafana_get_alerts

    @mcp.tool(name="grafana_get_alerts", description=grafana_get_alerts.description)
    async def grafana_get_alerts_mcp(state: str | None = None) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(grafana_get_alerts, state=state)

    @mcp.tool(name="grafana_get_alert_rules", description=grafana_get_alert_rules.description)
    async def grafana_get_alert_rules_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(grafana_get_alert_rules)


def _register_grafana_dashboard_tools(mcp: FastMCP) -> None:
    from src.agent.tools.grafana_dashboards import grafana_get_dashboard, grafana_search_dashboards

    @mcp.tool(name="grafana_get_dashboard", description=grafana_get_dashboard.description)
    async def grafana_get_dashboard_mcp(  # pyright: ignore[reportUnusedFunction]
        dashboard: str, panel: str | None = None
    ) -> str:
        return await _call_async(grafana_get_dashboard, dashboard=dashboard, panel=panel)

    @mcp.tool(name="grafana_search_dashboards", description=grafana_search_dashboards.description)
    async def grafana_search_dashboards_mcp(query: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(grafana_search_dashboards, query=query)


def _register_proxmox_tools(mcp: FastMCP) -> None:
    from src.agent.tools.proxmox import (
        proxmox_get_guest_config,
        proxmox_list_guests,
        proxmox_list_tasks,
        proxmox_node_status,
    )

    @mcp.tool(name="proxmox_list_guests", description=proxmox_list_guests.description)
    async def proxmox_list_guests_mcp(guest_type: str | None = None) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(proxmox_list_guests, guest_type=guest_type)

    @mcp.tool(name="proxmox_get_guest_config", description=proxmox_get_guest_config.description)
    async def proxmox_get_guest_config_mcp(  # pyright: ignore[reportUnusedFunction]
        vmid: int | None = None, name: str | None = None, guest_type: str = "qemu"
    ) -> str:
        return await _call_async(proxmox_get_guest_config, vmid=vmid, name=name, guest_type=guest_type)

    @mcp.tool(name="proxmox_node_status", description=proxmox_node_status.description)
    async def proxmox_node_status_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(proxmox_node_status)

    @mcp.tool(name="proxmox_list_tasks", description=proxmox_list_tasks.description)
    async def proxmox_list_tasks_mcp(  # pyright: ignore[reportUnusedFunction]
        limit: int = 20, errors_only: bool = False
    ) -> str:
        return await _call_async(proxmox_list_tasks, limit=limit, errors_only=errors_only)


def _register_truenas_tools(mcp: FastMCP) -> None:
    from src.agent.tools.truenas import (
        truenas_apps,
        truenas_list_shares,
        truenas_pool_status,
        truenas_snapshots,
        truenas_system_status,
    )

    @mcp.tool(name="truenas_pool_status", description=truenas_pool_status.description)
    async def truenas_pool_status_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(truenas_pool_status)

    @mcp.tool(name="truenas_list_shares", description=truenas_list_shares.description)
    async def truenas_list_shares_mcp(  # pyright: ignore[reportUnusedFunction]
        share_type: str | None = None, include_sessions: bool = False
    ) -> str:
        return await _call_async(truenas_list_shares, share_type=share_type, include_sessions=include_sessions)

    @mcp.tool(name="truenas_snapshots", description=truenas_snapshots.description)
    async def truenas_snapshots_mcp(  # pyright: ignore[reportUnusedFunction]
        dataset: str | None = None, limit: int = 50
    ) -> str:
        return await _call_async(truenas_snapshots, dataset=dataset, limit=limit)

    @mcp.tool(name="truenas_system_status", description=truenas_system_status.description)
    async def truenas_system_status_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(truenas_system_status)

    @mcp.tool(name="truenas_apps", description=truenas_apps.description)
    async def truenas_apps_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(truenas_apps)


def _register_disk_status_tools(mcp: FastMCP) -> None:
    try:
        from src.agent.tools.disk_status import hdd_power_status
    except Exception:
        return

    @mcp.tool(name="hdd_power_status", description=hdd_power_status.description)
    async def hdd_power_status_mcp(  # pyright: ignore[reportUnusedFunction]
        duration: str = "24h", pool: str | None = None
    ) -> str:
        return await _call_async(hdd_power_status, duration=duration, pool=pool)


def _register_loki_tools(mcp: FastMCP) -> None:
    from src.agent.tools.loki import (
        loki_correlate_changes,
        loki_list_label_values,
        loki_metric_query,
        loki_query_logs,
    )

    @mcp.tool(name="loki_query_logs", description=loki_query_logs.description)
    async def loki_query_logs_mcp(  # pyright: ignore[reportUnusedFunction]
        query: str,
        start: str = "1h",
        end: str = "now",
        limit: int = 100,
        direction: str = "backward",
    ) -> str:
        return await _call_async(loki_query_logs, query=query, start=start, end=end, limit=limit, direction=direction)

    @mcp.tool(name="loki_metric_query", description=loki_metric_query.description)
    async def loki_metric_query_mcp(  # pyright: ignore[reportUnusedFunction]
        query: str, start: str = "1h", end: str = "now", step: str | None = None
    ) -> str:
        return await _call_async(loki_metric_query, query=query, start=start, end=end, step=step)

    @mcp.tool(name="loki_list_label_values", description=loki_list_label_values.description)
    async def loki_list_label_values_mcp(  # pyright: ignore[reportUnusedFunction]
        label: str, query: str | None = None
    ) -> str:
        return await _call_async(loki_list_label_values, label=label, query=query)

    @mcp.tool(name="loki_correlate_changes", description=loki_correlate_changes.description)
    async def loki_correlate_changes_mcp(  # pyright: ignore[reportUnusedFunction]
        reference_time: str,
        window_minutes: int = 30,
        hostname: str | None = None,
        service_name: str | None = None,
    ) -> str:
        return await _call_async(
            loki_correlate_changes,
            reference_time=reference_time,
            window_minutes=window_minutes,
            hostname=hostname,
            service_name=service_name,
        )


def _register_pbs_tools(mcp: FastMCP) -> None:
    from src.agent.tools.pbs import pbs_datastore_status, pbs_list_backups, pbs_list_tasks

    @mcp.tool(name="pbs_datastore_status", description=pbs_datastore_status.description)
    async def pbs_datastore_status_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(pbs_datastore_status)

    @mcp.tool(name="pbs_list_backups", description=pbs_list_backups.description)
    async def pbs_list_backups_mcp(datastore: str | None = None) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(pbs_list_backups, datastore=datastore)

    @mcp.tool(name="pbs_list_tasks", description=pbs_list_tasks.description)
    async def pbs_list_tasks_mcp(limit: int = 20, errors_only: bool = False) -> str:  # pyright: ignore[reportUnusedFunction]
        return await _call_async(pbs_list_tasks, limit=limit, errors_only=errors_only)


def _register_runbook_tools(mcp: FastMCP) -> None:
    try:
        from src.agent.retrieval.runbooks import runbook_search
    except Exception:
        return

    @mcp.tool(name="runbook_search", description=runbook_search.description)
    async def runbook_search_mcp(query: str, num_results: int = 4) -> str:  # pyright: ignore[reportUnusedFunction]
        return await asyncio.to_thread(_call_sync, runbook_search, query=query, num_results=num_results)


def _register_conversation_tools(mcp: FastMCP, history_dir: str) -> None:
    import json

    from src.agent.history import (
        get_conversation,
        list_conversations,
    )
    from src.agent.tools import HOMELAB_CONTEXT

    @mcp.tool(
        name="sre_agent_list_conversations",
        description=(
            HOMELAB_CONTEXT + "List recent conversations the deployed SRE agent has had via the web UI or API. "
            "Returns session IDs, titles (derived from the first user message), timestamps, "
            "turn counts, and which LLM model/provider was used. "
            "Most-recently-updated conversations appear first. "
            "Use this to find a specific past conversation, then call sre_agent_get_conversation "
            "with its session_id to read the full dialogue."
        ),
    )
    async def sre_agent_list_conversations_mcp(limit: int = 20) -> str:  # pyright: ignore[reportUnusedFunction]
        items = list_conversations(history_dir)
        items = items[:limit]
        if not items:
            return "No conversations found."
        lines = [f"Found {len(items)} conversation(s):\n"]
        for item in items:
            lines.append(
                f"  [{item['session_id']}] {item['title'] or '(untitled)'} "
                f"— {item['turn_count']} turn(s), {item['provider']}/{item['model']}, "
                f"updated {item['updated_at']}"
            )
        return "\n".join(lines)

    @mcp.tool(
        name="sre_agent_get_conversation",
        description=(
            HOMELAB_CONTEXT + "Retrieve the full dialogue of a specific deployed SRE agent conversation by session ID. "
            "Returns all user and assistant turns with timestamps. "
            "Use sre_agent_list_conversations first to find the session_id you need."
        ),
    )
    async def sre_agent_get_conversation_mcp(session_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        data = get_conversation(history_dir, session_id)
        if data is None:
            return f"Conversation '{session_id}' not found."
        return json.dumps(data, indent=2, default=str)


def _register_memory_tools(mcp: FastMCP) -> None:
    try:
        from src.memory.store import is_memory_configured

        if not is_memory_configured():
            return

        from src.memory.tools import (
            memory_check_baseline,
            memory_get_previous_report,
            memory_record_incident,
            memory_search_incidents,
        )
    except Exception:
        return

    @mcp.tool(name="memory_search_incidents", description=memory_search_incidents.description)
    async def memory_search_incidents_mcp(  # pyright: ignore[reportUnusedFunction]
        query: str | None = None,
        alert_name: str | None = None,
        service: str | None = None,
        limit: int = 10,
    ) -> str:
        return await asyncio.to_thread(
            _call_sync, memory_search_incidents, query=query, alert_name=alert_name, service=service, limit=limit
        )

    @mcp.tool(name="memory_record_incident", description=memory_record_incident.description)
    async def memory_record_incident_mcp(  # pyright: ignore[reportUnusedFunction]
        title: str,
        description: str,
        alert_name: str | None = None,
        root_cause: str | None = None,
        resolution: str | None = None,
        severity: str = "info",
        services: str = "",
    ) -> str:
        return await asyncio.to_thread(
            _call_sync,
            memory_record_incident,
            title=title,
            description=description,
            alert_name=alert_name,
            root_cause=root_cause,
            resolution=resolution,
            severity=severity,
            services=services,
        )

    @mcp.tool(name="memory_get_previous_report", description=memory_get_previous_report.description)
    async def memory_get_previous_report_mcp(count: int = 1) -> str:  # pyright: ignore[reportUnusedFunction]
        return await asyncio.to_thread(_call_sync, memory_get_previous_report, count=count)

    @mcp.tool(name="memory_check_baseline", description=memory_check_baseline.description)
    async def memory_check_baseline_mcp(  # pyright: ignore[reportUnusedFunction]
        metric_name: str, current_value: float, labels: str | None = None
    ) -> str:
        return await asyncio.to_thread(
            _call_sync, memory_check_baseline, metric_name=metric_name, current_value=current_value, labels=labels
        )


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------


def build_fastmcp_server(settings: Settings | None = None) -> FastMCP:
    """Build a FastMCP server with all conditionally-registered SRE tools.

    Follows the same conditional registration logic as ``build_mcp_server()``
    in ``src/agent/mcp_tools.py`` and ``_get_tools()`` in ``src/agent/agent.py``.

    The server uses ``ToolException`` from LangChain tools as the error signal.
    FastMCP's ``@mcp.tool`` decorator catches exceptions and returns them as
    MCP error results automatically.
    """
    if settings is None:
        settings = get_settings()

    mcp = FastMCP("sre-assistant")

    # Always-on tools
    _register_prometheus_tools(mcp)
    _register_grafana_alert_tools(mcp)
    _register_grafana_dashboard_tools(mcp)

    # Conditional tools
    if settings.proxmox_url:
        _register_proxmox_tools(mcp)
        logger.info("MCP: Proxmox VE tools registered")
    else:
        logger.info("MCP: Proxmox VE tools disabled — PROXMOX_URL not set")

    if settings.truenas_url:
        _register_truenas_tools(mcp)
        if settings.prometheus_url:
            _register_disk_status_tools(mcp)
        logger.info("MCP: TrueNAS tools registered")
    else:
        logger.info("MCP: TrueNAS tools disabled — TRUENAS_URL not set")

    if settings.loki_url:
        _register_loki_tools(mcp)
        logger.info("MCP: Loki tools registered")
    else:
        logger.info("MCP: Loki tools disabled — LOKI_URL not set")

    if settings.pbs_url:
        _register_pbs_tools(mcp)
        logger.info("MCP: PBS tools registered")
    else:
        logger.info("MCP: PBS tools disabled — PBS_URL not set")

    _register_runbook_tools(mcp)
    _register_memory_tools(mcp)

    if settings.conversation_history_dir:
        _register_conversation_tools(mcp, settings.conversation_history_dir)
        logger.info("MCP: Conversation history tools registered")

    return mcp
