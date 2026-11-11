"""The real app serves /mcp over HTTP: initialize, tools/list and tools/call, across lifespans."""

import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from tests.test_tool_calls import LOGGER, fields, tool_call_records

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
PROM = "http://prometheus.test:9090"


def _rpc(client: TestClient, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """POST one JSON-RPC request to ``/mcp`` (no trailing slash) and return the last SSE ``data:`` payload."""
    response = client.post(
        "/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    assert response.status_code == 200, response.text
    data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert data, response.text
    payload: dict[str, Any] = json.loads(data[-1])
    return payload


def _initialize(client: TestClient) -> dict[str, Any]:
    return _rpc(
        client,
        1,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    )


@pytest.mark.integration
@respx.mock
def test_initialize_list_call_over_http(mock_settings: Any) -> None:
    respx.get(f"{PROM}/api/v1/label/__name__/values").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": ["node_cpu_seconds_total"]})
    )
    respx.get(f"{PROM}/api/v1/metadata").mock(return_value=httpx.Response(200, json={"status": "success", "data": {}}))
    with patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")):
        from src.api.main import app

        with TestClient(app) as client:
            init = _initialize(client)
            assert init["result"]["serverInfo"]["name"] == "sre-assistant"
            names = {t["name"] for t in _rpc(client, 2, "tools/list", {})["result"]["tools"]}
            assert "prometheus_search_metrics" in names
            call = _rpc(
                client, 3, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"search_term": "cpu"}}
            )
            assert call["result"]["isError"] is False
            assert "node_cpu_seconds_total" in call["result"]["content"][0]["text"]


@pytest.mark.integration
def test_mcp_served_after_second_lifespan(mock_settings: Any) -> None:
    """A restart of the lifespan (as TestClient does twice) must not leave /mcp dead or mounted twice."""
    with patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")):
        from src.api.main import app

        with TestClient(app):
            pass
        with TestClient(app) as client:
            assert _initialize(client)["result"]["serverInfo"]["name"] == "sre-assistant"
            assert "tools" in _rpc(client, 2, "tools/list", {})["result"]
            mounts = [r for r in app.routes if getattr(r, "path", "") == "/mcp"]
            assert len(mounts) == 1


@pytest.mark.integration
def test_mcp_503_before_startup(mock_settings: Any) -> None:
    """Outside the lifespan (before startup or after shutdown) /mcp answers 503 with a JSON error."""
    from src.api.main import app

    client = TestClient(app)  # not entered: no lifespan has run
    response = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 503
    assert response.json() == {"error": "MCP server not started"}

    with patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")), TestClient(app):
        pass
    after = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert after.status_code == 503


def _mock_search(metric_names: list[str]) -> None:
    respx.get(f"{PROM}/api/v1/label/__name__/values").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": metric_names})
    )
    respx.get(f"{PROM}/api/v1/metadata").mock(return_value=httpx.Response(200, json={"status": "success", "data": {}}))


def _search_calls() -> float:
    labels = {"tool_name": "prometheus_search_metrics", "status": "success", "source": "mcp", "client": "unknown"}
    return REGISTRY.get_sample_value("sre_assistant_tool_calls_total", labels) or 0.0


@pytest.mark.integration
@respx.mock
def test_one_log_line_per_mcp_call(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """One tools/call over HTTP writes one INFO tool_call line; initialize and tools/list write none."""
    _mock_search(["node_cpu_seconds_total"])
    with (
        patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")),
        caplog.at_level(logging.INFO, logger=LOGGER),
    ):
        from src.api.main import app

        with TestClient(app) as client:
            _initialize(client)
            _rpc(client, 2, "tools/list", {})
            _rpc(client, 3, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"search_term": "cpu"}})

    [record] = tool_call_records(caplog)
    assert record.levelno == logging.INFO
    parsed = fields(record)
    assert parsed["tool"] == "prometheus_search_metrics"
    assert parsed["source"] == "mcp"
    assert parsed["client"] == "unknown"  # an HTTP request with no access check result (W12 sets it)
    assert parsed["outcome"] == "success"
    assert int(parsed["duration_ms"]) >= 0
    assert int(parsed["result_chars"]) > 0
    assert parsed["args"] == "search_term='cpu'"


@pytest.mark.integration
@respx.mock
def test_http_call_increments_source_mcp(mock_settings: Any) -> None:
    _mock_search(["node_cpu_seconds_total"])
    before = _search_calls()
    with patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")):
        from src.api.main import app

        with TestClient(app) as client:
            _initialize(client)
            _rpc(client, 2, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"search_term": "cpu"}})
            _rpc(client, 3, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"search_term": "cpu"}})

    assert _search_calls() - before == 2


@pytest.mark.integration
@respx.mock
def test_result_never_logged(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The client gets the result, the logs get only its length."""
    marker = "w11_result_marker_metric"
    _mock_search([marker])
    with (
        patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")),
        caplog.at_level(logging.DEBUG),
        caplog.at_level(logging.DEBUG, logger=LOGGER),
    ):
        from src.api.main import app

        with TestClient(app) as client:
            _initialize(client)
            call = _rpc(
                client, 2, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"search_term": "w11"}}
            )

    assert marker in call["result"]["content"][0]["text"]
    assert tool_call_records(caplog), "no tool_call line was written"
    assert marker not in caplog.text


@pytest.mark.integration
@respx.mock
def test_expected_tool_errors_logged_once(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """A bad argument or a tool's own failure writes the tool_call line and no FastMCP ERROR line.

    FastMCP logs a ``ToolError`` at the error's ``log_level`` (ERROR by default) with no traceback, so W5's
    traceback filter does not catch it; the expected errors are raised at DEBUG instead.
    """
    respx.get(f"{PROM}/api/v1/label/__name__/values").mock(return_value=httpx.Response(500, text="down"))
    with (
        patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")),
        caplog.at_level(logging.INFO),
    ):
        from src.api.main import app

        with TestClient(app) as client:
            _initialize(client)
            bad_name = _rpc(client, 2, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"x": "1"}})
            bad_value = _rpc(
                client, 3, "tools/call", {"name": "loki_query_logs", "arguments": {"query": '{a="b"}', "limit": 1500}}
            )
            failed = _rpc(
                client, 4, "tools/call", {"name": "prometheus_search_metrics", "arguments": {"search_term": "cpu"}}
            )

    assert all(r["result"]["isError"] for r in (bad_name, bad_value, failed))
    outcomes = [fields(r)["outcome"] for r in tool_call_records(caplog)]
    assert outcomes == ["invalid_args", "invalid_args", "error"]
    fastmcp_errors = [
        r.getMessage() for r in caplog.records if r.name.startswith("fastmcp") and r.levelno >= logging.ERROR
    ]
    assert fastmcp_errors == []
