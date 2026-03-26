"""Integration tests for the FastAPI backend.

Uses TestClient with mocked agent and HTTP calls — no real services needed.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_agent() -> MagicMock:
    """A fake agent object to stand in for the real compiled graph."""
    return MagicMock(name="fake_agent")


@pytest.fixture
def client(mock_settings: object, mock_agent: MagicMock) -> TestClient:  # noqa: ARG001 — mock_settings activates patches
    """Create a TestClient with the agent pre-injected into app state."""
    with patch("src.api.main.build_agent", return_value=mock_agent):
        from src.api.main import app

        with TestClient(app) as tc:
            yield tc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# POST /ask
# ---------------------------------------------------------------------------


class TestAskEndpoint:
    """Tests for POST /ask."""

    @pytest.mark.integration
    def test_successful_question(self, client: TestClient) -> None:
        with patch(
            "src.api.main.invoke_agent",
            new_callable=AsyncMock,
            return_value="CPU is at 42% on node-3.",
        ):
            resp = client.post("/ask", json={"question": "What is CPU on node-3?"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "CPU is at 42% on node-3."
        assert "session_id" in body

    @pytest.mark.integration
    def test_server_generates_session_id(self, client: TestClient) -> None:
        with patch(
            "src.api.main.invoke_agent",
            new_callable=AsyncMock,
            return_value="ok",
        ):
            resp = client.post("/ask", json={"question": "hello"})

        body = resp.json()
        assert len(body["session_id"]) == 8

    @pytest.mark.integration
    def test_client_session_id_echoed(self, client: TestClient) -> None:
        with patch(
            "src.api.main.invoke_agent",
            new_callable=AsyncMock,
            return_value="ok",
        ):
            resp = client.post(
                "/ask",
                json={"question": "hello", "session_id": "my-sess-1"},
            )

        assert resp.json()["session_id"] == "my-sess-1"

    @pytest.mark.integration
    def test_empty_question_returns_422(self, client: TestClient) -> None:
        resp = client.post("/ask", json={})
        assert resp.status_code == 422

    @pytest.mark.integration
    def test_agent_failure_returns_500(self, client: TestClient) -> None:
        with patch(
            "src.api.main.invoke_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM exploded"),
        ):
            resp = client.post("/ask", json={"question": "boom"})

        assert resp.status_code == 500
        assert "LLM exploded" in resp.json()["detail"]

    @pytest.mark.integration
    def test_tool_call_pairing_error_recovered_by_invoke_agent(self, client: TestClient) -> None:
        """invoke_agent handles tool_call pairing errors internally, so the API
        should return 200 with the recovered response, not 500."""
        with patch(
            "src.api.main.invoke_agent",
            new_callable=AsyncMock,
            return_value="Recovered after session reset.",
        ):
            resp = client.post(
                "/ask",
                json={"question": "hello?", "session_id": "broken-sess"},
            )

        assert resp.status_code == 200
        assert resp.json()["response"] == "Recovered after session reset."

    @pytest.mark.integration
    def test_timeout_returns_504(self, client: TestClient) -> None:
        """A timeout from the request timeout guard returns 504 Gateway Timeout."""
        with patch(
            "src.api.main.invoke_agent",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timed out"),
        ):
            resp = client.post("/ask", json={"question": "slow"})

        assert resp.status_code == 504
        assert "timed out" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for GET /health."""

    @pytest.mark.integration
    @respx.mock
    def test_all_healthy(self, client: TestClient, tmp_path: object) -> None:
        respx.get("http://prometheus.test:9090/-/healthy").mock(
            return_value=httpx.Response(200, text="Prometheus Server is Healthy.")
        )
        respx.get("http://grafana.test:3000/api/health").mock(return_value=httpx.Response(200, json={"database": "ok"}))
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "8.1.3"}})
        )
        respx.get("https://pbs.test:8007/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "3.1.2"}})
        )
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(return_value=httpx.Response(200, text="pong"))

        with patch("src.api.main.CHROMA_PERSIST_DIR") as mock_chroma_dir:
            mock_chroma_dir.is_dir.return_value = True
            resp = client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["model"] == "gpt-4o-mini"
        assert len(body["components"]) == 7
        assert all(c["status"] == "healthy" for c in body["components"])

    @pytest.mark.integration
    @respx.mock
    def test_prometheus_unreachable(self, client: TestClient) -> None:
        respx.get("http://prometheus.test:9090/-/healthy").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("http://grafana.test:3000/api/health").mock(return_value=httpx.Response(200, json={"database": "ok"}))
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(return_value=httpx.Response(200, text="pong"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "8.1.3"}})
        )
        respx.get("https://pbs.test:8007/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "3.1.2"}})
        )

        with patch("src.api.main.CHROMA_PERSIST_DIR") as mock_chroma_dir:
            mock_chroma_dir.is_dir.return_value = True
            resp = client.get("/health")

        body = resp.json()
        assert body["status"] == "degraded"
        prom = next(c for c in body["components"] if c["name"] == "prometheus")
        assert prom["status"] == "unhealthy"

    @pytest.mark.integration
    @respx.mock
    def test_grafana_unreachable(self, client: TestClient) -> None:
        respx.get("http://prometheus.test:9090/-/healthy").mock(return_value=httpx.Response(200, text="ok"))
        respx.get("http://grafana.test:3000/api/health").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(return_value=httpx.Response(200, text="pong"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "8.1.3"}})
        )
        respx.get("https://pbs.test:8007/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "3.1.2"}})
        )

        with patch("src.api.main.CHROMA_PERSIST_DIR") as mock_chroma_dir:
            mock_chroma_dir.is_dir.return_value = True
            resp = client.get("/health")

        body = resp.json()
        assert body["status"] == "degraded"
        grafana = next(c for c in body["components"] if c["name"] == "grafana")
        assert grafana["status"] == "unhealthy"

    @pytest.mark.integration
    @respx.mock
    def test_anthropic_provider_shows_correct_model(self, client: TestClient, mock_settings: object) -> None:
        """When llm_provider=anthropic, /health returns the anthropic model name."""
        mock_settings.llm_provider = "anthropic"  # type: ignore[attr-defined]
        mock_settings.anthropic_model = "claude-sonnet-4-20250514"  # type: ignore[attr-defined]

        respx.get("http://prometheus.test:9090/-/healthy").mock(return_value=httpx.Response(200, text="ok"))
        respx.get("http://grafana.test:3000/api/health").mock(return_value=httpx.Response(200, json={"database": "ok"}))
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "8.1.3"}})
        )
        respx.get("https://pbs.test:8007/api2/json/version").mock(
            return_value=httpx.Response(200, json={"data": {"version": "3.1.2"}})
        )
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(return_value=httpx.Response(200, text="pong"))

        with patch("src.api.main.CHROMA_PERSIST_DIR") as mock_chroma_dir:
            mock_chroma_dir.is_dir.return_value = True
            resp = client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.integration
    @respx.mock
    def test_all_unhealthy(self, client: TestClient) -> None:
        respx.get("http://prometheus.test:9090/-/healthy").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("http://grafana.test:3000/api/health").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("http://loki.test:3100/ready").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("https://proxmox.test:8006/api2/json/version").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        respx.get("https://pbs.test:8007/api2/json/version").mock(side_effect=httpx.ConnectError("connection refused"))
        respx.get("https://truenas.test/api/v2.0/core/ping").mock(side_effect=httpx.ConnectError("connection refused"))

        with patch("src.api.main.CHROMA_PERSIST_DIR") as mock_chroma_dir:
            mock_chroma_dir.is_dir.return_value = False
            resp = client.get("/health")

        body = resp.json()
        assert body["status"] == "unhealthy"
        assert all(c["status"] == "unhealthy" for c in body["components"])


# ---------------------------------------------------------------------------
# POST /ask/stream
# ---------------------------------------------------------------------------


def _parse_sse_events(raw: str) -> list[dict[str, Any]]:
    """Parse SSE response text into a list of event dicts."""
    events: list[dict[str, Any]] = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:]
            events.append(json.loads(payload))
    return events


class TestAskStreamEndpoint:
    """Tests for POST /ask/stream (SSE)."""

    @pytest.mark.integration
    def test_streams_status_and_answer(self, client: TestClient) -> None:
        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"type": "status", "content": "Thinking..."}
            yield {"type": "answer", "content": "CPU at 42%.", "session_id": "abc"}

        with patch("src.api.main.stream_agent", return_value=fake_stream()):
            resp = client.post(
                "/ask/stream",
                json={"question": "CPU usage?"},
            )

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"

        events = _parse_sse_events(resp.text)
        types = [e["type"] for e in events]
        assert "status" in types
        assert "answer" in types

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "CPU at 42%."

    @pytest.mark.integration
    def test_streams_tool_events(self, client: TestClient) -> None:
        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"type": "status", "content": "Thinking..."}
            yield {
                "type": "tool_start",
                "content": "Querying Prometheus: `up`",
                "tool_name": "prometheus_instant_query",
            }
            yield {
                "type": "tool_end",
                "content": "Querying Prometheus — done",
                "tool_name": "prometheus_instant_query",
            }
            yield {"type": "answer", "content": "All up.", "session_id": "s1"}

        with patch("src.api.main.stream_agent", return_value=fake_stream()):
            resp = client.post(
                "/ask/stream",
                json={"question": "nodes up?"},
            )

        events = _parse_sse_events(resp.text)
        tool_start = next(e for e in events if e["type"] == "tool_start")
        assert tool_start["tool_name"] == "prometheus_instant_query"

    @pytest.mark.integration
    def test_server_generates_session_id(self, client: TestClient) -> None:
        """When no session_id is provided, the server generates one."""

        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"type": "answer", "content": "ok", "session_id": "generated"}

        with patch("src.api.main.stream_agent", return_value=fake_stream()):
            resp = client.post(
                "/ask/stream",
                json={"question": "hello"},
            )

        events = _parse_sse_events(resp.text)
        answer = next(e for e in events if e["type"] == "answer")
        assert "session_id" in answer

    @pytest.mark.integration
    def test_stream_error_yields_error_event(self, client: TestClient) -> None:
        async def failing_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise RuntimeError("LLM exploded")
            yield  # noqa: RET503

        with patch("src.api.main.stream_agent", return_value=failing_stream()):
            resp = client.post(
                "/ask/stream",
                json={"question": "boom"},
            )

        # SSE endpoint returns 200 with error event in the stream
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        error_event = next(e for e in events if e["type"] == "error")
        assert "LLM exploded" in error_event["content"]

    @pytest.mark.integration
    def test_empty_question_returns_422(self, client: TestClient) -> None:
        resp = client.post("/ask/stream", json={})
        assert resp.status_code == 422
