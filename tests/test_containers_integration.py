"""Integration tests for container_status and collect_containers with mocked Prometheus HTTP."""

import time
from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException

from src.agent.tools.containers import QUERIES, collect_containers, container_status

PROM_QUERY_URL = "http://prometheus.test:9090/api/v1/query"

type Series = list[tuple[dict[str, str], float]]
type Reply = Series | httpx.Response | Exception


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


def _exporter(host: str, name: str, image: str = "example/app:1") -> dict[str, str]:
    """An exporter series as stored: hostname is the exporter's host, exported_hostname the container's."""
    return {
        "job": "container-status",
        "instance": "192.0.2.10:8081",
        "hostname": "infra",
        "exported_hostname": host,
        "container_name": name,
        "image": image,
    }


def _cadvisor(host: str, name: str) -> dict[str, str]:
    return {"job": "cadvisor", "instance": "192.0.2.20:8080", "hostname": host, "name": name}


def _vector(series: Series) -> httpx.Response:
    result = [{"metric": labels, "value": [time.time(), str(value)]} for labels, value in series]
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


def _healthy_exporter() -> dict[str, Reply]:
    return {
        "exporter_up": [({"job": "container-status", "instance": "192.0.2.10:8081", "hostname": "infra"}, 1.0)],
        "last_collection": [({"job": "container-status", "hostname": "infra"}, time.time() - 20)],
    }


def _route(replies: dict[str, Reply]) -> respx.Route:
    """Answer each instant query by its QUERIES key; a key with no reply gets an empty vector."""
    by_query = {promql: key for key, promql in QUERIES.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        key = by_query[request.url.params["query"]]
        reply = replies.get(key, [])
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return _vector(reply)

    return respx.get(PROM_QUERY_URL).mock(side_effect=handler)


def _fleet() -> dict[str, Reply]:
    """Two containers on the media VM, one on the infra VM, one on the tubearchivist LXC."""
    return {
        **_healthy_exporter(),
        "state": [
            (_exporter("media vm", "jellyfin"), 1.0),
            (_exporter("media vm", "sonarr"), 1.0),
            (_exporter("infra vm", "grafana"), 1.0),
            (_exporter("tubearchivist lxc", "tubearchivist"), 1.0),
        ],
        "health": [
            (_exporter("media vm", "jellyfin"), 1.0),
            (_exporter("media vm", "sonarr"), 0.0),
            (_exporter("infra vm", "grafana"), 1.0),
            (_exporter("tubearchivist lxc", "tubearchivist"), 1.0),
        ],
        "restarts": [
            (_exporter("media vm", "jellyfin"), 0.0),
            (_exporter("media vm", "sonarr"), 0.0),
            (_exporter("infra vm", "grafana"), 0.0),
            (_exporter("tubearchivist lxc", "tubearchivist"), 0.0),
        ],
        "restarts_24h": [
            (_exporter("media vm", "jellyfin"), 0.0),
            (_exporter("media vm", "sonarr"), 0.0),
            (_exporter("infra vm", "grafana"), 0.0),
            (_exporter("tubearchivist lxc", "tubearchivist"), 0.0),
        ],
    }


async def _run(**kwargs: Any) -> str:
    args = {"host": None, "name": None, "only_problems": False} | kwargs
    return await container_status.coroutine(**args)  # pyright: ignore[reportOptionalCall]


@pytest.mark.integration
class TestContainerStatus:
    @respx.mock
    async def test_host_media_matches_exporter_media_vm(self) -> None:
        """F11: every exporter series carries hostname="infra"; the host to match is exported_hostname."""
        _route(_fleet())

        media = await _run(host="media")
        assert "media vm/jellyfin" in media
        assert "media vm/sonarr" in media
        assert "grafana" not in media
        assert "tubearchivist" not in media

        infra = await _run(host="infra")
        assert "infra vm/grafana" in infra
        assert "jellyfin" not in infra

    @respx.mock
    async def test_tubearchivist_joins_cadvisor_start_time(self) -> None:
        """Exporter "tubearchivist lxc" and cadvisor hostname "tube-archivist" are the same host."""
        started = time.time() - 3 * 3600
        replies = _fleet()
        replies["start_time"] = [
            (_cadvisor("tube-archivist", "tubearchivist"), started),
            (_cadvisor("media", "jellyfin"), started - 86400),
        ]
        _route(replies)

        report = await collect_containers(name="tubearchivist")
        assert [(r.host, r.name, r.started_at) for r in report.containers] == [
            ("tubearchivist lxc", "tubearchivist", started)
        ]
        text = await _run(name="tubearchivist")
        assert "(3h ago)" in text

    @respx.mock
    async def test_only_problems_filters_running_healthy(self) -> None:
        replies = _fleet()
        replies["state"] = [*replies["state"], (_exporter("media vm", "radarr"), 0.0)]  # type: ignore[misc]
        replies["health"] = [*replies["health"], (_exporter("media vm", "bazarr"), 2.0)]  # type: ignore[misc]
        replies["state"] = [*replies["state"], (_exporter("media vm", "bazarr"), 1.0)]  # type: ignore[misc]
        _route(replies)

        text = await _run(only_problems=True)
        assert "media vm/radarr: exited" in text
        assert "media vm/bazarr: running, unhealthy" in text
        assert "jellyfin" not in text
        assert "grafana" not in text
        assert "2 of 6 containers with problems" in text

    @respx.mock
    async def test_only_problems_none_found(self) -> None:
        _route(_fleet())
        text = await _run(only_problems=True, host="media")
        assert "No container problems among 2 containers" in text

    @respx.mock
    async def test_restart_delta_24h_shown(self) -> None:
        replies = _fleet()
        replies["restarts"] = [(_exporter("media vm", "sonarr"), 7.0)]
        replies["restarts_24h"] = [(_exporter("media vm", "sonarr"), 2.0384)]  # increase() extrapolates
        _route(replies)

        text = await _run(name="sonarr")
        assert "PROBLEM (2 restarts in 24h) media vm/sonarr: running, restarts 7 (+2 in 24h)" in text

    @respx.mock
    async def test_cadvisor_failure_visible_not_silent(self) -> None:
        replies = _fleet()
        replies["start_time"] = httpx.Response(500, text="cadvisor query exploded")
        _route(replies)

        text = await _run(host="media")
        assert "start time (cadvisor): unavailable: HTTP 500" in text
        assert "cadvisor query exploded" in text
        assert "media vm/jellyfin: running" in text  # the other sources still render

        report = await collect_containers(host="media")
        assert set(report.unavailable) == {"start_time"}

    @respx.mock
    async def test_exporter_down_flagged_stale(self) -> None:
        replies = _fleet()
        replies["exporter_up"] = [({"job": "container-status", "instance": "192.0.2.10:8081"}, 0.0)]
        replies["last_collection"] = [({"job": "container-status"}, time.time() - 1800)]
        _route(replies)

        text = await _run()
        assert "STALE" in text
        assert "up=0" in text
        assert "30m ago" in text

    @respx.mock
    async def test_unknown_host_lists_known_hosts(self) -> None:
        _route(_fleet())
        text = await _run(host="nosuchhost")
        assert "Unknown host 'nosuchhost'" in text
        assert "Known hosts: infra vm, media vm, tubearchivist lxc" in text

    @respx.mock
    async def test_no_name_match_on_known_host(self) -> None:
        _route(_fleet())
        text = await _run(host="media", name="nothing-called-this")
        assert "No containers match" in text
        assert "Unknown host" not in text

    @respx.mock
    async def test_state_failure_raises_naming_every_failed_source(self) -> None:
        replies = _fleet()
        replies["state"] = httpx.ConnectError("Connection refused")
        replies["health"] = httpx.ReadTimeout("slow")
        _route(replies)

        with pytest.raises(ToolException) as excinfo:
            await _run()
        message = str(excinfo.value)
        assert "container_state: unavailable" in message
        assert "container_health: unavailable" in message

    @respx.mock
    async def test_prometheus_error_status_is_unavailable(self) -> None:
        replies = _fleet()
        replies["image_info"] = httpx.Response(200, json={"status": "error", "error": "bad query"})
        _route(replies)

        report = await collect_containers()
        assert report.unavailable == {"image_info": "Prometheus error: bad query"}

    @respx.mock
    async def test_unexpected_exception_propagates(self) -> None:
        replies = _fleet()
        replies["health"] = RuntimeError("bug")
        _route(replies)

        with pytest.raises(RuntimeError, match="bug"):
            await collect_containers()

    @respx.mock
    async def test_queries_run_concurrently_once_each(self) -> None:
        route = _route(_fleet())
        await collect_containers()
        sent = sorted(call.request.url.params["query"] for call in route.calls)
        assert sent == sorted(QUERIES.values())

    @respx.mock
    async def test_restart_window_sets_query(self) -> None:
        route = respx.get(PROM_QUERY_URL).mock(side_effect=lambda _request: _vector([]))
        await collect_containers(restart_window_seconds=300)
        sent = {call.request.url.params["query"] for call in route.calls}
        assert 'increase(container_restart_count{job="container-status"}[300s])' in sent
        assert QUERIES["restarts_24h"] not in sent

    async def test_restart_window_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            await collect_containers(restart_window_seconds=0)
