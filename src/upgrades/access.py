"""Verify the Cloudflare Access JWT (``Cf-Access-Jwt-Assertion``) on the approval endpoints and on ``/mcp``.

The endpoint does not trust that Cloudflare sits in front of it: every request must carry a JWT signed (RS256) by one
of the team's Access keys, for this application's audience, issued by the team domain. Anything else is
``AccessDenied``. Log lines and exception messages name the reason only, never the token or its claims.

Two entry points share one decode (``_decode_claims``):

- ``verify_access_jwt`` is the approval check. It reads ``CF_ACCESS_AUD`` and requires an ``email`` claim, so a
  service token (which carries ``common_name`` and no ``email``) can never approve an upgrade.
- ``verify_access_identity`` names the caller of ``/mcp``. The caller passes the audiences, and either an ``email``
  (a person) or a ``common_name`` (a service token's Client ID) is accepted.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import jwt

from src.config import get_settings
from src.upgrades.http import ResponseTooLarge, bounded_request
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
JWKS_TTL_SECONDS = 3600.0
# An unknown kid triggers a refetch at most this often, counted from the last fetch attempt of any kind.
JWKS_MIN_REFETCH_SECONDS = 60.0
JWKS_TIMEOUT_SECONDS = 10.0
JWKS_MAX_BYTES = 256 * 1024
LEEWAY_SECONDS = 60
MAX_TOKEN_CHARS = 16 * 1024
MAX_EMAIL_CHARS = 320
MAX_COMMON_NAME_CHARS = 256
# A bare hostname (lowercase labels, no scheme, port, path or userinfo) under cloudflareaccess.com.
_TEAM_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+cloudflareaccess\.com")

# team domain -> (monotonic fetch time, kid -> key)
_jwks_cache: dict[str, tuple[float, dict[str, jwt.PyJWK]]] = {}
# team domain -> monotonic time of the last fetch attempt (successful or not)
_last_fetch_attempt: dict[str, float] = {}
# (context, reason) pairs already logged at error level
_logged_config_errors: set[tuple[str, str]] = set()


class AccessDenied(Exception):
    """The request carries no valid Cloudflare Access JWT. The message is a reason, never token text."""


@dataclass(frozen=True)
class _Context:
    """Which check a decode serves: names it in config-error logs and names its audience setting."""

    name: str
    aud_setting: str
    consequence: str


_APPROVAL = _Context("approval", "CF_ACCESS_AUD", "every approval request is denied")
_MCP = _Context("mcp", "MCP_CF_ACCESS_AUD", "no /mcp caller can be verified")


@dataclass(frozen=True)
class AccessIdentity:
    """A verified caller: ``user`` (the ``email`` claim) or ``service`` (a service token's ``common_name``).

    ``value`` is credential-adjacent (a service token's Client ID), so it is left out of ``repr``; log
    ``src.api.mcp_identity.identity_ref`` instead.
    """

    kind: Literal["user", "service"]
    value: str = field(repr=False)


def _monotonic() -> float:
    return time.monotonic()


def _reset_state() -> None:
    """Forget cached keys, fetch times and logged config errors (tests)."""
    _jwks_cache.clear()
    _last_fetch_attempt.clear()
    _logged_config_errors.clear()


def _config_error(reason: str, context: _Context) -> AccessDenied:
    """A misconfiguration denies every request; log it at error level once per (context, reason), not per request.
    The log line names the check it is about (approval or /mcp)."""
    if (context.name, reason) not in _logged_config_errors:
        _logged_config_errors.add((context.name, reason))
        logger.error("Cloudflare Access verification misconfigured (%s); %s", reason, context.consequence)
    return AccessDenied(reason)


def _team_domain(context: _Context) -> str:
    team = get_settings().cf_access_team_domain
    if not team:
        raise _config_error("CF_ACCESS_TEAM_DOMAIN is empty", context)
    if len(team) > 253 or not _TEAM_DOMAIN_RE.fullmatch(team):
        raise _config_error("CF_ACCESS_TEAM_DOMAIN is not a bare *.cloudflareaccess.com hostname", context)
    return team


async def _fetch_keys(team: str) -> dict[str, jwt.PyJWK]:
    """Fetch the team's signing keys; only RSA keys usable with RS256 and carrying a ``kid`` are kept."""
    url = f"https://{team}/cdn-cgi/access/certs"
    try:
        async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await bounded_request(client, "GET", url, max_bytes=JWKS_MAX_BYTES)
    except (httpx.HTTPError, httpx.InvalidURL, ResponseTooLarge) as exc:
        raise AccessDenied(f"JWKS fetch failed ({type(exc).__name__})") from None
    if not response.is_success:
        raise AccessDenied(f"JWKS fetch returned HTTP {response.status_code}")
    try:
        data: Any = response.json()
        if not isinstance(data, dict):
            raise AccessDenied("JWKS is not a JSON object")
        keyset = jwt.PyJWKSet.from_dict(data)
    except (ValueError, jwt.PyJWTError) as exc:
        raise AccessDenied(f"JWKS unreadable ({type(exc).__name__})") from None
    keys = {k.key_id: k for k in keyset.keys if k.key_id and k.key_type == "RSA" and k.algorithm_name == ALGORITHM}
    if not keys:
        raise AccessDenied("JWKS has no RS256 keys")
    return keys


async def _fetch_and_cache(team: str) -> dict[str, jwt.PyJWK]:
    _last_fetch_attempt[team] = _monotonic()
    keys = await _fetch_keys(team)
    _jwks_cache[team] = (_monotonic(), keys)
    return keys


async def _key_for(team: str, kid: str) -> jwt.PyJWK:
    """The key for ``kid``: from the 1 h cache, else a fetch. An unknown ``kid`` (key rotation) triggers a refetch,
    but at most once per ``JWKS_MIN_REFETCH_SECONDS`` since the last fetch attempt, so forged kids cannot make every
    request call out to Cloudflare."""
    cached = _jwks_cache.get(team)
    if cached is None or _monotonic() - cached[0] >= JWKS_TTL_SECONDS:
        keys = await _fetch_and_cache(team)
    else:
        keys = cached[1]
    if kid not in keys and _monotonic() - _last_fetch_attempt.get(team, float("-inf")) >= JWKS_MIN_REFETCH_SECONDS:
        keys = await _fetch_and_cache(team)
    if kid not in keys:
        raise AccessDenied("unknown key id")
    return keys[kid]


async def _decode_claims(token: str | None, audiences: list[str], *, context: _Context) -> dict[str, Any]:
    """The verified claims of an Access JWT: RS256 only, a known ``kid`` (JWKS cached, refetch rate-limited), the
    team's issuer, one of ``audiences``, and ``exp``/``iat`` within leeway. Raises ``AccessDenied``."""
    if not token:
        raise AccessDenied("missing Cf-Access-Jwt-Assertion header")
    if len(token) > MAX_TOKEN_CHARS:
        raise AccessDenied("token too long")
    team = _team_domain(context)
    audiences = [a for a in audiences if a]
    if not audiences:
        raise _config_error(f"{context.aud_setting} is empty", context)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AccessDenied(f"unreadable token header ({type(exc).__name__})") from None
    if header.get("alg") != ALGORITHM:
        raise AccessDenied("token algorithm is not RS256")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AccessDenied("token has no key id")
    key = await _key_for(team, kid)
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key.key,
            algorithms=[ALGORITHM],
            audience=audiences,
            issuer=f"https://{team}",
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise AccessDenied(f"token rejected ({type(exc).__name__})") from None
    return claims


def _usable(claims: dict[str, Any], name: str, max_chars: int) -> str | None:
    value = claims.get(name)
    if not isinstance(value, str) or not value or len(value) > max_chars:
        return None
    return value


async def _verify(token: str | None) -> str:
    claims = await _decode_claims(token, [get_settings().cf_access_aud], context=_APPROVAL)
    email = _usable(claims, "email", MAX_EMAIL_CHARS)
    if email is None:
        raise AccessDenied("token has no usable email claim")
    return email


async def verify_access_jwt(token: str | None) -> str:
    """Return the verified ``email`` claim of a Cloudflare Access JWT, or raise ``AccessDenied``."""
    try:
        return await _verify(token)
    except AccessDenied as exc:
        logger.warning("Cloudflare Access JWT rejected: %s", clean_text(str(exc)))
        raise


async def verify_access_identity(token: str | None, audiences: list[str]) -> AccessIdentity:
    """Return who a Cloudflare Access JWT for one of ``audiences`` speaks for, or raise ``AccessDenied``.

    A usable ``email`` makes a ``user``; otherwise a non-empty ``common_name`` of at most 256 characters makes a
    ``service`` (Cloudflare puts a service token's Client ID there and leaves ``email`` out). This never feeds the
    approval path, which stays on ``verify_access_jwt``. Rejections are not logged here, except a misconfiguration
    once: the caller decides how loudly to report a refused request.
    """
    claims = await _decode_claims(token, audiences, context=_MCP)
    email = _usable(claims, "email", MAX_EMAIL_CHARS)
    if email is not None:
        return AccessIdentity(kind="user", value=email)
    common_name = _usable(claims, "common_name", MAX_COMMON_NAME_CHARS)
    if common_name is not None:
        return AccessIdentity(kind="service", value=common_name)
    raise AccessDenied("token has no usable email or common_name claim")
