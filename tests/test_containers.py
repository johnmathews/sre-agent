"""Unit tests for container_status: pure record building and formatting (no IO)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.agent.tools.containers import (
    MAX_CONTAINERS_SHOWN,
    ContainerRecord,
    ContainerReport,
    ContainerStatusInput,
    ExporterStatus,
    Sample,
    build_report,
    format_report,
)

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
NOW_TS = NOW.timestamp()


def _exporter_labels(host: str, name: str, image: str = "example/app:1") -> dict[str, str]:
    """Labels as Prometheus stores them: the exporter's host in hostname, the container's in exported_hostname."""
    return {
        "job": "container-status",
        "instance": "192.0.2.10:8081",
        "hostname": "infra",
        "exported_hostname": host,
        "container_name": name,
        "image": image,
    }


def _results(**overrides: list[Sample] | BaseException) -> dict[str, list[Sample] | BaseException]:
    base: dict[str, list[Sample] | BaseException] = {
        "state": [],
        "health": [],
        "restarts": [],
        "restarts_24h": [],
        "outdated": [],
        "image_info": [],
        "start_time": [],
        "exporter_up": [({"job": "container-status", "instance": "192.0.2.10:8081"}, 1.0)],
        "last_collection": [({"job": "container-status"}, NOW_TS - 20)],
    }
    base.update(overrides)
    return base


def _record(name: str, host: str = "media vm", **kwargs: object) -> ContainerRecord:
    fields: dict[str, object] = {"host": host, "name": name, "image": "example/app:1", "state": "running"}
    fields.update(kwargs)
    return ContainerRecord(**fields)  # type: ignore[arg-type]


class TestInput:
    def test_host_pattern_rejects_quotes(self) -> None:
        with pytest.raises(ValidationError):
            ContainerStatusInput(host='media"} or vector(1)')

    def test_host_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            ContainerStatusInput(host="a" * 65)

    def test_name_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            ContainerStatusInput(name="a" * 129)

    def test_defaults(self) -> None:
        model = ContainerStatusInput()
        assert model.host is None and model.name is None and model.only_problems is False


class TestProblems:
    def test_running_healthy_has_no_problems(self) -> None:
        assert _record("app", health="healthy", restarts_24h=0, outdated=False).problems == []

    def test_each_problem_named(self) -> None:
        record = _record("app", state="exited", health="unhealthy", restarts_24h=2, outdated=True)
        assert record.problems == ["not running", "unhealthy", "2 restarts in 24h", "outdated image"]


class TestBuildReport:
    def test_state_values_mapped(self) -> None:
        state = [
            (_exporter_labels("media vm", "a"), 0.0),
            (_exporter_labels("media vm", "b"), 1.0),
            (_exporter_labels("media vm", "c"), 4.0),
            (_exporter_labels("media vm", "d"), 5.0),
            (_exporter_labels("media vm", "e"), 9.0),
        ]
        report = build_report(_results(state=state), host=None, name=None, now=NOW)
        assert {r.name: r.state for r in report.containers} == {
            "a": "exited",
            "b": "running",
            "c": "restarting",
            "d": "dead",
            "e": "unknown",
        }

    def test_name_filter_is_case_insensitive_substring(self) -> None:
        state = [
            (_exporter_labels("media vm", "Jellyfin"), 1.0),
            (_exporter_labels("media vm", "jellyfin-vue"), 1.0),
            (_exporter_labels("media vm", "sonarr"), 1.0),
        ]
        report = build_report(_results(state=state), host=None, name="JELLY", now=NOW)
        assert sorted(r.name for r in report.containers) == ["Jellyfin", "jellyfin-vue"]

    def test_problems_sorted_first(self) -> None:
        state = [
            (_exporter_labels("media vm", "aaa"), 1.0),
            (_exporter_labels("media vm", "zzz"), 0.0),
        ]
        report = build_report(_results(state=state), host=None, name=None, now=NOW)
        assert [r.name for r in report.containers] == ["zzz", "aaa"]

    def test_falls_back_to_hostname_without_exported_hostname(self) -> None:
        labels = {"job": "container-status", "hostname": "media", "container_name": "app", "image": "x"}
        report = build_report(_results(state=[(labels, 1.0)]), host="media vm", name=None, now=NOW)
        assert [r.host for r in report.containers] == ["media"]

    def test_image_info_versions_attached(self) -> None:
        state = [(_exporter_labels("media vm", "app"), 1.0)]
        info = [
            (
                {
                    **_exporter_labels("media vm", "app"),
                    "status": "outdated",
                    "current_version": "1.0",
                    "available_version": "1.1",
                },
                1.0,
            )
        ]
        outdated = [(_exporter_labels("media vm", "app"), 1.0)]
        report = build_report(_results(state=state, image_info=info, outdated=outdated), None, None, NOW)
        record = report.containers[0]
        assert (record.current_version, record.available_version, record.outdated) == ("1.0", "1.1", True)

    def test_state_failure_reported_not_raised(self) -> None:
        report = build_report(_results(state=ValueError("not JSON")), host=None, name=None, now=NOW)
        assert report.containers == []
        assert "state" in report.unavailable
        assert not report.state_available

    def test_exporter_fresh(self) -> None:
        exporter = build_report(_results(), None, None, NOW).exporter
        assert exporter.stale is False

    def test_exporter_old_collection_is_stale(self) -> None:
        results = _results(last_collection=[({"job": "container-status"}, NOW_TS - 900)])
        exporter = build_report(results, None, None, NOW).exporter
        assert exporter.stale is True
        assert "15m ago" in " ".join(exporter.reasons)

    def test_exporter_missing_up_series_is_stale(self) -> None:
        exporter = build_report(_results(exporter_up=[]), None, None, NOW).exporter
        assert exporter.stale is True


class TestFormat:
    def test_cap_says_n_of_m(self) -> None:
        records = [_record(f"app{i:03d}") for i in range(MAX_CONTAINERS_SHOWN + 30)]
        report = ContainerReport(
            containers=records,
            known_hosts=["media vm"],
            unknown_host=False,
            unavailable={},
            exporter=ExporterStatus(up=1.0, last_collection=NOW_TS - 20, stale=False, reasons=[]),
        )
        text = format_report(report, only_problems=False, now=NOW)
        assert f"showing {MAX_CONTAINERS_SHOWN} of {MAX_CONTAINERS_SHOWN + 30}" in text
        assert "app149" in text
        assert "app150" not in text

    def test_line_shows_state_restarts_start_and_image(self) -> None:
        record = _record(
            "app",
            health="healthy",
            restart_count=3,
            restarts_24h=0,
            started_at=NOW_TS - 7200,
            current_version="1.0",
        )
        report = ContainerReport([record], ["media vm"], False, {}, ExporterStatus(1.0, NOW_TS, False, []))
        text = format_report(report, only_problems=False, now=NOW)
        assert "media vm/app: running, healthy, restarts 3, started 2026-11-11 10:00:00Z (2h ago)" in text
        assert "image example/app:1 (1.0)" in text
        assert "PROBLEM" not in text

    def test_problem_line_prefixed_with_reasons(self) -> None:
        record = _record("app", state="exited", outdated=True, current_version="1.0", available_version="1.1")
        report = ContainerReport([record], ["media vm"], False, {}, ExporterStatus(1.0, NOW_TS, False, []))
        text = format_report(report, only_problems=False, now=NOW)
        assert "PROBLEM (not running, outdated image) media vm/app: exited" in text
        assert "1.0 -> 1.1 available" in text
