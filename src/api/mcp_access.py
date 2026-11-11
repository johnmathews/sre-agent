"""Check the Cloudflare Access JWT on every HTTP request to ``/mcp``, and name the client.

``McpAccessCheck`` is ASGI middleware inside the mounted FastMCP app (``http_app(middleware=...)``), so it runs ahead
of the MCP transport. It reads the ``Cf-Access-Jwt-Assertion`` header and verifies it with
``src.upgrades.access.verify_access_identity`` against ``MCP_CF_ACCESS_AUD``. ``MCP_ACCESS_MODE`` decides what a
failure does:

- ``off``: nothing is checked; the client is ``unknown``.
- ``log``: a verified caller is named by ``client_label``; a request without a JWT is ``anonymous`` and one with a bad
  JWT is ``invalid``. Every request passes. A refusal is logged at WARNING at most once per (peer, reason) per
  ``WARN_INTERVAL_SECONDS``.
- ``enforce``: the same check, but a request without a valid JWT gets 403.

The mode is read once, when the lifespan builds the MCP app (``mcp_access_config``). ``enforce`` with no AUD or team
domain logs an ERROR there and answers 503 on ``/mcp``; ``log`` with either missing warns there and checks nothing.

The client label goes into ``scope["state"]["mcp_client"]``, and for a verified caller ``identity_ref`` into
``scope["state"]["mcp_identity_ref"]``; ``ToolCallObserver`` (``src/api/mcp_server.py``) reads both through
``request.state``. Neither the token, its claims nor the raw identity is ever logged or used as a label.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.mcp_identity import client_label, identity_ref, mcp_audiences
from src.config import get_settings, parse_client_names
from src.observability.metrics import MCP_HTTP_REQUESTS_TOTAL
from src.upgrades.access import AccessDenied, verify_access_identity
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

type Mode = Literal["off", "log", "enforce"]
type AccessResult = Literal["verified", "missing", "invalid", "unconfigured", "off"]

JWT_HEADER = b"cf-access-jwt-assertion"
# request.state keys read by ToolCallObserver
STATE_CLIENT = "mcp_client"
STATE_IDENTITY_REF = "mcp_identity_ref"
# Fixed client labels for a request that is not verified (a verified one gets client_label()).
CLIENT_UNKNOWN = "unknown"
CLIENT_ANONYMOUS = "anonymous"
CLIENT_INVALID = "invalid"
WARN_INTERVAL_SECONDS = 600.0
# Bound on the (peer, reason) pairs remembered for warn-once; the oldest are dropped first.
MAX_WARN_KEYS = 1024
FORBIDDEN = {"error": "Cloudflare Access JWT required"}
MISCONFIGURED = {"error": "MCP access check misconfigured"}


def _monotonic() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class McpAccessConfig:
    """The access check's settings, read once at startup."""

    mode: Mode
    audiences: tuple[str, ...]
    team_domain_configured: bool

    @property
    def configured(self) -> bool:
        """Both an AUD and a team domain are set, so a JWT can be verified."""
        return bool(self.audiences) and self.team_domain_configured

    def missing_settings(self) -> list[str]:
        missing = [] if self.audiences else ["MCP_CF_ACCESS_AUD"]
        return missing if self.team_domain_configured else [*missing, "CF_ACCESS_TEAM_DOMAIN"]


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def mcp_access_config() -> McpAccessConfig:
    """Read the access check's settings and log them: one INFO line, plus an ERROR (``enforce``) or a WARNING
    (``log``) when the AUD or team domain is missing. Never logs a setting's value."""
    settings = get_settings()
    config = McpAccessConfig(
        mode=settings.mcp_access_mode,
        audiences=tuple(mcp_audiences()),
        team_domain_configured=bool(settings.cf_access_team_domain),
    )
    logger.info(
        "MCP access check: mode=%s aud_configured=%s team_domain_configured=%s client_names=%d",
        config.mode,
        _yes_no(bool(config.audiences)),
        _yes_no(config.team_domain_configured),
        len(parse_client_names(settings.mcp_client_names)),
    )
    if config.mode != "off" and not config.configured:
        missing = " and ".join(config.missing_settings())
        if config.mode == "enforce":
            logger.error(
                "MCP access check misconfigured: MCP_ACCESS_MODE=enforce but %s is empty; /mcp answers 503", missing
            )
        else:
            logger.warning(
                "MCP access check inactive: MCP_ACCESS_MODE=log but %s is empty; /mcp callers are not verified", missing
            )
    return config


def _token(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == JWT_HEADER:
            token: str = value.decode("latin-1")
            return token
    return None


def _peer(scope: Scope) -> str:
    client = scope.get("client")
    return clean_text(str(client[0])) if client else "unknown"


def _count(client: str, access: AccessResult) -> None:
    MCP_HTTP_REQUESTS_TOTAL.labels(client=client, access=access).inc()


class McpAccessCheck:
    """ASGI middleware: check the Access JWT on each HTTP request to ``/mcp`` in the configured mode."""

    def __init__(self, app: ASGIApp, config: McpAccessConfig) -> None:
        self.app = app
        self.config = config
        # (peer, reason) -> monotonic time of its last WARNING
        self._warned: dict[tuple[str, str], float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state: dict[str, Any] = scope.setdefault("state", {})
        if self.config.mode == "off" or not self.config.configured:
            _count(CLIENT_UNKNOWN, "off" if self.config.mode == "off" else "unconfigured")
            if self.config.mode == "enforce":
                await JSONResponse(MISCONFIGURED, status_code=503)(scope, receive, send)
                return
            state[STATE_CLIENT] = CLIENT_UNKNOWN
            await self.app(scope, receive, send)
            return

        token = _token(scope)
        try:
            identity = await verify_access_identity(token, list(self.config.audiences))
        except Exception as exc:
            client = CLIENT_INVALID if token else CLIENT_ANONYMOUS
            _count(client, "invalid" if token else "missing")
            self._report(_peer(scope), exc)
            if self.config.mode == "enforce":
                await JSONResponse(FORBIDDEN, status_code=403)(scope, receive, send)
                return
            state[STATE_CLIENT] = client
            await self.app(scope, receive, send)
            return

        label = client_label(identity)
        _count(label, "verified")
        state[STATE_CLIENT] = label
        state[STATE_IDENTITY_REF] = identity_ref(identity)
        await self.app(scope, receive, send)

    def _report(self, peer: str, exc: Exception) -> None:
        """Log a refused request at most once per (peer, reason) per ``WARN_INTERVAL_SECONDS``.

        An ``AccessDenied`` message is a reason, never token text. Any other exception is a bug in the check: ERROR,
        with its class only, since its message could quote the token."""
        expected = isinstance(exc, AccessDenied)
        reason = clean_text(str(exc)) if expected else f"verification failed ({type(exc).__name__})"
        if not self._first_in_interval((peer, reason)):
            return
        outcome = "refused with 403" if self.config.mode == "enforce" else "allowed (log mode)"
        level = logging.WARNING if expected else logging.ERROR
        logger.log(
            level,
            "MCP access check: request from peer %s without a valid Access JWT (%s), %s; repeats from this peer for "
            "this reason are not logged for %d minutes",
            peer,
            reason,
            outcome,
            int(WARN_INTERVAL_SECONDS // 60),
        )

    def _first_in_interval(self, key: tuple[str, str]) -> bool:
        now = _monotonic()
        last = self._warned.get(key)
        if last is not None and now - last < WARN_INTERVAL_SECONDS:
            return False
        # Re-inserting keeps the dict ordered by last warning, so the front holds the oldest (and any stale) keys.
        self._warned.pop(key, None)
        while len(self._warned) >= MAX_WARN_KEYS:
            del self._warned[next(iter(self._warned))]
        self._warned[key] = now
        return True
