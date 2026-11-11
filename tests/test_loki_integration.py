"""Integration tests for Loki tools with mocked HTTP responses."""

import json
import time
from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import ToolException
from pydantic import ValidationError

from src.agent.tools.loki import (
    loki_correlate_changes,
    loki_list_label_values,
    loki_metric_query,
    loki_query_logs,
)


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


# --- loki_query_logs ---


@pytest.mark.integration
class TestLokiQueryLogs:
    @respx.mock
    async def test_successful_query(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {
                                    "hostname": "media",
                                    "service_name": "traefik",
                                    "detected_level": "info",
                                },
                                "values": [
                                    ["1700000000000000000", "GET /api/health 200 OK"],
                                    ["1700000001000000000", "GET /dashboard 200 OK"],
                                ],
                            }
                        ],
                    },
                },
            )
        )

        result = await loki_query_logs.ainvoke({"query": '{hostname="media"}'})
        assert "Found 2 log lines" in result
        assert "media" in result
        assert "traefik" in result
        assert "GET /api/health" in result

    @respx.mock
    async def test_empty_result(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        result = await loki_query_logs.ainvoke({"query": '{hostname="nonexistent"}', "start": "now-2h"})
        assert "No log lines found in the last 2h." in result
        assert "valid selector" in result

    @respx.mock
    async def test_loki_unreachable(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await loki_query_logs.ainvoke({"query": '{hostname="media"}'})
        assert "Cannot connect" in result

    @respx.mock
    async def test_loki_timeout(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await loki_query_logs.ainvoke({"query": '{hostname="media"}'})
        assert "timed out" in result

    @respx.mock
    async def test_loki_http_error(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(400, text="parse error: invalid query")
        )

        result = await loki_query_logs.ainvoke({"query": '{hostname="media"'})
        assert "400" in result

    @respx.mock
    async def test_sends_correct_params(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        await loki_query_logs.ainvoke(
            {
                "query": '{hostname="media"}',
                "limit": 50,
                "direction": "forward",
            }
        )
        assert route.called
        params = route.calls.last.request.url.params
        assert params["query"] == '{hostname="media"}'
        assert params["limit"] == "50"
        assert params["direction"] == "forward"

    async def test_invalid_direction(self) -> None:
        result = await loki_query_logs.ainvoke(
            {
                "query": '{hostname="media"}',
                "direction": "invalid",
            }
        )
        assert "Invalid direction" in result

    @respx.mock
    async def test_with_iso_timestamps(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        await loki_query_logs.ainvoke(
            {
                "query": '{hostname="media"}',
                "start": "2024-06-15T13:00:00Z",
                "end": "2024-06-15T14:00:00Z",
            }
        )
        assert route.called

    async def test_end_before_start(self) -> None:
        result = await loki_query_logs.ainvoke(
            {
                "query": '{hostname="media"}',
                "start": "2024-06-15T14:00:00Z",
                "end": "2024-06-15T13:00:00Z",
            }
        )
        assert "End time must be after start time" in result

    @respx.mock
    async def test_multiple_streams(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {"hostname": "media", "service_name": "traefik"},
                                "values": [["1700000000000000000", "traefik log"]],
                            },
                            {
                                "stream": {"hostname": "media", "service_name": "jellyfin"},
                                "values": [["1700000001000000000", "jellyfin log"]],
                            },
                        ],
                    },
                },
            )
        )

        result = await loki_query_logs.ainvoke({"query": '{hostname="media"}'})
        assert "Found 2 log lines" in result
        assert "traefik log" in result
        assert "jellyfin log" in result


# --- loki_list_label_values ---


@pytest.mark.integration
class TestLokiListLabelValues:
    @respx.mock
    @pytest.mark.parametrize("label", ["../../x", "a/b", "a b", "1abc", "a-b", "a?x=1", "%2e%2e"])
    async def test_label_path_traversal_rejected(self, label: str) -> None:
        trap = respx.route().mock(return_value=httpx.Response(200, json={"status": "success", "data": []}))
        with pytest.raises(ToolException):
            await loki_list_label_values.coroutine(label=label)  # type: ignore[misc]
        assert not trap.called

    @respx.mock
    async def test_label_pattern_rejected_via_schema(self) -> None:
        trap = respx.route().mock(return_value=httpx.Response(200, json={"status": "success", "data": []}))
        with pytest.raises(ValidationError, match="string_pattern_mismatch"):
            await loki_list_label_values.ainvoke({"label": "../../x"})
        assert not trap.called

    @respx.mock
    async def test_label_values_capped_at_200(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/label/container/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": [f"c{i:04d}" for i in range(500)]})
        )
        result = await loki_list_label_values.ainvoke({"label": "container"})
        assert "showing 200 of 500" in result

    @respx.mock
    async def test_successful_query(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/label/hostname/values").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": ["infra", "media", "jellyfin", "proxmox"],
                },
            )
        )

        result = await loki_list_label_values.ainvoke({"label": "hostname"})
        assert "Found 4 values" in result
        assert "infra" in result
        assert "media" in result
        assert "jellyfin" in result
        assert "proxmox" in result

    @respx.mock
    async def test_empty_result(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/label/nonexistent/values").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": []},
            )
        )

        result = await loki_list_label_values.ainvoke({"label": "nonexistent"})
        assert "No values found" in result

    @respx.mock
    async def test_with_query_filter(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/label/service_name/values").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": ["traefik", "adguard"]},
            )
        )

        result = await loki_list_label_values.ainvoke(
            {
                "label": "service_name",
                "query": '{hostname="infra"}',
            }
        )
        assert route.called
        assert route.calls.last.request.url.params["query"] == '{hostname="infra"}'
        assert "Found 2 values" in result

    @respx.mock
    async def test_default_window_is_last_6h(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/label/container/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": ["portainer", "sabnzbd"]})
        )

        result = await loki_list_label_values.ainvoke({"label": "container"})
        params = route.calls.last.request.url.params
        span_ns = int(params["end"]) - int(params["start"])
        assert abs(span_ns - 6 * 3600 * 1_000_000_000) < 60 * 1_000_000_000
        assert abs(int(params["end"]) - time.time_ns()) < 60 * 1_000_000_000
        assert "Found 2 values for label 'container' in the last 6h:" in result

    @respx.mock
    async def test_start_widens_the_window(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/label/container/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": ["portainer"]})
        )

        result = await loki_list_label_values.ainvoke({"label": "container", "start": "7d"})
        params = route.calls.last.request.url.params
        span_ns = int(params["end"]) - int(params["start"])
        assert abs(span_ns - 7 * 86400 * 1_000_000_000) < 60 * 1_000_000_000
        assert "in the last 7d:" in result

    @respx.mock
    async def test_absolute_range_reaches_loki_as_nanoseconds(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/label/container/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )

        result = await loki_list_label_values.ainvoke(
            {"label": "container", "start": "2026-10-24T00:00:00Z", "end": "2026-10-31T00:00:00Z"}
        )
        params = route.calls.last.request.url.params
        assert params["start"] == "1792800000000000000"
        assert params["end"] == "1793404800000000000"
        assert "from 2026-10-24T00:00:00Z to 2026-10-31T00:00:00Z" in result
        assert "No values found" in result

    @respx.mock
    async def test_end_before_start_rejected(self) -> None:
        result = await loki_list_label_values.ainvoke({"label": "container", "start": "1h", "end": "2h"})
        assert "End time must be after start time" in result

    @respx.mock
    async def test_unparseable_start_rejected(self) -> None:
        result = await loki_list_label_values.ainvoke({"label": "container", "start": "last week"})
        assert "Invalid time 'last week'" in result

    @respx.mock
    async def test_loki_unreachable(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/label/hostname/values").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await loki_list_label_values.ainvoke({"label": "hostname"})
        assert "Cannot connect" in result

    @respx.mock
    async def test_loki_timeout(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/label/hostname/values").mock(
            side_effect=httpx.ReadTimeout("Read timed out")
        )

        result = await loki_list_label_values.ainvoke({"label": "hostname"})
        assert "timed out" in result


# --- loki_correlate_changes ---

LOKI = "http://loki.test:3100"
REF = "2024-06-15T14:00:00Z"
REF_NS = "1718460000000000000"  # 2024-06-15 14:00:00Z
EMPTY_STREAMS: dict[str, Any] = {"status": "success", "data": {"resultType": "streams", "result": []}}
Reply = httpx.Response | Exception


def _streams(*streams: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": list(streams)}})


def _empty() -> httpx.Response:
    return httpx.Response(200, json=EMPTY_STREAMS)


def _count(n: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1718460000, str(n)]}]},
        },
    )


def _error_stream(*lines: tuple[str, str], hostname: str = "infra", service: str = "traefik") -> dict[str, Any]:
    return {
        "stream": {"hostname": hostname, "service_name": service, "detected_level": "error"},
        "values": [[ts, text] for ts, text in lines],
    }


def _docker_event_line(action: str, name: str, at_ns: int, **attributes: str) -> str:
    return json.dumps(
        {
            "Type": "container",
            "Action": action,
            "Actor": {"ID": "abc", "Attributes": {"name": name, **attributes}},
            "timeNano": at_ns,
        }
    )


def _route_query_range(errors: Reply, lifecycle: Reply, other: Reply | None = None) -> respx.Route:
    """One query_range route that answers by query: error selector, docker-events stream, or anything else."""

    def answer(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "detected_level" in query:
            reply = errors
        elif 'service_name="docker-events"' in query:
            reply = lifecycle
        else:
            reply = other if other is not None else _empty()
        if isinstance(reply, Exception):
            raise reply
        return reply

    return respx.get(f"{LOKI}/loki/api/v1/query_range").mock(side_effect=answer)


def _sent_queries(route: respx.Route) -> list[dict[str, str]]:
    return [dict(call.request.url.params) for call in route.calls]


def _error_params(route: respx.Route) -> dict[str, str]:
    return next(p for p in _sent_queries(route) if "detected_level" in p["query"])


def _lifecycle_params(route: respx.Route) -> dict[str, str]:
    return next(p for p in _sent_queries(route) if 'service_name="docker-events"' in p["query"])


@pytest.mark.integration
class TestLokiCorrelateChanges:
    @respx.mock
    async def test_successful_correlation(self) -> None:
        _route_query_range(
            errors=_streams(_error_stream(("1718459700000000000", "502 Bad Gateway for backend"))),
            lifecycle=_streams(
                {
                    "stream": {"hostname": "infra", "service_name": "docker-events"},
                    "values": [["1718459400000000000", _docker_event_line("start", "adguard", 1718459400000000000)]],
                }
            ),
        )
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(1))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF, "window_minutes": 30})

        assert "Time window: 2024-06-15 13:30:00Z to 2024-06-15 14:00:00Z (30 minutes)" in result
        assert "Found 2 significant events across 2 services" in result
        assert "[2024-06-15 13:50:00Z] [LIFECYCLE] infra/adguard: start" in result
        assert "[2024-06-15 13:55:00Z] [ERROR] infra/traefik: 502 Bad Gateway for backend" in result
        # Timeline prints oldest first
        assert result.index("infra/adguard: start") < result.index("infra/traefik: 502")
        assert "WARNING" not in result

    @respx.mock
    async def test_correlation_requests_newest_first(self) -> None:
        route = _route_query_range(errors=_empty(), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        await loki_correlate_changes.ainvoke({"reference_time": REF})

        sent = _sent_queries(route)
        assert len(sent) == 2
        assert [p["direction"] for p in sent] == ["backward", "backward"]
        assert _error_params(route)["limit"] == "200"
        assert _lifecycle_params(route)["limit"] == "100"

    @respx.mock
    async def test_correlation_reports_n_of_m_when_capped(self) -> None:
        _route_query_range(
            errors=_streams(
                _error_stream(
                    ("1718459940000000000", "newest"),
                    ("1718459880000000000", "middle"),
                    ("1718459820000000000", "older"),
                )
            ),
            lifecycle=_empty(),
        )
        count_route = respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(775))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF, "window_minutes": 360})

        assert "showing newest 3 of 775 error/warn lines" in result
        params = dict(count_route.calls.last.request.url.params)
        assert params["time"] == REF_NS
        assert params["query"].startswith("sum(count_over_time({")
        assert params["query"].endswith("}[21600s]))")
        assert 'detected_level=~"error|err|fatal|critical|crit|warn|warning"' in params["query"]

    @respx.mock
    async def test_no_n_of_m_when_all_lines_returned(self) -> None:
        _route_query_range(errors=_streams(_error_stream(("1718459940000000000", "only one"))), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(1))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "Error/warn lines: 1 in the window" in result
        assert "showing newest" not in result

    @respx.mock
    async def test_count_failure_leaves_out_m_and_says_why(self) -> None:
        _route_query_range(errors=_streams(_error_stream(("1718459940000000000", "boom"))), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(
            return_value=httpx.Response(500, text="too many outstanding requests")
        )

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "Error/warn lines: 1, newest first (limit 200); the total count failed: HTTP 500" in result
        assert "too many outstanding requests" in result
        assert " of " not in result.split("Error/warn lines:")[1].splitlines()[0]
        # A failed count is not a failed sub-query
        assert "WARNING" not in result
        assert "infra/traefik: boom" in result

    @respx.mock
    async def test_error_selector_includes_warning_and_err(self) -> None:
        route = _route_query_range(errors=_empty(), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        await loki_correlate_changes.ainvoke({"reference_time": REF})

        query = _error_params(route)["query"]
        assert query == '{detected_level=~"error|err|fatal|critical|crit|warn|warning", service_name!="docker-events"}'

    @respx.mock
    async def test_lifecycle_from_docker_events_not_keyword_grep(self) -> None:
        wud_cron = {
            "stream": {"hostname": "infra", "service_name": "wud", "detected_level": "info"},
            "values": [["1718459000000000000", "Cron started (0 * * * *)"]],
        }
        route = _route_query_range(
            errors=_empty(),
            lifecycle=_streams(
                {
                    "stream": {"hostname": "media", "service_name": "docker-events"},
                    "values": [
                        [
                            "1718459100000000000",
                            _docker_event_line("die", "jellyfin", 1718459100000000000, exitCode="137"),
                        ],
                        [
                            "1718459000000000000",
                            _docker_event_line("kill", "jellyfin", 1718459000000000000, signal="9"),
                        ],
                    ],
                }
            ),
            other=_streams(wud_cron),
        )
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "Cron started" not in result
        assert "media/jellyfin: kill signal=9" in result
        assert "media/jellyfin: die exitCode=137" in result
        lifecycle_query = _lifecycle_params(route)["query"]
        assert lifecycle_query.startswith('{service_name="docker-events"}')
        assert lifecycle_query.endswith(
            ' | json Type="Type", Action="Action" | Type="container"'
            + ' | Action=~"start|stop|restart|die|kill|oom|health_status: unhealthy"'
        )
        assert "Container lifecycle events: 2 from docker-events" in result

    @respx.mock
    async def test_subquery_failure_is_visible(self) -> None:
        _route_query_range(
            errors=_streams(_error_stream(("1718459940000000000", "502 Bad Gateway"))),
            lifecycle=httpx.Response(500, text="lifecycle broke"),
        )
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(1))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "WARNING: lifecycle query failed: HTTP 500 - lifecycle broke; the timeline is incomplete" in result
        assert "infra/traefik: 502 Bad Gateway" in result

    @respx.mock
    async def test_error_subquery_failure_is_visible(self) -> None:
        _route_query_range(
            errors=httpx.TimeoutException("read timed out"),
            lifecycle=_streams(
                {
                    "stream": {"hostname": "media", "service_name": "docker-events"},
                    "values": [["1718459100000000000", _docker_event_line("oom", "sonarr", 1718459100000000000)]],
                }
            ),
        )
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "WARNING: error/warn query failed: Loki query timed out after 15s" in result
        assert "the timeline is incomplete" in result
        assert "media/sonarr: oom" in result
        # No "N error/warn lines" claim when that query failed
        assert "Error/warn lines:" not in result

    @respx.mock
    async def test_loki_error_status_counts_as_failure(self) -> None:
        _route_query_range(errors=httpx.Response(200, json={"status": "error"}), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "WARNING: error/warn query failed: Loki returned status 'error'" in result

    @respx.mock
    async def test_both_subqueries_fail_raises(self) -> None:
        respx.get(f"{LOKI}/loki/api/v1/query_range").mock(side_effect=httpx.ConnectError("Connection refused"))
        respx.get(f"{LOKI}/loki/api/v1/query").mock(side_effect=httpx.ConnectError("Connection refused"))
        ready = respx.get(f"{LOKI}/ready").mock(return_value=httpx.Response(200, text="ready"))

        with pytest.raises(ToolException) as excinfo:
            await loki_correlate_changes.coroutine(reference_time=REF)  # type: ignore[misc]  # pyright: ignore

        message = str(excinfo.value)
        assert "error/warn query failed: Cannot connect to Loki at http://loki.test:3100" in message
        assert "lifecycle query failed: Cannot connect to Loki" in message
        assert not ready.called

    @respx.mock
    async def test_no_events_found(self) -> None:
        """Both queries answer with nothing: an empty timeline, not an error, and no /ready probe."""
        _route_query_range(errors=_empty(), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))
        ready = respx.get(f"{LOKI}/ready").mock(return_value=httpx.Response(200, text="ready"))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert "No significant events" in result
        assert "Error/warn lines: 0 in the window" in result
        assert "WARNING" not in result
        assert not ready.called

    async def test_invalid_reference_time(self) -> None:
        result = await loki_correlate_changes.ainvoke({"reference_time": "not-a-time"})
        assert "Invalid time 'not-a-time'" in result

    @respx.mock
    async def test_correlation_with_filters(self) -> None:
        route = _route_query_range(errors=_empty(), lifecycle=_empty())
        count_route = respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        result = await loki_correlate_changes.ainvoke(
            {"reference_time": REF, "hostname": "infra", "service_name": "traefik"}
        )

        assert "Filters: hostname=infra, service_name=traefik" in result
        error_query = _error_params(route)["query"]
        assert error_query == (
            '{hostname="infra", service_name="traefik", detected_level=~"error|err|fatal|critical|crit|warn|warning"}'
        )
        assert count_route.calls.last.request.url.params["query"] == f"sum(count_over_time({error_query}[1800s]))"
        lifecycle_query = _lifecycle_params(route)["query"]
        assert lifecycle_query.startswith('{service_name="docker-events", hostname="infra"} |= "traefik" | json')

    @respx.mock
    async def test_hostname_with_quote_is_escaped(self) -> None:
        route = _route_query_range(errors=_empty(), lifecycle=_empty())
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        await loki_correlate_changes.ainvoke(
            {"reference_time": REF, "hostname": 'media"} or {x="', "service_name": "svc\\"}
        )

        error_query = _error_params(route)["query"]
        assert error_query.startswith('{hostname="media\\"} or {x=\\"", service_name="svc\\\\", ')
        lifecycle_query = _lifecycle_params(route)["query"]
        assert lifecycle_query.startswith(
            '{service_name="docker-events", hostname="media\\"} or {x=\\""} |= "svc\\\\" | json'
        )

    @respx.mock
    async def test_service_filter_drops_other_containers_on_the_stream(self) -> None:
        _route_query_range(
            errors=_empty(),
            lifecycle=_streams(
                {
                    "stream": {"hostname": "media", "service_name": "docker-events"},
                    "values": [
                        ["1718459100000000000", _docker_event_line("start", "jellyfin", 1718459100000000000)],
                        ["1718459000000000000", _docker_event_line("start", "jellyfin-vue", 1718459000000000000)],
                        ["1718458900000000000", "not json"],
                    ],
                }
            ),
        )
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF, "service_name": "jellyfin"})

        assert "media/jellyfin: start" in result
        assert "jellyfin-vue" not in result
        assert "1 docker-events line(s) were not docker events JSON and were skipped" in result

    @respx.mock
    async def test_lifecycle_limit_reached_is_named(self) -> None:
        values = [
            [str(1718459000000000000 + i), _docker_event_line("restart", "flappy", 1718459000000000000 + i)]
            for i in range(100)
        ]
        _route_query_range(
            errors=_empty(),
            lifecycle=_streams({"stream": {"hostname": "media", "service_name": "docker-events"}, "values": values}),
        )
        respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=_count(0))

        result = await loki_correlate_changes.ainvoke({"reference_time": REF})

        assert (
            "Container lifecycle events: 100 from docker-events (newest 100 lines read; older ones not shown)" in result
        )


# --- loki_metric_query ---


@pytest.mark.integration
class TestLokiMetricQuery:
    @respx.mock
    async def test_instant_query_vector(self) -> None:
        """Instant query (no step) should hit /loki/api/v1/query and return vector results."""
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {"hostname": "media"},
                                "value": ["1700000000", "45231"],
                            },
                            {
                                "metric": {"hostname": "infra"},
                                "value": ["1700000000", "12045"],
                            },
                        ],
                    },
                },
            )
        )

        result = await loki_metric_query.ainvoke(
            {"query": 'topk(5, sum by (hostname) (count_over_time({hostname=~".+"}[24h])))'}
        )
        assert "Found 2 series" in result
        assert "media" in result
        assert "infra" in result
        # media has higher value, should appear first
        media_pos = result.index("media")
        infra_pos = result.index("infra")
        assert media_pos < infra_pos

    @respx.mock
    async def test_instant_query_sends_correct_params(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": []}},
            )
        )

        await loki_metric_query.ainvoke({"query": 'sum(count_over_time({hostname="media"}[1h]))'})
        assert route.called
        params = route.calls.last.request.url.params
        assert params["query"] == 'sum(count_over_time({hostname="media"}[1h]))'
        # Should not have start/end/step for instant query
        assert "start" not in params
        assert "end" not in params
        assert "step" not in params

    @respx.mock
    async def test_range_query_matrix(self) -> None:
        """Range query (step provided) should hit /loki/api/v1/query_range and return matrix."""
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {"hostname": "media"},
                                "values": [
                                    ["1700000000", "1000"],
                                    ["1700003600", "1500"],
                                    ["1700007200", "2000"],
                                ],
                            }
                        ],
                    },
                },
            )
        )

        result = await loki_metric_query.ainvoke(
            {
                "query": 'sum by (hostname) (count_over_time({hostname="media"}[1h]))',
                "start": "6h",
                "end": "now",
                "step": "1h",
            }
        )
        assert "Found 1 series" in result
        assert "3 data points" in result
        assert "media" in result

    @respx.mock
    async def test_range_query_sends_correct_params(self) -> None:
        route = respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "matrix", "result": []}},
            )
        )

        await loki_metric_query.ainvoke(
            {
                "query": 'sum(count_over_time({hostname="media"}[1h]))',
                "start": "6h",
                "end": "now",
                "step": "1h",
            }
        )
        assert route.called
        params = route.calls.last.request.url.params
        assert params["query"] == 'sum(count_over_time({hostname="media"}[1h]))'
        assert params["step"] == "1h"
        assert "start" in params
        assert "end" in params

    @respx.mock
    async def test_empty_vector_result(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": []}},
            )
        )

        result = await loki_metric_query.ainvoke({"query": 'sum(count_over_time({hostname="nonexistent"}[1h]))'})
        assert "no results" in result.lower()

    @respx.mock
    async def test_loki_unreachable(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await loki_metric_query.ainvoke({"query": 'sum(count_over_time({hostname="media"}[1h]))'})
        assert "Cannot connect" in result

    @respx.mock
    async def test_loki_timeout(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await loki_metric_query.ainvoke({"query": 'sum(count_over_time({hostname="media"}[1h]))'})
        assert "timed out" in result

    @respx.mock
    async def test_loki_http_error(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(400, text="parse error: invalid LogQL")
        )

        result = await loki_metric_query.ainvoke({"query": "invalid query"})
        assert "400" in result

    async def test_range_query_end_before_start(self) -> None:
        result = await loki_metric_query.ainvoke(
            {
                "query": 'sum(count_over_time({hostname="media"}[1h]))',
                "start": "2024-06-15T14:00:00Z",
                "end": "2024-06-15T13:00:00Z",
                "step": "5m",
            }
        )
        assert "End time must be after start time" in result

    @respx.mock
    async def test_vector_with_many_labels(self) -> None:
        """Verify formatting works with complex label sets."""
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {
                                    "hostname": "media",
                                    "service_name": "traefik",
                                    "detected_level": "error",
                                },
                                "value": ["1700000000", "500"],
                            }
                        ],
                    },
                },
            )
        )

        result = await loki_metric_query.ainvoke(
            {"query": 'sum by (hostname, service_name, detected_level) (count_over_time({detected_level="error"}[1h]))'}
        )
        assert "media" in result
        assert "traefik" in result
        assert "error" in result
        assert "500" in result
