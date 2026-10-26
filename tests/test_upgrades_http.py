"""Integration tests for the bounded HTTP helper (respx with streamed bodies)."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from src.upgrades.http import ResponseTooLarge, bounded_request

pytestmark = pytest.mark.integration

URL = "https://example.com/data"
CAP = 5 * 1024 * 1024
CHUNK = 64 * 1024


class CountingStream(httpx.AsyncByteStream):
    """A body of ``total`` bytes in 64 KiB chunks that records how many chunks were pulled."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        sent = 0
        while sent < self.total:
            size = min(CHUNK, self.total - sent)
            self.pulled += 1
            sent += size
            yield b"x" * size


@respx.mock
async def test_content_length_over_cap_raises_without_reading_body() -> None:
    stream = CountingStream(6 * 1024 * 1024)
    respx.get(URL).mock(
        return_value=httpx.Response(200, headers={"Content-Length": str(6 * 1024 * 1024)}, stream=stream)
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ResponseTooLarge):
            await bounded_request(client, "GET", URL)
    assert stream.pulled == 0


@respx.mock
async def test_streamed_body_without_content_length_aborts_once_cap_passed() -> None:
    stream = CountingStream(6 * 1024 * 1024)
    respx.get(URL).mock(return_value=httpx.Response(200, stream=stream))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ResponseTooLarge):
            await bounded_request(client, "GET", URL)
    assert stream.pulled == CAP // CHUNK + 1  # the first chunk past the cap, nothing after it
    assert stream.pulled < (6 * 1024 * 1024) // CHUNK


@respx.mock
async def test_small_body_returns_content_json_and_metadata() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}, headers={"X-Test": "yes"}))
    async with httpx.AsyncClient() as client:
        response = await bounded_request(client, "GET", URL)
    assert response.status_code == 200
    assert response.is_success
    assert response.json() == {"a": 1}
    assert response.text.startswith("{")
    assert response.headers["x-test"] == "yes"
    assert str(response.url) == URL


@respx.mock
async def test_body_exactly_at_cap_is_accepted() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, content=b"x" * 100))
    async with httpx.AsyncClient() as client:
        response = await bounded_request(client, "POST", URL, max_bytes=100)
    assert len(response.content) == 100


@respx.mock
async def test_invalid_json_raises_value_error() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"not json"))
    async with httpx.AsyncClient() as client:
        response = await bounded_request(client, "GET", URL)
    with pytest.raises(ValueError):
        response.json()
