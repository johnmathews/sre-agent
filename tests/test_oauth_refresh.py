"""Tests for OAuth token auto-refresh logic."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agent.oauth_refresh import _REFRESH_BUFFER_MS, _credentials_path, ensure_valid_token


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


def test_skip_when_token_still_valid(tmp_path: Path) -> None:
    """Do not refresh when token is not near expiry."""
    creds_path = tmp_path / ".credentials.json"
    far_future_ms = int((time.time() + 3600) * 1000)  # 1 hour from now
    creds_path.write_text(json.dumps(_make_creds(far_future_ms)))

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx") as mock_httpx,
    ):
        ensure_valid_token()
        mock_httpx.post.assert_not_called()


def test_refresh_when_token_expired(tmp_path: Path) -> None:
    """Refresh token when access token has expired."""
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)  # 1 minute ago
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    new_access = "sk-ant-oat01-new-access"
    new_refresh = "sk-ant-ort01-new-refresh"
    mock_response = type("R", (), {
        "status_code": 200,
        "json": lambda self: {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": 28800,
            "token_type": "Bearer",
        },
        "text": "",
    })()

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.post", return_value=mock_response),
    ):
        ensure_valid_token()

    updated = json.loads(creds_path.read_text())
    assert updated["claudeAiOauth"]["accessToken"] == new_access
    assert updated["claudeAiOauth"]["refreshToken"] == new_refresh
    assert updated["claudeAiOauth"]["expiresAt"] > int(time.time() * 1000)


def test_refresh_within_buffer(tmp_path: Path) -> None:
    """Refresh token when within the 5-minute buffer before expiry."""
    creds_path = tmp_path / ".credentials.json"
    # Expires in 2 minutes (within the 5-minute buffer)
    near_expiry_ms = int((time.time() + 120) * 1000)
    creds_path.write_text(json.dumps(_make_creds(near_expiry_ms)))

    mock_response = type("R", (), {
        "status_code": 200,
        "json": lambda self: {
            "access_token": "sk-ant-oat01-refreshed",
            "refresh_token": "sk-ant-ort01-refreshed",
            "expires_in": 28800,
            "token_type": "Bearer",
        },
        "text": "",
    })()

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.post", return_value=mock_response),
    ):
        ensure_valid_token()

    updated = json.loads(creds_path.read_text())
    assert updated["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-refreshed"


def test_no_credentials_file(tmp_path: Path) -> None:
    """Silently skip when no credentials file exists."""
    creds_path = tmp_path / ".credentials.json"  # does not exist

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx") as mock_httpx,
    ):
        ensure_valid_token()  # should not raise
        mock_httpx.post.assert_not_called()


def test_refresh_failure_does_not_raise(tmp_path: Path) -> None:
    """A failed refresh logs a warning but does not raise."""
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    mock_response = type("R", (), {
        "status_code": 400,
        "json": lambda self: {"error": "invalid_grant"},
        "text": '{"error": "invalid_grant"}',
    })()

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.post", return_value=mock_response),
    ):
        ensure_valid_token()  # should not raise

    # Credentials should be unchanged
    unchanged = json.loads(creds_path.read_text())
    assert unchanged["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-old"


def test_network_error_does_not_raise(tmp_path: Path) -> None:
    """A network error during refresh does not raise."""
    creds_path = tmp_path / ".credentials.json"
    past_ms = int((time.time() - 60) * 1000)
    creds_path.write_text(json.dumps(_make_creds(past_ms)))

    with (
        patch("src.agent.oauth_refresh._credentials_path", return_value=creds_path),
        patch("src.agent.oauth_refresh.httpx.post", side_effect=Exception("connection refused")),
    ):
        ensure_valid_token()  # should not raise
