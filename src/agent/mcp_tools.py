"""MCP tool bridge for the Claude Agent SDK.

Wraps existing LangChain tool functions as SDK MCP tools so the same
business logic is used by both the LangChain (OpenAI) and SDK (Anthropic)
agent paths.
"""

import asyncio
import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig
from langchain_core.tools import ToolException

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """Build an MCP tool result dict with a single text content block."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


async def _call_async_tool(tool_obj: Any, **kwargs: Any) -> dict[str, Any]:
    """Call a LangChain @tool-decorated async function and wrap the result."""
    try:
        result: str = await tool_obj.coroutine(**kwargs)
        return _text_result(result)
    except ToolException as e:
        return _text_result(str(e), is_error=True)
    except Exception as e:
        logger.exception("MCP tool call failed: %s", tool_obj.name)
        return _text_result(f"Tool error: {e}", is_error=True)


def _call_sync_tool(tool_obj: Any, **kwargs: Any) -> dict[str, Any]:
    """Call a LangChain @tool-decorated sync function and wrap the result."""
    try:
        result: str = tool_obj.func(**kwargs)
        return _text_result(result)
    except ToolException as e:
        return _text_result(str(e), is_error=True)
    except Exception as e:
        logger.exception("MCP tool call failed: %s", tool_obj.name)
        return _text_result(f"Tool error: {e}", is_error=True)


# ---------------------------------------------------------------------------
# JSON Schema extraction from Pydantic models
# ---------------------------------------------------------------------------


def _schema_from_pydantic(model_cls: type) -> dict[str, Any]:
    """Extract a JSON Schema dict from a Pydantic BaseModel class."""
    schema: dict[str, Any] = model_cls.model_json_schema()  # type: ignore[attr-defined]
    # Remove $defs and other meta keys that the SDK may not understand
    schema.pop("$defs", None)
    schema.pop("definitions", None)
    return schema


# ---------------------------------------------------------------------------
# Individual MCP tool factories
# ---------------------------------------------------------------------------
# Each returns an SdkMcpTool that wraps the corresponding LangChain tool.


def _prometheus_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.prometheus import (
        PrometheusInstantInput,
        PrometheusRangeInput,
        PrometheusSearchInput,
        prometheus_instant_query,
        prometheus_range_query,
        prometheus_search_metrics,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _search(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(prometheus_search_metrics, search_term=args["search_term"])

    tools.append(
        SdkMcpTool(
            name="prometheus_search_metrics",
            description=prometheus_search_metrics.description,
            input_schema=_schema_from_pydantic(PrometheusSearchInput),
            handler=_search,
        )
    )

    async def _instant(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(prometheus_instant_query, query=args["query"], time=args.get("time"))

    tools.append(
        SdkMcpTool(
            name="prometheus_instant_query",
            description=prometheus_instant_query.description,
            input_schema=_schema_from_pydantic(PrometheusInstantInput),
            handler=_instant,
        )
    )

    async def _range(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            prometheus_range_query,
            query=args["query"],
            start=args["start"],
            end=args["end"],
            step=args.get("step", "60s"),
        )

    tools.append(
        SdkMcpTool(
            name="prometheus_range_query",
            description=prometheus_range_query.description,
            input_schema=_schema_from_pydantic(PrometheusRangeInput),
            handler=_range,
        )
    )

    return tools


def _grafana_alert_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.grafana_alerts import (
        GetAlertRulesInput,
        GetAlertsInput,
        grafana_get_alert_rules,
        grafana_get_alerts,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _alerts(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(grafana_get_alerts, state=args.get("state"))

    tools.append(
        SdkMcpTool(
            name="grafana_get_alerts",
            description=grafana_get_alerts.description,
            input_schema=_schema_from_pydantic(GetAlertsInput),
            handler=_alerts,
        )
    )

    async def _rules(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(grafana_get_alert_rules)

    tools.append(
        SdkMcpTool(
            name="grafana_get_alert_rules",
            description=grafana_get_alert_rules.description,
            input_schema=_schema_from_pydantic(GetAlertRulesInput),
            handler=_rules,
        )
    )

    return tools


def _grafana_dashboard_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.grafana_dashboards import (
        GetDashboardInput,
        SearchDashboardsInput,
        grafana_get_dashboard,
        grafana_search_dashboards,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _dashboard(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(grafana_get_dashboard, dashboard=args["dashboard"], panel=args.get("panel"))

    tools.append(
        SdkMcpTool(
            name="grafana_get_dashboard",
            description=grafana_get_dashboard.description,
            input_schema=_schema_from_pydantic(GetDashboardInput),
            handler=_dashboard,
        )
    )

    async def _search(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(grafana_search_dashboards, query=args["query"])

    tools.append(
        SdkMcpTool(
            name="grafana_search_dashboards",
            description=grafana_search_dashboards.description,
            input_schema=_schema_from_pydantic(SearchDashboardsInput),
            handler=_search,
        )
    )

    return tools


def _proxmox_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.proxmox import (
        GetGuestConfigInput,
        ListGuestsInput,
        ListTasksInput,
        NodeStatusInput,
        proxmox_get_guest_config,
        proxmox_list_guests,
        proxmox_list_tasks,
        proxmox_node_status,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _guests(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(proxmox_list_guests, guest_type=args.get("guest_type"))

    tools.append(
        SdkMcpTool(
            name="proxmox_list_guests",
            description=proxmox_list_guests.description,
            input_schema=_schema_from_pydantic(ListGuestsInput),
            handler=_guests,
        )
    )

    async def _config(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            proxmox_get_guest_config,
            vmid=args.get("vmid"),
            name=args.get("name"),
            guest_type=args.get("guest_type", "qemu"),
        )

    tools.append(
        SdkMcpTool(
            name="proxmox_get_guest_config",
            description=proxmox_get_guest_config.description,
            input_schema=_schema_from_pydantic(GetGuestConfigInput),
            handler=_config,
        )
    )

    async def _node(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(proxmox_node_status)

    tools.append(
        SdkMcpTool(
            name="proxmox_node_status",
            description=proxmox_node_status.description,
            input_schema=_schema_from_pydantic(NodeStatusInput),
            handler=_node,
        )
    )

    async def _tasks(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            proxmox_list_tasks,
            limit=args.get("limit", 20),
            errors_only=args.get("errors_only", False),
        )

    tools.append(
        SdkMcpTool(
            name="proxmox_list_tasks",
            description=proxmox_list_tasks.description,
            input_schema=_schema_from_pydantic(ListTasksInput),
            handler=_tasks,
        )
    )

    return tools


def _truenas_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.truenas import (
        AppsInput,
        ListSharesInput,
        PoolStatusInput,
        SnapshotsInput,
        SystemStatusInput,
        truenas_apps,
        truenas_list_shares,
        truenas_pool_status,
        truenas_snapshots,
        truenas_system_status,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _pools(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(truenas_pool_status)

    tools.append(
        SdkMcpTool(
            name="truenas_pool_status",
            description=truenas_pool_status.description,
            input_schema=_schema_from_pydantic(PoolStatusInput),
            handler=_pools,
        )
    )

    async def _shares(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            truenas_list_shares,
            share_type=args.get("share_type"),
            include_sessions=args.get("include_sessions", False),
        )

    tools.append(
        SdkMcpTool(
            name="truenas_list_shares",
            description=truenas_list_shares.description,
            input_schema=_schema_from_pydantic(ListSharesInput),
            handler=_shares,
        )
    )

    async def _snaps(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            truenas_snapshots,
            dataset=args.get("dataset"),
            limit=args.get("limit", 50),
        )

    tools.append(
        SdkMcpTool(
            name="truenas_snapshots",
            description=truenas_snapshots.description,
            input_schema=_schema_from_pydantic(SnapshotsInput),
            handler=_snaps,
        )
    )

    async def _sys(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(truenas_system_status)

    tools.append(
        SdkMcpTool(
            name="truenas_system_status",
            description=truenas_system_status.description,
            input_schema=_schema_from_pydantic(SystemStatusInput),
            handler=_sys,
        )
    )

    async def _apps(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(truenas_apps)

    tools.append(
        SdkMcpTool(
            name="truenas_apps",
            description=truenas_apps.description,
            input_schema=_schema_from_pydantic(AppsInput),
            handler=_apps,
        )
    )

    return tools


def _disk_status_tools() -> list[SdkMcpTool[Any]]:
    try:
        from src.agent.tools.disk_status import HddPowerStatusInput, hdd_power_status
    except Exception:
        return []

    async def _hdd(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            hdd_power_status,
            duration=args.get("duration", "24h"),
            pool=args.get("pool"),
        )

    return [
        SdkMcpTool(
            name="hdd_power_status",
            description=hdd_power_status.description,
            input_schema=_schema_from_pydantic(HddPowerStatusInput),
            handler=_hdd,
        )
    ]


def _loki_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.loki import (
        LokiCorrelateInput,
        LokiLabelValuesInput,
        LokiMetricQueryInput,
        LokiQueryInput,
        loki_correlate_changes,
        loki_list_label_values,
        loki_metric_query,
        loki_query_logs,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _logs(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            loki_query_logs,
            query=args["query"],
            start=args.get("start", "1h"),
            end=args.get("end", "now"),
            limit=args.get("limit", 100),
            direction=args.get("direction", "backward"),
        )

    tools.append(
        SdkMcpTool(
            name="loki_query_logs",
            description=loki_query_logs.description,
            input_schema=_schema_from_pydantic(LokiQueryInput),
            handler=_logs,
        )
    )

    async def _metric(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            loki_metric_query,
            query=args["query"],
            start=args.get("start", "1h"),
            end=args.get("end", "now"),
            step=args.get("step"),
        )

    tools.append(
        SdkMcpTool(
            name="loki_metric_query",
            description=loki_metric_query.description,
            input_schema=_schema_from_pydantic(LokiMetricQueryInput),
            handler=_metric,
        )
    )

    async def _labels(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            loki_list_label_values,
            label=args["label"],
            query=args.get("query"),
        )

    tools.append(
        SdkMcpTool(
            name="loki_list_label_values",
            description=loki_list_label_values.description,
            input_schema=_schema_from_pydantic(LokiLabelValuesInput),
            handler=_labels,
        )
    )

    async def _correlate(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            loki_correlate_changes,
            reference_time=args["reference_time"],
            window_minutes=args.get("window_minutes", 30),
            hostname=args.get("hostname"),
            service_name=args.get("service_name"),
        )

    tools.append(
        SdkMcpTool(
            name="loki_correlate_changes",
            description=loki_correlate_changes.description,
            input_schema=_schema_from_pydantic(LokiCorrelateInput),
            handler=_correlate,
        )
    )

    return tools


def _pbs_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.pbs import (
        DatastoreStatusInput,
        ListBackupsInput,
        ListPbsTasksInput,
        pbs_datastore_status,
        pbs_list_backups,
        pbs_list_tasks,
    )

    tools: list[SdkMcpTool[Any]] = []

    async def _status(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(pbs_datastore_status)

    tools.append(
        SdkMcpTool(
            name="pbs_datastore_status",
            description=pbs_datastore_status.description,
            input_schema=_schema_from_pydantic(DatastoreStatusInput),
            handler=_status,
        )
    )

    async def _backups(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(pbs_list_backups, datastore=args.get("datastore"))

    tools.append(
        SdkMcpTool(
            name="pbs_list_backups",
            description=pbs_list_backups.description,
            input_schema=_schema_from_pydantic(ListBackupsInput),
            handler=_backups,
        )
    )

    async def _tasks(args: dict[str, Any]) -> dict[str, Any]:
        return await _call_async_tool(
            pbs_list_tasks,
            limit=args.get("limit", 20),
            errors_only=args.get("errors_only", False),
        )

    tools.append(
        SdkMcpTool(
            name="pbs_list_tasks",
            description=pbs_list_tasks.description,
            input_schema=_schema_from_pydantic(ListPbsTasksInput),
            handler=_tasks,
        )
    )

    return tools


def _clock_tools() -> list[SdkMcpTool[Any]]:
    from src.agent.tools.clock import GetCurrentTimeInput, get_current_time

    async def _now(_args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(_call_sync_tool, get_current_time)

    return [
        SdkMcpTool(
            name="get_current_time",
            description=get_current_time.description,
            input_schema=_schema_from_pydantic(GetCurrentTimeInput),
            handler=_now,
        )
    ]


def _runbook_tools() -> list[SdkMcpTool[Any]]:
    try:
        from src.agent.retrieval.runbooks import RunbookSearchInput, runbook_search
    except Exception:
        return []

    async def _search(args: dict[str, Any]) -> dict[str, Any]:
        # runbook_search is sync — run in thread
        return await asyncio.to_thread(
            _call_sync_tool,
            runbook_search,
            query=args["query"],
            num_results=args.get("num_results", 4),
        )

    return [
        SdkMcpTool(
            name="runbook_search",
            description=runbook_search.description,
            input_schema=_schema_from_pydantic(RunbookSearchInput),
            handler=_search,
        )
    ]


def _memory_tools() -> list[SdkMcpTool[Any]]:
    try:
        from src.memory.store import is_memory_configured

        if not is_memory_configured():
            return []

        from src.memory.tools import (
            CheckBaselineInput,
            GetPreviousReportInput,
            RecordIncidentInput,
            SearchIncidentsInput,
            memory_check_baseline,
            memory_get_previous_report,
            memory_record_incident,
            memory_search_incidents,
        )
    except Exception:
        return []

    tools: list[SdkMcpTool[Any]] = []

    async def _search(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            _call_sync_tool,
            memory_search_incidents,
            query=args.get("query"),
            alert_name=args.get("alert_name"),
            service=args.get("service"),
            limit=args.get("limit", 10),
        )

    tools.append(
        SdkMcpTool(
            name="memory_search_incidents",
            description=memory_search_incidents.description,
            input_schema=_schema_from_pydantic(SearchIncidentsInput),
            handler=_search,
        )
    )

    async def _record(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            _call_sync_tool,
            memory_record_incident,
            title=args["title"],
            description=args["description"],
            alert_name=args.get("alert_name"),
            root_cause=args.get("root_cause"),
            resolution=args.get("resolution"),
            severity=args.get("severity", "info"),
            services=args.get("services", ""),
        )

    tools.append(
        SdkMcpTool(
            name="memory_record_incident",
            description=memory_record_incident.description,
            input_schema=_schema_from_pydantic(RecordIncidentInput),
            handler=_record,
        )
    )

    async def _report(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            _call_sync_tool,
            memory_get_previous_report,
            count=args.get("count", 1),
        )

    tools.append(
        SdkMcpTool(
            name="memory_get_previous_report",
            description=memory_get_previous_report.description,
            input_schema=_schema_from_pydantic(GetPreviousReportInput),
            handler=_report,
        )
    )

    async def _baseline(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            _call_sync_tool,
            memory_check_baseline,
            metric_name=args["metric_name"],
            current_value=args["current_value"],
            labels=args.get("labels"),
        )

    tools.append(
        SdkMcpTool(
            name="memory_check_baseline",
            description=memory_check_baseline.description,
            input_schema=_schema_from_pydantic(CheckBaselineInput),
            handler=_baseline,
        )
    )

    return tools


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------


def build_mcp_server(settings: Settings | None = None) -> McpSdkServerConfig:
    """Build the MCP server with all conditionally-registered SRE tools.

    Follows the same conditional registration logic as ``_get_tools()`` in
    ``src/agent/agent.py``.
    """
    if settings is None:
        settings = get_settings()

    tools: list[SdkMcpTool[Any]] = []

    # Always-on tools
    tools.extend(_clock_tools())
    tools.extend(_prometheus_tools())
    tools.extend(_grafana_alert_tools())
    tools.extend(_grafana_dashboard_tools())

    # Proxmox VE — conditional
    if settings.proxmox_url:
        tools.extend(_proxmox_tools())
    else:
        logger.info("SDK: Proxmox VE tools disabled — PROXMOX_URL not set")

    # TrueNAS SCALE — conditional
    if settings.truenas_url:
        tools.extend(_truenas_tools())
        tools.extend(_disk_status_tools())
    else:
        logger.info("SDK: TrueNAS tools disabled — TRUENAS_URL not set")

    # Loki — conditional
    if settings.loki_url:
        tools.extend(_loki_tools())
    else:
        logger.info("SDK: Loki tools disabled — LOKI_URL not set")

    # PBS — conditional
    if settings.pbs_url:
        tools.extend(_pbs_tools())
    else:
        logger.info("SDK: PBS tools disabled — PBS_URL not set")

    # Runbook search
    tools.extend(_runbook_tools())

    # Memory tools
    tools.extend(_memory_tools())

    logger.info("SDK MCP server: %d tools registered: %s", len(tools), [t.name for t in tools])
    return create_sdk_mcp_server("sre", tools=tools)
