"""OAuth token auto-refresh for Claude Agent SDK credentials.

The Claude CLI's OAuth access tokens expire every ~8 hours.  In headless
environments (Docker) the CLI does not auto-refresh them, so the app must
handle refresh before each SDK query.

Flow:
  1. Read ``$CLAUDE_CONFIG_DIR/.credentials.json``
  2. If ``expiresAt`` is in the past (or within a 5-minute buffer), call
     ``POST https://api.anthropic.com/v1/oauth/token`` with the stored
     refresh token to get a new access token.
  3. Write the updated credentials back.
  4. On any failure, log a warning and return — the SDK query will proceed
     with the (possibly stale) token and surface its own auth error.
"""

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_REFRESH_BUFFER_MS = 5 * 60 * 1000  # refresh 5 minutes before expiry


def _credentials_path() -> Path:
    """Return the path to the Claude credentials file."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    return Path(config_dir) / ".credentials.json"


def ensure_valid_token() -> None:
    """Check the OAuth access token and refresh it if expired or near-expiry.

    This is a best-effort operation — any failure is logged and swallowed
    so it never blocks the agent from attempting its query.
    """
    try:
        _refresh_if_needed()
    except Exception:
        logger.warning("OAuth token refresh failed", exc_info=True)


def _refresh_if_needed() -> None:
    """Read credentials, check expiry, refresh if needed."""
    creds_path = _credentials_path()
    if not creds_path.exists():
        logger.debug("No credentials file at %s — skipping refresh", creds_path)
        return

    creds_text = creds_path.read_text()
    creds: dict[str, object] = json.loads(creds_text)

    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        logger.debug("No claudeAiOauth in credentials — skipping refresh")
        return

    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        logger.debug("No expiresAt in credentials — skipping refresh")
        return

    now_ms = int(time.time() * 1000)
    if now_ms < expires_at - _REFRESH_BUFFER_MS:
        logger.debug("Token still valid (expires in %ds)", (expires_at - now_ms) / 1000)
        return

    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        logger.warning("No refresh token available — cannot auto-refresh")
        return

    logger.info("OAuth access token expired or near-expiry, refreshing...")
    _do_refresh(creds_path, oauth, refresh_token)


def _do_refresh(
    creds_path: Path,
    oauth: dict[str, object],
    refresh_token: str,
) -> None:
    """Call the OAuth token endpoint and save the new credentials."""
    resp = httpx.post(
        _OAUTH_TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLAUDE_CODE_CLIENT_ID,
        },
        headers={"Content-Type": "application/json"},
        follow_redirects=True,
        timeout=15.0,
    )

    if resp.status_code != 200:
        logger.warning(
            "OAuth refresh returned HTTP %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return

    data: dict[str, object] = resp.json()
    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")

    if not isinstance(new_access, str) or not isinstance(expires_in, (int, float)):
        logger.warning("OAuth refresh response missing expected fields: %s", list(data.keys()))
        return

    # Update credentials preserving existing fields (scopes, subscriptionType, etc.)
    oauth["accessToken"] = new_access
    if isinstance(new_refresh, str) and new_refresh:
        oauth["refreshToken"] = new_refresh
    oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)

    new_creds = {"claudeAiOauth": oauth}

    # Atomic write: write to temp file then rename
    tmp_path = creds_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(new_creds))
    tmp_path.rename(creds_path)

    logger.info(
        "OAuth token refreshed successfully (expires in %ds)",
        int(float(expires_in)),
    )
