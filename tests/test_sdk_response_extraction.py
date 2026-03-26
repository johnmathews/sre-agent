"""Tests for SDK agent response extraction — verifying ResultMessage.result is preferred."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.sdk_agent import invoke_sdk_agent


def _make_assistant_message(text: str) -> MagicMock:
    """Create a mock AssistantMessage with a TextBlock."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    block = MagicMock(spec=TextBlock)
    block.text = text
    # Make isinstance checks work
    block.__class__ = TextBlock

    msg = MagicMock(spec=AssistantMessage)
    msg.content = [block]
    msg.__class__ = AssistantMessage
    return msg


def _make_result_message(result_text: str, is_error: bool = False) -> MagicMock:
    """Create a mock ResultMessage."""
    from claude_agent_sdk.types import ResultMessage

    msg = MagicMock(spec=ResultMessage)
    msg.result = result_text
    msg.is_error = is_error
    msg.__class__ = ResultMessage
    return msg


@pytest.mark.asyncio
async def test_prefers_result_message_over_assistant_text(mock_settings: object) -> None:
    """The final response should come from ResultMessage.result, not intermediate TextBlocks."""
    thinking_msg = _make_assistant_message("Let me check the spinup transition times...")
    result_msg = _make_result_message("The tank pool HDDs were in standby for 18.5 hours.")

    async def mock_query(**kwargs: object):  # type: ignore[no-untyped-def]
        yield thinking_msg
        yield result_msg

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    with (
        patch("src.agent.sdk_agent.query", side_effect=mock_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent.record_sdk_metrics"),
        patch("src.agent.sdk_agent.extract_tool_names", return_value=[]),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
    ):
        response = await invoke_sdk_agent(options, "How long were HDDs spun down?")

    assert "18.5 hours" in response
    assert "Let me check" not in response


@pytest.mark.asyncio
async def test_falls_back_to_last_text_block_when_no_result(mock_settings: object) -> None:
    """When no ResultMessage is received, fall back to the last TextBlock."""
    text_msg = _make_assistant_message("The CPU is at 42%.")

    async def mock_query(**kwargs: object):  # type: ignore[no-untyped-def]
        yield text_msg

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    with (
        patch("src.agent.sdk_agent.query", side_effect=mock_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent.record_sdk_metrics"),
        patch("src.agent.sdk_agent.extract_tool_names", return_value=[]),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
    ):
        response = await invoke_sdk_agent(options, "What is CPU?")

    assert "42%" in response


@pytest.mark.asyncio
async def test_empty_result_falls_back_to_text_block(mock_settings: object) -> None:
    """When ResultMessage.result is empty, fall back to last TextBlock."""
    text_msg = _make_assistant_message("CPU usage is 42%.")
    result_msg = _make_result_message("   ")  # whitespace-only result

    async def mock_query(**kwargs: object):  # type: ignore[no-untyped-def]
        yield text_msg
        yield result_msg

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    with (
        patch("src.agent.sdk_agent.query", side_effect=mock_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent.record_sdk_metrics"),
        patch("src.agent.sdk_agent.extract_tool_names", return_value=[]),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
    ):
        response = await invoke_sdk_agent(options, "What is CPU?")

    assert "42%" in response


@pytest.mark.asyncio
async def test_no_messages_returns_default(mock_settings: object) -> None:
    """When no messages are received at all, return the default."""

    async def mock_query(**kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # make this an async generator

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    with (
        patch("src.agent.sdk_agent.query", side_effect=mock_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent.record_sdk_metrics"),
        patch("src.agent.sdk_agent.extract_tool_names", return_value=[]),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
    ):
        response = await invoke_sdk_agent(options, "hello")

    assert response == "No response generated."


def _make_tool_use_message(tool_name: str) -> MagicMock:
    """Create a mock AssistantMessage with a ToolUseBlock."""
    from claude_agent_sdk.types import AssistantMessage, ToolUseBlock

    block = MagicMock(spec=ToolUseBlock)
    block.name = tool_name
    block.id = "tool-1"
    block.input = {}
    block.__class__ = ToolUseBlock

    msg = MagicMock(spec=AssistantMessage)
    msg.content = [block]
    msg.__class__ = AssistantMessage
    return msg


@pytest.mark.asyncio
async def test_tool_durations_passed_to_metrics(mock_settings: object) -> None:
    """Tool durations computed from message timing are passed to record_sdk_metrics."""
    tool_msg = _make_tool_use_message("mcp__sre__prometheus_instant_query")
    result_msg = _make_result_message("Query result: 42%")

    async def mock_query(**kwargs: object):  # type: ignore[no-untyped-def]
        yield tool_msg
        await asyncio.sleep(0.05)  # simulate tool execution
        yield result_msg

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    mock_record = MagicMock()

    with (
        patch("src.agent.sdk_agent.query", side_effect=mock_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent.record_sdk_metrics", mock_record),
        patch("src.agent.sdk_agent.extract_tool_names", return_value=["prometheus_instant_query"]),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
    ):
        await invoke_sdk_agent(options, "What is CPU?")

    # record_sdk_metrics should have been called with tool_durations
    mock_record.assert_called_once()
    call_args = mock_record.call_args
    tool_durations = call_args[0][2]  # third positional arg
    assert len(tool_durations) == 1
    assert tool_durations[0][0] == "prometheus_instant_query"
    assert tool_durations[0][1] >= 0.04  # should be ~0.05s from the sleep


@pytest.mark.asyncio
async def test_parallel_tools_split_duration(mock_settings: object) -> None:
    """When multiple tools are in one message, duration is split evenly."""
    from claude_agent_sdk.types import AssistantMessage, ToolUseBlock

    block1 = MagicMock(spec=ToolUseBlock)
    block1.name = "mcp__sre__prometheus_instant_query"
    block1.__class__ = ToolUseBlock
    block2 = MagicMock(spec=ToolUseBlock)
    block2.name = "mcp__sre__grafana_get_alerts"
    block2.__class__ = ToolUseBlock

    multi_tool_msg = MagicMock(spec=AssistantMessage)
    multi_tool_msg.content = [block1, block2]
    multi_tool_msg.__class__ = AssistantMessage

    result_msg = _make_result_message("Done")

    async def mock_query(**kwargs: object):  # type: ignore[no-untyped-def]
        yield multi_tool_msg
        await asyncio.sleep(0.06)  # simulate both tools executing
        yield result_msg

    options = MagicMock()
    options.model = "test-model"
    options.env = {}

    mock_record = MagicMock()

    with (
        patch("src.agent.sdk_agent.query", side_effect=mock_query),
        patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
        patch("src.agent.sdk_agent.record_sdk_metrics", mock_record),
        patch("src.agent.sdk_agent.extract_tool_names", return_value=[]),
        patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
        patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
    ):
        await invoke_sdk_agent(options, "Check everything")

    tool_durations = mock_record.call_args[0][2]
    assert len(tool_durations) == 2
    # Each tool gets half the total elapsed time
    assert tool_durations[0][0] == "prometheus_instant_query"
    assert tool_durations[1][0] == "grafana_get_alerts"
    assert tool_durations[0][1] >= 0.02  # ~0.03s each (0.06 / 2)
