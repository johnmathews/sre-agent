"""Integration tests for the report module — mocked HTTP via respx."""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from src.report.email import send_report_email
from src.report.generator import (
    _collect_alert_summary,
    _collect_backup_health,
    _collect_cost_data,
    _collect_loki_errors,
    _collect_slo_status,
    _collect_tool_usage,
    collect_report_data,
    generate_report,
)
from src.report.scheduler import start_scheduler, stop_scheduler

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Prometheus mock helper
# ---------------------------------------------------------------------------


def _prom_response(result: list[dict[str, object]]) -> dict[str, object]:
    """Build a Prometheus API /api/v1/query response envelope."""
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _prom_scalar(value: float) -> list[dict[str, object]]:
    """Single-series Prometheus result with a scalar value."""
    return [{"metric": {}, "value": [1708300000, str(value)]}]


def _prom_by_label(label: str, entries: dict[str, float]) -> list[dict[str, object]]:
    """Multi-series Prometheus result keyed by a label."""
    return [{"metric": {label: name}, "value": [1708300000, str(val)]} for name, val in entries.items()]


# ---------------------------------------------------------------------------
# Collector tests
# ---------------------------------------------------------------------------


class TestCollectAlertSummary:
    @respx.mock
    async def test_success(self, mock_settings: Any) -> None:
        # Mock alert rules endpoint
        respx.get("http://grafana.test:3000/api/v1/provisioning/alert-rules").mock(
            return_value=httpx.Response(200, json=[{"uid": "1"}, {"uid": "2"}, {"uid": "3"}])
        )
        # Mock alert groups endpoint with one active alert
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "labels": {},
                        "alerts": [
                            {
                                "labels": {"alertname": "HighCPU", "severity": "critical"},
                                "status": {"state": "active"},
                            }
                        ],
                    }
                ],
            )
        )

        result = await _collect_alert_summary(7)

        assert result["total_rules"] == 3
        assert result["active_alerts"] == 1
        assert result["active_alert_names"] == ["HighCPU"]
        assert result["alerts_by_severity"] == {"critical": 1}

    @respx.mock
    async def test_grafana_down_raises(self, mock_settings: Any) -> None:
        respx.get("http://grafana.test:3000/api/v1/provisioning/alert-rules").mock(return_value=httpx.Response(503))

        with pytest.raises(httpx.HTTPStatusError):
            await _collect_alert_summary(7)


class TestCollectSloStatus:
    @respx.mock
    async def test_success(self, mock_settings: Any) -> None:
        # p95 latency
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            side_effect=[
                httpx.Response(200, json=_prom_response(_prom_scalar(3.5))),  # p95
                httpx.Response(200, json=_prom_response(_prom_scalar(200.0))),  # tool total
                httpx.Response(200, json=_prom_response(_prom_scalar(2.0))),  # tool errors
                httpx.Response(200, json=_prom_response(_prom_scalar(100.0))),  # llm total
                httpx.Response(200, json=_prom_response(_prom_scalar(1.0))),  # llm errors
                httpx.Response(200, json=_prom_response(_prom_scalar(0.995))),  # availability
            ]
        )

        result = await _collect_slo_status(7)

        assert result["p95_latency_seconds"] == 3.5
        assert result["tool_success_rate"] == pytest.approx(0.99)
        assert result["llm_error_rate"] == pytest.approx(0.01)
        assert result["availability"] == pytest.approx(0.995)

    @respx.mock
    async def test_prometheus_down_raises(self, mock_settings: Any) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(return_value=httpx.Response(503))

        with pytest.raises(httpx.HTTPStatusError):
            await _collect_slo_status(7)


class TestCollectToolUsage:
    @respx.mock
    async def test_success(self, mock_settings: Any) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_prom_response(
                        _prom_by_label("tool_name", {"prometheus_query": 100.0, "grafana_alerts": 50.0})
                    ),
                ),
                httpx.Response(200, json=_prom_response(_prom_by_label("tool_name", {"prometheus_query": 3.0}))),
            ]
        )

        result = await _collect_tool_usage(7)

        assert result["tool_calls"] == {"prometheus_query": 100, "grafana_alerts": 50}
        assert result["tool_errors"] == {"prometheus_query": 3}


class TestCollectCostData:
    @respx.mock
    async def test_success(self, mock_settings: Any) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            side_effect=[
                httpx.Response(200, json=_prom_response(_prom_scalar(40000.0))),  # prompt
                httpx.Response(200, json=_prom_response(_prom_scalar(10000.0))),  # completion
                httpx.Response(200, json=_prom_response(_prom_scalar(0.085))),  # cost
            ]
        )

        result = await _collect_cost_data(7)

        assert result["prompt_tokens"] == 40000
        assert result["completion_tokens"] == 10000
        assert result["total_tokens"] == 50000
        assert result["estimated_cost_usd"] == 0.085


def _loki_vector(entries: dict[str, float]) -> dict[str, object]:
    """Build a Loki instant query vector response."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"service_name": name}, "value": [1708300000, str(val)]} for name, val in entries.items()
            ],
        },
    }


def _loki_stream(service: str, lines: list[str]) -> dict[str, object]:
    """Build a Loki query_range stream response."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": service, "detected_level": "error"},
                    "values": [["1708300000000000000", line] for line in lines],
                }
            ],
        },
    }


class TestCollectLokiErrors:
    @respx.mock
    async def test_success_with_previous_period(self, mock_settings: Any) -> None:
        """Current + previous period queries both succeed."""
        # Two instant queries (current, previous) + sample queries for top-5
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            side_effect=[
                httpx.Response(200, json=_loki_vector({"traefik": 85, "jellyfin": 10})),
                httpx.Response(200, json=_loki_vector({"traefik": 60, "jellyfin": 15})),
            ]
        )
        # Error sample queries (one per top-5 service, 2 services here)
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            side_effect=[
                httpx.Response(200, json=_loki_stream("traefik", ["502 Bad Gateway"])),
                httpx.Response(200, json=_loki_stream("jellyfin", ["connection refused"])),
            ]
        )

        result = await _collect_loki_errors(7)

        assert result is not None
        assert result["errors_by_service"] == {"traefik": 85, "jellyfin": 10}
        assert result["total_errors"] == 95
        assert result.get("previous_total_errors") == 75
        assert result.get("error_samples", {}).get("traefik") == "502 Bad Gateway"

    @respx.mock
    async def test_normalizes_duplicate_service_names(self, mock_settings: Any) -> None:
        """node_exporter and node-exporter should merge."""
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            side_effect=[
                httpx.Response(200, json=_loki_vector({"node_exporter": 500, "node-exporter": 14})),
                httpx.Response(200, json=_loki_vector({})),
            ]
        )
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json=_loki_stream("node_exporter", ["scrape failed"]))
        )

        result = await _collect_loki_errors(7)

        assert result is not None
        # Merged under the higher-count name
        assert "node_exporter" in result["errors_by_service"]
        assert result["errors_by_service"]["node_exporter"] == 514
        assert "node-exporter" not in result["errors_by_service"]

    @respx.mock
    async def test_previous_period_failure_graceful(self, mock_settings: Any) -> None:
        """Previous period query failure doesn't crash — just omits comparison."""
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            side_effect=[
                httpx.Response(200, json=_loki_vector({"traefik": 85})),
                httpx.Response(503),  # previous period fails
            ]
        )
        respx.get("http://loki.test:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json=_loki_stream("traefik", ["error"]))
        )

        result = await _collect_loki_errors(7)

        assert result is not None
        assert result["total_errors"] == 85
        assert result.get("previous_total_errors") is None

    async def test_loki_not_configured(self, mock_settings: Any) -> None:
        mock_settings.loki_url = ""
        result = await _collect_loki_errors(7)
        assert result is None


class TestCollectBackupHealth:
    @respx.mock
    async def test_success(self, mock_settings: Any) -> None:
        now_ts = int(__import__("datetime").datetime.now(__import__("datetime").UTC).timestamp())
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "store": "backups",
                            "total": 2 * 1024**4,
                            "used": 1024**4,
                            "avail": 1024**4,
                        }
                    ]
                },
            )
        )
        respx.get("https://pbs.test:8007/api2/json/admin/datastore/backups/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "backup-type": "vm",
                            "backup-id": "100",
                            "last-backup": now_ts - 3600,
                            "backup-count": 10,
                        },
                        {
                            "backup-type": "ct",
                            "backup-id": "200",
                            "last-backup": now_ts - 172800,
                            "backup-count": 5,
                        },
                    ]
                },
            )
        )

        result = await _collect_backup_health(7)

        assert result is not None
        assert len(result["datastores"]) == 1
        assert result["datastores"][0]["store"] == "backups"
        assert result["datastores"][0]["usage_percent"] == 50.0
        assert result["total_count"] == 2
        assert result["stale_count"] == 1  # ct/200 is >24h old

    async def test_pbs_not_configured(self, mock_settings: Any) -> None:
        mock_settings.pbs_url = ""
        result = await _collect_backup_health(7)
        assert result is None

    @respx.mock
    async def test_pbs_down_raises(self, mock_settings: Any) -> None:
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(return_value=httpx.Response(503))

        with pytest.raises(httpx.HTTPStatusError):
            await _collect_backup_health(7)


# ---------------------------------------------------------------------------
# collect_report_data tests
# ---------------------------------------------------------------------------


class TestCollectReportData:
    @respx.mock
    async def test_graceful_degradation_all_down(self, mock_settings: Any) -> None:
        """When all services are unreachable, collectors return None instead of crashing."""
        respx.get("http://grafana.test:3000/api/v1/provisioning/alert-rules").mock(return_value=httpx.Response(503))
        respx.get("http://prometheus.test:9090/api/v1/query").mock(return_value=httpx.Response(503))
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(return_value=httpx.Response(503))
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(return_value=httpx.Response(503))

        data = await collect_report_data(7)

        # All should be None due to failures, not crash
        assert data["alerts"] is None
        assert data["slo_status"] is None
        assert data["tool_usage"] is None
        assert data["cost"] is None
        assert data["loki_errors"] is None
        assert data["backup_health"] is None


# ---------------------------------------------------------------------------
# generate_report end-to-end
# ---------------------------------------------------------------------------


class TestGenerateReport:
    @respx.mock
    async def test_full_report_generation(self, mock_settings: Any) -> None:
        """Full pipeline: mock all APIs + LLM, verify markdown output."""
        # Alert rules
        respx.get("http://grafana.test:3000/api/v1/provisioning/alert-rules").mock(
            return_value=httpx.Response(200, json=[{"uid": "1"}])
        )
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Prometheus (SLO + tool usage + cost = 3+2+3 = 8 calls)
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json=_prom_response(_prom_scalar(1.0)))
        )
        # Loki (current + previous period)
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
        )
        # PBS
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        # Mock LLM
        with patch("src.agent.llm.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_response = MagicMock()
            mock_response.content = "Test narrative summary."
            mock_llm.ainvoke = MagicMock(return_value=mock_response)

            async def fake_ainvoke(*args: Any, **kwargs: Any) -> Any:
                return mock_response

            mock_llm.ainvoke = fake_ainvoke
            mock_llm_cls.return_value = mock_llm

            result = await generate_report(7)

        assert "# Weekly Reliability Report" in result.markdown
        assert "Test narrative summary." in result.markdown
        assert "## Alert Summary" in result.markdown
        assert "## SLO Status" in result.markdown
        # HTML output also generated
        assert "<!DOCTYPE html>" in result.html
        assert "Test narrative summary." in result.html

    @respx.mock
    async def test_report_with_all_services_down(self, mock_settings: Any) -> None:
        """Even when all APIs fail, a report is still produced."""
        respx.get("http://grafana.test:3000/api/v1/provisioning/alert-rules").mock(return_value=httpx.Response(503))
        respx.get("http://prometheus.test:9090/api/v1/query").mock(return_value=httpx.Response(503))
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(return_value=httpx.Response(503))
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(return_value=httpx.Response(503))

        with patch("src.agent.llm.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_response = MagicMock()
            mock_response.content = "All data sources were unavailable."

            async def fake_ainvoke(*args: Any, **kwargs: Any) -> Any:
                return mock_response

            mock_llm.ainvoke = fake_ainvoke
            mock_llm_cls.return_value = mock_llm

            result = await generate_report(7)

        assert "# Weekly Reliability Report" in result.markdown
        assert "Alert data unavailable" in result.markdown
        assert "SLO data unavailable" in result.markdown


# ---------------------------------------------------------------------------
# Email tests
# ---------------------------------------------------------------------------


class TestSendReportEmail:
    def test_send_success_html(self, mock_settings: Any) -> None:
        """HTML email is sent as multipart/alternative."""
        with patch("src.report.email.smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = send_report_email("# Test Report", "<h1>Test Report</h1>")

        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("test@test.com", "test-password")
        mock_server.send_message.assert_called_once()
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg.get_content_type() == "multipart/alternative"

    def test_send_success_plain_only(self, mock_settings: Any) -> None:
        """Plain-text fallback when no HTML provided."""
        with patch("src.report.email.smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = send_report_email("# Test Report")

        assert result is True
        mock_server.send_message.assert_called_once()

    def test_send_failure(self, mock_settings: Any) -> None:
        with patch("src.report.email.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = MagicMock(side_effect=ConnectionError("SMTP down"))

            result = send_report_email("# Test Report")

        assert result is False

    def test_not_configured(self, mock_settings: Any) -> None:
        mock_settings.smtp_host = ""
        result = send_report_email("# Test Report")
        assert result is False


# ---------------------------------------------------------------------------
# Scheduler tests
# ---------------------------------------------------------------------------


class TestScheduler:
    async def test_start_stop_with_cron(self, mock_settings: Any) -> None:
        import src.report.scheduler as sched_mod

        mock_settings.report_schedule_cron = "0 8 * * 1"
        start_scheduler()
        assert sched_mod._scheduler is not None
        stop_scheduler()
        assert sched_mod._scheduler is None

    async def test_start_without_cron_is_noop(self, mock_settings: Any) -> None:
        import src.report.scheduler as sched_mod

        # Ensure clean state
        stop_scheduler()
        mock_settings.report_schedule_cron = ""
        start_scheduler()
        assert sched_mod._scheduler is None


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestReportEndpoint:
    @respx.mock
    def test_post_report(self, mock_settings: Any) -> None:
        """Test the /report endpoint via TestClient."""
        # Mock all external APIs
        respx.get("http://grafana.test:3000/api/v1/provisioning/alert-rules").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("http://grafana.test:3000/api/alertmanager/grafana/api/v2/alerts/groups").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json=_prom_response(_prom_scalar(0.0)))
        )
        respx.get("http://loki.test:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
        )
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        # Mock health endpoints for lifespan
        respx.get("http://prometheus.test:9090/-/healthy").mock(return_value=httpx.Response(200))
        respx.get("http://grafana.test:3000/api/health").mock(return_value=httpx.Response(200))

        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.api.main.build_agent") as mock_build,
        ):
            mock_build.return_value = MagicMock()
            mock_llm = MagicMock()
            mock_response = MagicMock()
            mock_response.content = "Test narrative."

            async def fake_ainvoke(*args: Any, **kwargs: Any) -> Any:
                return mock_response

            mock_llm.ainvoke = fake_ainvoke
            mock_llm_cls.return_value = mock_llm

            # Disable email for this test
            mock_settings.smtp_host = ""

            from fastapi.testclient import TestClient

            from src.api.main import app

            with TestClient(app) as client:
                resp = client.post("/report", json={"lookback_days": 7})

            assert resp.status_code == 200
            body = resp.json()
            assert "report" in body
            assert "# Weekly Reliability Report" in body["report"]
            assert body["emailed"] is False
            assert "timestamp" in body
