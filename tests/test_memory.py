"""Unit tests for the memory store — pure function tests with in-memory SQLite."""

import json
import sqlite3

from src.memory.models import BaselineRecord
from src.memory.store import (
    _extract_report_metrics,
    cleanup_old_query_patterns,
    get_baseline,
    get_baselines_for_metric,
    get_connection,
    get_latest_report,
    get_open_incidents,
    get_recent_query_patterns,
    get_reports,
    init_schema,
    save_baselines,
    save_incident,
    save_query_pattern,
    save_report,
    search_incidents,
    update_incident,
)


def _make_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with schema initialized."""
    conn = get_connection(":memory:")
    init_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemaInit:
    def test_creates_tables(self) -> None:
        conn = _make_conn()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {row["name"] for row in tables}
        assert "reports" in table_names
        assert "incidents" in table_names
        assert "metric_baselines" in table_names
        assert "query_patterns" in table_names

    def test_idempotent(self) -> None:
        conn = _make_conn()
        # Second call should not raise
        init_schema(conn)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        expected = {"reports", "incidents", "metric_baselines", "query_patterns"}
        assert len([r for r in tables if r["name"] in expected]) == 4

    def test_creates_indexes(self) -> None:
        conn = _make_conn()
        indexes = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        index_names = {row["name"] for row in indexes}
        assert "idx_reports_generated" in index_names
        assert "idx_incidents_alert" in index_names
        assert "idx_incidents_created" in index_names
        assert "idx_baselines_lookup" in index_names
        assert "idx_patterns_created" in index_names


# ---------------------------------------------------------------------------
# Report CRUD tests
# ---------------------------------------------------------------------------


class TestReportCrud:
    def test_save_and_retrieve(self) -> None:
        conn = _make_conn()
        row_id = save_report(
            conn,
            generated_at="2026-02-19T08:00:00+00:00",
            lookback_days=7,
            report_markdown="# Test Report",
            report_data='{"alerts": null}',
            active_alerts=2,
            slo_failures=1,
            total_log_errors=100,
            estimated_cost=0.05,
        )
        assert row_id > 0

        report = get_latest_report(conn)
        assert report is not None
        assert report["generated_at"] == "2026-02-19T08:00:00+00:00"
        assert report["lookback_days"] == 7
        assert report["report_markdown"] == "# Test Report"
        assert report["active_alerts"] == 2
        assert report["estimated_cost"] == 0.05

    def test_get_latest_returns_most_recent(self) -> None:
        conn = _make_conn()
        save_report(
            conn,
            generated_at="2026-02-12T08:00:00+00:00",
            lookback_days=7,
            report_markdown="# Older Report",
            report_data="{}",
        )
        save_report(
            conn,
            generated_at="2026-02-19T08:00:00+00:00",
            lookback_days=7,
            report_markdown="# Newer Report",
            report_data="{}",
        )

        report = get_latest_report(conn)
        assert report is not None
        assert report["report_markdown"] == "# Newer Report"

    def test_get_reports_returns_ordered(self) -> None:
        conn = _make_conn()
        for i in range(5):
            save_report(
                conn,
                generated_at=f"2026-02-{10 + i}T08:00:00+00:00",
                lookback_days=7,
                report_markdown=f"# Report {i}",
                report_data="{}",
            )

        reports = get_reports(conn, limit=3)
        assert len(reports) == 3
        # Most recent first
        assert reports[0]["generated_at"] > reports[1]["generated_at"]

    def test_empty_db_returns_none(self) -> None:
        conn = _make_conn()
        assert get_latest_report(conn) is None
        assert get_reports(conn) == []


# ---------------------------------------------------------------------------
# Incident CRUD tests
# ---------------------------------------------------------------------------


class TestIncidentCrud:
    def test_save_and_search(self) -> None:
        conn = _make_conn()
        inc_id = save_incident(
            conn,
            title="High CPU on media VM",
            description="CPU spiked to 95% for 2 hours",
            alert_name="HighCPU",
            root_cause="Jellyfin transcoding 4K stream",
            resolution="Added hardware acceleration",
            severity="warning",
            services="jellyfin,media",
        )
        assert inc_id > 0

        results = search_incidents(conn, query="CPU")
        assert len(results) == 1
        assert results[0]["title"] == "High CPU on media VM"
        assert results[0]["root_cause"] == "Jellyfin transcoding 4K stream"

    def test_search_by_alert_name(self) -> None:
        conn = _make_conn()
        save_incident(conn, title="Incident A", description="...", alert_name="DiskFull")
        save_incident(conn, title="Incident B", description="...", alert_name="HighCPU")

        results = search_incidents(conn, alert_name="DiskFull")
        assert len(results) == 1
        assert results[0]["title"] == "Incident A"

    def test_search_by_service(self) -> None:
        conn = _make_conn()
        save_incident(conn, title="Traefik 502s", description="...", services="traefik")
        save_incident(conn, title="Jellyfin slow", description="...", services="jellyfin")

        results = search_incidents(conn, service="traefik")
        assert len(results) == 1
        assert results[0]["title"] == "Traefik 502s"

    def test_search_no_results(self) -> None:
        conn = _make_conn()
        results = search_incidents(conn, query="nonexistent")
        assert results == []

    def test_update_incident(self) -> None:
        conn = _make_conn()
        inc_id = save_incident(conn, title="Open incident", description="Under investigation")

        update_incident(conn, inc_id, root_cause="Config error", resolved_at="2026-02-20T12:00:00+00:00")

        results = search_incidents(conn, query="Open incident")
        assert len(results) == 1
        assert results[0]["root_cause"] == "Config error"
        assert results[0]["resolved_at"] == "2026-02-20T12:00:00+00:00"

    def test_get_open_incidents(self) -> None:
        conn = _make_conn()
        save_incident(conn, title="Open", description="...")
        inc2 = save_incident(conn, title="Resolved", description="...")
        update_incident(conn, inc2, resolved_at="2026-02-20T12:00:00+00:00")

        open_incidents = get_open_incidents(conn)
        assert len(open_incidents) == 1
        assert open_incidents[0]["title"] == "Open"

    def test_search_with_multiple_filters(self) -> None:
        conn = _make_conn()
        save_incident(conn, title="CPU alert traefik", description="high cpu", alert_name="HighCPU", services="traefik")
        save_incident(
            conn, title="Disk alert traefik", description="disk full", alert_name="DiskFull", services="traefik"
        )

        # Both keyword and alert_name filter
        results = search_incidents(conn, query="cpu", alert_name="HighCPU")
        assert len(results) == 1
        assert results[0]["title"] == "CPU alert traefik"

    def test_search_limit(self) -> None:
        conn = _make_conn()
        for i in range(10):
            save_incident(conn, title=f"Incident {i}", description="test")

        results = search_incidents(conn, limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Baseline CRUD tests
# ---------------------------------------------------------------------------


class TestBaselineCrud:
    def test_save_and_retrieve(self) -> None:
        conn = _make_conn()
        baselines = [
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
        ]
        save_baselines(conn, baselines)

        result = get_baseline(conn, "node_cpu_usage_ratio")
        assert result is not None
        assert result["avg_value"] == 0.45
        assert result["p95_value"] == 0.85
        assert result["window_days"] == 7

    def test_get_latest_baseline(self) -> None:
        conn = _make_conn()
        baselines = [
            BaselineRecord(
                id=0,
                metric_name="cpu",
                labels="{}",
                avg_value=0.40,
                p95_value=None,
                min_value=None,
                max_value=None,
                sample_count=50,
                window_days=7,
                computed_at="2026-02-12T08:00:00+00:00",
            ),
            BaselineRecord(
                id=0,
                metric_name="cpu",
                labels="{}",
                avg_value=0.50,
                p95_value=None,
                min_value=None,
                max_value=None,
                sample_count=50,
                window_days=7,
                computed_at="2026-02-19T08:00:00+00:00",
            ),
        ]
        save_baselines(conn, baselines)

        result = get_baseline(conn, "cpu")
        assert result is not None
        # Most recent
        assert result["avg_value"] == 0.50

    def test_get_baseline_with_labels(self) -> None:
        conn = _make_conn()
        baselines = [
            BaselineRecord(
                id=0,
                metric_name="cpu",
                labels='{"hostname": "media"}',
                avg_value=0.60,
                p95_value=None,
                min_value=None,
                max_value=None,
                sample_count=50,
                window_days=7,
                computed_at="2026-02-19T08:00:00+00:00",
            ),
            BaselineRecord(
                id=0,
                metric_name="cpu",
                labels='{"hostname": "infra"}',
                avg_value=0.30,
                p95_value=None,
                min_value=None,
                max_value=None,
                sample_count=50,
                window_days=7,
                computed_at="2026-02-19T08:00:00+00:00",
            ),
        ]
        save_baselines(conn, baselines)

        result = get_baseline(conn, "cpu", labels='{"hostname": "media"}')
        assert result is not None
        assert result["avg_value"] == 0.60

    def test_no_baseline_returns_none(self) -> None:
        conn = _make_conn()
        assert get_baseline(conn, "nonexistent") is None

    def test_get_baselines_for_metric(self) -> None:
        conn = _make_conn()
        baselines = [
            BaselineRecord(
                id=0,
                metric_name="cpu",
                labels="{}",
                avg_value=float(i) / 10,
                p95_value=None,
                min_value=None,
                max_value=None,
                sample_count=50,
                window_days=7,
                computed_at=f"2026-02-{10 + i}T08:00:00+00:00",
            )
            for i in range(5)
        ]
        save_baselines(conn, baselines)

        results = get_baselines_for_metric(conn, "cpu", limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Query pattern CRUD tests
# ---------------------------------------------------------------------------


class TestQueryPatternCrud:
    def test_save_and_retrieve(self) -> None:
        conn = _make_conn()
        row_id = save_query_pattern(conn, question="What is CPU on media?", tool_names="prometheus_instant_query")
        assert row_id > 0

        patterns = get_recent_query_patterns(conn, limit=10)
        assert len(patterns) == 1
        assert patterns[0]["question"] == "What is CPU on media?"
        assert patterns[0]["tool_names"] == "prometheus_instant_query"

    def test_truncates_long_questions(self) -> None:
        conn = _make_conn()
        long_question = "x" * 500
        save_query_pattern(conn, question=long_question, tool_names="")

        patterns = get_recent_query_patterns(conn, limit=1)
        assert len(patterns[0]["question"]) == 200

    def test_get_recent_returns_ordered(self) -> None:
        conn = _make_conn()
        for i in range(5):
            save_query_pattern(conn, question=f"Question {i}", tool_names=f"tool_{i}")

        patterns = get_recent_query_patterns(conn, limit=3)
        assert len(patterns) == 3
        # Most recent first
        assert patterns[0]["question"] == "Question 4"

    def test_cleanup_old_patterns(self) -> None:
        conn = _make_conn()
        for i in range(20):
            save_query_pattern(conn, question=f"Question {i}", tool_names="")

        deleted = cleanup_old_query_patterns(conn, keep=5)
        assert deleted == 15

        remaining = get_recent_query_patterns(conn, limit=100)
        assert len(remaining) == 5

    def test_empty_db(self) -> None:
        conn = _make_conn()
        patterns = get_recent_query_patterns(conn, limit=10)
        assert patterns == []


# ---------------------------------------------------------------------------
# Report metrics extraction
# ---------------------------------------------------------------------------


class TestExtractReportMetrics:
    def test_full_data(self) -> None:
        data = {
            "alerts": {"active_alerts": 3},
            "slo_status": {
                # p95 latency is tracked but no longer counts toward slo_failures
                "p95_latency_seconds": 20.0,
                "tool_success_rate": 0.98,  # < 0.99 = fail
                "llm_error_rate": 0.005,  # < 0.01 = pass
                "availability": 0.999,  # > 0.995 = pass
            },
            "loki_errors": {"total_errors": 250},
            "cost": {"estimated_cost_usd": 0.12},
        }
        metrics = _extract_report_metrics(json.dumps(data))
        assert metrics["active_alerts"] == 3
        assert metrics["slo_failures"] == 1  # only tool_success_rate
        assert metrics["total_log_errors"] == 250
        assert metrics["estimated_cost"] == 0.12

    def test_empty_data(self) -> None:
        metrics = _extract_report_metrics("{}")
        assert metrics["active_alerts"] == 0
        assert metrics["slo_failures"] == 0

    def test_invalid_json(self) -> None:
        metrics = _extract_report_metrics("not json")
        assert metrics["active_alerts"] == 0

    def test_null_sections(self) -> None:
        data = {"alerts": None, "slo_status": None, "loki_errors": None, "cost": None}
        metrics = _extract_report_metrics(json.dumps(data))
        assert metrics["active_alerts"] == 0
        assert metrics["slo_failures"] == 0
