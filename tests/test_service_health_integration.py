"""Integration tests for service_health with mocked Prometheus, Grafana and Loki HTTP."""

import time
from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException

from src.agent.tools.containers import QUERIES as CONTAINER_QUERIES
from src.agent.tools.service_health import service_health

PROM_QUERY_URL = "http://prometheus.test:9090/api/v1/query"
GRAFANA_ALERTS_URL = "http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups"
LOKI_QUERY_URL = "http://loki.test:3100/loki/api/v1/query"

GATUS_NOW = "gatus_results_endpoint_success"
PROBES = "probe_success"

type Series = list[tuple[dict[str, str], float]]
type Reply = Series | httpx.Response | Exception


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


def _vector(series: Series) -> httpx.Response:
    result = [{"metric": labels, "value": [time.time(), str(value)]} for labels, value in series]
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


def _gatus(name: str, key: str, group: str = "apps") -> dict[str, str]:
    return {"group": group, "key": key, "name": name, "type": "HTTP", "job": "gatus", "instance": "192.0.2.30:8080"}


def _exporter(host: str, name: str) -> dict[str, str]:
    return {
        "job": "container-status",
        "instance": "192.0.2.10:8081",
        "hostname": "infra",
        "exported_hostname": host,
        "container_name": name,
        "image": "example/app:1",
    }


def _answer(reply: Reply) -> httpx.Response:
    if isinstance(reply, Exception):
        raise reply
    if isinstance(reply, httpx.Response):
        return reply
    return _vector(reply)


def _prometheus(replies: dict[str, Reply]) -> respx.Route:
    """Answer each instant query by its PromQL; the Gatus ratio is keyed 'gatus_ratio', containers by QUERIES key."""
    by_query = {promql: key for key, promql in CONTAINER_QUERIES.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if query.startswith(f"avg_over_time({GATUS_NOW}["):
            key = "gatus_ratio"
        elif query.startswith("increase(container_restart_count"):
            key = "restarts_24h"  # the restart window follows service_health's window
        else:
            key = by_query.get(query, query)
        return _answer(replies.get(key, []))

    return respx.get(PROM_QUERY_URL).mock(side_effect=handler)


def _grafana(groups: list[dict[str, Any]] | httpx.Response) -> respx.Route:
    reply = groups if isinstance(groups, httpx.Response) else httpx.Response(200, json=groups)
    return respx.get(GRAFANA_ALERTS_URL).mock(return_value=reply)


def _loki(reply: Reply) -> respx.Route:
    return respx.get(LOKI_QUERY_URL).mock(side_effect=lambda _request: _answer(reply))


def _healthy_jellyfin() -> dict[str, Reply]:
    gatus = _gatus("Jellyfin", "media_jellyfin", group="media")
    return {
        GATUS_NOW: [(gatus, 1.0)],
        "gatus_ratio": [(gatus, 1.0)],
        "state": [(_exporter("media vm", "jellyfin"), 1.0)],
        "health": [(_exporter("media vm", "jellyfin"), 1.0)],
        "restarts": [(_exporter("media vm", "jellyfin"), 0.0)],
        "restarts_24h": [(_exporter("media vm", "jellyfin"), 0.0)],
        "exporter_up": [({"job": "container-status"}, 1.0)],
        "last_collection": [({"job": "container-status"}, time.time() - 20)],
        PROBES: [({"instance": "https://jellyfin.example.com", "job": "blackbox_public"}, 1.0)],
    }


async def _run(name: str, window: str = "1h") -> str:
    return await service_health.coroutine(name=name, window=window)  # pyright: ignore[reportOptionalCall]


@pytest.mark.integration
class TestServiceHealth:
    @respx.mock
    async def test_up_when_sources_match_and_nothing_is_wrong(self) -> None:
        _prometheus(_healthy_jellyfin())
        _grafana([])
        _loki([])

        lines = (await _run("jellyfin")).splitlines()

        assert lines[0] == "UP: jellyfin over 1h: matched by Gatus, Containers, Blackbox probes"
        assert lines[1].startswith("Gatus: ok: Jellyfin (media_jellyfin)")
        assert lines[2].startswith("Containers: ok: media vm/jellyfin running")
        assert lines[3].startswith("Blackbox probes: ok: https://jellyfin.example.com")
        assert lines[4] == "Grafana alerts: no match"
        assert lines[5].startswith("Loki errors: no match")

    @respx.mock
    async def test_verdict_down_when_gatus_failing(self) -> None:
        replies = _healthy_jellyfin()
        gatus = _gatus("Jellyfin", "media_jellyfin", group="media")
        replies[GATUS_NOW] = [(gatus, 0.0)]
        replies["gatus_ratio"] = [(gatus, 0.5)]
        _prometheus(replies)
        _grafana([])
        _loki([])

        first = (await _run("jellyfin")).splitlines()[0]

        assert first == "DOWN: jellyfin over 1h: Gatus endpoint Jellyfin is failing now"

    @respx.mock
    async def test_verdict_down_when_container_not_running(self) -> None:
        replies = _healthy_jellyfin()
        replies["state"] = [(_exporter("media vm", "jellyfin"), 0.0)]
        _prometheus(replies)
        _grafana([])
        _loki([])

        first = (await _run("jellyfin")).splitlines()[0]

        assert first == "DOWN: jellyfin over 1h: container media vm/jellyfin is exited"

    @respx.mock
    async def test_verdict_degraded_on_restart(self) -> None:
        replies = _healthy_jellyfin()
        replies["restarts"] = [(_exporter("media vm", "jellyfin"), 3.0)]
        replies["restarts_24h"] = [(_exporter("media vm", "jellyfin"), 1.0)]
        _prometheus(replies)
        _grafana([])
        _loki([])

        first = (await _run("jellyfin")).splitlines()[0]

        assert first == "DEGRADED: jellyfin over 1h: container media vm/jellyfin restarted 1 time in 1h"

    @respx.mock
    async def test_degraded_on_alert_errors_and_ratio(self) -> None:
        replies = _healthy_jellyfin()
        replies["gatus_ratio"] = [(_gatus("Jellyfin", "media_jellyfin", group="media"), 0.95)]
        _prometheus(replies)
        _grafana(
            [
                {
                    "labels": {"grafana_folder": "Containers"},
                    "alerts": [
                        {
                            "labels": {"alertname": "ContainerUnhealthy", "container_name": "jellyfin"},
                            "annotations": {"summary": "jellyfin is unhealthy"},
                            "status": {"state": "active"},
                        },
                        {
                            "labels": {"alertname": "JellyfinSilenced"},
                            "status": {"state": "suppressed"},
                        },
                    ],
                }
            ]
        )
        _loki([({"service_name": "jellyfin"}, 7.0)])

        first = (await _run("jellyfin", window="24h")).splitlines()[0]

        assert first == (
            "DEGRADED: jellyfin over 24h: Gatus endpoint Jellyfin succeeded 95.0% of checks over 24h; "
            "Grafana alert ContainerUnhealthy is active; 7 error log lines in Loki over 24h"
        )

    @respx.mock
    async def test_unknown_when_no_source_matches(self) -> None:
        _prometheus(_healthy_jellyfin())
        _grafana([])
        _loki([])

        lines = (await _run("nextcloud")).splitlines()

        assert lines[0] == "UNKNOWN: nextcloud over 1h: no source matched"
        assert lines[1:5] == [
            "Gatus: no match",
            "Containers: no match",
            "Blackbox probes: no match",
            "Grafana alerts: no match",
        ]
        assert lines[5].startswith("Loki errors: no match")

    @respx.mock
    async def test_failed_source_reported_not_dropped(self) -> None:
        replies = _healthy_jellyfin()
        replies["restarts_24h"] = [(_exporter("media vm", "jellyfin"), 2.0)]
        _prometheus(replies)
        _grafana([])
        _loki(httpx.Response(500, text="too many outstanding requests"))

        lines = (await _run("jellyfin")).splitlines()

        assert lines[0] == "DEGRADED: jellyfin over 1h: container media vm/jellyfin restarted 2 times in 1h"
        assert lines[5] == "Loki errors: unavailable: HTTP 500 - too many outstanding requests"

    @respx.mock
    async def test_every_source_failing_is_unknown_naming_them(self) -> None:
        _prometheus({key: httpx.Response(503, text="down") for key in [GATUS_NOW, "gatus_ratio", PROBES]})
        _grafana(httpx.Response(500, text="grafana broke"))
        _loki(httpx.ConnectError("refused"))

        lines = (await _run("jellyfin")).splitlines()

        assert lines[0] == (
            "UNKNOWN: jellyfin over 1h: no source matched; "
            "unavailable: Gatus, Blackbox probes, Grafana alerts, Loki errors"
        )
        assert lines[1] == "Gatus: unavailable: HTTP 503 - down"
        assert lines[3] == "Blackbox probes: unavailable: HTTP 503 - down"
        assert lines[4].startswith("Grafana alerts: unavailable: Grafana API error: HTTP 500")
        assert lines[5].startswith("Loki errors: unavailable: cannot connect")

    @respx.mock
    async def test_container_query_failure_makes_containers_unavailable(self) -> None:
        replies = _healthy_jellyfin()
        replies["state"] = httpx.Response(500, text="boom")
        _prometheus(replies)
        _grafana([])
        _loki([])

        lines = (await _run("jellyfin")).splitlines()

        assert lines[2] == "Containers: unavailable: container_state: HTTP 500 - boom"
        assert lines[0].startswith("UP: ")

    @respx.mock
    async def test_name_regex_escaped(self) -> None:
        _prometheus({})
        _grafana([])
        route = _loki([])

        await _run("a.b(c)")

        sent = route.calls.last.request.url.params["query"]
        assert 'service_name=~"(?i).*a\\\\.b\\\\(c\\\\).*"' in sent
        assert "a.b(c)" not in sent

    @respx.mock
    async def test_window_sets_ratio_and_loki_ranges(self) -> None:
        prom = _prometheus({})
        _grafana([])
        loki = _loki([])

        await _run("jellyfin", window="1d")

        sent = {call.request.url.params["query"] for call in prom.calls}
        assert f"avg_over_time({GATUS_NOW}[86400s])" in sent
        assert {GATUS_NOW, PROBES} <= sent
        assert loki.calls.last.request.url.params["query"].endswith("[86400s]))")

    async def test_loki_not_configured_is_unavailable(self, mock_settings: Any) -> None:
        mock_settings.loki_url = ""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(PROM_QUERY_URL).mock(side_effect=lambda _request: _vector([]))
            mock.get(GRAFANA_ALERTS_URL).mock(return_value=httpx.Response(200, json=[]))
            loki = mock.get(LOKI_QUERY_URL)

            lines = (await _run("jellyfin")).splitlines()

        assert lines[5] == "Loki errors: unavailable: LOKI_URL not set"
        assert not loki.called

    async def test_window_out_of_range_refused(self) -> None:
        with pytest.raises(ToolException, match="5m to 7d"):
            await _run("jellyfin", window="30d")

    @respx.mock
    async def test_unexpected_exception_propagates(self) -> None:
        _prometheus({PROBES: RuntimeError("bug")})
        _grafana([])
        _loki([])

        with pytest.raises(RuntimeError, match="bug"):
            await _run("jellyfin")

    @respx.mock
    async def test_loki_error_status_is_unavailable(self) -> None:
        _prometheus({})
        _grafana([])
        _loki(httpx.Response(200, json={"status": "error", "error": "parse error"}))

        lines = (await _run("jellyfin")).splitlines()

        assert lines[5] == "Loki errors: unavailable: Loki error: parse error"

    @respx.mock
    async def test_odd_responses_are_unavailable_with_reasons(self) -> None:
        _prometheus(
            {
                GATUS_NOW: httpx.Response(200, json=["not", "a", "dict"]),
                PROBES: httpx.Response(200, json={"status": "success", "data": {"result": [{"metric": {}}]}}),
            }
        )
        _grafana(httpx.Response(200, json={"not": "a list"}))
        _loki(httpx.Response(200, text="<html>proxy error</html>"))

        lines = (await _run("jellyfin")).splitlines()

        assert lines[1] == "Gatus: unavailable: Prometheus returned an unexpected response"
        assert lines[3] == "Blackbox probes: unavailable: Prometheus returned an unexpected instant-vector shape"
        assert lines[4] == "Grafana alerts: unavailable: Grafana alerts API returned dict, expected a list"
        assert lines[5] == "Loki errors: unavailable: response body is not JSON"

    @respx.mock
    async def test_timeouts_and_transport_errors_are_unavailable(self) -> None:
        _prometheus({PROBES: httpx.ReadTimeout("slow")})
        _grafana([])
        _loki(httpx.RemoteProtocolError("peer closed"))

        lines = (await _run("jellyfin")).splitlines()

        assert lines[3] == "Blackbox probes: unavailable: request timed out"
        assert lines[5] == "Loki errors: unavailable: request failed (RemoteProtocolError: peer closed)"

    async def test_blank_name_refused(self) -> None:
        with pytest.raises(ToolException, match="name is blank"):
            await _run("   ")

    @respx.mock
    async def test_restart_window_follows_window(self) -> None:
        replies = _healthy_jellyfin()
        replies["restarts_24h"] = [(_exporter("media vm", "jellyfin"), 1.0)]
        prom = _prometheus(replies)
        _grafana([])
        _loki([])

        first = (await _run("jellyfin", window="5m")).splitlines()[0]

        sent = {call.request.url.params["query"] for call in prom.calls}
        assert 'increase(container_restart_count{job="container-status"}[300s])' in sent
        assert first == "DEGRADED: jellyfin over 5m: container media vm/jellyfin restarted 1 time in 5m"

    @respx.mock
    async def test_degraded_when_probe_failing(self) -> None:
        replies = _healthy_jellyfin()
        replies[PROBES] = [({"instance": "https://jellyfin.example.com", "job": "blackbox_public"}, 0.0)]
        _prometheus(replies)
        _grafana([])
        _loki([])

        first = (await _run("jellyfin")).splitlines()[0]

        assert first == "DEGRADED: jellyfin over 1h: probe https://jellyfin.example.com failing now"

    @respx.mock
    async def test_partial_container_data_still_decides(self) -> None:
        replies = _healthy_jellyfin()
        replies["state"] = [(_exporter("media vm", "jellyfin"), 0.0)]
        replies["image_info"] = httpx.Response(500, text="boom")
        _prometheus(replies)
        _grafana([])
        _loki([])

        lines = (await _run("jellyfin")).splitlines()

        assert lines[0] == "DOWN: jellyfin over 1h: container media vm/jellyfin is exited"
        assert lines[2].startswith("Containers: ok: media vm/jellyfin exited")
        assert lines[2].endswith("; container_image_info unavailable: HTTP 500 - boom")
