"""Tests for the get_current_time tool and prompt time renderer."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.agent.tools.clock import (
    RETENTION_DAYS,
    _format_now,
    _resolve_timezone,
    get_current_time,
    render_prompt_time_fields,
)


class _FakeSettings:
    def __init__(self, tz: str = "UTC") -> None:
        self.user_timezone = tz


class TestResolveTimezone:
    def test_known_timezone(self) -> None:
        tz = _resolve_timezone("Europe/Madrid")
        assert tz.key == "Europe/Madrid"

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        tz = _resolve_timezone("Atlantis/SunkenCity")
        assert tz.key == "UTC"

    def test_utc_passthrough(self) -> None:
        tz = _resolve_timezone("UTC")
        assert tz.key == "UTC"


class TestFormatNow:
    def test_returns_all_keys(self) -> None:
        now = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
        out = _format_now(_FakeSettings("UTC"), now)
        for key in (
            "utc_iso",
            "utc_epoch",
            "weekday",
            "date",
            "user_timezone",
            "user_local_iso",
            "user_local_human",
        ):
            assert key in out

    def test_weekday_is_correct(self) -> None:
        # 2026-04-27 is a Monday — this is the date that previously confused the agent
        monday = datetime(2026, 4, 27, 21, 58, 19, tzinfo=UTC)
        assert _format_now(_FakeSettings(), monday)["weekday"] == "Monday"

        tuesday = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
        assert _format_now(_FakeSettings(), tuesday)["weekday"] == "Tuesday"

    def test_local_time_uses_user_timezone(self) -> None:
        now = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
        out = _format_now(_FakeSettings("Europe/Madrid"), now)
        # Madrid is UTC+2 in late April (CEST)
        assert "10:01" in out["user_local_human"]
        assert "Tuesday" in out["user_local_human"]
        assert out["user_timezone"] == "Europe/Madrid"

    def test_utc_epoch_matches_input(self) -> None:
        now = datetime(2026, 4, 27, 21, 58, 19, tzinfo=UTC)
        # 1777327099 is the documented boot epoch from the production conversation
        assert _format_now(_FakeSettings(), now)["utc_epoch"] == 1777327099


class TestRenderPromptTimeFields:
    def test_returns_required_placeholders(self, mock_settings: object) -> None:
        fields = render_prompt_time_fields()
        for key in (
            "current_time_full",
            "current_weekday",
            "current_date",
            "current_local_time",
            "user_timezone",
            "retention_cutoff",
            "current_time",
        ):
            assert key in fields

    def test_weekday_present_in_full_time(self) -> None:
        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
            fields = render_prompt_time_fields(_FakeSettings("UTC"))
        assert "Tuesday" in fields["current_time_full"]
        assert fields["current_weekday"] == "Tuesday"
        assert fields["current_date"] == "2026-04-28"

    def test_retention_cutoff_is_90_days_back(self) -> None:
        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)
            fields = render_prompt_time_fields(_FakeSettings())
        assert fields["retention_cutoff"] == "2026-01-28"
        assert RETENTION_DAYS == 90

    def test_local_time_renders_in_user_timezone(self) -> None:
        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
            fields = render_prompt_time_fields(_FakeSettings("Europe/Madrid"))
        assert "10:01" in fields["current_local_time"]


class TestGetCurrentTimeTool:
    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    def test_tool_returns_human_readable_string(self) -> None:
        result = get_current_time.func()
        assert "UTC ISO:" in result
        assert "UTC epoch:" in result
        assert "UTC weekday:" in result
        assert "User timezone:" in result

    def test_tool_returns_correct_weekday(self) -> None:
        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 27, 21, 58, 19, tzinfo=UTC)
            result = get_current_time.func()
        assert "Monday" in result

    def test_tool_returns_epoch_seconds(self) -> None:
        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 27, 21, 58, 19, tzinfo=UTC)
            result = get_current_time.func()
        assert "1777327099" in result
