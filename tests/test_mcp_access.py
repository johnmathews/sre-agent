"""The Cloudflare Access check on /mcp (src/api/mcp_access.py): off, log and enforce modes.

JWKS is always a respx mock on the placeholder team domain, so no test reaches Cloudflare.
"""

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.types import Message, Receive, Scope, Send

from src.api import mcp_access
from src.api.mcp_access import McpAccessCheck, mcp_access_config
from src.api.mcp_identity import identity_ref
from src.upgrades import access
from src.upgrades.access import AccessIdentity
from tests.test_mcp_http_integration import HEADERS, _initialize, _mock_search, _rpc
from tests.test_tool_calls import LOGGER as TOOL_CALL_LOGGER
from tests.test_tool_calls import fields, tool_call_records
from tests.upgrades_access_helpers import AUD, CERTS_URL, SERVICE_CLIENT_ID, TEAM, Signer, jwks

K1 = Signer("k1")
STRANGER = Signer("k-unknown")  # its kid is not in the JWKS
ACCESS_LOGGER = "src.api.mcp_access"
JWT_HEADER = "Cf-Access-Jwt-Assertion"
SEARCH = {"name": "prometheus_search_metrics", "arguments": {"search_term": "cpu"}}


@pytest.fixture(autouse=True)
def _reset_access() -> Iterator[None]:
    access._reset_state()
    yield
    access._reset_state()


def _configure(mock_settings: Any, mode: str, *, aud: str = AUD, team: str = TEAM, names: str = "") -> None:
    mock_settings.mcp_access_mode = mode
    mock_settings.mcp_cf_access_aud = aud
    mock_settings.cf_access_team_domain = team
    mock_settings.mcp_client_names = names


def _requests(client: str, access_result: str) -> float:
    labels = {"client": client, "access": access_result}
    return REGISTRY.get_sample_value("sre_assistant_mcp_http_requests_total", labels) or 0.0


def _tool_calls(client: str) -> float:
    labels = {"tool_name": "prometheus_search_metrics", "status": "success", "source": "mcp", "client": client}
    return REGISTRY.get_sample_value("sre_assistant_tool_calls_total", labels) or 0.0


def _certs() -> respx.Route:
    return respx.get(CERTS_URL).mock(return_value=httpx.Response(200, json=jwks(K1)))


def _post(client: TestClient, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return client.post("/mcp", headers={**HEADERS, **(headers or {})}, json=body)


def _app() -> Any:
    from src.api.main import app

    return app


def _no_agent() -> Any:
    return patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent"))


# --- log mode ---


@pytest.mark.integration
@respx.mock
def test_log_mode_passes_missing_jwt_and_counts_missing(mock_settings: Any) -> None:
    """Log mode never rejects: no JWT, a malformed one and one from an unknown key all reach the MCP server."""
    _configure(mock_settings, "log")
    _certs()
    _mock_search(["node_cpu_seconds_total"])
    missing, invalid = _requests("anonymous", "missing"), _requests("invalid", "invalid")
    anonymous_calls, invalid_calls = _tool_calls("anonymous"), _tool_calls("invalid")

    with _no_agent(), TestClient(_app()) as client:
        assert _initialize(client)["result"]["serverInfo"]["name"] == "sre-assistant"
        assert _rpc(client, 2, "tools/call", SEARCH)["result"]["isError"] is False
        assert _rpc(client, 3, "tools/call", SEARCH, {JWT_HEADER: "not-a-jwt"})["result"]["isError"] is False
        assert "tools" in _rpc(client, 4, "tools/list", {}, {JWT_HEADER: STRANGER.service_token()})["result"]

    assert _requests("anonymous", "missing") - missing == 2
    assert _requests("invalid", "invalid") - invalid == 2
    # A refused identity still gets a fixed client label on the tool metric.
    assert _tool_calls("anonymous") - anonymous_calls == 1
    assert _tool_calls("invalid") - invalid_calls == 1


def _scope(peer: str | None, token: str | None = None, scope_type: str = "http") -> Scope:
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((JWT_HEADER.lower().encode(), token.encode()))
    scope: dict[str, Any] = {"type": scope_type, "method": "POST", "path": "/", "headers": headers}
    if peer is not None:
        scope["client"] = (peer, 40000)
    return scope


class _Inner:
    """The app behind the middleware: records each scope and answers 200."""

    def __init__(self) -> None:
        self.scopes: list[Scope] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.scopes.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


async def _call(check: McpAccessCheck, scope: Scope) -> int:
    """Run one request through ``check`` and return the response status."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await check(scope, receive, send)
    [start] = [m for m in sent if m["type"] == "http.response.start"]
    status: int = start["status"]
    return status


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER and r.levelno == logging.WARNING]


@pytest.mark.integration
@respx.mock
async def test_log_mode_warns_once_per_peer_reason(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """One WARNING per (peer, reason) per 10 minutes; every request still passes; no token text is logged."""
    _configure(mock_settings, "log")
    _certs()
    inner = _Inner()
    check = McpAccessCheck(inner, config=mcp_access_config())
    now = [1000.0]
    garbage = "garbage-token-text"
    with patch("src.api.mcp_access._monotonic", lambda: now[0]), caplog.at_level(logging.WARNING, ACCESS_LOGGER):
        caplog.clear()
        statuses = [await _call(check, _scope("192.0.2.10")) for _ in range(3)]
        assert len(_warnings(caplog)) == 1
        statuses.append(await _call(check, _scope("192.0.2.10", garbage)))  # another reason
        statuses.append(await _call(check, _scope("192.0.2.10", garbage)))
        assert len(_warnings(caplog)) == 2
        statuses.append(await _call(check, _scope("192.0.2.11")))  # another peer
        statuses.append(await _call(check, _scope(None)))  # no peer at all: "unknown"
        assert len(_warnings(caplog)) == 4
        now[0] += 599
        statuses.append(await _call(check, _scope("192.0.2.10")))
        assert len(_warnings(caplog)) == 4
        now[0] += 2
        statuses.append(await _call(check, _scope("192.0.2.10")))
        assert len(_warnings(caplog)) == 5

    assert statuses == [200] * 9
    assert len(inner.scopes) == 9
    messages = _warnings(caplog)
    assert "192.0.2.10" in messages[0] and "192.0.2.11" in messages[2] and "unknown" in messages[3]
    assert garbage not in caplog.text


@pytest.mark.integration
async def test_warn_cache_is_bounded(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Many peers never grow the warn-once cache past its bound."""
    _configure(mock_settings, "log")
    check = McpAccessCheck(_Inner(), config=mcp_access_config())
    with patch.object(mcp_access, "MAX_WARN_KEYS", 8), caplog.at_level(logging.WARNING, ACCESS_LOGGER):
        for i in range(20):
            assert await _call(check, _scope(f"192.0.2.{i}")) == 200
    assert len(check._warned) <= 8
    assert len(_warnings(caplog)) == 20


@pytest.mark.integration
async def test_log_mode_unexpected_error_still_passes(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """A bug in verification is logged at ERROR (class only) and, in log mode, the request still passes."""
    _configure(mock_settings, "log")
    inner = _Inner()
    check = McpAccessCheck(inner, config=mcp_access_config())
    with (
        patch("src.api.mcp_access.verify_access_identity", AsyncMock(side_effect=RuntimeError("secret-detail"))),
        caplog.at_level(logging.WARNING, ACCESS_LOGGER),
    ):
        assert await _call(check, _scope("192.0.2.10", "some-token")) == 200
    errors = [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "RuntimeError" in errors[0]
    assert "secret-detail" not in caplog.text and "some-token" not in caplog.text
    assert inner.scopes[0]["state"]["mcp_client"] == "invalid"


@pytest.mark.integration
async def test_non_http_scope_passes_through(mock_settings: Any) -> None:
    """Lifespan (and any other non-http) scopes go straight to the inner app, unchecked and uncounted."""
    _configure(mock_settings, "enforce")
    inner = _Inner()
    check = McpAccessCheck(inner, config=mcp_access_config())
    before = _requests("anonymous", "missing")

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await check({"type": "lifespan"}, receive, send)
    assert [s["type"] for s in inner.scopes] == ["lifespan"]
    assert _requests("anonymous", "missing") == before


@pytest.mark.integration
@respx.mock
def test_log_mode_unconfigured_warns_and_acts_like_off(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Log mode with no AUD: one startup WARNING, then no verification and client ``unknown``."""
    _configure(mock_settings, "log", aud="")
    certs = _certs()
    before = _requests("unknown", "unconfigured")
    with _no_agent(), caplog.at_level(logging.INFO, ACCESS_LOGGER), TestClient(_app()) as client:
        _initialize(client, {JWT_HEADER: K1.service_token()})
        assert _post(client).status_code == 200

    assert not certs.called
    assert _requests("unknown", "unconfigured") - before == 2
    warnings = _warnings(caplog)
    assert len(warnings) == 1 and "MCP_CF_ACCESS_AUD" in warnings[0]


# --- enforce mode ---


@pytest.mark.integration
@respx.mock
def test_enforce_mode_rejects_missing_jwt_403(mock_settings: Any) -> None:
    """Enforce mode refuses a request with no JWT, or an invalid one, before the MCP server sees it."""
    _configure(mock_settings, "enforce")
    _certs()
    missing, invalid = _requests("anonymous", "missing"), _requests("invalid", "invalid")
    with _no_agent(), TestClient(_app()) as client:
        no_jwt = _post(client)
        bad_jwt = _post(client, {JWT_HEADER: STRANGER.service_token()})
        no_slash = client.post("/mcp/", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    for response in (no_jwt, bad_jwt, no_slash):
        assert response.status_code == 403
        assert response.json() == {"error": "Cloudflare Access JWT required"}
    assert _requests("anonymous", "missing") - missing == 2
    assert _requests("invalid", "invalid") - invalid == 1


@pytest.mark.integration
@respx.mock
def test_enforce_mode_accepts_service_token(mock_settings: Any) -> None:
    """A valid service token passes enforce mode and is counted under its mapped client name."""
    _configure(mock_settings, "enforce", names=f"{SERVICE_CLIENT_ID}=nanoclaw")
    _certs()
    before = _requests("nanoclaw", "verified")
    token = {JWT_HEADER: K1.service_token()}
    with _no_agent(), TestClient(_app()) as client:
        assert _initialize(client, token)["result"]["serverInfo"]["name"] == "sre-assistant"
        assert "tools" in _rpc(client, 2, "tools/list", {}, token)["result"]

    assert _requests("nanoclaw", "verified") - before == 2


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize(
    ("aud", "team", "named"),
    [("", TEAM, "MCP_CF_ACCESS_AUD"), (AUD, "", "CF_ACCESS_TEAM_DOMAIN")],
    ids=["no-aud", "no-team-domain"],
)
def test_enforce_misconfigured_returns_503(
    mock_settings: Any, caplog: pytest.LogCaptureFixture, aud: str, team: str, named: str
) -> None:
    """Enforce mode without an AUD or team domain: an ERROR at startup, 503 on /mcp, the rest of the app up."""
    _configure(mock_settings, "enforce", aud=aud, team=team)
    certs = _certs()
    before = _requests("unknown", "unconfigured")
    with _no_agent(), caplog.at_level(logging.INFO, ACCESS_LOGGER), TestClient(_app()) as client:
        startup_errors = [
            r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER and r.levelno == logging.ERROR
        ]
        response = _post(client, {JWT_HEADER: K1.service_token()})
        metrics = client.get("/metrics")

    assert len(startup_errors) == 1 and named in startup_errors[0]
    assert response.status_code == 503
    assert response.json() == {"error": "MCP access check misconfigured"}
    assert metrics.status_code == 200
    assert not certs.called
    assert _requests("unknown", "unconfigured") - before == 1


# --- off mode ---


@pytest.mark.integration
@respx.mock
def test_off_mode_skips_verification(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Off mode never verifies (even a configured AUD and a token present): client ``unknown``, no identity_ref."""
    _configure(mock_settings, "off")
    certs = _certs()
    _mock_search(["node_cpu_seconds_total"])
    before = _requests("unknown", "off")
    verify = AsyncMock()
    with (
        _no_agent(),
        patch("src.api.mcp_access.verify_access_identity", verify),
        caplog.at_level(logging.INFO, TOOL_CALL_LOGGER),
        TestClient(_app()) as client,
    ):
        _initialize(client, {JWT_HEADER: K1.service_token()})
        _rpc(client, 2, "tools/call", SEARCH)

    verify.assert_not_awaited()
    assert not certs.called
    assert _requests("unknown", "off") - before == 2
    [record] = tool_call_records(caplog)
    assert fields(record)["client"] == "unknown"
    assert "identity_ref" not in fields(record)


# --- startup line ---


def test_startup_line(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    _configure(mock_settings, "log", names=f"{SERVICE_CLIENT_ID}=nanoclaw,admin@example.com=owner")
    with caplog.at_level(logging.INFO, ACCESS_LOGGER):
        config = mcp_access_config()
    assert config.mode == "log" and config.configured
    infos = [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER and r.levelno == logging.INFO]
    assert infos == ["MCP access check: mode=log aud_configured=yes team_domain_configured=yes client_names=2"]
    assert SERVICE_CLIENT_ID not in caplog.text and AUD not in caplog.text


# --- the verified identity reaches the tool metric and line ---


def _label_values() -> set[str]:
    return {value for metric in REGISTRY.collect() for sample in metric.samples for value in sample.labels.values()}


@pytest.mark.integration
@respx.mock
def test_verified_client_label_reaches_tool_metric(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Over real HTTP a mapped service token becomes ``client=<name>`` on the tool metric and line, with
    ``identity_ref`` on the line; the raw Client ID is in no metric label and no log line."""
    _configure(mock_settings, "log", names=f"{SERVICE_CLIENT_ID}=nanoclaw")
    _certs()
    _mock_search(["node_cpu_seconds_total"])
    before = _tool_calls("nanoclaw")
    token = {JWT_HEADER: K1.service_token()}
    with (
        _no_agent(),
        caplog.at_level(logging.DEBUG),
        caplog.at_level(logging.DEBUG, TOOL_CALL_LOGGER),
        TestClient(_app()) as client,
    ):
        _initialize(client, token)
        assert _rpc(client, 2, "tools/call", SEARCH, token)["result"]["isError"] is False

    assert _tool_calls("nanoclaw") - before == 1
    [record] = tool_call_records(caplog)
    parsed = fields(record)
    assert parsed["client"] == "nanoclaw"
    assert parsed["identity_ref"] == identity_ref(AccessIdentity(kind="service", value=SERVICE_CLIENT_ID))
    raw_id = SERVICE_CLIENT_ID.split(".")[0]
    assert not any(raw_id in value for value in _label_values())
    assert raw_id not in caplog.text
