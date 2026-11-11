"""Integration tests for Grafana dashboard tools with mocked HTTP responses."""

from typing import Any

import httpx
import pytest
import respx

from src.agent.tools.grafana_dashboards import grafana_get_dashboard, grafana_search_dashboards

SAMPLE_DASHBOARD_RESPONSE: dict[str, Any] = {
    "dashboard": {
        "uid": "dekkfibh9454wb",
        "title": "Home Server",
        "tags": ["homelab"],
        "refresh": "10s",
        "panels": [
            {
                "id": 1,
                "title": "Tech Shelf Power",
                "type": "timeseries",
                "datasource": {"type": "prometheus", "uid": "prom1"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": 'homeassistant_sensor_power_w{entity="sensor.tech_shelf_power"}',
                        "legendFormat": "Power (W)",
                    }
                ],
                "fieldConfig": {"defaults": {"unit": "watt"}},
            },
            {
                "id": 10,
                "title": "Compute",
                "type": "row",
                "panels": [
                    {
                        "id": 11,
                        "title": "CPU per VM/LXC",
                        "type": "timeseries",
                        "datasource": {"type": "prometheus", "uid": "prom1"},
                        "targets": [
                            {
                                "refId": "A",
                                "expr": 'pve_cpu_usage_ratio{name=~"$hostname"} * 100',
                                "legendFormat": "{{name}}",
                            }
                        ],
                        "fieldConfig": {
                            "defaults": {
                                "unit": "percent",
                                "thresholds": {
                                    "steps": [
                                        {"value": None, "color": "green"},
                                        {"value": 80, "color": "red"},
                                    ]
                                },
                            }
                        },
                    },
                    {
                        "id": 12,
                        "title": "Memory per VM/LXC",
                        "type": "timeseries",
                        "datasource": {"type": "prometheus", "uid": "prom1"},
                        "targets": [
                            {
                                "refId": "A",
                                "expr": 'pve_memory_usage_bytes{name=~"$hostname"}',
                                "legendFormat": "{{name}}",
                            }
                        ],
                        "fieldConfig": {"defaults": {"unit": "bytes"}},
                    },
                ],
            },
        ],
        "templating": {
            "list": [
                {
                    "name": "hostname",
                    "type": "query",
                    "query": "label_values(pve_guest_info, name)",
                    "current": {"text": "All"},
                }
            ]
        },
        "annotations": {"list": []},
        "links": [],
    },
    "meta": {"folderTitle": "General"},
}


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


@pytest.mark.integration
class TestGrafanaGetDashboard:
    @respx.mock
    async def test_fetch_by_uid(self) -> None:
        respx.get("http://grafana.test:3000/api/dashboards/uid/dekkfibh9454wb").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "dekkfibh9454wb"})
        assert "Home Server" in result
        assert "Tech Shelf Power" in result
        assert "CPU per VM/LXC" in result

    @respx.mock
    async def test_fetch_by_name_searches_first(self) -> None:
        search_route = respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "uid": "dekkfibh9454wb",
                        "title": "Home Server",
                        "url": "/d/dekkfibh9454wb/home-server",
                        "folderTitle": "General",
                        "type": "dash-db",
                    }
                ],
            ),
        )
        dash_route = respx.get("http://grafana.test:3000/api/dashboards/uid/dekkfibh9454wb").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "Home Server"})
        assert search_route.called
        assert dash_route.called
        assert "Home Server" in result

    @respx.mock
    async def test_extract_single_panel(self) -> None:
        respx.get("http://grafana.test:3000/api/dashboards/uid/dekkfibh9454wb").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "dekkfibh9454wb", "panel": "CPU per VM"})
        assert "CPU per VM/LXC" in result
        assert "pve_cpu_usage_ratio" in result
        assert "hostname" in result
        # Should not contain other panels
        assert "Tech Shelf Power" not in result

    @respx.mock
    async def test_panel_not_found(self) -> None:
        respx.get("http://grafana.test:3000/api/dashboards/uid/dekkfibh9454wb").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "dekkfibh9454wb", "panel": "Nonexistent Panel"})
        assert "No panel matching" in result
        assert "Available panels" in result
        assert "Tech Shelf Power" in result

    @respx.mock
    async def test_uid_404_falls_back_to_search(self) -> None:
        respx.get("http://grafana.test:3000/api/dashboards/uid/notauid").mock(
            return_value=httpx.Response(404, text="Dashboard not found"),
        )
        respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(
                200,
                json=[{"uid": "dekkfibh9454wb", "title": "Home Server", "type": "dash-db"}],
            ),
        )
        respx.get("http://grafana.test:3000/api/dashboards/uid/dekkfibh9454wb").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "notauid"})
        assert "Home Server" in result

    @respx.mock
    async def test_dashboard_not_found(self) -> None:
        respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(200, json=[]),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "No Such Dashboard"})
        assert "No dashboard found" in result

    @respx.mock
    async def test_grafana_unreachable(self) -> None:
        respx.get("http://grafana.test:3000/api/dashboards/uid/abc123").mock(
            side_effect=httpx.ConnectError("Connection refused"),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "abc123"})
        assert "Cannot connect" in result

    @respx.mock
    async def test_auth_failure(self) -> None:
        respx.get("http://grafana.test:3000/api/dashboards/uid/abc123").mock(
            return_value=httpx.Response(401, text="Unauthorized"),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "abc123"})
        assert "401" in result

    @respx.mock
    async def test_sends_auth_header(self) -> None:
        route = respx.get("http://grafana.test:3000/api/dashboards/uid/abc123").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        await grafana_get_dashboard.ainvoke({"dashboard": "abc123"})
        auth_header = route.calls.last.request.headers.get("authorization")
        assert auth_header == "Bearer glsa_test_fake"

    @respx.mock
    async def test_row_panels_flattened_in_summary(self) -> None:
        """Panels nested inside row panels should appear in the summary."""
        respx.get("http://grafana.test:3000/api/dashboards/uid/dekkfibh9454wb").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        result = await grafana_get_dashboard.ainvoke({"dashboard": "dekkfibh9454wb"})
        # Nested panels should be listed
        assert "CPU per VM/LXC" in result
        assert "Memory per VM/LXC" in result

    @respx.mock
    async def test_dashboard_uid_is_path_quoted(self) -> None:
        """A UID from a search result cannot walk to another API path (F18)."""
        respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(
                200,
                json=[{"uid": "../../admin/settings?x=", "title": "Sneaky", "type": "dash-db"}],
            ),
        )
        catch_all = respx.get(host="grafana.test").mock(
            return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_RESPONSE),
        )

        await grafana_get_dashboard.ainvoke({"dashboard": "Sneaky dashboard"})
        assert catch_all.called
        request = catch_all.calls.last.request
        assert request.url.raw_path == b"/api/dashboards/uid/..%2F..%2Fadmin%2Fsettings%3Fx%3D"


@pytest.mark.integration
class TestGrafanaSearchDashboards:
    @respx.mock
    async def test_successful_search(self) -> None:
        respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"title": "Home Server", "uid": "aaa", "folderTitle": "General", "url": "/d/aaa"},
                    {"title": "Network", "uid": "bbb", "folderTitle": "Infra", "url": "/d/bbb"},
                ],
            ),
        )

        result = await grafana_search_dashboards.ainvoke({"query": "server"})
        assert "Home Server" in result
        assert "Network" in result
        assert "2 dashboard" in result

    @respx.mock
    async def test_empty_results(self) -> None:
        respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(200, json=[]),
        )

        result = await grafana_search_dashboards.ainvoke({"query": "nonexistent"})
        assert "No dashboards found" in result

    @respx.mock
    async def test_grafana_timeout(self) -> None:
        respx.get("http://grafana.test:3000/api/search").mock(
            side_effect=httpx.ReadTimeout("Read timed out"),
        )

        result = await grafana_search_dashboards.ainvoke({"query": "test"})
        assert "timed out" in result

    @respx.mock
    async def test_sends_search_params(self) -> None:
        route = respx.get("http://grafana.test:3000/api/search").mock(
            return_value=httpx.Response(200, json=[]),
        )

        await grafana_search_dashboards.ainvoke({"query": "Home Server"})
        params = dict(route.calls.last.request.url.params)
        assert params["query"] == "Home Server"
        assert params["type"] == "dash-db"
