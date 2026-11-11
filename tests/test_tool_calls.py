"""The per-call tool log line and metrics (src/observability/tool_calls.py), and how /mcp classifies each call."""

import logging
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastmcp.exceptions import NotFoundError, ToolError
from prometheus_client import REGISTRY

from src.observability.tool_calls import MAX_ARGS_CHARS, MAX_VALUE_CHARS, observe_tool_call, summarize_args

LOGGER = "src.observability.tool_calls"
PROM = "http://prometheus.test:9090"
_FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _calls(tool: str, status: str, source: str, client: str) -> float:
    labels = {"tool_name": tool, "status": status, "source": source, "client": client}
    return _sample("sre_assistant_tool_calls_total", labels)


def tool_call_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def fields(record: logging.LogRecord) -> dict[str, str]:
    """The ``key=value`` pairs of a tool_call line, with quoted values unquoted and unescaped."""
    message = record.getMessage()
    assert message.startswith("tool_call "), message
    out: dict[str, str] = {}
    for key, value in _FIELD.findall(message):
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out[key] = value
    return out


def _observe(**overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "tool": "prometheus_instant_query",
        "source": "mcp",
        "client": "unknown",
        "outcome": "success",
        "error_class": None,
        "error": None,
        "duration_s": 0.1234,
        "result_chars": 42,
        "arguments": {"query": "up"},
    }
    kwargs.update(overrides)
    observe_tool_call(**kwargs)


# ---------------------------------------------------------------------------
# observe_tool_call
# ---------------------------------------------------------------------------


def test_success_line_format(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _observe(arguments={"query": 'up{job="x"}', "time": None, "limit": 5})

    [record] = tool_call_records(caplog)
    assert record.levelno == logging.INFO
    assert fields(record) == {
        "tool": "prometheus_instant_query",
        "source": "mcp",
        "client": "unknown",
        "outcome": "success",
        "duration_ms": "123",
        "result_chars": "42",
        "args": "limit=5 query='up{job=\"x\"}' time=None",
    }


def test_args_summary_bounded() -> None:
    long_value = "x" * 500
    summary = summarize_args({"b": long_value, "a": [1, 2], "c": {"k": "v"}, "d": "line\nbreak\x00", "e": True})
    # Keys sorted, non-scalars as their type name, control characters gone, each value cut to 80.
    assert summary.startswith("a=list b='" + "x" * MAX_VALUE_CHARS + "' c=dict d='line break ' e=True")
    assert "\n" not in summary and "\x00" not in summary

    many = summarize_args({f"key{i:02d}": "v" * 200 for i in range(40)})
    assert len(many) <= MAX_ARGS_CHARS
    assert many.endswith("…")
    assert summarize_args({}) == ""


def test_error_fields_capped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _observe(outcome="error", error_class="ToolException", error='bad "thing"\n' + "e" * 400, result_chars=0)

    [record] = tool_call_records(caplog)
    parsed = fields(record)
    assert record.levelno == logging.INFO
    assert parsed["outcome"] == "error"
    assert parsed["error_class"] == "ToolException"
    assert parsed["error"].startswith('bad "thing" eee')
    assert len(parsed["error"]) == 200


def test_internal_logs_error_with_exc_info(caplog: pytest.LogCaptureFixture) -> None:
    boom = RuntimeError("socket gone")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _observe(outcome="internal", error=boom, result_chars=0)

    [record] = tool_call_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[1] is boom
    parsed = fields(record)
    assert parsed["error_class"] == "RuntimeError"
    assert parsed["error"] == "socket gone"


def test_tool_and_client_are_bare_tokens(caplog: pytest.LogCaptureFixture) -> None:
    """A tool name sent by a client cannot break the line apart."""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _observe(tool='evil name="x"\n' + "n" * 200, outcome="unknown_tool", client="in-process")

    parsed = fields(tool_call_records(caplog)[0])
    assert parsed["tool"].startswith("evil_name__x__")
    assert len(parsed["tool"]) == MAX_VALUE_CHARS
    assert parsed["outcome"] == "unknown_tool"


def test_identity_ref_appended_when_given(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _observe(identity_ref="ref-1234")
    assert fields(tool_call_records(caplog)[0])["identity_ref"] == "ref-1234"


def test_metrics_recorded_once() -> None:
    before = _calls("w11_metric_tool", "success", "agent", "agent")
    count_before = _sample(
        "sre_assistant_tool_call_duration_seconds_count", {"tool_name": "w11_metric_tool", "source": "agent"}
    )

    _observe(tool="w11_metric_tool", source="agent", client="agent", duration_s=0.5, result_chars=3000)

    assert _calls("w11_metric_tool", "success", "agent", "agent") - before == 1
    count_after = _sample(
        "sre_assistant_tool_call_duration_seconds_count", {"tool_name": "w11_metric_tool", "source": "agent"}
    )
    assert count_after - count_before == 1


def test_unknown_tool_metric_label_is_fixed() -> None:
    """Names sent by clients never become label values."""
    before = _calls("unknown", "unknown_tool", "mcp", "unknown")
    _observe(tool="made_up_tool_w11", outcome="unknown_tool", result_chars=0)
    assert _calls("unknown", "unknown_tool", "mcp", "unknown") - before == 1
    assert _calls("made_up_tool_w11", "unknown_tool", "mcp", "unknown") == 0


# ---------------------------------------------------------------------------
# ToolCallObserver on the FastMCP server (in-process calls: client=in-process)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@respx.mock
async def test_result_chars_histogram(mock_settings: Any) -> None:
    respx.get(f"{PROM}/api/v1/label/__name__/values").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": ["node_cpu_seconds_total"]})
    )
    respx.get(f"{PROM}/api/v1/metadata").mock(return_value=httpx.Response(200, json={"status": "success", "data": {}}))
    from src.api.mcp_server import build_fastmcp_server

    labels = {"tool_name": "prometheus_search_metrics", "source": "mcp"}
    sum_before = _sample("sre_assistant_tool_result_chars_sum", labels)
    count_before = _sample("sre_assistant_tool_result_chars_count", labels)

    result = await build_fastmcp_server(mock_settings).call_tool("prometheus_search_metrics", {"search_term": "cpu"})

    text_chars = sum(len(block.text) for block in result.content)  # type: ignore[union-attr]
    assert text_chars > 0
    assert _sample("sre_assistant_tool_result_chars_sum", labels) - sum_before == text_chars
    assert _sample("sre_assistant_tool_result_chars_count", labels) - count_before == 1


@pytest.mark.integration
@respx.mock
async def test_invalid_args_outcome(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """An out-of-range value and an unknown argument name are both invalid_args; the client still gets the error."""
    route = respx.get("http://loki.test:3100/loki/api/v1/query_range")
    from src.api.mcp_server import build_fastmcp_server

    server = build_fastmcp_server(mock_settings)
    before = _calls("loki_query_logs", "invalid_args", "mcp", "in-process")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(ToolError, match="less than or equal to 1000"):
            await server.call_tool("loki_query_logs", {"query": '{job="x"}', "limit": 1500})
        with pytest.raises(ToolError, match="Unknown argument"):
            await server.call_tool("loki_query_logs", {"query": '{job="x"}', "limt": 5})

    assert not route.called
    records = tool_call_records(caplog)
    assert [fields(r)["outcome"] for r in records] == ["invalid_args", "invalid_args"]
    assert all(r.levelno == logging.INFO for r in records)
    assert fields(records[0])["client"] == "in-process"
    assert fields(records[0])["args"] == "limit=1500 query='{job=\"x\"}'"
    assert _calls("loki_query_logs", "invalid_args", "mcp", "in-process") - before == 2


async def test_unknown_tool_outcome(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    from src.api.mcp_server import build_fastmcp_server

    before = _calls("unknown", "unknown_tool", "mcp", "in-process")
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(NotFoundError):
        await build_fastmcp_server(mock_settings).call_tool("no_such_tool_w11", {})

    [record] = tool_call_records(caplog)
    parsed = fields(record)
    assert parsed["outcome"] == "unknown_tool"
    assert parsed["tool"] == "no_such_tool_w11"
    assert parsed["error_class"] == "NotFoundError"
    assert _calls("unknown", "unknown_tool", "mcp", "in-process") - before == 1


@pytest.mark.integration
async def test_tool_exception_outcome_error_class(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The tool's own ToolException is outcome=error, named by its class, not FastMCP's wrapping ToolError."""
    from src.api.mcp_server import build_fastmcp_server

    before = _calls("prometheus_range_query", "error", "mcp", "in-process")
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(ToolError, match="Invalid range query"):
        await build_fastmcp_server(mock_settings).call_tool(
            "prometheus_range_query", {"query": "up", "start": "not-a-time", "end": "now"}
        )

    [record] = tool_call_records(caplog)
    parsed = fields(record)
    assert record.levelno == logging.INFO
    assert parsed["outcome"] == "error"
    assert parsed["error_class"] == "ToolException"
    assert parsed["error"].startswith("Invalid range query")
    assert parsed["result_chars"] == "0"
    assert _calls("prometheus_range_query", "error", "mcp", "in-process") - before == 1


async def test_timeout_outcome(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    from src.api.mcp_server import build_fastmcp_server

    with (
        patch("src.api.mcp_server.run_tool", AsyncMock(side_effect=httpx.ReadTimeout("slow upstream"))),
        caplog.at_level(logging.INFO, logger=LOGGER),
        pytest.raises(ToolError, match="timed out"),
    ):
        await build_fastmcp_server(mock_settings).call_tool("get_current_time", {})

    parsed = fields(tool_call_records(caplog)[0])
    assert parsed["outcome"] == "timeout"
    assert parsed["error_class"] == "ReadTimeout"


async def test_unexpected_exception_is_internal(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    from src.api.mcp_server import build_fastmcp_server

    with (
        patch("src.api.mcp_server.run_tool", AsyncMock(side_effect=RuntimeError("socket gone"))),
        caplog.at_level(logging.INFO, logger=LOGGER),
        pytest.raises(ToolError),
    ):
        await build_fastmcp_server(mock_settings).call_tool("get_current_time", {})

    [record] = tool_call_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    parsed = fields(record)
    assert parsed["outcome"] == "internal"
    assert parsed["error_class"] == "RuntimeError"


async def test_client_from_request_state(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The client label comes from request.state.mcp_client when an access check has set it."""
    from src.api.mcp_server import build_fastmcp_server

    request = SimpleNamespace(state=SimpleNamespace(mcp_client="claude-code"))
    with (
        patch("src.api.mcp_server.get_http_request", return_value=request),
        caplog.at_level(logging.INFO, logger=LOGGER),
    ):
        await build_fastmcp_server(mock_settings).call_tool("get_current_time", {})

    assert fields(tool_call_records(caplog)[0])["client"] == "claude-code"


@pytest.mark.integration
@respx.mock
async def test_upstream_timeout_inside_tool_exception_is_timeout(
    mock_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Tools wrap an httpx timeout in a ToolException; the outcome is still timeout, not error."""
    respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(side_effect=httpx.ReadTimeout("read timed out"))
    from src.api.mcp_server import build_fastmcp_server

    before = _calls("loki_query_logs", "timeout", "mcp", "in-process")
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(ToolError, match="timed out"):
        await build_fastmcp_server(mock_settings).call_tool("loki_query_logs", {"query": '{job="x"}'})

    parsed = fields(tool_call_records(caplog)[0])
    assert parsed["outcome"] == "timeout"
    assert parsed["error_class"] == "ReadTimeout"
    assert _calls("loki_query_logs", "timeout", "mcp", "in-process") - before == 1


def test_classify_tool_error_branches() -> None:
    from langchain_core.tools import ToolException

    from src.observability.tool_calls import classify_tool_error

    bare = ToolError("no function to call")
    assert classify_tool_error(bare) == ("error", bare)

    crash = RuntimeError("socket gone")
    try:
        raise ToolError("Error calling tool") from crash
    except ToolError as wrapped:
        assert classify_tool_error(wrapped) == ("internal", crash)

    tool_failure = ToolException("upstream said no")
    try:
        raise ToolError("upstream said no") from tool_failure
    except ToolError as wrapped:
        assert classify_tool_error(wrapped) == ("error", tool_failure)
