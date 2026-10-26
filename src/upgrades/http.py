"""Bounded HTTP reads for the Upgrade Advisor's clients: the size cap is enforced while streaming."""

import json
from dataclasses import dataclass
from typing import Any

import httpx

from src.upgrades.textutil import MAX_RESPONSE_BYTES


class ResponseTooLarge(Exception):
    """A response body exceeded the byte cap (declared by Content-Length or counted while streaming)."""


@dataclass(frozen=True)
class BoundedResponse:
    """A fully read response whose body is known to be within the cap."""

    status_code: int
    headers: httpx.Headers
    url: httpx.URL
    content: bytes

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON; raises ``ValueError`` on invalid JSON."""
        return json.loads(self.content)


async def bounded_request(
    client: httpx.AsyncClient, method: str, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES, **kwargs: Any
) -> BoundedResponse:
    """Send a request and read at most ``max_bytes`` of the body, raising ``ResponseTooLarge`` as soon as it is clear
    the body is larger (before reading when Content-Length says so, otherwise mid-stream)."""
    async with client.stream(method, url, **kwargs) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(f"Content-Length {declared} exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(f"body exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return BoundedResponse(
            status_code=response.status_code, headers=response.headers, url=response.url, content=b"".join(chunks)
        )
