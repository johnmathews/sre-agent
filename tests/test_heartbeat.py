"""Tests for the SSE heartbeat wrapper in the API layer."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from src.api.main import _with_heartbeats


async def _events_from_list(items: list[dict[str, str]], delay: float = 0.0) -> AsyncIterator[dict[str, str]]:
    """Helper: yield events from a list with an optional delay between each."""
    for item in items:
        if delay > 0:
            await asyncio.sleep(delay)
        yield item


class TestWithHeartbeats:
    """Tests for _with_heartbeats async generator."""

    async def test_passes_through_events(self) -> None:
        """All source events are yielded in order."""
        source = [
            {"type": "status", "content": "Thinking..."},
            {"type": "tool_start", "content": "Querying Prometheus"},
            {"type": "answer", "content": "CPU is at 42%"},
        ]
        result: list[dict[str, str]] = []
        async for event in _with_heartbeats(_events_from_list(source), interval=100.0):
            result.append(event)

        # All original events present, in order
        assert [e["type"] for e in result] == ["status", "tool_start", "answer"]

    async def test_heartbeats_injected_during_delay(self) -> None:
        """When the source is slow, heartbeat events are injected."""
        source = [
            {"type": "status", "content": "Thinking..."},
            {"type": "answer", "content": "Done"},
        ]
        result: list[dict[str, str]] = []
        # Source yields with 0.3s delay, heartbeat every 0.1s
        async for event in _with_heartbeats(_events_from_list(source, delay=0.3), interval=0.1):
            result.append(event)

        types = [e["type"] for e in result]
        assert "heartbeat" in types
        # Original events are still present
        assert "status" in types
        assert "answer" in types

    async def test_heartbeat_event_shape(self) -> None:
        """Heartbeat events have the expected structure."""
        source = [{"type": "answer", "content": "Done"}]
        result: list[dict[str, str]] = []
        async for event in _with_heartbeats(_events_from_list(source, delay=0.15), interval=0.05):
            result.append(event)

        heartbeats = [e for e in result if e["type"] == "heartbeat"]
        assert len(heartbeats) >= 1
        assert heartbeats[0] == {"type": "heartbeat", "content": ""}

    async def test_no_heartbeats_when_fast(self) -> None:
        """When the source is fast, no heartbeats should be injected."""
        source = [
            {"type": "status", "content": "Thinking..."},
            {"type": "answer", "content": "Done"},
        ]
        result: list[dict[str, str]] = []
        # No delay, heartbeat interval very long
        async for event in _with_heartbeats(_events_from_list(source), interval=100.0):
            result.append(event)

        types = [e["type"] for e in result]
        assert "heartbeat" not in types

    async def test_empty_source(self) -> None:
        """An empty source stream terminates cleanly."""
        result: list[dict[str, str]] = []
        async for event in _with_heartbeats(_events_from_list([]), interval=0.05):
            result.append(event)

        # May have zero or a few heartbeats, but should terminate
        assert all(e["type"] == "heartbeat" for e in result)

    async def test_source_exception_propagates(self) -> None:
        """Exceptions from the source stream propagate to the consumer."""

        async def _failing_source() -> AsyncIterator[dict[str, str]]:
            yield {"type": "status", "content": "start"}
            raise RuntimeError("source exploded")

        with pytest.raises(RuntimeError, match="source exploded"):
            async for _ in _with_heartbeats(_failing_source(), interval=100.0):
                pass

    async def test_preserves_event_order(self) -> None:
        """Source events maintain their relative ordering even with heartbeats interleaved."""
        source = [{"type": "event", "content": str(i)} for i in range(5)]
        result: list[dict[str, str]] = []
        async for event in _with_heartbeats(_events_from_list(source, delay=0.05), interval=0.03):
            result.append(event)

        # Filter out heartbeats and check order
        non_hb = [e for e in result if e["type"] != "heartbeat"]
        assert [e["content"] for e in non_hb] == ["0", "1", "2", "3", "4"]
