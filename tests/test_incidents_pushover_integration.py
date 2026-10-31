"""Integration tests for the Pushover client (mocked HTTP via respx; the retry sleep is patched out)."""

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from src.incidents.pushover import PUSHOVER_URL, RETRY_DELAY_SECONDS, PushoverResult, send

TOKEN = "a-fake-pushover-app-token"
USER = "u-fake-pushover-user-key"

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _settings(mock_settings: Any) -> None:
    mock_settings.pushover_app_token = TOKEN
    mock_settings.pushover_user_key = USER


@pytest.fixture
def sleep() -> Iterator[AsyncMock]:
    with patch("src.incidents.pushover.asyncio.sleep", new=AsyncMock()) as fake:
        yield fake


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


@respx.mock
async def test_success_posts_form_and_parses_request_id(sleep: AsyncMock) -> None:
    route = respx.post(PUSHOVER_URL).mock(return_value=httpx.Response(200, json={"status": 1, "request": "req-123"}))
    result = await send("Why Wiki went down", "Down 11:06–11:09 CET", "https://status.example.com/x", "Gatus history")
    assert result == PushoverResult(ok=True, request_id="req-123", detail="sent (request req-123)")
    form = _form(route.calls.last.request)
    assert form["token"] == [TOKEN]
    assert form["user"] == [USER]
    assert form["priority"] == ["0"]
    assert form["title"] == ["Why Wiki went down"]
    assert form["message"] == ["Down 11:06–11:09 CET"]
    assert form["url"] == ["https://status.example.com/x"]
    assert form["url_title"] == ["Gatus history"]
    assert sleep.await_count == 0


@respx.mock
async def test_no_url_fields_without_a_url(sleep: AsyncMock) -> None:
    route = respx.post(PUSHOVER_URL).mock(return_value=httpx.Response(200, json={"status": 1, "request": "r"}))
    await send("t", "m", None, None)
    form = _form(route.calls.last.request)
    assert "url" not in form and "url_title" not in form


@respx.mock
async def test_500_then_200_retries_once(sleep: AsyncMock) -> None:
    route = respx.post(PUSHOVER_URL).mock(
        side_effect=[httpx.Response(500, text="oops"), httpx.Response(200, json={"status": 1, "request": "r2"})]
    )
    result = await send("t", "m", None, None)
    assert result.ok and result.request_id == "r2"
    assert route.call_count == 2
    sleep.assert_awaited_once_with(RETRY_DELAY_SECONDS)


@respx.mock
async def test_500_twice_gives_up(sleep: AsyncMock) -> None:
    route = respx.post(PUSHOVER_URL).mock(return_value=httpx.Response(503, text="down"))
    result = await send("t", "m", None, None)
    assert not result.ok
    assert result.detail == "Pushover answered HTTP 503"
    assert route.call_count == 2


@respx.mock
async def test_400_is_not_retried(sleep: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    route = respx.post(PUSHOVER_URL).mock(
        return_value=httpx.Response(400, json={"status": 0, "errors": ["user identifier is invalid"], "request": "r"})
    )
    with caplog.at_level(logging.WARNING, logger="src.incidents.pushover"):
        result = await send("t", "m", None, None)
    assert route.call_count == 1
    assert sleep.await_count == 0
    assert result == PushoverResult(
        ok=False, request_id="r", detail="Pushover answered HTTP 400: user identifier is invalid"
    )
    assert "HTTP 400" in caplog.text


@respx.mock
async def test_network_error_twice_is_not_ok(sleep: AsyncMock) -> None:
    route = respx.post(PUSHOVER_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = await send("t", "m", None, None)
    assert not result.ok and result.request_id is None
    assert result.detail == "Pushover unreachable: ConnectError"
    assert route.call_count == 2
    assert sleep.await_count == 1


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow connect"), httpx.PoolTimeout("no connection")],
    ids=["ConnectError", "ConnectTimeout", "PoolTimeout"],
)
@respx.mock
async def test_not_sent_error_then_success_retries(sleep: AsyncMock, error: httpx.TransportError) -> None:
    """The request never left: a retry cannot send the push twice."""
    route = respx.post(PUSHOVER_URL).mock(side_effect=[error, httpx.Response(200, json={"status": 1, "request": "r3"})])
    result = await send("t", "m", None, None)
    assert result.ok and result.request_id == "r3"
    assert route.call_count == 2


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("slow"), httpx.WriteTimeout("slow write"), httpx.RemoteProtocolError("closed")],
    ids=["ReadTimeout", "WriteTimeout", "RemoteProtocolError"],
)
@respx.mock
async def test_maybe_delivered_error_is_not_retried(
    sleep: AsyncMock, error: httpx.TransportError, caplog: pytest.LogCaptureFixture
) -> None:
    """The request may have reached Pushover: a retry could send the push twice, so it is not resent."""
    route = respx.post(PUSHOVER_URL).mock(side_effect=[error, httpx.Response(200, json={"status": 1, "request": "r"})])
    with caplog.at_level(logging.WARNING, logger="src.incidents.pushover"):
        result = await send("t", "m", None, None)
    assert route.call_count == 1
    assert sleep.await_count == 0
    assert result == PushoverResult(ok=False, detail=f"delivery unknown: {type(error).__name__}; not resent")
    assert "delivery unknown" in caplog.text


@respx.mock
async def test_200_with_status_0_or_bad_json_is_not_ok(sleep: AsyncMock) -> None:
    respx.post(PUSHOVER_URL).mock(
        side_effect=[httpx.Response(200, json={"status": 0, "request": "r4"}), httpx.Response(200, text="<html>")]
    )
    first = await send("t", "m", None, None)
    assert not first.ok and first.request_id == "r4" and "status 0" in first.detail
    second = await send("t", "m", None, None)
    assert not second.ok and second.detail == "Pushover answered HTTP 200 without a JSON body"


@pytest.mark.parametrize("missing", ["pushover_app_token", "pushover_user_key"])
@respx.mock
async def test_not_configured_sends_nothing(mock_settings: Any, missing: str, sleep: AsyncMock) -> None:
    setattr(mock_settings, missing, "")
    route = respx.post(PUSHOVER_URL).mock(return_value=httpx.Response(200, json={"status": 1, "request": "r"}))
    result = await send("t", "m", None, None)
    assert result == PushoverResult(ok=False, request_id=None, detail="Pushover not configured")
    assert route.call_count == 0


@respx.mock
async def test_token_and_user_key_never_logged(sleep: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    respx.post(PUSHOVER_URL).mock(
        side_effect=[
            httpx.ConnectError(f"refused token={TOKEN}"),
            httpx.Response(400, json={"status": 0, "errors": [f"token {TOKEN} invalid for {USER}"], "request": "r"}),
        ]
    )
    with caplog.at_level(logging.DEBUG):
        result = await send("t", "m", None, None)
    assert TOKEN not in caplog.text and USER not in caplog.text
    assert TOKEN not in result.detail and USER not in result.detail
