"""Tests for exponential backoff retry in _generate_narrative()."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.report.generator import (
    _RETRY_INITIAL_DELAY,
    _is_retryable_llm_error,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# _is_retryable_llm_error unit tests
# ---------------------------------------------------------------------------


class TestIsRetryableLlmError:
    def test_rate_limit_429(self) -> None:
        exc = Exception("rate limit")
        exc.status_code = 429  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is True

    def test_server_error_500(self) -> None:
        exc = Exception("internal error")
        exc.status_code = 500  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is True

    def test_server_error_502(self) -> None:
        exc = Exception("bad gateway")
        exc.status_code = 502  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is True

    def test_server_error_503(self) -> None:
        exc = Exception("service unavailable")
        exc.status_code = 503  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is True

    def test_auth_error_401_not_retryable(self) -> None:
        exc = Exception("unauthorized")
        exc.status_code = 401  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is False

    def test_auth_error_403_not_retryable(self) -> None:
        exc = Exception("forbidden")
        exc.status_code = 403  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is False

    def test_bad_request_400_not_retryable(self) -> None:
        exc = Exception("bad request")
        exc.status_code = 400  # type: ignore[attr-defined]
        assert _is_retryable_llm_error(exc) is False

    def test_connection_error_retryable(self) -> None:
        assert _is_retryable_llm_error(ConnectionError("refused")) is True

    def test_timeout_error_retryable(self) -> None:
        assert _is_retryable_llm_error(TimeoutError("timed out")) is True

    def test_os_error_retryable(self) -> None:
        assert _is_retryable_llm_error(OSError("network unreachable")) is True

    def test_generic_exception_not_retryable(self) -> None:
        assert _is_retryable_llm_error(ValueError("bad value")) is False


# ---------------------------------------------------------------------------
# _generate_narrative retry behaviour
# ---------------------------------------------------------------------------


def _make_rate_limit_error(message: str = "rate limit") -> Exception:
    """Create an exception that looks like a 429 from an LLM SDK."""
    exc = Exception(message)
    exc.status_code = 429  # type: ignore[attr-defined]
    return exc


def _make_auth_error(message: str = "unauthorized") -> Exception:
    exc = Exception(message)
    exc.status_code = 401  # type: ignore[attr-defined]
    return exc


class TestGenerateNarrativeRetry:
    @pytest.mark.asyncio
    async def test_no_retry_when_max_retry_zero(self, mock_settings: Any) -> None:
        """With max_retry_seconds=0 (default), a 429 fails immediately."""
        with patch("src.agent.llm.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(side_effect=_make_rate_limit_error())
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            result = await _generate_narrative({"alerts": None}, max_retry_seconds=0)

        assert "Narrative unavailable" in result
        assert mock_llm.ainvoke.await_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self, mock_settings: Any) -> None:
        """Transient 429 is retried; second attempt succeeds."""
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.report.generator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            success_response = MagicMock()
            success_response.content = "All systems operational."
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(
                side_effect=[_make_rate_limit_error(), success_response],
            )
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            result = await _generate_narrative(
                {"alerts": None},
                max_retry_seconds=300,
            )

        assert result == "All systems operational."
        assert mock_llm.ainvoke.await_count == 2
        mock_sleep.assert_awaited_once()
        slept = mock_sleep.await_args[0][0]
        assert slept == pytest.approx(_RETRY_INITIAL_DELAY)

    @pytest.mark.asyncio
    async def test_non_retryable_error_fails_immediately(self, mock_settings: Any) -> None:
        """A 401 auth error is not retried, even with a large retry budget."""
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.report.generator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(side_effect=_make_auth_error())
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            result = await _generate_narrative(
                {"alerts": None},
                max_retry_seconds=21600,
            )

        assert "Narrative unavailable" in result
        assert mock_llm.ainvoke.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self, mock_settings: Any) -> None:
        """Verify delays double on consecutive failures."""
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.report.generator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            success_response = MagicMock()
            success_response.content = "Report narrative."
            mock_llm = MagicMock()
            # Fail 3 times, then succeed
            mock_llm.ainvoke = AsyncMock(
                side_effect=[
                    _make_rate_limit_error(),
                    _make_rate_limit_error(),
                    _make_rate_limit_error(),
                    success_response,
                ],
            )
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            result = await _generate_narrative(
                {"alerts": None},
                max_retry_seconds=21600,
            )

        assert result == "Report narrative."
        assert mock_llm.ainvoke.await_count == 4
        assert mock_sleep.await_count == 3

        delays = [call.args[0] for call in mock_sleep.await_args_list]
        # 30, 60, 120 — each doubles
        assert delays[0] == pytest.approx(30.0)
        assert delays[1] == pytest.approx(60.0)
        assert delays[2] == pytest.approx(120.0)

    @pytest.mark.asyncio
    async def test_gives_up_when_budget_exhausted(self, mock_settings: Any) -> None:
        """When the retry budget runs out, returns the fallback string."""
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.report.generator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("src.report.generator.time.monotonic") as mock_monotonic,
        ):
            # Simulate: first call at t=0, budget=60s.
            # After first failure + sleep(30), clock is at t=35.
            # Second failure: remaining = 60 - 35 = 25, next delay would be 60
            # but remaining > 0 so it sleeps 25. Clock at t=62 → remaining < 0 → give up.
            clock = [0.0]

            def advancing_clock() -> float:
                val = clock[0]
                clock[0] += 5.0  # each monotonic call advances 5s
                return val

            mock_monotonic.side_effect = advancing_clock

            async def fake_sleep(seconds: float) -> None:
                clock[0] += seconds

            mock_sleep.side_effect = fake_sleep

            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(side_effect=_make_rate_limit_error())
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            result = await _generate_narrative(
                {"alerts": None},
                max_retry_seconds=60,
            )

        assert "Narrative unavailable" in result
        # Should have retried at least once before giving up
        assert mock_llm.ainvoke.await_count >= 2

    @pytest.mark.asyncio
    async def test_success_on_first_try_no_retry(self, mock_settings: Any) -> None:
        """Happy path: LLM succeeds immediately, no retries needed."""
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.report.generator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            success_response = MagicMock()
            success_response.content = "Everything is fine."
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=success_response)
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            result = await _generate_narrative(
                {"alerts": None},
                max_retry_seconds=300,
            )

        assert result == "Everything is fine."
        assert mock_llm.ainvoke.await_count == 1
        mock_sleep.assert_not_awaited()
