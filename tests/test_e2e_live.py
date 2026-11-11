"""Live end-to-end tests (``--run-e2e`` only). Each skips with a reason when its inputs are unset.

They call real systems.
1. Prometheus through the tool function (needs ``PROMETHEUS_URL`` in ``.env``).
2. The deployed ``/mcp`` endpoint behind Cloudflare Access (needs ``SRE_MCP_URL``, ``CF_ACCESS_CLIENT_ID`` and
   ``CF_ACCESS_CLIENT_SECRET``, a service token). Sends authentication headers: run it by hand.
3. One ``ask_text`` model call (needs ``E2E_LLM=1`` and LLM credentials in ``.env``; costs money).
"""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import ValidationError

from src.agent.llm import ask_text
from src.agent.tools.prometheus import prometheus_instant_query
from src.config import get_settings

pytestmark = pytest.mark.e2e

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _env(name: str) -> str | None:
    """The shell's value, else the repo's ``.env`` (``uv run`` does not export it, and these are not Settings)."""
    return os.environ.get(name) or dotenv_values(ENV_FILE).get(name) or None


@pytest.mark.asyncio
async def test_prometheus_up_query_returns_series() -> None:
    get_settings.cache_clear()
    try:
        settings = get_settings()
    except ValidationError as exc:
        pytest.skip(f"settings do not load from .env: {exc.error_count()} validation error(s)")
    if not settings.prometheus_url:
        pytest.skip("PROMETHEUS_URL is not set")

    result = await prometheus_instant_query.ainvoke({"query": "up"})

    assert isinstance(result, str)
    assert result.strip()
    assert "error" not in result.lower()[:80]


@pytest.mark.asyncio
async def test_mcp_endpoint_initialize_list_and_query() -> None:
    url = _env("SRE_MCP_URL")
    client_id = _env("CF_ACCESS_CLIENT_ID")
    client_secret = _env("CF_ACCESS_CLIENT_SECRET")
    if not (url and client_id and client_secret):
        pytest.skip("SRE_MCP_URL, CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must all be set")

    transport = StreamableHttpTransport(
        url,
        headers={"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret},
    )
    async with Client(transport) as client:
        init = client.initialize_result
        assert init is not None
        assert init.instructions, "initialize returned no server instructions"

        tools = await client.list_tools()
        assert tools, "tools/list is empty"
        assert "prometheus_instant_query" in {t.name for t in tools}

        result = await client.call_tool("prometheus_instant_query", {"query": "up"})
        assert not result.is_error
        assert result.content


@pytest.mark.asyncio
async def test_ask_text_returns_an_answer() -> None:
    if _env("E2E_LLM") != "1":
        pytest.skip("E2E_LLM=1 is required (the call costs money)")
    get_settings.cache_clear()
    try:
        get_settings()
    except ValidationError as exc:
        pytest.skip(f"settings do not load from .env: {exc.error_count()} validation error(s)")

    answer = await ask_text("Answer in one word.", "Reply with the word: pong", budget_s=30)

    assert "pong" in answer.lower()
