"""Integration tests for Grafana alerting tools with mocked HTTP responses."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from langchain_core.tools import BaseTool, StructuredTool, ToolException

from src.agent.tools.grafana_alerts import grafana_get_alert_rules, grafana_get_alerts

ALERT_GROUPS_URL = "http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups"
RULES_URL = "http://grafana.test:3000/api/prometheus/grafana/api/v1/rules"

MIXED_STATE_GROUPS: list[dict[str, Any]] = [
    {
        "labels": {"grafana_folder": "Infrastructure"},
        "alerts": [
            {
                "labels": {"alertname": "HighCPU", "hostname": "jellyfin"},
                "annotations": {},
                "startsAt": "2024-01-15T10:00:00Z",
                "status": {"state": "active"},
            },
            {
                "labels": {"alertname": "DiskFull"},
                "annotations": {},
                "startsAt": "2024-01-15T09:00:00Z",
                "status": {"state": "suppressed"},
            },
        ],
    }
]

# Prometheus-compatible rules format. The live shape for this Grafana version is [SUSPECTED]; W37 checks it.
RULES_PAYLOAD: dict[str, Any] = {
    "status": "success",
    "data": {
        "groups": [
            {
                "name": "node-alerts",
                "file": "Infrastructure",
                "folderUid": "infra",
                "rules": [
                    {
                        "uid": "abc123",
                        "name": "High CPU Usage",
                        "type": "alerting",
                        "state": "firing",
                        "health": "ok",
                        "lastError": "",
                        "lastEvaluation": "2024-01-15T10:00:00Z",
                        "labels": {"severity": "warning"},
                        "annotations": {"summary": "CPU > 90% for 5 minutes"},
                        "alerts": [
                            {
                                "labels": {"alertname": "High CPU Usage", "hostname": "jellyfin"},
                                "state": "Alerting",
                                "activeAt": "2024-01-15T09:50:00Z",
                            }
                        ],
                    },
                    {
                        "uid": "def456",
                        "name": "Disk Fine",
                        "type": "alerting",
                        "state": "inactive",
                        "health": "ok",
                        "lastError": "",
                        "lastEvaluation": "2024-01-15T10:00:00Z",
                        "labels": {"severity": "critical"},
                        "annotations": {},
                        "alerts": [],
                    },
                    {
                        "uid": "ghi789",
                        "name": "Broken Query",
                        "type": "alerting",
                        "state": "inactive",
                        "health": "error",
                        "lastError": "datasource not found",
                        "lastEvaluation": "2024-01-15T10:00:00Z",
                        "labels": {},
                        "annotations": {},
                        "alerts": [],
                    },
                ],
            }
        ]
    },
}


def _body(tool: BaseTool) -> Callable[..., Awaitable[str]]:
    """The tool's own coroutine, called without the args_schema (as a direct or LangGraph caller could)."""
    assert isinstance(tool, StructuredTool)
    assert tool.coroutine is not None
    body: Callable[..., Awaitable[str]] = tool.coroutine
    return body


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


@pytest.mark.integration
class TestGrafanaGetAlerts:
    @respx.mock
    async def test_successful_fetch(self) -> None:
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "labels": {"grafana_folder": "Infrastructure"},
                        "receiver": {"name": "pushover"},
                        "alerts": [
                            {
                                "labels": {"alertname": "HighCPU", "severity": "warning", "hostname": "jellyfin"},
                                "annotations": {"summary": "CPU above 90%"},
                                "startsAt": "2024-01-15T10:00:00Z",
                                "status": {"state": "active"},
                            }
                        ],
                    }
                ],
            )
        )

        result = await grafana_get_alerts.ainvoke({})
        assert "HighCPU" in result
        assert "jellyfin" in result

    @respx.mock
    async def test_state_filter_applied_locally_not_upstream(self) -> None:
        route = respx.get(ALERT_GROUPS_URL).mock(return_value=httpx.Response(200, json=MIXED_STATE_GROUPS))

        result = await grafana_get_alerts.ainvoke({"state": "active"})
        assert route.called
        assert "filter" not in route.calls.last.request.url.params
        assert "HighCPU" in result
        assert "DiskFull" not in result

    @respx.mock
    async def test_firing_alias_maps_to_active(self) -> None:
        respx.get(ALERT_GROUPS_URL).mock(return_value=httpx.Response(200, json=MIXED_STATE_GROUPS))

        result = await grafana_get_alerts.ainvoke({"state": "firing"})
        assert "HighCPU" in result
        assert "DiskFull" not in result
        assert "No alerts with state" not in result

    @respx.mock
    async def test_invalid_state_rejected_in_body(self) -> None:
        route = respx.get(ALERT_GROUPS_URL).mock(return_value=httpx.Response(200, json=[]))

        with pytest.raises(ToolException) as excinfo:
            await _body(grafana_get_alerts)(state="bogus")
        message = str(excinfo.value)
        for accepted in ("active", "suppressed", "unprocessed", "firing"):
            assert accepted in message
        assert not route.called

    @respx.mock
    async def test_enrichment_failure_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        respx.get(ALERT_GROUPS_URL).mock(return_value=httpx.Response(200, json=MIXED_STATE_GROUPS))

        with (
            patch(
                "src.memory.context.enrich_alerts_with_incident_history",
                side_effect=RuntimeError("memory exploded"),
            ),
            caplog.at_level(logging.WARNING, logger="src.agent.tools.grafana_alerts"),
        ):
            result = await grafana_get_alerts.ainvoke({})

        assert "HighCPU" in result
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name.endswith("grafana_alerts")]
        assert warnings, "a failed incident-history lookup must log a WARNING"
        assert "incident history" in warnings[0].getMessage()

    @respx.mock
    async def test_empty_response(self) -> None:
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(200, json=[]),
        )

        result = await grafana_get_alerts.ainvoke({})
        assert "No alerts found" in result

    @respx.mock
    async def test_grafana_unreachable(self) -> None:
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            side_effect=httpx.ConnectError("Connection refused"),
        )

        result = await grafana_get_alerts.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_auth_failure(self) -> None:
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(401, text="Unauthorized"),
        )

        result = await grafana_get_alerts.ainvoke({})
        assert "401" in result

    @respx.mock
    async def test_sends_auth_header(self) -> None:
        route = respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(200, json=[]),
        )

        await grafana_get_alerts.ainvoke({})
        auth_header = route.calls.last.request.headers.get("authorization")
        assert auth_header == "Bearer glsa_test_fake"


@pytest.mark.integration
class TestGrafanaGetAlertRules:
    @respx.mock
    async def test_successful_fetch(self) -> None:
        route = respx.get(RULES_URL).mock(return_value=httpx.Response(200, json=RULES_PAYLOAD))

        result = await grafana_get_alert_rules.ainvoke({})
        assert route.called
        assert "High CPU Usage" in result
        assert "abc123" in result
        assert "Showing 3 of 3 alert rules" in result

    @respx.mock
    async def test_rule_state_filters_error_health(self) -> None:
        respx.get(RULES_URL).mock(return_value=httpx.Response(200, json=RULES_PAYLOAD))

        result = await grafana_get_alert_rules.ainvoke({"health": "error"})
        assert "Broken Query" in result
        assert "datasource not found" in result
        assert "High CPU Usage" not in result
        assert "Disk Fine" not in result
        assert "Showing 1 of 3 alert rules" in result

    @respx.mock
    async def test_rule_state_filter_firing(self) -> None:
        respx.get(RULES_URL).mock(return_value=httpx.Response(200, json=RULES_PAYLOAD))

        result = await grafana_get_alert_rules.ainvoke({"state": "firing"})
        assert "High CPU Usage" in result
        assert "Disk Fine" not in result
        assert 'hostname="jellyfin"' in result

    @respx.mock
    async def test_invalid_rule_filters_rejected_in_body(self) -> None:
        route = respx.get(RULES_URL).mock(return_value=httpx.Response(200, json=RULES_PAYLOAD))

        with pytest.raises(ToolException, match="firing, pending, inactive, recovering"):
            await _body(grafana_get_alert_rules)(state="alerting")
        with pytest.raises(ToolException, match="ok, error, nodata"):
            await _body(grafana_get_alert_rules)(health="bad")
        with pytest.raises(ToolException, match="100"):
            await _body(grafana_get_alert_rules)(name_contains="x" * 101)
        assert not route.called

    @respx.mock
    async def test_empty_rules(self) -> None:
        respx.get(RULES_URL).mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {"groups": []}}),
        )

        result = await grafana_get_alert_rules.ainvoke({})
        assert "No alert rules found" in result

    @respx.mock
    async def test_grafana_timeout(self) -> None:
        respx.get(RULES_URL).mock(side_effect=httpx.ReadTimeout("Read timed out"))

        result = await grafana_get_alert_rules.ainvoke({})
        assert "timed out" in result
