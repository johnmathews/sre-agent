"""Unit tests for the evidence collector's pure helpers: timestamps, window derivation, docker events, capping."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.incidents.evidence import (
    FALLBACK_SPAN,
    MAX_LINE_CHARS,
    MAX_LINES_PER_SOURCE,
    TRIGGER_DELAY,
    WINDOW_LEAD,
    StatusEvent,
    cap_lines,
    derive_window,
    format_hms,
    loki_hostname,
    parse_docker_events,
    parse_status_events,
    parse_timestamp,
    prometheus_hostname,
)
from src.incidents.models import Incident

NOW = datetime(2026, 10, 27, 11, 0, 0, tzinfo=UTC)
RESOLVED = datetime(2026, 10, 27, 10, 12, 30, tzinfo=UTC)
TRIGGERED = datetime(2026, 10, 27, 10, 9, 50, tzinfo=UTC)


def _incident(
    *, triggered_at: datetime | None = TRIGGERED, resolved_at: datetime | None = RESOLVED, host: str | None = "infra"
) -> Incident:
    return Incident(
        id=1,
        endpoint_key="monitoring_dozzle",
        group="monitoring",
        name="Dozzle",
        url="https://dozzle.example.com",
        host=host,
        container="dozzle",
        triggered_at=triggered_at,
        resolved_at=resolved_at,
        state="resolved",
        outcome=None,
        detail=None,
    )


# --- timestamps ---


class TestParseTimestamp:
    def test_nanoseconds_truncated_to_microseconds(self) -> None:
        assert parse_timestamp("2026-10-27T10:06:44.928614545Z") == datetime(
            2026, 10, 27, 10, 6, 44, 928614, tzinfo=UTC
        )

    def test_no_fraction(self) -> None:
        assert parse_timestamp("2026-10-27T10:06:44Z") == datetime(2026, 10, 27, 10, 6, 44, tzinfo=UTC)

    def test_short_fraction(self) -> None:
        assert parse_timestamp("2026-10-27T10:06:44.5Z") == datetime(2026, 10, 27, 10, 6, 44, 500000, tzinfo=UTC)

    def test_offset_converted_to_utc(self) -> None:
        assert parse_timestamp("2026-10-27T12:06:44.123456789+02:00") == datetime(
            2026, 10, 27, 10, 6, 44, 123456, tzinfo=UTC
        )

    def test_naive_is_taken_as_utc(self) -> None:
        assert parse_timestamp("2026-10-27T10:06:44") == datetime(2026, 10, 27, 10, 6, 44, tzinfo=UTC)

    def test_garbage_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_timestamp("yesterday")

    def test_format_hms_is_utc(self) -> None:
        local = datetime(2026, 10, 27, 12, 6, 44, 999000, tzinfo=UTC).astimezone()
        assert format_hms(local) == "12:06:44Z"


# --- window derivation ---


def _events(*pairs: tuple[str, str]) -> list[StatusEvent]:
    return parse_status_events([{"type": t, "timestamp": ts} for t, ts in pairs])


class TestParseStatusEvents:
    def test_parses_and_sorts_oldest_first(self) -> None:
        events = parse_status_events(
            [
                {"type": "HEALTHY", "timestamp": "2026-10-27T10:11:44.1Z"},
                {"type": "START", "timestamp": "2026-10-20T08:00:00Z"},
                {"type": "UNHEALTHY", "timestamp": "2026-10-27T10:06:44.928614545Z"},
            ]
        )
        assert [e.type for e in events] == ["START", "UNHEALTHY", "HEALTHY"]

    def test_skips_malformed_entries(self) -> None:
        events = parse_status_events(
            [
                {"type": "UNHEALTHY", "timestamp": "not a time"},
                {"timestamp": "2026-10-27T10:06:44Z"},
                "junk",
                {"type": "HEALTHY", "timestamp": "2026-10-27T10:11:44Z"},
            ]
        )
        assert [e.type for e in events] == ["HEALTHY"]


class TestDeriveWindow:
    def test_one_outage(self) -> None:
        events = _events(
            ("START", "2026-10-20T08:00:00Z"),
            ("UNHEALTHY", "2026-10-27T10:06:44.928614545Z"),
            ("HEALTHY", "2026-10-27T10:11:44.000000001Z"),
        )
        window = derive_window(events, _incident(), NOW)
        first = datetime(2026, 10, 27, 10, 6, 44, 928614, tzinfo=UTC)
        assert window.first_failure_at == first
        assert window.recovered_at == datetime(2026, 10, 27, 10, 11, 44, tzinfo=UTC)
        assert window.start == first - WINDOW_LEAD
        assert window.end == RESOLVED
        assert window.fallback is None

    def test_two_outages_only_last_before_resolve_counts(self) -> None:
        events = _events(
            ("UNHEALTHY", "2026-10-27T08:00:00Z"),
            ("HEALTHY", "2026-10-27T08:05:00Z"),
            ("UNHEALTHY", "2026-10-27T10:06:44Z"),
            ("HEALTHY", "2026-10-27T10:11:44Z"),
            # A later outage after this incident's resolve must not be picked.
            ("UNHEALTHY", "2026-10-27T10:40:00Z"),
        )
        window = derive_window(events, _incident(), NOW)
        assert window.first_failure_at == datetime(2026, 10, 27, 10, 6, 44, tzinfo=UTC)
        assert window.recovered_at == datetime(2026, 10, 27, 10, 11, 44, tzinfo=UTC)

    def test_unresolved_uses_now(self) -> None:
        events = _events(("UNHEALTHY", "2026-10-27T10:50:00Z"))
        window = derive_window(events, _incident(resolved_at=None), NOW)
        assert window.end == NOW
        assert window.first_failure_at == datetime(2026, 10, 27, 10, 50, tzinfo=UTC)
        assert window.recovered_at is None

    def test_no_unhealthy_falls_back_to_triggered_time(self) -> None:
        events = _events(("START", "2026-10-20T08:00:00Z"))
        window = derive_window(events, _incident(), NOW)
        assert window.first_failure_at is None
        assert window.start == TRIGGERED - TRIGGER_DELAY - WINDOW_LEAD
        assert window.end == RESOLVED
        assert window.fallback is not None
        assert "TRIGGERED" in window.fallback

    def test_gatus_unavailable_without_trigger_falls_back_to_span(self) -> None:
        window = derive_window(None, _incident(triggered_at=None), NOW)
        assert window.first_failure_at is None
        assert window.start == RESOLVED - FALLBACK_SPAN
        assert window.end == RESOLVED
        assert window.fallback is not None
        assert "30 min" in window.fallback

    def test_override_keeps_first_failure_but_uses_given_bounds(self) -> None:
        events = _events(("UNHEALTHY", "2026-10-27T10:06:44Z"), ("HEALTHY", "2026-10-27T10:11:44Z"))
        start, end = RESOLVED - timedelta(hours=1), RESOLVED + timedelta(minutes=5)
        window = derive_window(events, _incident(), NOW, override=(start, end))
        assert (window.start, window.end) == (start, end)
        assert window.first_failure_at == datetime(2026, 10, 27, 10, 6, 44, tzinfo=UTC)

    def test_pivot_is_first_failure_or_estimate(self) -> None:
        events = _events(("UNHEALTHY", "2026-10-27T10:06:44Z"))
        assert derive_window(events, _incident(), NOW).pivot == datetime(2026, 10, 27, 10, 6, 44, tzinfo=UTC)
        fallback = derive_window(None, _incident(), NOW)
        assert fallback.pivot == TRIGGERED - TRIGGER_DELAY


# --- docker events ---


def _docker_line(action: str, name: str = "dozzle", second: int = 12, **attrs: str) -> str:
    attributes = {"image": "amir20/dozzle:v11", "name": name, "com.docker.compose.project": "monitoring", **attrs}
    base = datetime(2026, 10, 27, 10, 6, second, tzinfo=UTC)
    nano = int(base.timestamp()) * 1_000_000_000 + 123456789
    return json.dumps(
        {
            "status": action,
            "id": "2f13abcdef",
            "from": "amir20/dozzle:v11",
            "Type": "container",
            "Action": action,
            "Actor": {"ID": "2f13abcdef", "Attributes": attributes},
            "scope": "local",
            "time": int(base.timestamp()),
            "timeNano": nano,
        }
    )


class TestParseDockerEvents:
    def test_lifecycle_actions(self) -> None:
        lines = [
            _docker_line("kill", second=12, signal="15"),
            _docker_line("die", second=13, exitCode="0"),
            _docker_line("stop", second=13),
            _docker_line("oom", second=14),
            _docker_line("start", second=30),
        ]
        parsed, junk = parse_docker_events(lines, "dozzle")
        assert parsed == [
            "10:06:12Z kill signal=15",
            "10:06:13Z die exitCode=0",
            "10:06:13Z stop",
            "10:06:14Z oom",
            "10:06:30Z start",
        ]
        assert junk == 0

    def test_real_docker_events_line(self) -> None:
        # Shape of `docker events --format '{{json .}}'` as shipped to Loki (labels trimmed).
        line = (
            '{"status":"die","id":"2f13","from":"amir20/dozzle:v11","Type":"container","Action":"die",'
            '"Actor":{"ID":"2f13","Attributes":{"exitCode":"137","image":"amir20/dozzle:v11","name":"dozzle",'
            '"org.opencontainers.image.version":"v11"}},"scope":"local","time":1793092000,'
            '"timeNano":1793092000123456789}'
        )
        parsed, junk = parse_docker_events([line], "dozzle")
        assert parsed == [f"{format_hms(datetime.fromtimestamp(1793092000, tz=UTC))} die exitCode=137"]
        assert junk == 0

    def test_other_containers_and_exec_noise_filtered(self) -> None:
        lines = [
            _docker_line("die", name="dozzle-agent", exitCode="1"),
            _docker_line("exec_start: /bin/healthcheck"),
            _docker_line("health_status: unhealthy", second=20),
            json.dumps({"Type": "network", "Action": "connect", "Actor": {"Attributes": {"name": "dozzle"}}}),
        ]
        parsed, junk = parse_docker_events(lines, "dozzle")
        assert parsed == ["10:06:20Z health_status: unhealthy"]
        assert junk == 0

    def test_junk_lines_counted(self) -> None:
        lines = ["not json", '{"Action": "die"}', "[1, 2]", _docker_line("start")]
        parsed, junk = parse_docker_events(lines, "dozzle")
        assert parsed == ["10:06:12Z start"]
        assert junk == 3

    def test_sorted_by_time(self) -> None:
        lines = [_docker_line("start", second=40), _docker_line("die", second=10, exitCode="0")]
        parsed, _ = parse_docker_events(lines, "dozzle")
        assert parsed == ["10:06:10Z die exitCode=0", "10:06:40Z start"]


# --- capping ---


class TestCapLines:
    def test_under_cap_unchanged(self) -> None:
        assert cap_lines(["a", "b"]) == (["a", "b"], 0)

    def test_line_count_capped(self) -> None:
        lines = [f"line {i}" for i in range(MAX_LINES_PER_SOURCE + 7)]
        kept, dropped = cap_lines(lines)
        assert kept == lines[:MAX_LINES_PER_SOURCE]
        assert dropped == 7

    def test_long_line_truncated_with_ellipsis(self) -> None:
        kept, _ = cap_lines(["x" * (MAX_LINE_CHARS + 50)])
        assert len(kept[0]) == MAX_LINE_CHARS
        assert kept[0].endswith("…")

    def test_whitespace_collapsed_to_one_line(self) -> None:
        kept, _ = cap_lines(["panic: boom\n\tat main.go:12\r\n"])
        assert kept == ["panic: boom at main.go:12"]

    def test_custom_limits(self) -> None:
        assert cap_lines(["abcdef", "g", "h"], max_lines=2, max_chars=4) == (["abc…", "g"], 1)


# --- host mapping ---


class TestHostMapping:
    def test_proxmox_host_differs_between_loki_and_prometheus(self) -> None:
        assert prometheus_hostname("pve") == "proxmox"
        assert loki_hostname("proxmox") == "pve"

    def test_other_hosts_are_the_same(self) -> None:
        assert prometheus_hostname("infra") == "infra"
        assert loki_hostname("infra") == "infra"
        assert loki_hostname("pve") == "pve"
        assert prometheus_hostname("proxmox") == "proxmox"
