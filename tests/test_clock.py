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
        out = _format_now("UTC", now)
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
        assert _format_now("UTC", monday)["weekday"] == "Monday"

        tuesday = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
        assert _format_now("UTC", tuesday)["weekday"] == "Tuesday"

    def test_local_time_uses_user_timezone(self) -> None:
        now = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
        out = _format_now("Europe/Madrid", now)
        # Madrid is UTC+2 in late April (CEST)
        assert "10:01" in out["user_local_human"]
        assert "Tuesday" in out["user_local_human"]
        assert out["user_timezone"] == "Europe/Madrid"

    def test_utc_epoch_matches_input(self) -> None:
        now = datetime(2026, 4, 27, 21, 58, 19, tzinfo=UTC)
        # 1777327099 is the documented boot epoch from the production conversation
        assert _format_now("UTC", now)["utc_epoch"] == 1777327099


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


class TestPerRequestTimezoneOverride:
    """The contextvar set by request_user_timezone() wins over settings."""

    def test_no_override_falls_back_to_settings(self, mock_settings: object) -> None:
        from src.agent.tools.clock import effective_timezone

        # mock_settings sets user_timezone='UTC'
        assert effective_timezone() == "UTC"

    def test_override_wins_over_settings(self, mock_settings: object) -> None:
        from src.agent.tools.clock import effective_timezone, request_user_timezone

        with request_user_timezone("Asia/Seoul"):
            assert effective_timezone() == "Asia/Seoul"
        # contextvar resets after the with-block
        assert effective_timezone() == "UTC"

    def test_render_prompt_time_fields_uses_override(self, mock_settings: object) -> None:
        from src.agent.tools.clock import render_prompt_time_fields, request_user_timezone

        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
            with request_user_timezone("Asia/Seoul"):
                fields = render_prompt_time_fields()
        # Seoul is UTC+9 → 17:01 local
        assert "17:01" in fields["current_local_time"]
        assert fields["user_timezone"] == "Asia/Seoul"

    def test_get_current_time_tool_uses_override(self, mock_settings: object) -> None:
        from src.agent.tools.clock import request_user_timezone

        with patch("src.agent.tools.clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 28, 8, 1, 0, tzinfo=UTC)
            with request_user_timezone("Asia/Seoul"):
                result = get_current_time.func()
        assert "Asia/Seoul" in result
        assert "17:01" in result

    def test_none_override_does_nothing(self, mock_settings: object) -> None:
        from src.agent.tools.clock import effective_timezone, request_user_timezone

        with request_user_timezone(None):
            assert effective_timezone() == "UTC"

    def test_nested_overrides_restore_correctly(self, mock_settings: object) -> None:
        from src.agent.tools.clock import effective_timezone, request_user_timezone

        with request_user_timezone("Europe/Amsterdam"):
            assert effective_timezone() == "Europe/Amsterdam"
            with request_user_timezone("Asia/Seoul"):
                assert effective_timezone() == "Asia/Seoul"
            assert effective_timezone() == "Europe/Amsterdam"
        assert effective_timezone() == "UTC"


class TestIsValidTimezone:
    def test_accepts_iana(self) -> None:
        from src.agent.tools.clock import is_valid_timezone

        assert is_valid_timezone("Europe/Amsterdam") is True
        assert is_valid_timezone("Asia/Seoul") is True
        assert is_valid_timezone("UTC") is True

    def test_rejects_short_abbreviation(self) -> None:
        from src.agent.tools.clock import is_valid_timezone

        # CEST/PDT are not in the IANA tz database (they are wall-clock
        # abbreviations, not zones). Note: 'EST' and 'GMT' historically ship
        # in the tzdata package as fixed-offset aliases, so we don't test
        # rejection of those — the contract is "CEST and similar must fail."
        assert is_valid_timezone("CEST") is False
        assert is_valid_timezone("PDT") is False
        assert is_valid_timezone("BST") is False

    def test_rejects_offset(self) -> None:
        from src.agent.tools.clock import is_valid_timezone

        assert is_valid_timezone("+02:00") is False
        assert is_valid_timezone("-08:00") is False


class TestUserTimezoneSettingValidation:
    """Reject non-IANA values at startup so DST is handled automatically."""

    @staticmethod
    def _build_settings(tz: str) -> object:
        from src.config import Settings

        return Settings(
            llm_provider="openai",
            openai_api_key="sk-test",
            prometheus_url="http://x:9090",
            grafana_url="http://x:3000",
            grafana_service_account_token="x",
            user_timezone=tz,
        )

    def test_rejects_short_abbreviation(self) -> None:
        with pytest.raises(ValueError, match="not a valid IANA timezone"):
            self._build_settings("CEST")

    def test_rejects_fixed_offset(self) -> None:
        with pytest.raises(ValueError, match="not a valid IANA timezone"):
            self._build_settings("+02:00")

    def test_accepts_iana_name(self) -> None:
        s = self._build_settings("Europe/Amsterdam")
        assert s.user_timezone == "Europe/Amsterdam"  # type: ignore[attr-defined]
