"""Unit tests for the shared no-tools model call ``src.agent.llm.ask_text`` (chat model stubbed)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent import llm as llm_mod
from src.agent.llm import CLAUDE_CODE_SYSTEM_PREFIX, ask_text, response_text


class Transient(Exception):
    status_code = 503


class Auth(Exception):
    status_code = 401


def _stub(*results: Any) -> tuple[Any, MagicMock]:
    """Patch ``create_llm`` with a model whose ``ainvoke`` returns or raises each result in turn."""
    model = MagicMock()
    side = [r if isinstance(r, Exception) else MagicMock(content=r) for r in results]
    model.ainvoke = AsyncMock(side_effect=side)
    return patch("src.agent.llm.create_llm", return_value=model), model


async def test_ask_text_returns_text_with_temperature_zero(mock_settings: Any) -> None:
    p, model = _stub("hello")
    with p as create:
        assert await ask_text("sys", "prompt") == "hello"
    assert create.call_args.kwargs["temperature"] == 0
    assert create.call_args.kwargs["model_override"] is None
    system, human = model.ainvoke.call_args.args[0]
    assert system.content == "sys"
    assert human.content == "prompt"


async def test_ask_text_passes_model_override(mock_settings: Any) -> None:
    p, _ = _stub("ok")
    with p as create:
        await ask_text("sys", "prompt", model_override="claude-sonnet-5-5")
    assert create.call_args.kwargs["model_override"] == "claude-sonnet-5-5"
    assert create.call_args.args[0] is mock_settings


async def test_ask_text_prefixes_system_for_oauth_token(mock_settings: Any) -> None:
    mock_settings.llm_provider = "anthropic"
    mock_settings.anthropic_api_key = "sk-ant-oat01-fake"
    p, model = _stub("ok")
    with p:
        await ask_text("sys", "prompt")
    system = model.ainvoke.call_args.args[0][0]
    assert system.content == CLAUDE_CODE_SYSTEM_PREFIX + "sys"
    assert system.content.startswith("You are Claude Code")


async def test_ask_text_no_prefix_for_regular_anthropic_key(mock_settings: Any) -> None:
    mock_settings.llm_provider = "anthropic"
    mock_settings.anthropic_api_key = "sk-ant-api03-fake"
    p, model = _stub("ok")
    with p:
        await ask_text("sys", "prompt")
    assert model.ainvoke.call_args.args[0][0].content == "sys"


async def test_ask_text_retries_transient_error_then_succeeds(mock_settings: Any) -> None:
    p, model = _stub(Transient("busy"), "second time")
    with p, patch("src.agent.llm.asyncio.sleep", new=AsyncMock()) as sleep:
        assert await ask_text("sys", "prompt") == "second time"
    assert model.ainvoke.await_count == 2
    assert sleep.await_count == 1


async def test_ask_text_non_retryable_error_raises_at_once(mock_settings: Any) -> None:
    p, model = _stub(Auth("no"))
    with p, patch("src.agent.llm.asyncio.sleep", new=AsyncMock()) as sleep, pytest.raises(Auth):
        await ask_text("sys", "prompt")
    assert model.ainvoke.await_count == 1
    assert sleep.await_count == 0


async def test_ask_text_budget_bounds_retries(mock_settings: Any) -> None:
    clock = [0.0]

    async def fake_sleep(s: float) -> None:
        clock[0] += s

    model = MagicMock()
    model.ainvoke = AsyncMock(side_effect=Transient("busy"))
    with (
        patch("src.agent.llm.create_llm", return_value=model),
        patch("src.agent.llm.asyncio.sleep", new=fake_sleep),
        patch("src.agent.llm.time.monotonic", side_effect=lambda: clock[0]),
        pytest.raises(Transient),
    ):
        await ask_text("sys", "prompt", budget_s=60.0)
    assert 1 < model.ainvoke.await_count < 10
    assert clock[0] <= 60.0


async def test_ask_text_hung_call_times_out(mock_settings: Any) -> None:
    import asyncio

    async def hang(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(3600)

    model = MagicMock()
    model.ainvoke = hang
    with (
        patch("src.agent.llm.create_llm", return_value=model),
        patch("src.agent.llm.MIN_CALL_TIMEOUT", 0.05),
        pytest.raises(TimeoutError),
    ):
        await ask_text("sys", "prompt", budget_s=0.0)


def test_response_text_joins_content_blocks() -> None:
    assert response_text("plain") == "plain"
    assert response_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}, "c"]) == "abc"


def test_default_budget_matches_review() -> None:
    from src.upgrades import review

    assert llm_mod.DEFAULT_RETRY_BUDGET_SECONDS == review.RETRY_BUDGET_SECONDS == 120.0


@pytest.mark.parametrize(
    ("model", "accepts"),
    [
        ("claude-opus-4-6", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-sonnet-4-20250514", True),
        ("claude-3-5-sonnet-20241022", True),
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),
        ("claude-opus-5", False),
        ("claude-opus-5-5", False),
        ("claude-sonnet-5", False),
        ("claude-sonnet-5-5", False),
        ("claude-haiku-5-5", False),
        ("claude-fable-5-1", False),
    ],
)
def test_accepts_sampling_params(model: str, accepts: bool) -> None:
    """Newer Claude models reject temperature/top_p/top_k with a 400; only older ones take them."""
    assert llm_mod.accepts_sampling_params(model) is accepts


def test_anthropic_chat_omits_temperature_for_models_that_reject_it() -> None:
    chat = llm_mod.create_anthropic_chat("sk-ant-api-fake", "claude-sonnet-5-5", temperature=0.0, max_tokens=10)
    payload = chat._get_request_payload([("human", "hi")])  # pyright: ignore[reportPrivateUsage]
    assert "temperature" not in payload


def test_anthropic_chat_keeps_temperature_for_older_models() -> None:
    chat = llm_mod.create_anthropic_chat("sk-ant-api-fake", "claude-opus-4-6", temperature=0.0, max_tokens=10)
    payload = chat._get_request_payload([("human", "hi")])  # pyright: ignore[reportPrivateUsage]
    assert payload["temperature"] == 0.0
