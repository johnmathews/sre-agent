"""Integration tests for Prometheus tools with mocked HTTP responses."""

import logging
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from src.agent.tools.prometheus import (
    PrometheusResponse,
    _get_baseline_enrichment,
    prometheus_instant_query,
    prometheus_range_query,
    prometheus_search_metrics,
)

_EMPTY_MATRIX = {"status": "success", "data": {"resultType": "matrix", "result": []}}
_EMPTY_VECTOR = {"status": "success", "data": {"resultType": "vector", "result": []}}


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


@pytest.mark.integration
class TestPrometheusInstantQuery:
    @respx.mock
    async def test_successful_query(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {"__name__": "up", "hostname": "jellyfin", "job": "node_exporter"},
                                "value": [1700000000, "1"],
                            }
                        ],
                    },
                },
            )
        )

        result = await prometheus_instant_query.ainvoke({"query": 'up{hostname="jellyfin"}'})
        assert "jellyfin" in result
        assert "1" in result

    @respx.mock
    async def test_empty_result(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": []}},
            )
        )

        result = await prometheus_instant_query.ainvoke({"query": 'up{hostname="nonexistent"}'})
        assert "no results" in result.lower()

    @respx.mock
    async def test_prometheus_unreachable(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await prometheus_instant_query.ainvoke({"query": "up"})
        assert "Cannot connect" in result

    @respx.mock
    async def test_prometheus_timeout(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await prometheus_instant_query.ainvoke({"query": "up"})
        assert "timed out" in result

    @respx.mock
    async def test_prometheus_http_error(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(422, text="invalid expression")
        )

        result = await prometheus_instant_query.ainvoke({"query": "invalid{{"})
        assert "422" in result

    @respx.mock
    async def test_query_with_optional_time(self) -> None:
        route = respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": []}},
            )
        )

        await prometheus_instant_query.ainvoke({"query": "up", "time": "1700000000"})
        assert route.called
        assert route.calls.last.request.url.params["time"] == "1700000000"

    @respx.mock
    async def test_relative_time_sent_as_unix_seconds(self) -> None:
        route = respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json=_EMPTY_VECTOR)
        )

        before = time.time()
        await prometheus_instant_query.ainvoke({"query": "up", "time": "now-1h"})
        sent = float(route.calls.last.request.url.params["time"])
        assert before - 3600 - 1 <= sent <= time.time() - 3600 + 1

    @respx.mock
    async def test_invalid_time_lists_accepted_forms(self) -> None:
        result = await prometheus_instant_query.ainvoke({"query": "up", "time": "yesterday"})
        assert "now-24h" in result
        assert "RFC3339" in result


@pytest.mark.integration
class TestPrometheusRangeQuery:
    @respx.mock
    async def test_successful_range_query(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {"__name__": "node_cpu_seconds_total", "hostname": "jellyfin"},
                                "values": [
                                    [1700000000, "100"],
                                    [1700000060, "105"],
                                    [1700000120, "110"],
                                ],
                            }
                        ],
                    },
                },
            )
        )

        result = await prometheus_range_query.ainvoke(
            {
                "query": 'node_cpu_seconds_total{hostname="jellyfin"}',
                "start": "1700000000",
                "end": "1700003600",
                "step": "60s",
            }
        )
        assert "jellyfin" in result
        assert "3 samples" in result

    @respx.mock
    async def test_range_query_sends_correct_params(self) -> None:
        route = respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "matrix", "result": []}},
            )
        )

        await prometheus_range_query.ainvoke(
            {
                "query": "up",
                "start": "1700000000",
                "end": "1700003600",
                "step": "5m",
            }
        )
        params = route.calls.last.request.url.params
        assert params["query"] == "up"
        assert params["start"] == "1700000000"
        assert params["end"] == "1700003600"
        assert params["step"] == "300"

    @respx.mock
    async def test_range_accepts_now_minus_24h(self) -> None:
        """start="24h" used to fail with 'Invalid isoformat string' (F10); Unix seconds go upstream."""
        route = respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            return_value=httpx.Response(200, json=_EMPTY_MATRIX)
        )

        result = await prometheus_range_query.ainvoke({"query": "up", "start": "now-24h"})
        assert "no results" in result.lower()
        params = route.calls.last.request.url.params
        start, end = float(params["start"]), float(params["end"])
        assert end - start == 86400
        assert abs(end - time.time()) < 5
        assert params["step"] == "900"

    @respx.mock
    async def test_defaults_query_the_last_hour(self) -> None:
        route = respx.get("http://prometheus.test:9090/api/v1/query_range").mock(
            return_value=httpx.Response(200, json=_EMPTY_MATRIX)
        )

        await prometheus_range_query.ainvoke({"query": "up"})
        params = route.calls.last.request.url.params
        assert float(params["end"]) - float(params["start"]) == 3600
        assert params["step"] == "15"

    @respx.mock
    async def test_range_91_days_rejected(self) -> None:
        result = await prometheus_range_query.ainvoke({"query": "up", "start": "91d"})
        assert "Invalid range query parameters" in result
        assert "90 days" in result

    async def test_invalid_range_end_before_start(self) -> None:
        result = await prometheus_range_query.ainvoke(
            {
                "query": "up",
                "start": "1700003600",
                "end": "1700000000",
                "step": "60s",
            }
        )
        assert "end must be after start" in result

    async def test_invalid_range_too_many_points(self) -> None:
        result = await prometheus_range_query.ainvoke(
            {
                "query": "up",
                "start": "1700000000",
                "end": "1700604800",
                "step": "1",
            }
        )
        assert "oo many data points" in result
        assert "step='55s'" in result


@pytest.mark.integration
class TestPrometheusSearchMetrics:
    @respx.mock
    async def test_successful_search(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": ["mktxp_dhcp_lease_count", "mktxp_dhcp_lease_info", "mktxp_interface_rx_bytes_total"],
                },
            )
        )
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "mktxp_dhcp_lease_count": [
                            {"type": "gauge", "help": "Number of active DHCP leases", "unit": ""},
                        ],
                        "mktxp_dhcp_lease_info": [
                            {"type": "gauge", "help": "DHCP lease information", "unit": ""},
                        ],
                        "mktxp_interface_rx_bytes_total": [
                            {"type": "counter", "help": "Total received bytes", "unit": ""},
                        ],
                    },
                },
            )
        )

        result = await prometheus_search_metrics.ainvoke({"search_term": "mktxp"})
        assert "Found 3 metrics" in result
        assert "mktxp_dhcp_lease_count (gauge)" in result
        assert "mktxp_dhcp_lease_info (gauge)" in result
        assert "mktxp_interface_rx_bytes_total (counter)" in result

    @respx.mock
    async def test_correct_match_regex_parameter(self) -> None:
        route = respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )

        await prometheus_search_metrics.ainvoke({"search_term": "node_cpu"})
        assert route.called
        match_param = route.calls.last.request.url.params["match[]"]
        assert "node_cpu" in match_param
        assert '=~".*node_cpu.*"' in match_param

    @respx.mock
    async def test_no_matching_metrics(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )

        result = await prometheus_search_metrics.ainvoke({"search_term": "nonexistent_metric"})
        assert "No metrics found" in result

    @respx.mock
    async def test_special_characters_escaped(self) -> None:
        route = respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )

        await prometheus_search_metrics.ainvoke({"search_term": "foo.bar+baz"})
        match_param = route.calls.last.request.url.params["match[]"]
        # Dots and plus should be escaped for regex safety
        # Regex-escaped, then PromQL-quoted: each backslash is doubled inside the string literal.
        assert match_param == r'{__name__=~".*foo\\.bar\\+baz.*"}'

    @respx.mock
    async def test_connect_error_raises_tool_exception(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await prometheus_search_metrics.ainvoke({"search_term": "up"})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout_raises_tool_exception(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            side_effect=httpx.ReadTimeout("Read timed out")
        )

        result = await prometheus_search_metrics.ainvoke({"search_term": "up"})
        assert "timed out" in result

    @respx.mock
    async def test_metadata_failure_still_returns_names(self) -> None:
        respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": ["up", "node_cpu_seconds_total"]},
            )
        )
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            side_effect=httpx.ConnectError("metadata endpoint down")
        )

        result = await prometheus_search_metrics.ainvoke({"search_term": "node"})
        assert "Found 2 metrics" in result
        assert "node_cpu_seconds_total" in result
        assert "up" in result


@pytest.mark.integration
async def test_baseline_enrichment_failure_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A broken memory store must be visible in the logs, not silently dropped."""
    data: PrometheusResponse = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {"__name__": "up"}, "value": [1, "1"]}]},
    }
    with (
        patch("src.memory.context.enrich_with_baseline_context", side_effect=RuntimeError("db locked")),
        caplog.at_level(logging.WARNING, logger="src.agent.tools.prometheus"),
    ):
        assert _get_baseline_enrichment(data) == ""
    [record] = [r for r in caplog.records if r.name == "src.agent.tools.prometheus"]
    assert record.levelno == logging.WARNING
    assert "db locked" in record.getMessage()


def _promql_literal_is_valid(selector: str) -> bool:
    """True when the quoted literal in ``selector`` escapes only a backslash or a double quote."""
    body = selector[selector.index('"') + 1 : selector.rindex('"')]
    i = 0
    while i < len(body):
        if body[i] == "\\":
            if i + 1 >= len(body) or body[i + 1] not in '\\"':
                return False
            i += 2
            continue
        if body[i] == '"':
            return False
        i += 1
    return True


@pytest.mark.integration
class TestPrometheusSearchQuoting:
    """Prometheus rejects Python's re.escape output as unknown escapes (HTTP 400, seen live 2026-11-11)."""

    @pytest.mark.parametrize("term", ["process_start.time", "tube-archivist", 'x".*|{', "a b"])
    @respx.mock
    async def test_search_term_sent_as_valid_promql_literal(self, term: str) -> None:
        values = respx.get("http://prometheus.test:9090/api/v1/label/__name__/values").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )
        respx.get("http://prometheus.test:9090/api/v1/metadata").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )
        await prometheus_search_metrics.ainvoke({"search_term": term})
        selector = values.calls.last.request.url.params["match[]"]
        assert _promql_literal_is_valid(selector), selector
