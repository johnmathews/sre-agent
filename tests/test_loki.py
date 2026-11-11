"""Unit tests for Loki tool helpers: time parsing, formatting, validation, timeline building."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.agent.tools.loki import (
    MAX_LOG_OUTPUT_CHARS,
    LokiMatrixSeries,
    LokiMetricData,
    LokiMetricResponse,
    LokiQueryResponse,
    LokiStreamValues,
    LokiVectorSample,
    _build_timeline,
    _datetime_to_nanoseconds,
    _extract_events_from_response,
    _format_label_values,
    _format_log_lines,
    _format_matrix_results,
    _format_metric_labels,
    _format_metric_response,
    _format_vector_results,
    _lifecycle_events_from_response,
    _parse_relative_time,
    _TimelineEvent,
)


class TestParseRelativeTime:
    def test_now(self) -> None:
        before = datetime.now(tz=UTC)
        result = _parse_relative_time("now")
        after = datetime.now(tz=UTC)
        assert before <= result <= after

    def test_seconds(self) -> None:
        expected = datetime.now(tz=UTC) - timedelta(seconds=30)
        result = _parse_relative_time("30s")
        assert abs((result - expected).total_seconds()) < 1

    def test_minutes(self) -> None:
        result = _parse_relative_time("5m")
        expected = datetime.now(tz=UTC) - timedelta(minutes=5)
        assert abs((result - expected).total_seconds()) < 1

    def test_hours(self) -> None:
        result = _parse_relative_time("1h")
        expected = datetime.now(tz=UTC) - timedelta(hours=1)
        assert abs((result - expected).total_seconds()) < 1

    def test_days(self) -> None:
        result = _parse_relative_time("2d")
        expected = datetime.now(tz=UTC) - timedelta(days=2)
        assert abs((result - expected).total_seconds()) < 1

    def test_weeks(self) -> None:
        result = _parse_relative_time("1w")
        expected = datetime.now(tz=UTC) - timedelta(weeks=1)
        assert abs((result - expected).total_seconds()) < 1

    def test_iso_timestamp(self) -> None:
        result = _parse_relative_time("2024-06-15T14:00:00Z")
        assert result.year == 2024
        assert result.month == 6
        assert result.day == 15
        assert result.hour == 14

    def test_iso_timestamp_with_offset(self) -> None:
        result = _parse_relative_time("2024-06-15T14:00:00+02:00")
        assert result.year == 2024
        assert result.tzinfo is not None

    def test_now_minus_relative_accepted(self) -> None:
        for text, delta in (
            ("now-1h", timedelta(hours=1)),
            ("-30m", timedelta(minutes=30)),
            ("1h30m", timedelta(minutes=90)),
        ):
            result = _parse_relative_time(text)
            expected = datetime.now(tz=UTC) - delta
            assert abs((result - expected).total_seconds()) < 1, text

    def test_epoch_seconds_accepted(self) -> None:
        assert _parse_relative_time("1700000000") == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid time"):
            _parse_relative_time("invalid")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid time"):
            _parse_relative_time("")


class TestDatetimeToNanoseconds:
    def test_converts_correctly(self) -> None:
        dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        result = _datetime_to_nanoseconds(dt)
        expected_seconds = int(dt.timestamp())
        assert result == str(expected_seconds * 1_000_000_000)

    def test_returns_string(self) -> None:
        dt = datetime.now(tz=UTC)
        result = _datetime_to_nanoseconds(dt)
        assert isinstance(result, str)
        assert result.isdigit()


class TestFormatLogLines:
    def test_empty_result(self) -> None:
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": []},
        }
        output = _format_log_lines(data, 100, "in the last 6h")
        assert "No log lines found in the last 6h" in output

    def test_empty_result_does_not_blame_selector(self) -> None:
        data: LokiQueryResponse = {"status": "success", "data": {"resultType": "streams", "result": []}}
        output = _format_log_lines(data, 100, "in the last 1h")
        assert "Check that the stream selector labels are correct" not in output
        assert "in the last 1h" in output
        assert "valid selector" in output
        assert "quiet" in output
        assert "Widen start" in output
        assert "loki_list_label_values" in output

    def test_error_status(self) -> None:
        data: LokiQueryResponse = {"status": "error"}
        output = _format_log_lines(data, 100)
        assert "failed" in output.lower()

    def test_formats_log_lines(self) -> None:
        stream: LokiStreamValues = {
            "stream": {
                "hostname": "media",
                "service_name": "traefik",
                "detected_level": "error",
            },
            "values": [
                ["1700000000000000000", "Connection refused to backend"],
                ["1700000001000000000", "Retrying connection"],
            ],
        }
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream]},
        }
        output = _format_log_lines(data, 100)
        assert "Found 2 log lines" in output
        assert "media" in output
        assert "traefik" in output
        assert "Connection refused" in output
        assert "[2023-11-14 22:13:20Z] Connection refused to backend" in output

    def test_stream_labels_printed_once(self) -> None:
        values = [[f"{1700000000000000000 + i * 1000000000}", f"Line {i}"] for i in range(5)]
        stream: LokiStreamValues = {"stream": {"hostname": "media", "service_name": "traefik"}, "values": values}
        data: LokiQueryResponse = {"status": "success", "data": {"resultType": "streams", "result": [stream]}}
        output = _format_log_lines(data, 100)
        assert output.count("service_name=traefik") == 1
        assert output.count("hostname=media") == 1
        log_lines = [line for line in output.split("\n") if line.startswith("[")]
        assert len(log_lines) == 5
        assert all("traefik" not in line and "(" not in line for line in log_lines)

    def test_output_budget_says_n_of_m(self) -> None:
        text = "y" * 400
        values = [[f"{1700000000000000000 + i * 1000000000}", text] for i in range(400)]
        stream: LokiStreamValues = {"stream": {"hostname": "test"}, "values": values}
        data: LokiQueryResponse = {"status": "success", "data": {"resultType": "streams", "result": [stream]}}
        output = _format_log_lines(data, 1000)
        assert len(output) <= MAX_LOG_OUTPUT_CHARS
        match = re.search(r"showing (\d+) of 400 lines", output)
        assert match is not None
        shown = int(match.group(1))
        assert 0 < shown < 400
        assert len([line for line in output.split("\n") if line.startswith("[")]) == shown

    def test_respects_limit(self) -> None:
        values = [[f"{1700000000000000000 + i * 1000000000}", f"Line {i}"] for i in range(50)]
        stream: LokiStreamValues = {
            "stream": {"hostname": "test"},
            "values": values,
        }
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream]},
        }
        output = _format_log_lines(data, 10)
        assert "limited to 10" in output
        # Count actual log lines (lines starting with [)
        log_lines = [line for line in output.split("\n") if line.startswith("[")]
        assert len(log_lines) == 10

    def test_truncates_long_lines(self) -> None:
        long_message = "x" * 1000
        stream: LokiStreamValues = {
            "stream": {"hostname": "test"},
            "values": [["1700000000000000000", long_message]],
        }
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream]},
        }
        output = _format_log_lines(data, 100)
        assert "..." in output
        # Ensure the full 1000 char line isn't present
        assert long_message not in output


class TestFormatLabelValues:
    def test_empty_values(self) -> None:
        output = _format_label_values([], "hostname", "in the last 6h")
        assert "No values found for label 'hostname' in the last 6h" in output
        assert "widen start" in output

    def test_formats_values(self) -> None:
        values = ["media", "infra", "jellyfin"]
        output = _format_label_values(values, "hostname", "in the last 6h")
        assert "Found 3 values for label 'hostname' in the last 6h:" in output
        assert "infra" in output
        assert "jellyfin" in output
        assert "media" in output

    def test_values_sorted(self) -> None:
        values = ["zebra", "apple", "mango"]
        output = _format_label_values(values, "service_name", "in the last 6h")
        lines = [line.strip() for line in output.split("\n") if line.startswith("  ")]
        assert lines == ["apple", "mango", "zebra"]

    def test_label_values_capped_with_n_of_m(self) -> None:
        values = [f"svc{i:04d}" for i in range(450)]
        output = _format_label_values(values, "service_name", "in the last 6h")
        assert "showing 200 of 450" in output
        assert output.count("\n  svc") == 200


class TestExtractEventsFromResponse:
    def test_extracts_events(self) -> None:
        stream: LokiStreamValues = {
            "stream": {
                "hostname": "infra",
                "service_name": "traefik",
                "detected_level": "error",
            },
            "values": [
                ["1700000000000000000", "502 Bad Gateway"],
                ["1700000001000000000", "Connection reset"],
            ],
        }
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream]},
        }
        events = _extract_events_from_response(data)
        assert len(events) == 2
        assert events[0].service == "traefik"
        assert events[0].hostname == "infra"
        assert events[0].level == "error"
        assert "502" in events[0].message

    def test_respects_max_events(self) -> None:
        values = [[f"{1700000000000000000 + i * 1000000000}", f"Event {i}"] for i in range(50)]
        stream: LokiStreamValues = {
            "stream": {"hostname": "test", "service_name": "svc"},
            "values": values,
        }
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream]},
        }
        events = _extract_events_from_response(data, max_events=5)
        assert len(events) == 5

    def test_empty_response(self) -> None:
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": []},
        }
        events = _extract_events_from_response(data)
        assert events == []

    def test_uses_container_label_as_fallback(self) -> None:
        stream: LokiStreamValues = {
            "stream": {
                "hostname": "media",
                "container": "jellyfin-app",
                "detected_level": "warn",
            },
            "values": [["1700000000000000000", "Slow transcode"]],
        }
        data: LokiQueryResponse = {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream]},
        }
        events = _extract_events_from_response(data)
        assert events[0].service == "jellyfin-app"


class TestBuildTimeline:
    def test_empty_events(self) -> None:
        output = _build_timeline([])
        assert "No significant events" in output

    def test_builds_chronological_timeline(self) -> None:
        events = [
            _TimelineEvent(
                timestamp=datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC),
                service="traefik",
                hostname="infra",
                level="error",
                message="502 Bad Gateway",
            ),
            _TimelineEvent(
                timestamp=datetime(2024, 6, 15, 13, 55, 0, tzinfo=UTC),
                service="jellyfin",
                hostname="media",
                level="warn",
                message="Slow response",
            ),
        ]
        output = _build_timeline(events)
        assert "Found 2 significant events across 2 services" in output
        assert "Chronological Timeline" in output
        assert "Summary by Service" in output
        # Timestamps carry the date and the UTC zone; the earlier event comes first
        lines = output.split("\n")
        first_idx = next(i for i, line in enumerate(lines) if "[2024-06-15 13:55:00Z]" in line)
        second_idx = next(i for i, line in enumerate(lines) if "[2024-06-15 14:00:00Z]" in line)
        assert first_idx < second_idx

    def test_groups_by_service(self) -> None:
        events = [
            _TimelineEvent(
                timestamp=datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC),
                service="traefik",
                hostname="infra",
                level="error",
                message="Error 1",
            ),
            _TimelineEvent(
                timestamp=datetime(2024, 6, 15, 14, 1, 0, tzinfo=UTC),
                service="traefik",
                hostname="infra",
                level="error",
                message="Error 2",
            ),
            _TimelineEvent(
                timestamp=datetime(2024, 6, 15, 14, 2, 0, tzinfo=UTC),
                service="jellyfin",
                hostname="media",
                level="warn",
                message="Warning 1",
            ),
        ]
        output = _build_timeline(events)
        assert "infra/traefik: 2 events" in output
        assert "media/jellyfin: 1 events" in output


def _docker_event(action: str, name: str, at_ns: int, **attributes: str) -> str:
    """One ``docker events`` JSON line for a container."""
    event: dict[str, Any] = {
        "Type": "container",
        "Action": action,
        "Actor": {"ID": "abc123", "Attributes": {"name": name, **attributes}},
        "time": at_ns // 1_000_000_000,
        "timeNano": at_ns,
    }
    return json.dumps(event)


def _docker_events_response(*lines: str, hostname: str = "media") -> LokiQueryResponse:
    stream: LokiStreamValues = {
        "stream": {"hostname": hostname, "service_name": "docker-events"},
        "values": [["1718452000000000000", line] for line in lines],
    }
    return {"status": "success", "data": {"resultType": "streams", "result": [stream]}}


class TestLifecycleEventsFromResponse:
    BASE_NS = 1_718_452_800_000_000_000  # 2024-06-15 12:00:00Z

    def test_keeps_lifecycle_actions_with_exit_code_and_signal(self) -> None:
        data = _docker_events_response(
            _docker_event("kill", "jellyfin", self.BASE_NS, signal="15"),
            _docker_event("die", "jellyfin", self.BASE_NS + 1_000_000_000, exitCode="137"),
            _docker_event("start", "jellyfin", self.BASE_NS + 2_000_000_000),
            _docker_event("oom", "sonarr", self.BASE_NS + 3_000_000_000),
            _docker_event("restart", "radarr", self.BASE_NS + 4_000_000_000),
            _docker_event("stop", "radarr", self.BASE_NS + 5_000_000_000),
            _docker_event("health_status: unhealthy", "traefik", self.BASE_NS + 6_000_000_000),
        )
        events, unparseable = _lifecycle_events_from_response(data, None)
        assert unparseable == 0
        assert [(e.service, e.message) for e in events] == [
            ("jellyfin", "kill signal=15"),
            ("jellyfin", "die exitCode=137"),
            ("jellyfin", "start"),
            ("sonarr", "oom"),
            ("radarr", "restart"),
            ("radarr", "stop"),
            ("traefik", "health_status: unhealthy"),
        ]
        assert all(e.hostname == "media" and e.level == "lifecycle" for e in events)
        # The event's own timeNano is the timestamp, not Loki's ingestion time
        assert events[0].timestamp == datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    def test_drops_exec_healthy_and_non_container_events(self) -> None:
        network = json.dumps({"Type": "network", "Action": "connect", "Actor": {"Attributes": {"name": "bridge"}}})
        data = _docker_events_response(
            _docker_event("exec_start: /bin/sh -c healthcheck", "jellyfin", self.BASE_NS),
            _docker_event("exec_die", "jellyfin", self.BASE_NS),
            _docker_event("health_status: healthy", "jellyfin", self.BASE_NS),
            _docker_event("create", "jellyfin", self.BASE_NS),
            network,
        )
        events, unparseable = _lifecycle_events_from_response(data, None)
        assert events == []
        assert unparseable == 0

    def test_counts_lines_that_are_not_docker_events(self) -> None:
        data = _docker_events_response("Cron started", "[]", '{"Type": "container"}')
        events, unparseable = _lifecycle_events_from_response(data, None)
        assert events == []
        assert unparseable == 3

    def test_service_filter_matches_container_name_or_compose_service(self) -> None:
        data = _docker_events_response(
            _docker_event("die", "jellyfin", self.BASE_NS, exitCode="0"),
            _docker_event("start", "media-jellyfin-1", self.BASE_NS, **{"com.docker.compose.service": "jellyfin"}),
            _docker_event("start", "jellyfin-vue", self.BASE_NS),
        )
        events, _ = _lifecycle_events_from_response(data, "jellyfin")
        assert [(e.service, e.message) for e in events] == [
            ("jellyfin", "die exitCode=0"),
            ("media-jellyfin-1", "start"),
        ]

    def test_falls_back_to_seconds_then_loki_timestamp(self) -> None:
        no_nano = json.dumps(
            {"Type": "container", "Action": "stop", "Actor": {"Attributes": {"name": "a"}}, "time": 1718452800}
        )
        no_time = json.dumps({"Type": "container", "Action": "stop", "Actor": {"Attributes": {"name": "b"}}})
        out_of_range = json.dumps(
            {"Type": "container", "Action": "stop", "Actor": {"Attributes": {"name": "c"}}, "timeNano": 10**30}
        )
        events, _ = _lifecycle_events_from_response(_docker_events_response(no_nano, no_time, out_of_range), None)
        assert events[0].timestamp == datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        assert events[1].timestamp == datetime.fromtimestamp(1718452000, tz=UTC)
        assert events[2].timestamp == datetime.fromtimestamp(1718452000, tz=UTC)


class TestFormatMetricLabels:
    def test_empty_labels(self) -> None:
        assert _format_metric_labels({}) == "{}"

    def test_single_label(self) -> None:
        assert _format_metric_labels({"hostname": "media"}) == '{hostname="media"}'

    def test_multiple_labels_sorted(self) -> None:
        result = _format_metric_labels({"service_name": "traefik", "hostname": "infra"})
        assert result == '{hostname="infra", service_name="traefik"}'


class TestFormatVectorResults:
    def test_empty_results(self) -> None:
        data: LokiMetricData = {"resultType": "vector", "result": []}
        output = _format_vector_results(data)
        assert "no results" in output.lower()

    def test_single_series(self) -> None:
        sample: LokiVectorSample = {
            "metric": {"hostname": "media"},
            "value": ["1700000000", "12345"],
        }
        data: LokiMetricData = {"resultType": "vector", "result": [sample]}  # type: ignore[typeddict-item]
        output = _format_vector_results(data)
        assert "Found 1 series" in output
        assert "media" in output
        assert "12,345" in output

    def test_sorted_descending_by_value(self) -> None:
        samples: list[LokiVectorSample] = [
            {"metric": {"hostname": "infra"}, "value": ["1700000000", "100"]},
            {"metric": {"hostname": "media"}, "value": ["1700000000", "5000"]},
            {"metric": {"hostname": "pve"}, "value": ["1700000000", "2000"]},
        ]
        data: LokiMetricData = {"resultType": "vector", "result": samples}  # type: ignore[typeddict-item]
        output = _format_vector_results(data)
        lines = [line for line in output.split("\n") if line.strip().startswith("{")]
        # media (5000) should be first, then pve (2000), then infra (100)
        assert "media" in lines[0]
        assert "pve" in lines[1]
        assert "infra" in lines[2]

    def test_formats_large_numbers_with_commas(self) -> None:
        sample: LokiVectorSample = {
            "metric": {"hostname": "media"},
            "value": ["1700000000", "1234567"],
        }
        data: LokiMetricData = {"resultType": "vector", "result": [sample]}  # type: ignore[typeddict-item]
        output = _format_vector_results(data)
        assert "1,234,567" in output

    def test_formats_float_values(self) -> None:
        sample: LokiVectorSample = {
            "metric": {"hostname": "media"},
            "value": ["1700000000", "3.14159"],
        }
        data: LokiMetricData = {"resultType": "vector", "result": [sample]}  # type: ignore[typeddict-item]
        output = _format_vector_results(data)
        assert "3.14" in output

    def test_truncates_many_series(self) -> None:
        samples: list[LokiVectorSample] = [
            {"metric": {"hostname": f"host-{i}"}, "value": ["1700000000", str(i)]} for i in range(250)
        ]
        data: LokiMetricData = {"resultType": "vector", "result": samples}  # type: ignore[typeddict-item]
        output = _format_vector_results(data)
        assert "truncated" in output
        assert "250 total series" in output


class TestFormatMatrixResults:
    def test_empty_results(self) -> None:
        data: LokiMetricData = {"resultType": "matrix", "result": []}
        output = _format_matrix_results(data)
        assert "no results" in output.lower()

    def test_single_series_with_datapoints(self) -> None:
        series: LokiMatrixSeries = {
            "metric": {"hostname": "media"},
            "values": [
                ["1700000000", "100"],
                ["1700000300", "150"],
                ["1700000600", "200"],
            ],
        }
        data: LokiMetricData = {"resultType": "matrix", "result": [series]}  # type: ignore[typeddict-item]
        output = _format_matrix_results(data)
        assert "Found 1 series" in output
        assert "3 data points" in output
        assert "media" in output
        assert "100" in output
        assert "200" in output

    def test_truncates_many_datapoints(self) -> None:
        values = [[str(1700000000 + i * 60), str(i)] for i in range(300)]
        series: LokiMatrixSeries = {
            "metric": {"hostname": "media"},
            "values": values,
        }
        data: LokiMetricData = {"resultType": "matrix", "result": [series]}  # type: ignore[typeddict-item]
        output = _format_matrix_results(data)
        assert "truncated" in output


class TestFormatMetricResponse:
    def test_error_status(self) -> None:
        data: LokiMetricResponse = {"status": "error"}
        output = _format_metric_response(data)
        assert "failed" in output.lower()

    def test_vector_dispatch(self) -> None:
        sample: LokiVectorSample = {
            "metric": {"hostname": "media"},
            "value": ["1700000000", "42"],
        }
        data: LokiMetricResponse = {
            "status": "success",
            "data": {"resultType": "vector", "result": [sample]},  # type: ignore[typeddict-item]
        }
        output = _format_metric_response(data)
        assert "Found 1 series" in output
        assert "42" in output

    def test_matrix_dispatch(self) -> None:
        series: LokiMatrixSeries = {
            "metric": {"hostname": "infra"},
            "values": [["1700000000", "10"]],
        }
        data: LokiMetricResponse = {
            "status": "success",
            "data": {"resultType": "matrix", "result": [series]},  # type: ignore[typeddict-item]
        }
        output = _format_metric_response(data)
        assert "Found 1 series" in output

    def test_unknown_result_type(self) -> None:
        data: LokiMetricResponse = {
            "status": "success",
            "data": {"resultType": "scalar", "result": []},
        }
        output = _format_metric_response(data)
        assert "Unexpected result type" in output
