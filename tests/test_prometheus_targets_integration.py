"""Integration tests for the scrape_targets tool with mocked Prometheus HTTP."""

from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException

from src.agent.tool_registry import get_spec
from src.agent.tools.prometheus_targets import scrape_targets

URL = "http://prometheus.test:9090/api/v1/targets"


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


def _active(job: str, instance: str, health: str, error: str = "") -> dict[str, Any]:
    return {
        "discoveredLabels": {},
        "labels": {"job": job, "instance": instance, "hostname": instance.split(":")[0]},
        "scrapePool": job,
        "scrapeUrl": f"http://{instance}/metrics",
        "globalUrl": f"http://{instance}/metrics",
        "lastError": error,
        "lastScrape": "2026-11-11T12:59:30.123456789Z",
        "lastScrapeDuration": 0.0123,
        "health": health,
    }


def _body(*targets: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"activeTargets": list(targets), "droppedTargets": []}}


@pytest.mark.integration
class TestScrapeTargets:
    @respx.mock
    async def test_only_down_lists_down_targets_with_last_error(self) -> None:
        route = respx.get(URL).mock(
            return_value=httpx.Response(
                200,
                json=_body(
                    _active("node", "healthy-host:9100", "up"),
                    _active("fastapi", "key-server:8000", "down", "connection refused"),
                ),
            )
        )
        result = await scrape_targets.coroutine(job=None, only_down=True)  # pyright: ignore[reportOptionalCall]
        assert route.calls.last.request.url.params["state"] == "active"
        assert "2 targets, 1 down across 2 jobs" in result
        assert "key-server:8000" in result
        assert "connection refused" in result
        assert "healthy-host" not in result

    @respx.mock
    async def test_job_filter_sent_and_applied_locally(self) -> None:
        route = respx.get(URL).mock(
            return_value=httpx.Response(
                200,
                json=_body(
                    _active("node", "a-host:9100", "up"),
                    _active("cadvisor", "b-host:8080", "up"),
                ),
            )
        )
        result = await scrape_targets.coroutine(job="node", only_down=False)  # pyright: ignore[reportOptionalCall]
        assert route.calls.last.request.url.params["scrapePool"] == "node"
        assert "a-host" in result
        assert "b-host" not in result
        assert "1 targets, 0 down across 1 jobs" in result

    @respx.mock
    async def test_no_job_sends_no_scrape_pool(self) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=_body()))
        await scrape_targets.coroutine(job=None, only_down=False)  # pyright: ignore[reportOptionalCall]
        assert "scrapePool" not in route.calls.last.request.url.params

    @respx.mock
    async def test_invalid_job_raises_and_sends_no_request(self) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=_body()))
        with pytest.raises(ToolException):
            await scrape_targets.coroutine(job='a"b', only_down=False)  # pyright: ignore[reportOptionalCall]
        assert not route.called

    @respx.mock
    async def test_prometheus_unreachable_raises_tool_exception(self) -> None:
        respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ToolException, match="Cannot connect to Prometheus"):
            await scrape_targets.coroutine(job=None, only_down=False)  # pyright: ignore[reportOptionalCall]

    @respx.mock
    async def test_timeout_and_http_error_raise_tool_exception(self) -> None:
        respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(ToolException, match="timed out"):
            await scrape_targets.coroutine(job=None, only_down=False)  # pyright: ignore[reportOptionalCall]
        respx.get(URL).mock(return_value=httpx.Response(503, text="unavailable"))
        with pytest.raises(ToolException, match="HTTP 503"):
            await scrape_targets.coroutine(job=None, only_down=False)  # pyright: ignore[reportOptionalCall]

    @respx.mock
    async def test_cap_n_of_m(self) -> None:
        targets = [_active("node", f"host{i}:9100", "up") for i in range(130)]
        respx.get(URL).mock(return_value=httpx.Response(200, json=_body(*targets)))
        result = await scrape_targets.coroutine(job=None, only_down=False)  # pyright: ignore[reportOptionalCall]
        assert "showing 100 of 130" in result

    @respx.mock
    async def test_unexpected_payload_raises_tool_exception(self) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, json={"status": "error", "error": "boom"}))
        with pytest.raises(ToolException, match="boom"):
            await scrape_targets.coroutine(job=None, only_down=False)  # pyright: ignore[reportOptionalCall]


def test_registered_in_core_group() -> None:
    spec = get_spec("scrape_targets")
    assert spec is not None
    assert spec.group == "core"
    assert spec.label == "Listing Prometheus scrape targets"
