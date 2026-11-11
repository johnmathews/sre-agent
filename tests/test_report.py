"""Unit tests for the report module — pure function tests, no I/O (except the respx-mocked SLI query test)."""

import re
from typing import Any

import httpx
import pytest
import respx

from src.report.email import is_email_configured
from src.report.generator import (
    AlertSummaryData,
    BackupGroupHealth,
    BackupHealthData,
    CostData,
    DatastoreHealth,
    LokiErrorSummary,
    ReportData,
    SLOStatusData,
    ToolUsageData,
    _aggregate_by_normalized_name,
    _collect_slo_status,
    _collect_tool_usage,
    _format_slo_row,
    _normalize_service_name,
    format_report_html,
    format_report_markdown,
)


def _complete_report_data() -> ReportData:
    """Return a complete ReportData fixture with all sections populated."""
    return ReportData(
        generated_at="2026-02-19T08:00:00+00:00",
        lookback_days=7,
        narrative="Everything looks healthy this week. No SLO violations detected.",
        alerts=AlertSummaryData(
            total_rules=12,
            active_alerts=2,
            alerts_by_severity={"critical": 1, "warning": 1},
            active_alert_names=["HighCPU", "DiskSpaceLow"],
        ),
        slo_status=SLOStatusData(
            p95_latency_seconds=3.2,
            tool_success_rate=0.997,
            llm_error_rate=0.005,
            availability=0.998,
        ),
        tool_usage=ToolUsageData(
            tool_calls={"prometheus_query": 150, "grafana_alerts": 45},
            tool_errors={"prometheus_query": 2},
        ),
        cost=CostData(
            prompt_tokens=50000,
            completion_tokens=15000,
            total_tokens=65000,
            estimated_cost_usd=0.1234,
        ),
        loki_errors=LokiErrorSummary(
            errors_by_service={"traefik": 120, "jellyfin": 5},
            total_errors=125,
        ),
    )


class TestFormatReportMarkdown:
    def test_complete_report_has_all_sections(self) -> None:
        data = _complete_report_data()
        md = format_report_markdown(data)

        assert "# Weekly Reliability Report" in md
        assert "## Executive Summary" in md
        assert "## Alert Summary" in md
        assert "## SLO Status" in md
        assert "## Tool Usage" in md
        assert "## Cost & Token Usage" in md
        assert "## Log Error Summary" in md

    def test_complete_report_includes_alert_details(self) -> None:
        data = _complete_report_data()
        md = format_report_markdown(data)

        assert "Total alert rules:** 12" in md
        assert "Currently active:** 2" in md
        assert "HighCPU" in md
        assert "critical: 1" in md

    def test_complete_report_includes_slo_table(self) -> None:
        data = _complete_report_data()
        md = format_report_markdown(data)

        assert "P95 Latency" in md
        assert "Tool Success Rate" in md
        assert "PASS" in md

    def test_complete_report_includes_tool_usage(self) -> None:
        data = _complete_report_data()
        md = format_report_markdown(data)

        assert "prometheus_query" in md
        assert "150" in md

    def test_complete_report_includes_cost(self) -> None:
        data = _complete_report_data()
        md = format_report_markdown(data)

        assert "50,000" in md
        assert "$0.1234" in md

    def test_complete_report_includes_loki_errors(self) -> None:
        data = _complete_report_data()
        md = format_report_markdown(data)

        assert "traefik" in md
        assert "120" in md
        assert "Total errors/critical logs:** 125" in md

    def test_partial_data_none_sections(self) -> None:
        data = ReportData(
            generated_at="2026-02-19T08:00:00+00:00",
            lookback_days=7,
            narrative="Partial data only.",
            alerts=None,
            slo_status=None,
            tool_usage=None,
            cost=None,
            loki_errors=None,
        )
        md = format_report_markdown(data)

        assert "Alert data unavailable" in md
        assert "SLO data unavailable" in md
        assert "Tool usage data unavailable" in md
        assert "Cost data unavailable" in md
        # Loki section should be omitted entirely when None
        assert "Log Error Summary" not in md

    def test_empty_tool_calls(self) -> None:
        data = _complete_report_data()
        data["tool_usage"] = ToolUsageData(tool_calls={}, tool_errors={})
        md = format_report_markdown(data)

        assert "No tool calls recorded" in md

    def test_no_active_alerts(self) -> None:
        data = _complete_report_data()
        data["alerts"] = AlertSummaryData(
            total_rules=10,
            active_alerts=0,
            alerts_by_severity={},
            active_alert_names=[],
        )
        md = format_report_markdown(data)

        assert "Currently active:** 0" in md
        assert "Active alerts:" not in md


class TestFormatSloRow:
    def test_pass_higher_is_better(self) -> None:
        row = _format_slo_row("Tool Success Rate", "> 99%", 0.997)
        assert "PASS" in row

    def test_fail_higher_is_better(self) -> None:
        row = _format_slo_row("Tool Success Rate", "> 99%", 0.98)
        assert "FAIL" in row

    def test_pass_lower_is_better(self) -> None:
        row = _format_slo_row("P95 Latency", "< 15s", 3.2, higher_is_better=False)
        assert "PASS" in row

    def test_fail_lower_is_better(self) -> None:
        row = _format_slo_row("P95 Latency", "< 15s", 20.0, higher_is_better=False)
        assert "FAIL" in row

    def test_none_value(self) -> None:
        row = _format_slo_row("Availability", "> 99.5%", None)
        assert "N/A" in row


class TestNormalizeServiceName:
    def test_hyphen_to_underscore(self) -> None:
        assert _normalize_service_name("node-exporter") == "node_exporter"

    def test_already_underscore(self) -> None:
        assert _normalize_service_name("node_exporter") == "node_exporter"

    def test_no_separator(self) -> None:
        assert _normalize_service_name("traefik") == "traefik"


class TestAggregateByNormalizedName:
    def test_merges_hyphen_underscore_variants(self) -> None:
        raw = {"node_exporter": 582, "node-exporter": 14}
        merged = _aggregate_by_normalized_name(raw)
        # node_exporter has the higher count, so it's the canonical name
        assert merged == {"node_exporter": 596}

    def test_no_duplicates_unchanged(self) -> None:
        raw = {"traefik": 120, "jellyfin": 5}
        assert _aggregate_by_normalized_name(raw) == raw

    def test_empty(self) -> None:
        assert _aggregate_by_normalized_name({}) == {}


class TestFormatSloRowHumanReadable:
    """Tests that _format_slo_row produces human-readable values with units."""

    def test_percentage_target_shows_percent(self) -> None:
        row = _format_slo_row("Availability", "> 99.5%", 0.9997)
        assert "99.97%" in row

    def test_seconds_target_shows_seconds(self) -> None:
        row = _format_slo_row("P95 Latency", "< 15s", 16.5, higher_is_better=False)
        assert "16.50s" in row

    def test_no_target_with_explicit_unit(self) -> None:
        # Latency is now tracked without a fixed SLO; the row still renders the
        # value in seconds and reports a non-PASS/FAIL status.
        row = _format_slo_row("P95 Latency", "—", 12.3, actual_unit="s")
        assert "12.30s" in row
        assert "—" in row
        assert "PASS" not in row
        assert "FAIL" not in row


class TestFormatLokiWithDelta:
    def test_week_over_week_delta_up(self) -> None:
        data = _complete_report_data()
        data["loki_errors"] = LokiErrorSummary(
            errors_by_service={"traefik": 120},
            total_errors=120,
            previous_total_errors=80,
            previous_errors_by_service={"traefik": 80},
        )
        md = format_report_markdown(data)
        assert "up 40" in md
        assert "vs Prev" in md

    def test_week_over_week_delta_down(self) -> None:
        data = _complete_report_data()
        data["loki_errors"] = LokiErrorSummary(
            errors_by_service={"traefik": 50},
            total_errors=50,
            previous_total_errors=100,
            previous_errors_by_service={"traefik": 100},
        )
        md = format_report_markdown(data)
        assert "down 50" in md

    def test_new_service_delta(self) -> None:
        data = _complete_report_data()
        data["loki_errors"] = LokiErrorSummary(
            errors_by_service={"traefik": 120},
            total_errors=120,
            previous_total_errors=0,
            previous_errors_by_service={},
        )
        md = format_report_markdown(data)
        assert "new" in md

    def test_no_previous_data_omits_delta_column(self) -> None:
        data = _complete_report_data()
        data["loki_errors"] = LokiErrorSummary(
            errors_by_service={"traefik": 120},
            total_errors=120,
        )
        md = format_report_markdown(data)
        assert "vs Prev" not in md


class TestFormatErrorSamples:
    def test_error_samples_shown(self) -> None:
        data = _complete_report_data()
        data["loki_errors"] = LokiErrorSummary(
            errors_by_service={"traefik": 120},
            total_errors=120,
            error_samples={"traefik": 'level=error msg="502 Bad Gateway"'},
        )
        md = format_report_markdown(data)
        assert "Top error samples:" in md
        assert "502 Bad Gateway" in md


class TestFormatComponentAvailability:
    def test_degraded_components_shown(self) -> None:
        data = _complete_report_data()
        data["slo_status"] = SLOStatusData(
            p95_latency_seconds=3.2,
            tool_success_rate=0.997,
            llm_error_rate=0.005,
            availability=0.998,
            component_availability={"prometheus": 1.0, "grafana": 0.995, "loki": 0.99},
        )
        md = format_report_markdown(data)
        assert "Components with degraded availability:" in md
        assert "grafana" in md
        assert "loki" in md
        # prometheus is at 100%, should not be listed as degraded
        assert "prometheus: 100" not in md

    def test_all_components_healthy(self) -> None:
        data = _complete_report_data()
        data["slo_status"] = SLOStatusData(
            p95_latency_seconds=3.2,
            tool_success_rate=0.997,
            llm_error_rate=0.005,
            availability=1.0,
            component_availability={"prometheus": 1.0, "grafana": 1.0},
        )
        md = format_report_markdown(data)
        assert "All components at 100% availability." in md


class TestFormatBackupHealth:
    def test_backup_section_shown(self) -> None:
        data = _complete_report_data()
        now_ts = int(__import__("datetime").datetime.now(__import__("datetime").UTC).timestamp())
        data["backup_health"] = BackupHealthData(
            datastores=[
                DatastoreHealth(
                    store="backups",
                    total_bytes=2 * 1024**4,
                    used_bytes=1024**4,
                    usage_percent=50.0,
                )
            ],
            backups=[
                BackupGroupHealth(
                    backup_type="vm",
                    backup_id="100",
                    last_backup_ts=now_ts - 3600,  # 1h ago, fresh
                    backup_count=10,
                    stale=False,
                ),
                BackupGroupHealth(
                    backup_type="ct",
                    backup_id="200",
                    last_backup_ts=now_ts - 172800,  # 48h ago, stale
                    backup_count=5,
                    stale=True,
                ),
            ],
            stale_count=1,
            total_count=2,
        )
        md = format_report_markdown(data)
        assert "## Backup Health" in md
        assert "backups:" in md
        assert "50.0% used" in md
        assert "2 total, 1 stale" in md
        assert "CT/200" in md

    def test_backup_section_omitted_when_none(self) -> None:
        data = _complete_report_data()
        # backup_health not set (NotRequired field)
        md = format_report_markdown(data)
        assert "Backup Health" not in md

    def test_all_backups_fresh(self) -> None:
        data = _complete_report_data()
        now_ts = int(__import__("datetime").datetime.now(__import__("datetime").UTC).timestamp())
        data["backup_health"] = BackupHealthData(
            datastores=[
                DatastoreHealth(store="backups", total_bytes=1024**4, used_bytes=512 * 1024**3, usage_percent=50.0)
            ],
            backups=[
                BackupGroupHealth(
                    backup_type="vm", backup_id="100", last_backup_ts=now_ts - 3600, backup_count=10, stale=False
                )
            ],
            stale_count=0,
            total_count=1,
        )
        md = format_report_markdown(data)
        assert "All backups are fresh" in md


class TestFormatReportHtml:
    def test_complete_report_html(self) -> None:
        """HTML output contains all sections and proper structure."""
        data = _complete_report_data()
        html = format_report_html(data)
        assert "<!DOCTYPE html>" in html
        assert "Weekly Reliability Report" in html
        assert "Executive Summary" in html
        assert "Alert Summary" in html
        assert "SLO Status" in html
        assert "Tool Usage" in html
        assert "Cost &amp; Token Usage" in html
        assert "<table" in html

    def test_html_pass_fail_badges(self) -> None:
        """SLO table includes styled PASS/FAIL badges."""
        data = _complete_report_data()
        html = format_report_html(data)
        assert "PASS" in html
        assert "#d4edda" in html  # PASS badge background

    def test_html_escapes_content(self) -> None:
        """User-provided content is HTML-escaped."""
        data = _complete_report_data()
        data["narrative"] = "- Alert <script>alert(1)</script> status is fine"
        html = format_report_html(data)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_missing_sections(self) -> None:
        """HTML renders gracefully when data sections are None."""
        data = _complete_report_data()
        data["alerts"] = None
        data["slo_status"] = None
        data["tool_usage"] = None
        data["cost"] = None
        html = format_report_html(data)
        assert "Alert data unavailable" in html
        assert "SLO data unavailable" in html
        assert "Tool usage data unavailable" in html
        assert "Cost data unavailable" in html


class TestIsEmailConfigured:
    def test_all_fields_set(self, mock_settings: Any) -> None:
        assert is_email_configured() is True

    def test_missing_host(self, mock_settings: Any) -> None:
        mock_settings.smtp_host = ""
        assert is_email_configured() is False

    def test_missing_username(self, mock_settings: Any) -> None:
        mock_settings.smtp_username = ""
        assert is_email_configured() is False

    def test_missing_password(self, mock_settings: Any) -> None:
        mock_settings.smtp_password = ""
        assert is_email_configured() is False

    def test_missing_recipient(self, mock_settings: Any) -> None:
        mock_settings.report_recipient_email = ""
        assert is_email_configured() is False


# ---------------------------------------------------------------------------
# Tool success excludes client errors
# ---------------------------------------------------------------------------

# Calls in the window by (tool_name, status). invalid_args and unknown_tool are the client's mistakes.
_TOOL_CALLS: dict[tuple[str, str], float] = {
    ("prometheus_instant_query", "success"): 95.0,
    ("prometheus_instant_query", "timeout"): 1.0,
    ("prometheus_instant_query", "invalid_args"): 40.0,
    ("grafana_get_alerts", "error"): 3.0,
    ("grafana_get_alerts", "internal"): 1.0,
    ("unknown", "unknown_tool"): 10.0,
}
_SELECTOR = re.compile(r"sre_assistant_tool_calls_total(?:\{([^}]*)\})?\[")
_MATCHER = re.compile(r'(\w+)(=~|!~|!=|=)"([^"]*)"')


def _matches(op: str, actual: str, wanted: str) -> bool:
    if op == "=":
        return actual == wanted
    if op == "!=":
        return actual != wanted
    hit = re.fullmatch(wanted, actual) is not None
    return hit if op == "=~" else not hit


def _fake_prometheus(request: httpx.Request) -> httpx.Response:
    """Answer ``sum[ by (tool_name)](increase(sre_assistant_tool_calls_total{...}[w]))`` from ``_TOOL_CALLS``."""
    query = request.url.params["query"]
    selector = _SELECTOR.search(query)
    result: list[dict[str, object]] = []
    if selector:
        matchers = _MATCHER.findall(selector.group(1) or "")
        sums: dict[str, float] = {}
        for (tool, status), value in _TOOL_CALLS.items():
            labels = {"tool_name": tool, "status": status}
            if all(_matches(op, labels[name], wanted) for name, op, wanted in matchers):
                key = tool if "by (tool_name)" in query else ""
                sums[key] = sums.get(key, 0.0) + value
        result = [
            {"metric": {"tool_name": key} if key else {}, "value": [1708300000, str(value)]}
            for key, value in sums.items()
        ]
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


@pytest.mark.integration
class TestToolSuccessExcludesClientErrors:
    @respx.mock
    async def test_report_tool_success_excludes_client_errors(self, mock_settings: Any) -> None:
        """95 successes over the 100 calls that were not client mistakes: 95%, not 1 - 3/150."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(side_effect=_fake_prometheus)

        result = await _collect_slo_status(7)

        assert result["tool_success_rate"] == pytest.approx(0.95)

    @respx.mock
    async def test_report_tool_errors_are_service_failures(self, mock_settings: Any) -> None:
        """Per-tool errors are error, timeout and internal; every call still counts towards the tool's total."""
        respx.get("http://prometheus.test:9090/api/v1/query").mock(side_effect=_fake_prometheus)

        result = await _collect_tool_usage(7)

        assert result["tool_errors"] == {"prometheus_instant_query": 1, "grafana_get_alerts": 4}
        assert result["tool_calls"] == {"prometheus_instant_query": 136, "grafana_get_alerts": 4, "unknown": 10}


async def test_report_cron_runs_in_user_timezone(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import src.report.scheduler as sched_mod

    mock_settings.report_schedule_cron = "0 8 * * 1"
    mock_settings.user_timezone = "Pacific/Auckland"
    mock_settings.upgrade_advisor_cron = ""
    with caplog.at_level(logging.INFO, logger="src.report.scheduler"):
        sched_mod.start_scheduler()
    try:
        assert sched_mod._scheduler is not None
        job = sched_mod._scheduler.get_job("weekly_report")
        assert job is not None
        assert str(job.trigger.timezone) == mock_settings.user_timezone
        assert "Pacific/Auckland" in caplog.text
    finally:
        sched_mod.stop_scheduler()
