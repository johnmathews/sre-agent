"""The real app serves /mcp over HTTP: initialize, tools/list and tools/call, across lifespans."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

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
