"""Integration tests for the /metrics endpoint and request instrumentation.

Uses TestClient with mocked agent and HTTP calls — no real services needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.observability.metrics import COMPONENT_HEALTHY
from tests._index_states import HEALTHY_INDEX

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_agent() -> MagicMock:
    return MagicMock(name="fake_agent")


@pytest.fixture
def client(mock_settings: object, mock_agent: MagicMock) -> TestClient:  # noqa: ARG001
    with patch("src.api.main.build_agent", return_value=mock_agent):
        from src.api.main import app

        with TestClient(app) as tc:
            yield tc  # type: ignore[misc]


def _sample(metric_name: str, labels: dict[str, str] | None = None) -> float | None:
    return REGISTRY.get_sample_value(metric_name, labels or {})


# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Tests for GET /metrics."""

    @pytest.mark.integration
    def test_metrics_returns_prometheus_format(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.integration
    def test_metrics_contains_expected_metric_names(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        body = resp.text
        assert "sre_assistant_request_duration_seconds" in body
        assert "sre_assistant_requests_total" in body
        assert "sre_assistant_tool_calls_total" in body
        assert "sre_assistant_llm_calls_total" in body
        assert "sre_assistant_component_healthy" in body


# ---------------------------------------------------------------------------
# Request counting on /ask
# ---------------------------------------------------------------------------


class TestRequestInstrumentation:
    """Test that /ask increments request metrics."""

    @pytest.mark.integration
    def test_successful_request_increments_counter(self, client: TestClient) -> None:
        before = _sample("sre_assistant_requests_total", {"endpoint": "/ask", "status": "success"})

        with patch("src.api.main.invoke_agent", new_callable=AsyncMock, return_value="ok"):
            resp = client.post("/ask", json={"question": "hello"})

        assert resp.status_code == 200
        after = _sample("sre_assistant_requests_total", {"endpoint": "/ask", "status": "success"})
        assert after is not None
        assert after - (before or 0.0) == 1.0

    @pytest.mark.integration
    def test_failed_request_increments_error_counter(self, client: TestClient) -> None:
        before = _sample("sre_assistant_requests_total", {"endpoint": "/ask", "status": "error"})

        with patch("src.api.main.invoke_agent", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            resp = client.post("/ask", json={"question": "fail"})

        assert resp.status_code == 500
        after = _sample("sre_assistant_requests_total", {"endpoint": "/ask", "status": "error"})
        assert after is not None
        assert after - (before or 0.0) == 1.0

    @pytest.mark.integration
    def test_request_duration_recorded(self, client: TestClient) -> None:
        before = _sample("sre_assistant_request_duration_seconds_count", {"endpoint": "/ask"})

        with patch("src.api.main.invoke_agent", new_callable=AsyncMock, return_value="ok"):
            client.post("/ask", json={"question": "hello"})

        after = _sample("sre_assistant_request_duration_seconds_count", {"endpoint": "/ask"})
        assert after is not None
        assert after - (before or 0.0) == 1.0

    @pytest.mark.integration
    def test_in_progress_gauge_returns_to_zero(self, client: TestClient) -> None:
        with patch("src.api.main.invoke_agent", new_callable=AsyncMock, return_value="ok"):
            client.post("/ask", json={"question": "hello"})

        val = _sample("sre_assistant_requests_in_progress", {"endpoint": "/ask"})
        assert val == 0.0


# ---------------------------------------------------------------------------
# Component health gauges
# ---------------------------------------------------------------------------


class TestHealthGauges:
    """Test that /health updates the component_healthy gauge."""

    @pytest.mark.integration
    @respx.mock
    def test_healthy_components_set_gauge_to_one(self, client: TestClient) -> None:
        respx.get("http://prometheus.test:9090/-/healthy").mock(return_value=httpx.Response(200, text="ok"))
        respx.get("http://grafana.test:3000/api/health").mock(return_value=httpx.Response(200, json={}))
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(return_value=httpx.Response(200, text="pong"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(return_value=httpx.Response(200, json={}))
        respx.get("https://pbs.test:8007/api2/json/version").mock(return_value=httpx.Response(200, json={}))

        with patch("src.api.main.index_state", return_value=HEALTHY_INDEX):
            client.get("/health")

        assert COMPONENT_HEALTHY.labels(component="prometheus")._value.get() == 1.0
        assert COMPONENT_HEALTHY.labels(component="grafana")._value.get() == 1.0

    @pytest.mark.integration
    @respx.mock
    def test_unhealthy_component_sets_gauge_to_zero(self, client: TestClient) -> None:
        respx.get("http://prometheus.test:9090/-/healthy").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("http://grafana.test:3000/api/health").mock(return_value=httpx.Response(200, json={}))
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(return_value=httpx.Response(200, text="pong"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(return_value=httpx.Response(200, json={}))
        respx.get("https://pbs.test:8007/api2/json/version").mock(return_value=httpx.Response(200, json={}))

        with patch("src.api.main.index_state", return_value=HEALTHY_INDEX):
            client.get("/health")

        assert COMPONENT_HEALTHY.labels(component="prometheus")._value.get() == 0.0
