"""The one list of SRE tools, and the helpers every surface builds from.

Each tool appears once in ``TOOL_SPECS`` with the condition that enables it, its progress label,
its MCP annotations and the surfaces it belongs to:

- ``"mcp"``: the Streamable HTTP server at ``/mcp`` (``src/api/mcp_server.py``).
- ``"agent"``: the chat agent's own tools (moved onto this registry by W10).

The imports below are unconditional on purpose: a tool module that fails to import fails startup
instead of silently dropping its tools.

``run_tool`` validates arguments against the tool's ``args_schema`` and calls the raw function. It
does not go through ``ainvoke``: tools set ``handle_tool_error=True``, which would turn a
``ToolException`` into a successful string result.
"""

import asyncio
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ValidationError

from src.agent.retrieval.runbooks import runbook_search
from src.agent.tools.clock import get_current_time
from src.agent.tools.conversations import sre_agent_get_conversation, sre_agent_list_conversations
from src.agent.tools.disk_status import hdd_power_status
from src.agent.tools.grafana_alerts import grafana_get_alert_rules, grafana_get_alerts
from src.agent.tools.grafana_dashboards import grafana_get_dashboard, grafana_search_dashboards
from src.agent.tools.loki import loki_correlate_changes, loki_list_label_values, loki_metric_query, loki_query_logs
from src.agent.tools.pbs import pbs_datastore_status, pbs_list_backups, pbs_list_tasks
from src.agent.tools.prometheus import prometheus_instant_query, prometheus_range_query, prometheus_search_metrics
from src.agent.tools.proxmox import (
    proxmox_get_guest_config,
    proxmox_list_guests,
    proxmox_list_tasks,
    proxmox_node_status,
)
from src.agent.tools.truenas import (
    truenas_apps,
    truenas_list_shares,
    truenas_pool_status,
    truenas_snapshots,
    truenas_system_status,
)
from src.config import Settings
from src.memory.tools import (
    memory_check_baseline,
    memory_get_previous_report,
    memory_record_incident,
    memory_search_incidents,
)

logger = logging.getLogger(__name__)

type Surface = Literal["agent", "mcp"]

BOTH: frozenset[Surface] = frozenset({"agent", "mcp"})
MCP_ONLY: frozenset[Surface] = frozenset({"mcp"})


class InvalidToolArguments(ToolError):
    """A call's arguments failed the tool's schema: an unknown name, or (as its cause) a ``ValidationError``.

    A ``ToolError``, so every surface reports it as an error result; its own class lets the per-call log line tell a
    bad call (``invalid_args``) from a tool that failed.

    It logs at DEBUG: FastMCP logs a ``ToolError`` at its ``log_level`` (ERROR by default), and the per-call
    ``tool_call`` line already records a bad call.
    """

    def __init__(self, *args: object, log_level: int = logging.DEBUG) -> None:
        super().__init__(*args, log_level=log_level)


@dataclass(frozen=True)
class ToolSpec:
    """One tool, the condition that enables it, and how surfaces present it.

    ``group`` names the integration, so a disabled integration is logged once rather than per tool.
    ``read_only=False`` marks a tool that writes; it is presented as non-destructive and
    non-idempotent. ``open_world=False`` marks a tool that only reads local state (files, the
    vector store, the clock) rather than a remote system.
    """

    tool: StructuredTool
    enabled: Callable[[Settings], bool]
    label: str
    group: str
    read_only: bool = True
    open_world: bool = True
    surfaces: frozenset[Surface] = BOTH

    @property
    def name(self) -> str:
        return self.tool.name


def _always(_: Settings) -> bool:
    return True


def _proxmox(s: Settings) -> bool:
    return bool(s.proxmox_url)


def _truenas(s: Settings) -> bool:
    return bool(s.truenas_url)


def _hdd(s: Settings) -> bool:
    """The HDD power tool reads TrueNAS pool layout and Prometheus disk metrics."""
    return bool(s.truenas_url) and bool(s.prometheus_url)


def _loki(s: Settings) -> bool:
    return bool(s.loki_url)


def _pbs(s: Settings) -> bool:
    return bool(s.pbs_url)


def _memory(s: Settings) -> bool:
    return bool(s.memory_db_path)


def _conversations(s: Settings) -> bool:
    return bool(s.conversation_history_dir)


# Why each conditional group is off, logged once per enabled_specs() call.
_DISABLED_REASON: dict[str, str] = {
    "proxmox": "PROXMOX_URL not set",
    "truenas": "TRUENAS_URL (and, for hdd_power_status, PROMETHEUS_URL) not set",
    "loki": "LOKI_URL not set",
    "pbs": "PBS_URL not set",
    "memory": "MEMORY_DB_PATH not set",
    "conversations": "CONVERSATION_HISTORY_DIR not set",
}


def _spec(
    tool: Any,
    enabled: Callable[[Settings], bool],
    label: str,
    group: str,
    *,
    read_only: bool = True,
    open_world: bool = True,
    surfaces: frozenset[Surface] = BOTH,
) -> ToolSpec:
    """Build a spec, checking the decorated tool is a StructuredTool with a Pydantic args_schema."""
    if not isinstance(tool, StructuredTool):
        raise TypeError(f"{tool!r} is not a StructuredTool")
    schema = tool.args_schema
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        raise TypeError(f"{tool.name} has no Pydantic args_schema")
    return ToolSpec(tool, enabled, label, group, read_only, open_world, surfaces)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec(get_current_time, _always, "Checking the current time", "core", open_world=False),
    _spec(prometheus_search_metrics, _always, "Searching Prometheus metrics", "core"),
    _spec(prometheus_instant_query, _always, "Querying Prometheus", "core"),
    _spec(prometheus_range_query, _always, "Querying Prometheus (range)", "core"),
    _spec(grafana_get_alerts, _always, "Checking Grafana alerts", "core"),
    _spec(grafana_get_alert_rules, _always, "Fetching Grafana alert rules", "core"),
    _spec(grafana_get_dashboard, _always, "Loading Grafana dashboard", "core"),
    _spec(grafana_search_dashboards, _always, "Searching Grafana dashboards", "core"),
    _spec(proxmox_list_guests, _proxmox, "Listing Proxmox VMs/CTs", "proxmox"),
    _spec(proxmox_get_guest_config, _proxmox, "Fetching guest config", "proxmox"),
    _spec(proxmox_node_status, _proxmox, "Checking Proxmox node status", "proxmox"),
    _spec(proxmox_list_tasks, _proxmox, "Listing Proxmox tasks", "proxmox"),
    _spec(truenas_pool_status, _truenas, "Checking TrueNAS pools", "truenas"),
    _spec(truenas_list_shares, _truenas, "Listing NFS/SMB shares", "truenas"),
    _spec(truenas_snapshots, _truenas, "Listing TrueNAS snapshots", "truenas"),
    _spec(truenas_system_status, _truenas, "Checking TrueNAS system status", "truenas"),
    _spec(truenas_apps, _truenas, "Listing TrueNAS apps", "truenas"),
    _spec(hdd_power_status, _hdd, "Checking HDD power states", "truenas"),
    _spec(loki_query_logs, _loki, "Querying Loki logs", "loki"),
    _spec(loki_metric_query, _loki, "Running Loki metric query", "loki"),
    _spec(loki_list_label_values, _loki, "Listing Loki label values", "loki"),
    _spec(loki_correlate_changes, _loki, "Correlating log changes", "loki"),
    _spec(pbs_datastore_status, _pbs, "Checking PBS datastore", "pbs"),
    _spec(pbs_list_backups, _pbs, "Listing PBS backups", "pbs"),
    _spec(pbs_list_tasks, _pbs, "Listing PBS tasks", "pbs"),
    _spec(runbook_search, _always, "Searching runbooks", "core", open_world=False),
    _spec(memory_search_incidents, _memory, "Searching incident history", "memory", open_world=False),
    _spec(memory_record_incident, _memory, "Recording incident", "memory", read_only=False, open_world=False),
    _spec(memory_get_previous_report, _memory, "Retrieving previous report", "memory", open_world=False),
    _spec(memory_check_baseline, _memory, "Checking baseline metrics", "memory", open_world=False),
    _spec(
        sre_agent_list_conversations,
        _conversations,
        "Listing SRE agent conversations",
        "conversations",
        open_world=False,
        surfaces=MCP_ONLY,
    ),
    _spec(
        sre_agent_get_conversation,
        _conversations,
        "Reading an SRE agent conversation",
        "conversations",
        open_world=False,
        surfaces=MCP_ONLY,
    ),
)

_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}
if len(_BY_NAME) != len(TOOL_SPECS):  # pragma: no cover - a duplicate name is a coding error
    raise RuntimeError("Duplicate tool name in TOOL_SPECS")


def enabled_specs(settings: Settings, surface: Surface, exclude: Collection[str] = ()) -> list[ToolSpec]:
    """The specs on ``surface`` that ``settings`` enables, in registry order, minus ``exclude``.

    Each group with a disabled tool is logged once at INFO, naming the tools and the reason.
    """
    enabled: list[ToolSpec] = []
    disabled: dict[str, list[str]] = {}
    for spec in TOOL_SPECS:
        if surface not in spec.surfaces or spec.name in exclude:
            continue
        if spec.enabled(settings):
            enabled.append(spec)
        else:
            disabled.setdefault(spec.group, []).append(spec.name)
    for group, names in disabled.items():
        reason = _DISABLED_REASON.get(group, "not configured")
        logger.info("%s tools disabled on %s: %s (%s)", group, surface, ", ".join(names), reason)
    return enabled


def get_spec(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)


def tool_label(name: str) -> str:
    """The human-readable progress label for a tool, or ``Running <name>`` for an unknown one."""
    spec = _BY_NAME.get(name)
    return spec.label if spec else f"Running {name}"


def _schema_model(spec: ToolSpec) -> type[BaseModel]:
    schema = spec.tool.args_schema
    assert isinstance(schema, type) and issubclass(schema, BaseModel)  # checked by _spec()
    return schema


def _strip_titles(node: Any) -> Any:
    """Drop Pydantic's generated ``title`` keys, keeping properties that are named ``title``."""
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        else:
            out[key] = _strip_titles(value)
    return out


def input_schema(spec: ToolSpec) -> dict[str, Any]:
    """The tool's JSON input schema: its ``args_schema`` with descriptions and bounds, minus titles."""
    schema: dict[str, Any] = _strip_titles(_schema_model(spec).model_json_schema())
    # run_tool refuses unknown names; say so in the schema so clients don't guess.
    schema["additionalProperties"] = False
    return schema


def _describe_validation_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_url=False):
        where = ".".join(str(part) for part in item["loc"]) or "arguments"
        parts.append(f"{where}: {item['msg']}")
    return "; ".join(parts)


async def run_tool(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """Validate ``arguments`` and call the tool's raw function.

    Raises ``InvalidToolArguments`` (a ``ToolError``) for an argument name the schema does not
    declare (naming the accepted ones), and (caused by the ``ValidationError``) for invalid values. A
    ``ToolException`` raised by the tool propagates unchanged, so each surface reports it as an
    error. Sync tools run in a worker thread.
    """
    model = _schema_model(spec)
    unknown = sorted(set(arguments) - set(model.model_fields))
    if unknown:
        accepted = ", ".join(sorted(model.model_fields)) or "none"
        raise InvalidToolArguments(f"Unknown argument(s) for {spec.name}: {', '.join(unknown)}. Accepted: {accepted}")
    try:
        validated = model.model_validate(arguments)
    except ValidationError as e:
        raise InvalidToolArguments(f"Invalid arguments for {spec.name}: {_describe_validation_error(e)}") from e
    kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
    tool = spec.tool
    if tool.coroutine is not None:
        result = await tool.coroutine(**kwargs)
    elif tool.func is not None:
        result = await asyncio.to_thread(tool.func, **kwargs)
    else:  # pragma: no cover - StructuredTool always has one of the two
        raise ToolError(f"{spec.name} has no function to call")
    return result if isinstance(result, str) else str(result)
