"""Tests for OAuth token auto-refresh logic."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.agent import oauth_refresh
from src.agent.oauth_refresh import ensure_valid_token, get_token_health


@pytest.fixture(autouse=True)
def _reset_rejected_token() -> "object":
    """Clear the module-level rejected-refresh-token state around each test.

    The rejection flag is process-global (single-worker deployment), so without
    this reset state would leak between tests depending on execution order.
    """
    oauth_refresh._rejected_refresh_token_hash = None
    yield
    oauth_refresh._rejected_refresh_token_hash = None


def _make_creds(expires_at: int, refresh_token: str = "sk-ant-ort01-fake") -> dict[str, object]:
    return {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-old",
            "refreshToken": refresh_token,
            "expiresAt": expires_at,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        }
    }


def _mock_async_client(mock_response: object) -> AsyncMock:
    """Build a mock httpx.AsyncClient context manager returning mock_response."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm, mock_client


@pytest.mark.asyncio
async def test_skip_when_token_still_valid(tmp_path: Path) -> None:
    """Do not refresh when token is not near expiry."""
    creds_path = tmp_path / ".credentials.json"
    far_future_ms = int((time.time() + 3600) * 1000)  # 1 hour from now
    creds_path.write_text(json.dumps(_make_creds(far_future_ms)))

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient") as mock_cls,
    ):
        await ensure_valid_token()
        mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_when_token_expired(tmp_path: Path) -> None:
    """Refresh token when access token has expired."""
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)  # 1 minute ago
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    new_access = "sk-ant-oat01-new-access"
    new_refresh = "sk-ant-ort01-new-refresh"
    mock_response = type(
        "R",
        (),
        {
            "status_code": 200,
            "json": lambda self: {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_in": 28800,
                "token_type": "Bearer",
            },
            "text": "",
        },
    )()

    mock_cm, mock_client = _mock_async_client(mock_response)

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient", return_value=mock_cm),
    ):
        await ensure_valid_token()

    updated = json.loads(creds_path.read_text())
    assert updated["claudeAiOauth"]["accessToken"] == new_access
    assert updated["claudeAiOauth"]["refreshToken"] == new_refresh
    assert updated["claudeAiOauth"]["expiresAt"] > int(time.time() * 1000)


@pytest.mark.asyncio
async def test_refresh_within_buffer(tmp_path: Path) -> None:
    """Refresh token when within the 5-minute buffer before expiry."""
    creds_path = tmp_path / ".credentials.json"
    # Expires in 2 minutes (within the 5-minute buffer)
    near_expiry_ms = int((time.time() + 120) * 1000)
    creds_path.write_text(json.dumps(_make_creds(near_expiry_ms)))

    mock_response = type(
        "R",
        (),
        {
            "status_code": 200,
            "json": lambda self: {
                "access_token": "sk-ant-oat01-refreshed",
                "refresh_token": "sk-ant-ort01-refreshed",
                "expires_in": 28800,
                "token_type": "Bearer",
            },
            "text": "",
        },
    )()

    mock_cm, _ = _mock_async_client(mock_response)

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient", return_value=mock_cm),
    ):
        await ensure_valid_token()

    updated = json.loads(creds_path.read_text())
    assert updated["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-refreshed"


@pytest.mark.asyncio
async def test_no_credentials_file(tmp_path: Path) -> None:
    """Silently skip when no credentials file exists."""
    creds_path = tmp_path / ".credentials.json"  # does not exist

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient") as mock_cls,
    ):
        await ensure_valid_token()  # should not raise
        mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_failure_does_not_raise(tmp_path: Path) -> None:
    """A failed refresh logs a warning but does not raise."""
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    mock_response = type(
        "R",
        (),
        {
            "status_code": 400,
            "json": lambda self: {"error": "invalid_grant"},
            "text": '{"error": "invalid_grant"}',
        },
    )()

    mock_cm, _ = _mock_async_client(mock_response)

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient", return_value=mock_cm),
    ):
        await ensure_valid_token()  # should not raise

    # Credentials should be unchanged
    unchanged = json.loads(creds_path.read_text())
    assert unchanged["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-old"


@pytest.mark.asyncio
async def test_network_error_does_not_raise(tmp_path: Path) -> None:
    """A network error during refresh does not raise."""
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient", return_value=mock_cm),
    ):
        await ensure_valid_token()  # should not raise


@pytest.mark.asyncio
async def test_concurrent_refresh_serialized(tmp_path: Path) -> None:
    """Concurrent ensure_valid_token calls are serialized by the lock.

    Two concurrent calls with an expired token should only produce one
    refresh HTTP call (the second caller sees the already-refreshed token).
    """
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    call_count = 0

    async def _mock_post(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        # Simulate the refresh updating the credentials file
        new_expiry = int((time.time() + 28800) * 1000)
        creds_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-refreshed",
                        "refreshToken": "sk-ant-ort01-refreshed",
                        "expiresAt": new_expiry,
                        "scopes": ["user:inference"],
                        "subscriptionType": "max",
                        "rateLimitTier": "default_claude_max_5x",
                    }
                }
            )
        )
        return type(
            "R",
            (),
            {
                "status_code": 200,
                "json": lambda self: {
                    "access_token": "sk-ant-oat01-refreshed",
                    "refresh_token": "sk-ant-ort01-refreshed",
                    "expires_in": 28800,
                },
                "text": "",
            },
        )()

    mock_client = AsyncMock()
    mock_client.post = _mock_post

    def _make_mock_cm(*args: object, **kwargs: object) -> AsyncMock:
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient", side_effect=_make_mock_cm),
    ):
        # Fire two concurrent refresh calls
        await asyncio.gather(ensure_valid_token(), ensure_valid_token())

    # The lock serializes access: the first call refreshes, the second sees
    # the updated token and skips the HTTP call.
    assert call_count == 1, f"Expected 1 refresh call but got {call_count}"


# ---------------------------------------------------------------------------
# get_token_health: status reflects "does a human need to act?", not expiry timing.
# Refresh-token-present means the system self-heals on the next LLM call.
# ---------------------------------------------------------------------------


class TestGetTokenHealth:
    def test_healthy_when_token_valid_with_refresh(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        far_future_ms = int((time.time() + 3600) * 1000)  # 1h ahead
        creds_path.write_text(json.dumps(_make_creds(far_future_ms)))
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "healthy"

    def test_healthy_when_near_expiry_with_refresh(self, tmp_path: Path) -> None:
        # Inside the previous "< 1h remaining" window — must no longer be degraded.
        creds_path = tmp_path / ".credentials.json"
        soon_ms = int((time.time() + 600) * 1000)  # 10 min ahead
        creds_path.write_text(json.dumps(_make_creds(soon_ms)))
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "healthy"

    def test_healthy_when_expired_with_refresh(self, tmp_path: Path) -> None:
        # Self-heals on next call; not a real degradation.
        creds_path = tmp_path / ".credentials.json"
        past_ms = int((time.time() - 600) * 1000)  # expired 10 min ago
        creds_path.write_text(json.dumps(_make_creds(past_ms)))
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, detail = get_token_health()
        assert status == "healthy"
        assert detail is not None
        assert "refresh" in detail.lower()

    def test_degraded_when_valid_but_no_refresh_token(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        far_future_ms = int((time.time() + 3600) * 1000)
        creds_path.write_text(json.dumps(_make_creds(far_future_ms, refresh_token="")))
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "degraded"

    def test_unhealthy_when_expired_and_no_refresh_token(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        past_ms = int((time.time() - 600) * 1000)
        creds_path.write_text(json.dumps(_make_creds(past_ms, refresh_token="")))
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "unhealthy"

    def test_healthy_when_no_credentials_file(self, tmp_path: Path) -> None:
        creds_path = tmp_path / "missing.json"
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "healthy"

    def test_unhealthy_when_credentials_unreadable(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text("{not valid json")
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "unhealthy"


# ---------------------------------------------------------------------------
# Rejected refresh token: a refresh token can be *present but rejected* by the
# server (HTTP 400 invalid_grant). That is NOT self-healing — a human must
# re-authenticate — so health must report "unhealthy" while that token is on disk.
# ---------------------------------------------------------------------------


async def _attempt_refresh(creds_path: Path, *, status_code: int, body: dict[str, object]) -> None:
    """Run ensure_valid_token() against a mocked token endpoint response."""
    mock_response = type(
        "R",
        (),
        {
            "status_code": status_code,
            "json": lambda self: body,
            "text": json.dumps(body),
        },
    )()
    mock_cm, _ = _mock_async_client(mock_response)
    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.AsyncClient", return_value=mock_cm),
    ):
        await ensure_valid_token()


class TestRejectedRefreshToken:
    @pytest.mark.asyncio
    async def test_unhealthy_after_invalid_grant_for_same_token(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        past_ms = int((time.time() - 60) * 1000)
        creds_path.write_text(json.dumps(_make_creds(past_ms, refresh_token="sk-ant-ort01-dead")))

        await _attempt_refresh(creds_path, status_code=400, body={"error": "invalid_grant"})

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, detail = get_token_health()
        assert status == "unhealthy"
        assert detail is not None
        assert "claude login" in detail.lower()

    @pytest.mark.asyncio
    async def test_self_clears_when_token_replaced_by_reauth(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        past_ms = int((time.time() - 60) * 1000)
        creds_path.write_text(json.dumps(_make_creds(past_ms, refresh_token="sk-ant-ort01-dead")))

        await _attempt_refresh(creds_path, status_code=400, body={"error": "invalid_grant"})

        # Operator re-authenticates: a brand-new credentials file lands on disk.
        far_future_ms = int((time.time() + 3600) * 1000)
        creds_path.write_text(json.dumps(_make_creds(far_future_ms, refresh_token="sk-ant-ort01-fresh")))

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "healthy"

    @pytest.mark.asyncio
    async def test_transient_server_error_does_not_mark_unhealthy(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        past_ms = int((time.time() - 60) * 1000)
        creds_path.write_text(json.dumps(_make_creds(past_ms)))

        await _attempt_refresh(creds_path, status_code=503, body={"error": "service_unavailable"})

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "healthy"

    @pytest.mark.asyncio
    async def test_successful_refresh_clears_prior_rejection(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        past_ms = int((time.time() - 60) * 1000)
        creds_path.write_text(json.dumps(_make_creds(past_ms, refresh_token="sk-ant-ort01-dead")))

        await _attempt_refresh(creds_path, status_code=400, body={"error": "invalid_grant"})
        assert oauth_refresh._rejected_refresh_token_hash is not None

        # A later refresh of the same (still-expired) token succeeds.
        await _attempt_refresh(
            creds_path,
            status_code=200,
            body={
                "access_token": "sk-ant-oat01-new",
                "refresh_token": "sk-ant-ort01-new",
                "expires_in": 28800,
            },
        )

        assert oauth_refresh._rejected_refresh_token_hash is None
        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            status, _ = get_token_health()
        assert status == "healthy"


class TestCredentialsHealthy:
    def test_one_while_healthy(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() + 3600) * 1000))))

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            assert oauth_refresh.credentials_healthy() == 1.0

    def test_one_while_access_token_expired_but_refreshable(self, tmp_path: Path) -> None:
        # Routine: the access token lapses every ~8h and the next refresh fixes it.
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() - 60) * 1000))))

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            assert oauth_refresh.credentials_healthy() == 1.0

    @pytest.mark.asyncio
    async def test_zero_after_refresh_token_rejected(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() - 60) * 1000))))

        await _attempt_refresh(creds_path, status_code=400, body={"error": "invalid_grant"})

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            assert oauth_refresh.credentials_healthy() == 0.0

    def test_zero_when_degraded(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() + 3600) * 1000), refresh_token="")))

        with patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path):
            assert oauth_refresh.credentials_healthy() == 0.0

    def test_zero_when_health_check_raises(self) -> None:
        with patch("src.agent.oauth_refresh.get_token_health", side_effect=RuntimeError("boom")):
            assert oauth_refresh.credentials_healthy() == 0.0


class _StopLoop(BaseException):
    """Raised from a patched sleep to end keep_fresh's infinite loop."""


def _sleep_then_stop(after: int) -> AsyncMock:
    """A replacement for asyncio.sleep that lets ``after`` iterations run, then stops."""
    calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= after:
            raise _StopLoop

    return AsyncMock(side_effect=fake_sleep)


class TestKeepFresh:
    @pytest.mark.asyncio
    async def test_idle_dead_refresh_token_turns_gauge_red_without_a_question(self, tmp_path: Path) -> None:
        # The 2026-10-24 failure: access token long expired, refresh token dead,
        # nobody asking questions. The timer alone must surface it.
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() - 61 * 86400) * 1000))))
        body = {"error": "invalid_grant", "error_description": "Refresh token expired"}
        mock_response = type(
            "R",
            (),
            {"status_code": 400, "json": lambda self: body, "text": json.dumps(body)},
        )()
        mock_cm, mock_client = _mock_async_client(mock_response)

        with (
            patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
            patch("src.agent.oauth_refresh.httpx.AsyncClient", return_value=mock_cm),
            patch("src.agent.oauth_refresh.asyncio.sleep", _sleep_then_stop(after=1)),
        ):
            assert oauth_refresh.credentials_healthy() == 1.0
            with pytest.raises(_StopLoop):
                await oauth_refresh.keep_fresh(900.0)
            assert oauth_refresh.credentials_healthy() == 0.0

        mock_client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runs_every_interval(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() + 3600) * 1000))))
        sleep = _sleep_then_stop(after=3)

        with (
            patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
            patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock) as mock_ensure,
            patch("src.agent.oauth_refresh.asyncio.sleep", sleep),
            pytest.raises(_StopLoop),
        ):
            await oauth_refresh.keep_fresh(900.0)

        assert mock_ensure.await_count == 3
        sleep.assert_awaited_with(900.0)

    @pytest.mark.asyncio
    async def test_survives_a_failing_iteration(self, tmp_path: Path) -> None:
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(json.dumps(_make_creds(int((time.time() + 3600) * 1000))))

        with (
            patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
            patch(
                "src.agent.oauth_refresh.ensure_valid_token",
                new_callable=AsyncMock,
                side_effect=[RuntimeError("boom"), None],
            ) as mock_ensure,
            patch("src.agent.oauth_refresh.asyncio.sleep", _sleep_then_stop(after=2)),
            pytest.raises(_StopLoop),
        ):
            await oauth_refresh.keep_fresh(900.0)

        assert mock_ensure.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_credentials_file(self, tmp_path: Path) -> None:
        with (
            patch("src.agent.oauth_refresh._credentials_path", return_value=tmp_path / "missing.json"),
            patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock) as mock_ensure,
            patch("src.agent.oauth_refresh.asyncio.sleep", _sleep_then_stop(after=1)),
            pytest.raises(_StopLoop),
        ):
            await oauth_refresh.keep_fresh(900.0)

        mock_ensure.assert_not_awaited()
