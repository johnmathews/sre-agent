"""Tests for the Prometheus tool input validation and result formatting."""

from datetime import UTC, datetime

import pytest

from src.agent.tools.prometheus import (
    MAX_RANGE_DURATION_SECONDS,
    MAX_SEARCH_RESULTS,
    TOOL_DESCRIPTION_INSTANT,
    PrometheusData,
    PrometheusMetadataEntry,
    PrometheusResponse,
    PrometheusSeries,
    _auto_step,
    _check_negative_max_over_time,
    _format_result,
    _format_search_results,
    _parse_duration,
    _parse_timestamp,
    _resolve_range,
)

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
NOW_TS = NOW.timestamp()


class TestParseTimestamp:
    def test_unix_timestamp(self) -> None:
        assert _parse_timestamp("1700000000", NOW) == 1700000000.0

    def test_rfc3339(self) -> None:
        assert _parse_timestamp("2024-01-15T10:00:00Z", NOW) == datetime(2024, 1, 15, 10, tzinfo=UTC).timestamp()

    def test_rfc3339_with_offset(self) -> None:
        assert _parse_timestamp("2024-01-15T10:00:00+01:00", NOW) == datetime(2024, 1, 15, 9, tzinfo=UTC).timestamp()

    def test_relative_forms_count_back_from_now(self) -> None:
        assert _parse_timestamp("now", NOW) == NOW_TS
        assert _parse_timestamp("24h", NOW) == NOW_TS - 86400
        assert _parse_timestamp("now-24h", NOW) == NOW_TS - 86400

    def test_invalid_names_accepted_forms(self) -> None:
        with pytest.raises(ValueError, match="now-24h") as exc:
            _ = _parse_timestamp("yesterday", NOW)
        assert "RFC3339" in str(exc.value)


class TestParseDuration:
    def test_seconds(self) -> None:
        assert _parse_duration("60s") == 60.0

    def test_minutes(self) -> None:
        assert _parse_duration("5m") == 300.0

    def test_hours(self) -> None:
        assert _parse_duration("1h") == 3600.0

    def test_days(self) -> None:
        assert _parse_duration("1d") == 86400.0

    def test_weeks(self) -> None:
        assert _parse_duration("1w") == 604800.0

    def test_numeric_string(self) -> None:
        assert _parse_duration("60") == 60.0

    def test_compound(self) -> None:
        assert _parse_duration("1h30m") == 5400.0

    def test_unparseable(self) -> None:
        assert _parse_duration("abc") is None


class TestResolveRange:
    def test_valid_params(self) -> None:
        window = _resolve_range("1700000000", "1700003600", "60s", NOW)
        assert (window.start, window.end, window.step) == (1700000000.0, 1700003600.0, 60.0)

    def test_defaults_are_last_hour(self) -> None:
        window = _resolve_range("1h", "now", None, NOW)
        assert (window.start, window.end) == (NOW_TS - 3600, NOW_TS)

    def test_range_accepts_now_minus_24h_relative_forms(self) -> None:
        for start in ("now-24h", "-24h", "24h"):
            window = _resolve_range(start, "now", None, NOW)
            assert window.end - window.start == 86400

    def test_end_before_start(self) -> None:
        with pytest.raises(ValueError, match="end must be after start"):
            _ = _resolve_range("1700003600", "1700000000", "60s", NOW)

    def test_range_too_large(self) -> None:
        with pytest.raises(ValueError, match="too large"):
            _ = _resolve_range("100d", "now", None, NOW)

    def test_range_60_days_allowed(self) -> None:
        """Retention is 90 days; the old 30-day cap rejected 60-day queries (F10)."""
        window = _resolve_range("60d", "now", None, NOW)
        assert window.end - window.start == 60 * 86400

    def test_range_90_days_exactly_allowed(self) -> None:
        window = _resolve_range("90d", "now", None, NOW)
        assert window.end - window.start == MAX_RANGE_DURATION_SECONDS

    def test_range_91_days_rejected_with_format_hint(self) -> None:
        with pytest.raises(ValueError) as exc:
            _ = _resolve_range("91d", "now", None, NOW)
        message = str(exc.value)
        assert "90 days" in message
        assert "now-24h" in message
        assert "RFC3339" in message

    def test_invalid_time_lists_accepted_forms(self) -> None:
        with pytest.raises(ValueError) as exc:
            _ = _resolve_range("last tuesday", "now", None, NOW)
        assert "now-24h" in str(exc.value)
        assert "RFC3339" in str(exc.value)

    def test_invalid_step_lists_accepted_forms(self) -> None:
        with pytest.raises(ValueError, match="step") as exc:
            _ = _resolve_range("1h", "now", "fast", NOW)
        assert "1h30m" in str(exc.value)

    def test_step_too_large(self) -> None:
        with pytest.raises(ValueError, match="Step too large"):
            _ = _resolve_range("30d", "now", "2d", NOW)

    def test_auto_step_keeps_under_point_cap(self) -> None:
        """90 days with no step must fit Prometheus's 11,000-point limit, and stay readable (<= 250 points)."""
        window = _resolve_range("90d", "now", None, NOW)
        points = (window.end - window.start) / window.step
        assert points <= 250
        assert points <= 11000

    def test_too_many_points_message_suggests_step(self) -> None:
        # 7 days at 1s = 604,800 points; the smallest step that fits 11,000 points is 55s.
        with pytest.raises(ValueError, match="oo many data points") as exc:
            _ = _resolve_range("1700000000", "1700604800", "1", NOW)
        assert "step='55s'" in str(exc.value)
        assert 604800 / 55 <= 11000


class TestAutoStep:
    @pytest.mark.parametrize(
        ("duration", "step"),
        [
            (60, 15),  # floor of 15s
            (3600, 15),  # 1h / 250 = 14.4s
            (6 * 3600, 300),  # 86.4s rounds up to 5m
            (86400, 900),  # 345.6s rounds up to 15m
            (7 * 86400, 3600),  # 2419s rounds up to 1h
            (30 * 86400, 21600),  # 10368s rounds up to 6h
            (90 * 86400, 86400),  # 31104s rounds up to 1d
        ],
    )
    def test_ladder(self, duration: float, step: int) -> None:
        assert _auto_step(duration) == step


class TestFormatResult:
    def test_empty_result(self) -> None:
        result_data: PrometheusData = {"resultType": "vector", "result": []}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        output = _format_result(data)
        assert "no results" in output.lower()

    def test_error_status(self) -> None:
        data: PrometheusResponse = {"status": "error", "error": "bad query"}
        output = _format_result(data)
        assert "bad query" in output

    def test_vector_result(self) -> None:
        series: PrometheusSeries = {"metric": {"hostname": "jellyfin", "__name__": "up"}, "value": [1700000000, "1"]}
        result_data: PrometheusData = {"resultType": "vector", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        output = _format_result(data)
        assert "jellyfin" in output
        assert "1" in output

    def test_large_result_truncated(self) -> None:
        series_list: list[PrometheusSeries] = [
            {"metric": {"hostname": f"host-{i}"}, "value": [1700000000, str(i)]} for i in range(100)
        ]
        result_data: PrometheusData = {"resultType": "vector", "result": series_list}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        output = _format_result(data)
        assert "WARNING" in output

    def test_series_cap_says_n_of_m(self) -> None:
        series_list: list[PrometheusSeries] = [
            {"metric": {"hostname": f"host-{i}"}, "value": [1700000000, str(i)]} for i in range(120)
        ]
        result_data: PrometheusData = {"resultType": "vector", "result": series_list}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        output = _format_result(data)
        assert "showing 50 of 120 series" in output
        assert "host-49" in output
        assert "host-50" not in output

    def test_empty_result_names_90_day_retention(self) -> None:
        result_data: PrometheusData = {"resultType": "matrix", "result": []}
        output = _format_result({"status": "success", "data": result_data})
        assert "90 days" in output
        assert "100 days" not in output

    def test_matrix_timestamps_include_date_and_zone(self) -> None:
        """A 30-day query used to print only [14:09:54] for every sample (F31)."""
        series: PrometheusSeries = {
            "metric": {"hostname": "media"},
            "values": [[1700000000, "1"], [1700086400, "2"]],
        }
        result_data: PrometheusData = {"resultType": "matrix", "result": [series]}
        output = _format_result({"status": "success", "data": result_data})
        assert "[2023-11-14 22:13:20Z] 1" in output
        assert "[2023-11-15 22:13:20Z] 2" in output

    def test_short_matrix_prints_each_sample_once(self) -> None:
        """Up to 6 samples print once each; the first-3 and last-3 slices used to overlap."""
        series: PrometheusSeries = {"metric": {"hostname": "media"}, "values": [[1700000000, "1"], [1700000060, "2"]]}
        result_data: PrometheusData = {"resultType": "matrix", "result": [series]}
        output = _format_result({"status": "success", "data": result_data})
        assert output.count("22:13:20Z] 1") == 1
        assert output.count("22:14:20Z] 2") == 1

    def test_container_label_hidden_unless_queried(self) -> None:
        """cadvisor's container_label_* Compose labels cost ~1.4k chars per series (F13)."""
        series: PrometheusSeries = {
            "metric": {
                "__name__": "container_memory_working_set_bytes",
                "hostname": "media",
                "name": "jellyfin",
                "container_label_com_docker_compose_project": "media",
                "container_label_com_docker_compose_service": "jellyfin",
            },
            "value": [1700000000, "123"],
        }
        other: PrometheusSeries = {
            "metric": {"hostname": "media", "name": "sonarr", "container_label_com_docker_compose_project": "media"},
            "value": [1700000000, "456"],
        }
        result_data: PrometheusData = {"resultType": "vector", "result": [series, other]}
        data: PrometheusResponse = {"status": "success", "data": result_data}

        hidden = _format_result(data, query='container_memory_working_set_bytes{hostname="media"}')
        assert 'container_label_com_docker_compose_project="' not in hidden
        assert 'name="jellyfin"' in hidden
        assert hidden.count("container_label_* labels hidden") == 1
        assert "2 cadvisor container_label_* labels hidden" in hidden

        shown = _format_result(
            data, query='container_memory_working_set_bytes{container_label_com_docker_compose_project="media"}'
        )
        assert 'container_label_com_docker_compose_service="jellyfin"' in shown
        assert "labels hidden" not in shown

    def test_matrix_result_shows_summary_stats(self) -> None:
        """Matrix results include min/max/avg so the agent sees the full range."""
        series: PrometheusSeries = {
            "metric": {"name": "ether1", "__name__": "mktxp_interface_download_bytes_per_second"},
            "values": [
                [1700000000, "-5000"],
                [1700003600, "-100000"],
                [1700007200, "-2000"],
                [1700010800, "-350000"],
                [1700014400, "-8000"],
                [1700018000, "-1500"],
                [1700021600, "-12000"],
            ],
        }
        result_data: PrometheusData = {"resultType": "matrix", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        output = _format_result(data)
        assert "7 samples" in output
        assert "min: -3.5e+05" in output
        assert "max: -1500" in output
        assert "avg:" in output

    def test_matrix_summary_stats_with_positive_values(self) -> None:
        """Summary stats work correctly with positive values."""
        series: PrometheusSeries = {
            "metric": {"hostname": "media"},
            "values": [
                [1700000000, "10"],
                [1700003600, "50"],
                [1700007200, "30"],
            ],
        }
        result_data: PrometheusData = {"resultType": "matrix", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        output = _format_result(data)
        assert "min: 10" in output
        assert "max: 50" in output
        assert "avg: 30" in output


class TestCheckNegativeMaxOverTime:
    def test_warns_on_max_over_time_with_negative_values(self) -> None:
        series: PrometheusSeries = {
            "metric": {"name": "youfone.nl"},
            "value": [1700000000, "-1234.5"],
        }
        result_data: PrometheusData = {"resultType": "vector", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        warning = _check_negative_max_over_time(
            "max_over_time(mktxp_interface_download_bytes_per_second{name='youfone.nl'}[7d])",
            data,
        )
        assert "WARNING" in warning
        assert "min_over_time" in warning

    def test_warns_on_abs_max_over_time_even_with_positive_result(self) -> None:
        """abs(max_over_time(...)) hides the sign but still returns wrong answer."""
        series: PrometheusSeries = {
            "metric": {"name": "youfone.nl"},
            "value": [1700000000, "100.5"],  # abs made it positive — still wrong
        }
        result_data: PrometheusData = {"resultType": "vector", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        warning = _check_negative_max_over_time(
            "abs(max_over_time(mktxp_interface_download_bytes_per_second{name='youfone.nl'}[7d]))",
            data,
        )
        assert "WARNING" in warning
        assert "abs() does not fix" in warning

    def test_no_warning_on_max_over_time_with_positive_values(self) -> None:
        series: PrometheusSeries = {
            "metric": {"hostname": "jellyfin"},
            "value": [1700000000, "0.85"],
        }
        result_data: PrometheusData = {"resultType": "vector", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        warning = _check_negative_max_over_time(
            "max_over_time(pve_cpu_usage_ratio{name='jellyfin'}[7d])",
            data,
        )
        assert warning == ""

    def test_no_warning_on_min_over_time_with_negative_values(self) -> None:
        """min_over_time on negative values is the correct approach — no warning."""
        series: PrometheusSeries = {
            "metric": {"name": "youfone.nl"},
            "value": [1700000000, "-50000000"],
        }
        result_data: PrometheusData = {"resultType": "vector", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        warning = _check_negative_max_over_time(
            "abs(min_over_time(mktxp_interface_download_bytes_per_second{name='youfone.nl'}[7d]))",
            data,
        )
        assert warning == ""

    def test_no_warning_on_unrelated_query(self) -> None:
        series: PrometheusSeries = {
            "metric": {"hostname": "media"},
            "value": [1700000000, "-5"],
        }
        result_data: PrometheusData = {"resultType": "vector", "result": [series]}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        warning = _check_negative_max_over_time("up{hostname='media'}", data)
        assert warning == ""

    def test_no_warning_on_empty_results(self) -> None:
        result_data: PrometheusData = {"resultType": "vector", "result": []}
        data: PrometheusResponse = {"status": "success", "data": result_data}
        warning = _check_negative_max_over_time("max_over_time(some_metric[7d])", data)
        assert warning == ""


class TestFormatSearchResults:
    def test_formats_metrics_with_metadata(self) -> None:
        names = ["mktxp_dhcp_lease_count", "mktxp_dhcp_lease_info"]
        metadata: dict[str, list[PrometheusMetadataEntry]] = {
            "mktxp_dhcp_lease_count": [{"type": "gauge", "help": "Number of active DHCP leases", "unit": ""}],
            "mktxp_dhcp_lease_info": [{"type": "gauge", "help": "DHCP lease information", "unit": ""}],
        }
        output = _format_search_results(names, metadata, "mktxp_dhcp")
        assert "Found 2 metrics" in output
        assert "mktxp_dhcp_lease_count (gauge): Number of active DHCP leases" in output
        assert "mktxp_dhcp_lease_info (gauge): DHCP lease information" in output
        assert "prometheus_instant_query" in output

    def test_empty_results_shows_suggestions(self) -> None:
        output = _format_search_results([], {}, "nonexistent")
        assert "No metrics found" in output
        assert "nonexistent" in output
        assert "node_" in output  # suggests common prefixes

    def test_truncates_at_max_search_results(self) -> None:
        names = [f"metric_{i:04d}" for i in range(80)]
        output = _format_search_results(names, {}, "metric")
        assert f"first {MAX_SEARCH_RESULTS} of 80" in output
        # Should only show MAX_SEARCH_RESULTS metric lines
        metric_lines = [line for line in output.split("\n") if line.startswith("  metric_")]
        assert len(metric_lines) == MAX_SEARCH_RESULTS

    def test_handles_missing_metadata(self) -> None:
        names = ["up", "node_cpu_seconds_total"]
        output = _format_search_results(names, {}, "node")
        assert "  up" in output
        assert "  node_cpu_seconds_total" in output
        # No parenthesized type info since metadata is empty
        assert "(gauge)" not in output

    def test_results_sorted_alphabetically(self) -> None:
        names = ["z_metric", "a_metric", "m_metric"]
        output = _format_search_results(names, {}, "metric")
        lines = [line.strip() for line in output.split("\n") if line.startswith("  ")]
        assert lines == ["a_metric", "m_metric", "z_metric"]

    def test_metadata_with_type_only(self) -> None:
        names = ["up"]
        metadata: dict[str, list[PrometheusMetadataEntry]] = {
            "up": [{"type": "gauge", "help": "", "unit": ""}],
        }
        output = _format_search_results(names, metadata, "up")
        assert "up (gauge)" in output
        # No trailing colon when help is empty
        assert "up (gauge):" not in output


class TestInstantDescription:
    def test_names_exported_hostname_and_container_status(self) -> None:
        """Container-status series carry hostname="infra"; a raw hostname="media" query comes back empty (F11)."""
        assert "exported_hostname" in TOOL_DESCRIPTION_INSTANT
        assert 'hostname="infra"' in TOOL_DESCRIPTION_INSTANT
        assert "container_status" in TOOL_DESCRIPTION_INSTANT


class TestHugeStep:
    """A step too large for a float is a validation error, not a raw OverflowError."""

    def test_digits_overflow_is_unparseable(self) -> None:
        assert _parse_duration("9" * 400) is None

    def test_infinite_duration_is_unparseable(self) -> None:
        assert _parse_duration("9" * 400 + "s") is None
