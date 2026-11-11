"""Integration tests for the memory module — tool invocations and report archive flow."""

import sqlite3
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from src.memory.baselines import compute_baselines
from src.memory.models import BaselineRecord
from src.memory.store import (
    get_connection,
    get_initialized_connection,
    init_schema,
    is_memory_configured,
    save_baselines,
    save_incident,
    save_report,
    search_incidents,
)
from src.memory.tools import (
    memory_check_baseline,
    memory_get_previous_report,
    memory_record_incident,
    memory_search_incidents,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with schema initialized."""
    conn = get_connection(":memory:")
    init_schema(conn)
    return conn


def _prom_response(result: list[dict[str, object]]) -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _prom_scalar(value: float) -> list[dict[str, object]]:
    return [{"metric": {}, "value": [1708300000, str(value)]}]


# ---------------------------------------------------------------------------
# Configuration tests
# ---------------------------------------------------------------------------


class TestMemoryConfiguration:
    def test_is_configured_when_set(self, mock_settings: Any) -> None:
        mock_settings.memory_db_path = "/tmp/test-memory.db"
        assert is_memory_configured() is True

    def test_is_not_configured_when_empty(self, mock_settings: Any) -> None:
        mock_settings.memory_db_path = ""
        assert is_memory_configured() is False

    def test_get_connection_raises_when_not_configured(self, mock_settings: Any) -> None:
        mock_settings.memory_db_path = ""
        with pytest.raises(ValueError, match="not configured"):
            get_connection()


# ---------------------------------------------------------------------------
# Tool invocation tests
# ---------------------------------------------------------------------------


class TestSearchIncidentsTool:
    def test_search_with_results(self, mock_settings: Any) -> None:
        conn = _make_conn()
        save_incident(conn, title="DNS outage", description="CoreDNS crashed", services="coredns")

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_search_incidents.invoke({"query": "DNS"})

        assert "DNS outage" in result
        assert "CoreDNS crashed" in result

    def test_search_no_results(self, mock_settings: Any) -> None:
        conn = _make_conn()

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_search_incidents.invoke({"query": "nonexistent"})

        assert "No matching incidents" in result

    def test_search_not_configured(self, mock_settings: Any) -> None:
        with patch("src.memory.tools.get_initialized_connection", side_effect=ValueError("not configured")):
            result = memory_search_incidents.invoke({"query": "test"})
        assert "not configured" in result


class TestRecordIncidentTool:
    def test_record_success(self, mock_settings: Any, tmp_path: Any) -> None:
        db_path = str(tmp_path / "test.db")
        conn = get_connection(db_path)
        init_schema(conn)
        conn.close()

        def _connect() -> sqlite3.Connection:
            return get_initialized_connection(db_path)

        with patch("src.memory.tools.get_initialized_connection", side_effect=_connect):
            result = memory_record_incident.invoke(
                {
                    "title": "Traefik 502s",
                    "description": "Backend service unreachable",
                    "severity": "warning",
                    "services": "traefik",
                }
            )

        assert "recorded successfully" in result
        assert "Traefik 502s" in result

        # Verify it was actually saved (separate connection)
        verify_conn = get_connection(db_path)
        incidents = search_incidents(verify_conn, query="Traefik")
        verify_conn.close()
        assert len(incidents) == 1


class TestGetPreviousReportTool:
    def test_get_single_report(self, mock_settings: Any) -> None:
        conn = _make_conn()
        save_report(
            conn,
            generated_at="2026-02-19T08:00:00+00:00",
            lookback_days=7,
            report_markdown="# Weekly Report\n\nAll clear.",
            report_data="{}",
        )

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_get_previous_report.invoke({"count": 1})

        assert "Weekly Report" in result
        assert "All clear." in result

    def test_get_multiple_reports(self, mock_settings: Any) -> None:
        conn = _make_conn()
        for i in range(3):
            save_report(
                conn,
                generated_at=f"2026-02-{17 + i}T08:00:00+00:00",
                lookback_days=7,
                report_markdown=f"# Report {i}",
                report_data="{}",
                active_alerts=i,
            )

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_get_previous_report.invoke({"count": 3})

        assert "Found 3 report(s)" in result

    def test_no_reports(self, mock_settings: Any) -> None:
        conn = _make_conn()

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_get_previous_report.invoke({"count": 1})

        assert "No previous reports" in result


class TestCheckBaselineTool:
    def test_within_range(self, mock_settings: Any) -> None:
        conn = _make_conn()
        save_baselines(
            conn,
            [
                BaselineRecord(
                    id=0,
                    metric_name="node_cpu_usage_ratio",
                    labels="{}",
                    avg_value=0.45,
                    p95_value=0.85,
                    min_value=0.10,
                    max_value=0.95,
                    sample_count=100,
                    window_days=7,
                    computed_at="2026-02-19T08:00:00+00:00",
                )
            ],
        )

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_check_baseline.invoke(
                {
                    "metric_name": "node_cpu_usage_ratio",
                    "current_value": 0.50,
                }
            )

        assert "WITHIN NORMAL RANGE" in result

    def test_above_p95(self, mock_settings: Any) -> None:
        conn = _make_conn()
        save_baselines(
            conn,
            [
                BaselineRecord(
                    id=0,
                    metric_name="cpu",
                    labels="{}",
                    avg_value=0.45,
                    p95_value=0.85,
                    min_value=0.10,
                    max_value=0.95,
                    sample_count=100,
                    window_days=7,
                    computed_at="2026-02-19T08:00:00+00:00",
                )
            ],
        )

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_check_baseline.invoke(
                {
                    "metric_name": "cpu",
                    "current_value": 0.90,
                }
            )

        assert "ABOVE P95" in result

    def test_below_min(self, mock_settings: Any) -> None:
        conn = _make_conn()
        save_baselines(
            conn,
            [
                BaselineRecord(
                    id=0,
                    metric_name="cpu",
                    labels="{}",
                    avg_value=0.45,
                    p95_value=0.85,
                    min_value=0.10,
                    max_value=0.95,
                    sample_count=100,
                    window_days=7,
                    computed_at="2026-02-19T08:00:00+00:00",
                )
            ],
        )

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_check_baseline.invoke(
                {
                    "metric_name": "cpu",
                    "current_value": 0.05,
                }
            )

        assert "BELOW MIN" in result

    def test_no_baseline(self, mock_settings: Any) -> None:
        conn = _make_conn()

        with patch("src.memory.tools.get_initialized_connection", return_value=conn):
            result = memory_check_baseline.invoke(
                {
                    "metric_name": "unknown_metric",
                    "current_value": 42.0,
                }
            )

        assert "No baseline found" in result


# ---------------------------------------------------------------------------
# Baseline computation tests
# ---------------------------------------------------------------------------


class TestComputeBaselines:
    @respx.mock
    async def test_compute_success(self, mock_settings: Any) -> None:
        # Mock Prometheus for each baseline metric query
        respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json=_prom_response(_prom_scalar(0.45)))
        )

        baselines = await compute_baselines(7)

        assert len(baselines) > 0
        assert all(b["avg_value"] == 0.45 for b in baselines)
        assert all(b["window_days"] == 7 for b in baselines)

    @respx.mock
    async def test_baseline_queries_are_valid_and_exclude_client_errors(self, mock_settings: Any) -> None:
        """Each query reaches Prometheus with single braces, and tool success leaves out client mistakes."""
        route = respx.get("http://prometheus.test:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json=_prom_response(_prom_scalar(0.45)))
        )

        _ = await compute_baselines(7)

        queries = [call.request.url.params["query"] for call in route.calls]
        assert queries
        assert not [q for q in queries if "{{" in q or "}}" in q or "{window}" in q]
        [tool_success] = [q for q in queries if "sre_assistant_tool_calls_total" in q]
        assert tool_success == (
            'sum(increase(sre_assistant_tool_calls_total{status="success"}[7d]))'
            ' / sum(increase(sre_assistant_tool_calls_total{status!~"invalid_args|unknown_tool"}[7d]))'
        )

    @respx.mock
    async def test_compute_prometheus_down(self, mock_settings: Any) -> None:
        respx.get("http://prometheus.test:9090/api/v1/query").mock(return_value=httpx.Response(503))

        baselines = await compute_baselines(7)

        assert baselines == []


# ---------------------------------------------------------------------------
# Report archive integration tests
# ---------------------------------------------------------------------------


class TestReportArchiveFlow:
    def test_graceful_when_memory_disabled(self, mock_settings: Any) -> None:
        """Report archive functions should not crash when memory is not configured."""
        mock_settings.memory_db_path = ""

        from src.report.generator import _archive_report, _load_previous_report

        # _load_previous_report should return None
        assert _load_previous_report() is None

        # _archive_report should silently do nothing
        from src.report.generator import ReportData

        data = ReportData(
            generated_at="2026-02-19T08:00:00+00:00",
            lookback_days=7,
            alerts=None,
            slo_status=None,
            tool_usage=None,
            cost=None,
            loki_errors=None,
            narrative="test",
        )
        _archive_report(data, "# Test")  # Should not raise
