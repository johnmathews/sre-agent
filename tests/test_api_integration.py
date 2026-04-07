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
        assert len(body["components"]) == 8  # includes MCP (always enabled)
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
        # MCP is always healthy (always mounted), so status is degraded not unhealthy
        assert body["status"] == "degraded"
        non_mcp = [c for c in body["components"] if c["name"] != "mcp_server"]
        assert all(c["status"] == "unhealthy" for c in non_mcp)


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


# ---------------------------------------------------------------------------
# GET /conversations, GET/DELETE/PATCH /conversations/{id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestConversationEndpoints:
    """Tests for conversation management API endpoints."""

    def _populate(self, history_dir: str, session_id: str, content: str = "hello question") -> None:
        """Seed a conversation file using save_turn."""
        from src.agent.history import save_turn

        save_turn(history_dir, session_id, "user", content, "gpt-4o-mini", "openai")
        save_turn(history_dir, session_id, "assistant", "hi there", "gpt-4o-mini", "openai")

    def test_list_returns_503_when_history_disabled(self, client: TestClient, mock_settings: Any) -> None:
        mock_settings.conversation_history_dir = ""
        resp = client.get("/conversations")
        assert resp.status_code == 503

    def test_list_empty(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.get("/conversations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_metadata(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        self._populate(str(tmp_path), "abc12345", "what's up with prometheus?")

        resp = client.get("/conversations")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["session_id"] == "abc12345"
        assert items[0]["title"] == "what's up with prometheus?"
        assert items[0]["provider"] == "openai"
        assert items[0]["turn_count"] == 1
        assert items[0]["model"] == "gpt-4o-mini"

    def test_list_sorted_most_recent_first(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        import time

        mock_settings.conversation_history_dir = str(tmp_path)
        self._populate(str(tmp_path), "sess1")
        time.sleep(0.02)
        self._populate(str(tmp_path), "sess2")
        time.sleep(0.02)
        self._populate(str(tmp_path), "sess3")

        resp = client.get("/conversations")
        ids = [c["session_id"] for c in resp.json()]
        assert ids == ["sess3", "sess2", "sess1"]

    def test_get_returns_full_payload(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        self._populate(str(tmp_path), "detail1")

        resp = client.get("/conversations/detail1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "detail1"
        assert len(body["turns"]) == 2
        assert body["turns"][0]["role"] == "user"
        assert body["turns"][1]["role"] == "assistant"

    def test_get_404_when_missing(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.get("/conversations/nonexistent")
        assert resp.status_code == 404

    def test_get_400_on_dots(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.get("/conversations/a.b.c")
        assert resp.status_code == 400

    def test_get_400_on_invalid_chars(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.get("/conversations/has spaces")
        assert resp.status_code == 400

    def test_delete_removes_file(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        self._populate(str(tmp_path), "todelete")

        resp = client.delete("/conversations/todelete")
        assert resp.status_code == 204

        resp2 = client.get("/conversations/todelete")
        assert resp2.status_code == 404

    def test_delete_404_when_missing(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.delete("/conversations/nonexistent")
        assert resp.status_code == 404

    def test_delete_400_on_dots(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.delete("/conversations/a.b")
        assert resp.status_code == 400

    def test_rename_updates_title(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        self._populate(str(tmp_path), "renameme", "original title stuff")

        resp = client.patch("/conversations/renameme", json={"title": "My Custom Name"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "My Custom Name"

        resp2 = client.get("/conversations/renameme")
        assert resp2.json()["title"] == "My Custom Name"

    def test_rename_422_when_empty_title(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        self._populate(str(tmp_path), "r1")
        resp = client.patch("/conversations/r1", json={"title": "   "})
        assert resp.status_code == 422

    def test_rename_404_when_missing(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.patch("/conversations/nonexistent", json={"title": "x"})
        assert resp.status_code == 404

    def test_rename_400_on_dots(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        mock_settings.conversation_history_dir = str(tmp_path)
        resp = client.patch("/conversations/a.b", json={"title": "x"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /conversations/search
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSearchConversations:
    """GET /conversations/search"""

    def test_search_returns_matching_conversations(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        from src.agent.history import save_turn
        from src.config import get_settings

        settings = get_settings()
        settings.conversation_history_dir = str(tmp_path)
        save_turn(str(tmp_path), "s1", "user", "CPU spike investigation", "m", "anthropic")
        save_turn(str(tmp_path), "s1", "assistant", "CPU is at 95%.", "m", "anthropic")

        resp = client.get("/conversations/search", params={"q": "CPU"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["session_id"] == "s1"
        assert len(data[0]["matches"]) >= 1

    def test_search_empty_query_returns_empty(self, client: TestClient, mock_settings: Any) -> None:
        resp = client.get("/conversations/search", params={"q": ""})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_no_matches(self, client: TestClient, mock_settings: Any, tmp_path: Any) -> None:
        from src.agent.history import save_turn
        from src.config import get_settings

        settings = get_settings()
        settings.conversation_history_dir = str(tmp_path)
        save_turn(str(tmp_path), "s1", "user", "Disk check", "m", "anthropic")

        resp = client.get("/conversations/search", params={"q": "kubernetes"})
        assert resp.status_code == 200
        assert resp.json() == []
