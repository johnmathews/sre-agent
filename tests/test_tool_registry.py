"""Tests for the tool registry (src/agent/tool_registry.py)."""

import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field, ValidationError

from src.agent.tool_registry import (
    TOOL_SPECS,
    ToolSpec,
    enabled_specs,
    get_spec,
    input_schema,
    run_tool,
    tool_label,
)

# The guard list: adding, renaming or dropping a tool must be a deliberate edit here.
EXPECTED_NAMES = [
    "get_current_time",
    "prometheus_search_metrics",
    "prometheus_instant_query",
    "prometheus_range_query",
    "scrape_targets",
    "container_status",
    "grafana_get_alerts",
    "grafana_get_alert_rules",
    "grafana_get_dashboard",
    "grafana_search_dashboards",
    "upgrade_status",
    "service_health",
    "proxmox_list_guests",
    "proxmox_get_guest_config",
    "proxmox_node_status",
    "proxmox_list_tasks",
    "truenas_pool_status",
    "truenas_list_shares",
    "truenas_snapshots",
    "truenas_system_status",
    "truenas_apps",
    "hdd_power_status",
    "loki_query_logs",
    "loki_metric_query",
    "loki_list_label_values",
    "loki_correlate_changes",
    "pbs_datastore_status",
    "pbs_list_backups",
    "pbs_list_tasks",
    "runbook_search",
    "memory_search_incidents",
    "memory_record_incident",
    "memory_get_previous_report",
    "memory_check_baseline",
    "sre_agent_list_conversations",
    "sre_agent_get_conversation",
]
CONVERSATION_TOOLS = {"sre_agent_list_conversations", "sre_agent_get_conversation"}
PROXMOX_TOOLS = {"proxmox_list_guests", "proxmox_get_guest_config", "proxmox_node_status", "proxmox_list_tasks"}
MEMORY_TOOLS = {
    "memory_search_incidents",
    "memory_record_incident",
    "memory_get_previous_report",
    "memory_check_baseline",
}


def _names(specs: list[ToolSpec]) -> list[str]:
    return [s.name for s in specs]


def _everything_on(settings: Any, tmp_path: Path) -> Any:
    settings.conversation_history_dir = str(tmp_path)
    settings.memory_db_path = str(tmp_path / "memory.db")
    return settings


def test_registry_names_match_expected() -> None:
    assert [s.name for s in TOOL_SPECS] == EXPECTED_NAMES


def test_no_tool_from_upgrades_package() -> None:
    """The deploy capability is never a tool: no spec's function or schema lives in the upgrades package."""
    for spec in TOOL_SPECS:
        fn = spec.tool.coroutine or spec.tool.func
        assert fn is not None
        assert not fn.__module__.startswith("src.upgrades"), f"{spec.name} comes from {fn.__module__}"
        schema = spec.tool.args_schema
        assert isinstance(schema, type)
        assert not schema.__module__.startswith("src.upgrades"), f"{spec.name} schema from {schema.__module__}"
        assert "deploy" not in spec.name


def test_every_spec_has_label_and_object_schema() -> None:
    for spec in TOOL_SPECS:
        assert spec.label and tool_label(spec.name) == spec.label
        assert input_schema(spec)["type"] == "object"
        assert get_spec(spec.name) is spec


def test_tool_label_unknown() -> None:
    assert tool_label("no_such_tool") == "Running no_such_tool"
    assert get_spec("no_such_tool") is None


def test_only_memory_record_incident_writes() -> None:
    assert [s.name for s in TOOL_SPECS if not s.read_only] == ["memory_record_incident"]


def test_all_configured_mcp_and_agent_surfaces(mock_settings: Any, tmp_path: Path) -> None:
    settings = _everything_on(mock_settings, tmp_path)
    assert _names(enabled_specs(settings, "mcp")) == EXPECTED_NAMES
    agent = _names(enabled_specs(settings, "agent"))
    assert agent == [n for n in EXPECTED_NAMES if n not in CONVERSATION_TOOLS]


@pytest.mark.parametrize(
    ("setting", "gone"),
    [
        ("proxmox_url", PROXMOX_TOOLS),
        (
            "truenas_url",
            {
                "truenas_pool_status",
                "truenas_list_shares",
                "truenas_snapshots",
                "truenas_system_status",
                "truenas_apps",
                "hdd_power_status",
            },
        ),
        ("prometheus_url", {"hdd_power_status"}),
        ("loki_url", {"loki_query_logs", "loki_metric_query", "loki_list_label_values", "loki_correlate_changes"}),
        ("pbs_url", {"pbs_datastore_status", "pbs_list_backups", "pbs_list_tasks"}),
        (
            "memory_db_path",
            MEMORY_TOOLS,
        ),
        ("conversation_history_dir", CONVERSATION_TOOLS),
    ],
)
def test_conditions(mock_settings: Any, tmp_path: Path, setting: str, gone: set[str]) -> None:
    settings = _everything_on(mock_settings, tmp_path)
    setattr(settings, setting, "")
    names = _names(enabled_specs(settings, "mcp"))
    assert set(EXPECTED_NAMES) - set(names) == gone


def _drift(left_name: str, left: list[str], right_name: str, right: list[str]) -> str:
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    return f"{left_name}-only {only_left}, {right_name}-only {only_right}"


async def test_surfaces_built_from_registry(mock_settings: Any, tmp_path: Path) -> None:
    """The agent's tools, the SDK server and /mcp show the same tools with the same schemas.

    The conversation tools are /mcp only; every other tool is on all three surfaces.
    """
    from fastmcp import Client
    from mcp.types import ListToolsRequest

    from src.agent.agent import _get_tools
    from src.agent.mcp_tools import build_mcp_server
    from src.api.mcp_server import build_fastmcp_server

    settings = _everything_on(mock_settings, tmp_path)

    agent_tools = _get_tools()
    sdk_server = build_mcp_server(settings)["instance"]
    sdk_tools = (await sdk_server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))).root.tools
    async with Client(build_fastmcp_server(settings)) as client:
        mcp_tools = await client.list_tools()

    mcp_only = {s.name for s in TOOL_SPECS if "agent" not in s.surfaces}
    assert mcp_only == CONVERSATION_TOOLS
    agent_names = [t.name for t in agent_tools]
    sdk_names = [t.name for t in sdk_tools]
    shared_names = [t.name for t in mcp_tools if t.name not in mcp_only]
    assert agent_names == shared_names, _drift("agent", agent_names, "mcp", shared_names)
    assert sdk_names == shared_names, _drift("sdk", sdk_names, "mcp", shared_names)
    assert set(t.name for t in mcp_tools) - set(shared_names) == mcp_only

    sdk_by_name = {t.name: t for t in sdk_tools}
    mcp_by_name = {t.name: t for t in mcp_tools}
    for tool in agent_tools:
        spec = get_spec(tool.name)
        assert spec is not None and tool is spec.tool, f"{tool.name}: the agent's tool is not the registry's"
        served = mcp_by_name[tool.name]
        assert input_schema(spec) == served.inputSchema, f"{tool.name}: agent and /mcp schemas differ"
        assert sdk_by_name[tool.name].inputSchema == served.inputSchema, f"{tool.name}: SDK and /mcp schemas differ"
        assert sdk_by_name[tool.name].description == served.description == tool.description


def test_exclude(mock_settings: Any) -> None:
    names = _names(enabled_specs(mock_settings, "agent", exclude={"runbook_search"}))
    assert "runbook_search" not in names
    assert "prometheus_instant_query" in names


def test_disabled_group_logged_once(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    mock_settings.proxmox_url = ""
    with caplog.at_level(logging.INFO, logger="src.agent.tool_registry"):
        enabled_specs(mock_settings, "mcp")
    proxmox = [r.getMessage() for r in caplog.records if r.getMessage().startswith("proxmox ")]
    assert proxmox == [
        "proxmox tools disabled on mcp: proxmox_list_guests, proxmox_get_guest_config, proxmox_node_status, "
        "proxmox_list_tasks (PROXMOX_URL not set)"
    ]
    assert all(r.levelno == logging.INFO for r in caplog.records)


def test_input_schema_strips_titles_but_keeps_a_title_parameter() -> None:
    spec = get_spec("memory_record_incident")
    assert spec is not None
    schema = input_schema(spec)
    assert "title" not in schema
    assert "title" in schema["properties"]
    assert all("title" not in prop for prop in schema["properties"].values())
    assert schema["properties"]["title"]["description"]
    assert set(schema["required"]) == {"title", "description"}


def test_input_schema_keeps_limits_and_descriptions() -> None:
    spec = get_spec("loki_query_logs")
    assert spec is not None
    limit = input_schema(spec)["properties"]["limit"]
    assert limit["maximum"] == 1000
    assert limit["description"]


# ---------------------------------------------------------------------------
# run_tool
# ---------------------------------------------------------------------------


class _EchoInput(BaseModel):
    word: str = Field(description="A word")
    times: int = Field(2, ge=1, le=5, description="Repeat count")


def _fake_spec(tool: StructuredTool) -> ToolSpec:
    return ToolSpec(tool=tool, enabled=lambda _: True, label="Echoing", group="test")


async def test_run_tool_rejects_out_of_range_limit() -> None:
    spec = get_spec("loki_query_logs")
    assert spec is not None
    with pytest.raises(ToolError) as caught:
        await run_tool(spec, {"query": '{job="x"}', "limit": 1500})
    expected = "Invalid arguments for loki_query_logs: limit: Input should be less than or equal to 1000"
    assert str(caught.value) == expected
    assert isinstance(caught.value.__cause__, ValidationError)


async def test_run_tool_reports_every_invalid_field() -> None:
    spec = get_spec("loki_query_logs")
    assert spec is not None
    with pytest.raises(ToolError, match=r"query: Field required; limit: Input should be a valid integer"):
        await run_tool(spec, {"limit": "many"})


async def test_run_tool_passes_validated_fields_with_defaults() -> None:
    seen: dict[str, Any] = {}

    async def echo(word: str, times: int = 2) -> str:
        seen.update(word=word, times=times)
        return " ".join([word] * times)

    tool = StructuredTool.from_function(coroutine=echo, name="echo", description="Echo", args_schema=_EchoInput)
    assert await run_tool(_fake_spec(tool), {"word": "hi"}) == "hi hi"
    assert seen == {"word": "hi", "times": 2}


async def test_run_tool_does_not_swallow_tool_exception() -> None:
    """handle_tool_error=True would make ainvoke return the message as success; run_tool must raise."""

    async def fail(word: str, times: int = 2) -> str:
        raise ToolException(f"upstream refused {word}")

    tool = StructuredTool.from_function(coroutine=fail, name="fail", description="Fail", args_schema=_EchoInput)
    tool.handle_tool_error = True
    assert await tool.ainvoke({"word": "x"}) == "upstream refused x"  # the behaviour run_tool avoids
    with pytest.raises(ToolException, match="upstream refused x"):
        await run_tool(_fake_spec(tool), {"word": "x"})


async def test_run_tool_runs_sync_tools_in_a_thread() -> None:
    threads: list[int] = []

    def echo(word: str, times: int = 2) -> str:
        threads.append(threading.get_ident())
        return word

    tool = StructuredTool.from_function(func=echo, name="echo", description="Echo", args_schema=_EchoInput)
    assert await run_tool(_fake_spec(tool), {"word": "hi", "times": 1}) == "hi"
    assert threads and threads[0] != threading.get_ident()


def test_enabled_count_matches_enabled_specs(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The /health expected count uses the same filter as the server, and logs nothing."""
    from src.agent.tool_registry import enabled_count, enabled_specs
    from src.config import get_settings

    settings = get_settings()
    for surface in ("mcp", "agent"):
        with caplog.at_level(logging.INFO, logger="src.agent.tool_registry"):
            caplog.clear()
            count = enabled_count(settings, surface)
            assert caplog.records == []
        assert count == len(enabled_specs(settings, surface)) > 0
