"""Unit tests for the shared tool helpers in src/agent/tools/_common.py."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.agent.tools._common import (
    ERROR_LEVELS,
    WARN_LEVELS,
    format_age,
    format_bytes,
    format_utc,
    host_key,
    parse_duration,
    parse_time,
    promql_string,
    regex_literal,
)

NOW = datetime(2026, 11, 11, 13, 0, 0, tzinfo=UTC)


# --- format_bytes ---


def test_format_bytes_rounds_not_truncates() -> None:
    assert format_bytes(int(1.99 * 1024**4)) == "2.0 TiB"
    assert format_bytes(int(7.9 * 1024**3)) == "7.9 GiB"


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0.0 B"),
        (512, "512.0 B"),
        (2048, "2.0 KiB"),
        (1536, "1.5 KiB"),
        (1048576, "1.0 MiB"),
        (16 * 1024**4, "16.0 TiB"),
        (3 * 1024**5, "3.0 PiB"),
        (2048 * 1024**5, "2048.0 PiB"),
        (-2048, "-2.0 KiB"),
    ],
)
def test_format_bytes_units(n: int, expected: str) -> None:
    assert format_bytes(n) == expected


def test_format_bytes_rolls_over_when_rounding_reaches_1024() -> None:
    # 1023.97 KiB would print "1024.0 KiB"; it must move to the next unit.
    assert format_bytes(1024 * 1024 - 30) == "1.0 MiB"


def test_format_bytes_accepts_float() -> None:
    assert format_bytes(1.5 * 1024**3) == "1.5 GiB"


# --- format_utc / format_age ---


def test_format_utc_from_epoch() -> None:
    assert format_utc(0) == "1970-01-01 00:00:00Z"
    assert format_utc(1794369609.9) == "2026-11-11 04:00:09Z"


def test_format_utc_converts_aware_datetime_to_utc() -> None:
    cet = timezone(timedelta(hours=1))
    assert format_utc(datetime(2026, 11, 11, 14, 5, 6, tzinfo=cet)) == "2026-11-11 13:05:06Z"


def test_format_utc_treats_naive_datetime_as_utc() -> None:
    assert format_utc(datetime(2026, 11, 11, 13, 5, 6)) == "2026-11-11 13:05:06Z"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "0s ago"),
        (timedelta(seconds=42), "42s ago"),
        (timedelta(minutes=5, seconds=59), "5m ago"),
        (timedelta(hours=8, minutes=59), "8h ago"),
        (timedelta(days=3, hours=23), "3d ago"),
        (timedelta(minutes=-5), "in 5m"),
    ],
)
def test_format_age(delta: timedelta, expected: str) -> None:
    assert format_age(NOW - delta, NOW) == expected


def test_format_age_accepts_epoch_and_naive_now() -> None:
    assert format_age(NOW.timestamp() - 8 * 3600, NOW.replace(tzinfo=None)) == "8h ago"


# --- parse_duration ---


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("30s", 30),
        ("5m", 300),
        ("24h", 86400),
        ("7d", 604800),
        ("2w", 1209600),
        ("1h30m", 5400),
        ("1d12h", 129600),
        ("1.5h", 5400),
        ("90", 90),
        ("500ms", 0.5),
        (" 15m ", 900),
    ],
)
def test_parse_duration(value: str, seconds: float) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "h", "5x", "1h-30m", "abc", "-5m", "inf", "nan", "1h 30m"])
def test_parse_duration_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


# --- parse_time ---


def test_parse_time_accepts_relative_forms() -> None:
    expected = NOW - timedelta(hours=24)
    assert parse_time("24h", NOW) == expected
    assert parse_time("now-24h", NOW) == expected
    assert parse_time("-24h", NOW) == expected


def test_parse_time_now() -> None:
    assert parse_time("now", NOW) == NOW
    assert parse_time(" NOW ", NOW) == NOW


def test_parse_time_compound_relative() -> None:
    assert parse_time("now-1h30m", NOW) == NOW - timedelta(minutes=90)


def test_parse_time_rfc3339_z_and_offset() -> None:
    assert parse_time("2026-11-10T13:00:00Z", NOW) == datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
    assert parse_time("2026-11-10T14:00:00+01:00", NOW) == datetime(2026, 11, 10, 13, 0, tzinfo=UTC)


def test_parse_time_naive_iso_is_utc() -> None:
    assert parse_time("2026-11-10 13:00:00", NOW) == datetime(2026, 11, 10, 13, 0, tzinfo=UTC)


def test_parse_time_unix_epoch() -> None:
    assert parse_time("1794369609", NOW) == datetime.fromtimestamp(1794369609, tz=UTC)
    assert parse_time("1794369609.5", NOW) == datetime.fromtimestamp(1794369609.5, tz=UTC)


def test_parse_time_result_is_utc_aware() -> None:
    for value in ("now", "24h", "2026-11-10T14:00:00+01:00", "1794369609"):
        result = parse_time(value, NOW)
        assert result.tzinfo is UTC


def test_parse_time_relative_uses_injected_now_in_utc() -> None:
    cet = timezone(timedelta(hours=1))
    local_now = NOW.astimezone(cet)
    result = parse_time("1h", local_now)
    assert result == NOW - timedelta(hours=1)
    assert result.tzinfo is UTC


def test_parse_time_naive_now_is_treated_as_utc() -> None:
    assert parse_time("1h", NOW.replace(tzinfo=None)) == NOW - timedelta(hours=1)


@pytest.mark.parametrize("value", ["", "yesterday", "now+", "now-", "24x", "2026-13-40", "-", "now-abc"])
def test_parse_time_error_names_formats(value: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_time(value, NOW)
    message = str(exc_info.value)
    assert "now-24h" in message
    assert "RFC3339" in message
    assert "now" in message
    assert "24h" in message
    assert "-24h" in message
    assert "Unix epoch" in message


# --- promql_string / regex_literal ---


def test_promql_string_escapes_quotes_and_backslashes() -> None:
    assert promql_string("media") == '"media"'
    assert promql_string('a"b') == '"a\\"b"'
    assert promql_string("a\\b") == '"a\\\\b"'
    # backslash escaped first, so an escaped quote cannot be un-escaped
    assert promql_string('\\"}') == '"\\\\\\"}"'


def test_regex_literal_escapes_metacharacters() -> None:
    assert regex_literal("plain-name_1") == "plain-name_1"
    assert regex_literal("a.b") == "a\\.b"
    assert regex_literal("x(y)|z*+?[]{}^$\\") == "x\\(y\\)\\|z\\*\\+\\?\\[\\]\\{\\}\\^\\$\\\\"


def test_regex_literal_matches_itself_only() -> None:
    value = "tube.archivist(1)"
    pattern = re.compile("^" + regex_literal(value) + "$")
    assert pattern.match(value)
    assert not pattern.match("tubeXarchivist(1)")


# --- host_key ---


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("media vm", "media"),
        ("tubearchivist lxc", "tube-archivist"),
        ("proxmox ve", "proxmox"),
        ("pve", "proxmox"),
    ],
)
def test_host_key_joins_exporter_and_prometheus_names(left: str, right: str) -> None:
    assert host_key(left) == host_key(right)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Media VM", "media"),
        ("tube-archivist", "tubearchivist"),
        ("PVE", "proxmox"),
        ("Proxmox VE", "proxmox"),
        ("home_assistant lxc", "homeassistant"),
        ("infra", "infra"),
        ("  media  ", "media"),
        # the suffix is only stripped as a separate word
        ("devve", "devve"),
        ("vm", "vm"),
    ],
)
def test_host_key_values(name: str, key: str) -> None:
    assert host_key(name) == key


# --- level vocabularies ---


def test_level_vocabularies() -> None:
    assert ERROR_LEVELS.split("|") == ["error", "err", "fatal", "critical", "crit"]
    assert WARN_LEVELS.split("|") == ["warn", "warning"]


def test_common_does_not_import_settings() -> None:
    import ast
    import inspect

    import src.agent.tools._common as common

    tree = ast.parse(inspect.getsource(common))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names
    }
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "get_settings" not in imported
    assert "src.config" not in modules
