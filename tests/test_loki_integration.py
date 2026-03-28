"""Integration tests for Loki tools with mocked HTTP responses."""

from typing import Any

import httpx
import pytest
import respx

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

        result = await loki_query_logs.ainvoke({"query": '{hostname="nonexistent"}'})
        assert "No log lines found" in result

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


@pytest.mark.integration
class TestLokiCorrelateChanges:
    @respx.mock
    async def test_successful_correlation(self) -> None:
        # Mock error query
        respx.get(
            "http://loki.test:3100/loki/api/v1/query_range",
            params__contains={"query": '{detected_level=~"error|warn|fatal"}'},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {
                                    "hostname": "infra",
                                    "service_name": "traefik",
                                    "detected_level": "error",
                                },
                                "values": [
                                    ["1718452800000000000", "502 Bad Gateway for backend"],
                                ],
                            }
                        ],
                    },
                },
            )
        )

        # Mock lifecycle query
        respx.get(
            "http://loki.test:3100/loki/api/v1/query_range",
            params__contains={"limit": "100"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {
                                    "hostname": "infra",
                                    "service_name": "adguard",
                                    "detected_level": "info",
                                },
                                "values": [
                                    ["1718452500000000000", "Container started"],
                                ],
                            }
                        ],
                    },
                },
            )
        )

        result = await loki_correlate_changes.ainvoke(
            {
                "reference_time": "2024-06-15T14:00:00Z",
                "window_minutes": 30,
            }
        )
        assert "Time window" in result
        assert "significant events" in result
        assert "Chronological Timeline" in result

    @respx.mock
    async def test_correlation_with_filters(self) -> None:
        route_error = respx.get(
            "http://loki.test:3100/loki/api/v1/query_range",
            params__contains={"limit": "200"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        respx.get(
            "http://loki.test:3100/loki/api/v1/query_range",
            params__contains={"limit": "100"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        result = await loki_correlate_changes.ainvoke(
            {
                "reference_time": "2024-06-15T14:00:00Z",
                "hostname": "infra",
                "service_name": "traefik",
            }
        )
        assert "hostname=infra" in result
        assert "service_name=traefik" in result
        # Error query should include the hostname and service filters
        assert route_error.called
        query_param = route_error.calls.last.request.url.params["query"]
        assert "infra" in query_param

    @respx.mock
    async def test_no_events_found(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )
        # Mock /ready for connectivity check
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))

        result = await loki_correlate_changes.ainvoke(
            {
                "reference_time": "2024-06-15T14:00:00Z",
            }
        )
        assert "No significant events" in result

    @respx.mock
    async def test_loki_unreachable(self) -> None:
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        respx.get("http://loki.test:3100/ready").mock(side_effect=httpx.ConnectError("Connection refused"))

        result = await loki_correlate_changes.ainvoke(
            {
                "reference_time": "2024-06-15T14:00:00Z",
            }
        )
        assert "Cannot connect" in result

    async def test_invalid_reference_time(self) -> None:
        result = await loki_correlate_changes.ainvoke(
            {
                "reference_time": "not-a-time",
            }
        )
        assert "Cannot parse time" in result

    @respx.mock
    async def test_no_filters_uses_valid_selector(self) -> None:
        """Without hostname/service filters, queries must NOT use empty '{}' selector."""
        error_route = respx.get(
            "http://loki.test:3100/loki/api/v1/query_range",
            params__contains={"limit": "200"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        lifecycle_route = respx.get(
            "http://loki.test:3100/loki/api/v1/query_range",
            params__contains={"limit": "100"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": []}},
            )
        )

        # Mock /ready for connectivity check
        respx.get("http://loki.test:3100/ready").mock(return_value=httpx.Response(200, text="ready"))

        await loki_correlate_changes.ainvoke(
            {
                "reference_time": "2024-06-15T14:00:00Z",
                "window_minutes": 10,
            }
        )

        # Lifecycle query must NOT contain an empty {} selector
        assert lifecycle_route.called
        lifecycle_query = lifecycle_route.calls.last.request.url.params["query"]
        assert not lifecycle_query.startswith("{}")
        assert 'hostname=~".+"' in lifecycle_query

        # Error query should also use a valid selector
        assert error_route.called
        error_query = error_route.calls.last.request.url.params["query"]
        assert not error_query.startswith("{}")

    @respx.mock
    async def test_deduplicates_events(self) -> None:
        """Events found by both error and lifecycle queries should not be duplicated."""
        same_event = {
            "stream": {
                "hostname": "infra",
                "service_name": "traefik",
                "detected_level": "error",
            },
            "values": [
                ["1718452800000000000", "Container exited with error"],
            ],
        }

        # Both queries return the same event
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "streams", "result": [same_event]},
                },
            )
        )

        result = await loki_correlate_changes.ainvoke(
            {
                "reference_time": "2024-06-15T14:00:00Z",
            }
        )
        # Should find 1 event, not 2
        assert "1 significant events" in result


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
