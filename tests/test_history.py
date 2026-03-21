"""Tests for conversation history persistence."""

import glob
import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agent.history import save_conversation


def _find_session_file(history_dir: str, session_id: str) -> str:
    """Find the JSON file for a session ID (datetime prefix varies)."""
    matches = glob.glob(os.path.join(history_dir, f"*_{session_id}.json"))
    assert len(matches) == 1, f"Expected 1 file for {session_id}, found {len(matches)}"
    return matches[0]


class TestSaveConversation:
    """Unit tests for save_conversation."""

    def test_saves_basic_conversation(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path / "history")
        messages = [
            HumanMessage(content="What is CPU usage?"),
            AIMessage(content="CPU is at 42%."),
        ]

        save_conversation(history_dir, "session-1", messages, "gpt-4o-mini")

        filepath = _find_session_file(history_dir, "session-1")
        with open(filepath) as f:
            data: dict[str, Any] = json.load(f)

        assert data["session_id"] == "session-1"
        assert data["model"] == "gpt-4o-mini"
        assert data["turn_count"] == 1
        assert len(data["messages"]) == 2
        assert "created_at" in data
        assert "updated_at" in data
        # Filename has datetime prefix
        basename = os.path.basename(filepath)
        assert basename.endswith("_session-1.json")
        assert len(basename) > len("_session-1.json")  # Has datetime prefix

    def test_includes_tool_call_messages(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        messages = [
            HumanMessage(content="Check alerts"),
            AIMessage(
                content="",
                additional_kwargs={
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "grafana_get_alerts", "arguments": "{}"},
                        }
                    ]
                },
            ),
            ToolMessage(content='{"alerts": []}', tool_call_id="call_123"),
            AIMessage(content="No active alerts."),
        ]

        save_conversation(history_dir, "tool-session", messages, "gpt-4o-mini")

        filepath = _find_session_file(history_dir, "tool-session")
        with open(filepath) as f:
            data: dict[str, Any] = json.load(f)

        assert len(data["messages"]) == 4
        # Verify tool message is included
        types = [m["type"] for m in data["messages"]]
        assert "tool" in types

    def test_preserves_created_at_on_update(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        messages = [HumanMessage(content="First turn"), AIMessage(content="Response 1")]

        save_conversation(history_dir, "persist-test", messages, "gpt-4o-mini")

        filepath = _find_session_file(history_dir, "persist-test")
        with open(filepath) as f:
            first_data: dict[str, Any] = json.load(f)
        original_created_at = first_data["created_at"]

        # Save again — created_at should be preserved, same file reused
        messages.extend([HumanMessage(content="Second turn"), AIMessage(content="Response 2")])
        save_conversation(history_dir, "persist-test", messages, "gpt-4o-mini")

        with open(filepath) as f:
            second_data: dict[str, Any] = json.load(f)

        assert second_data["created_at"] == original_created_at
        assert second_data["turn_count"] == 2
        assert len(second_data["messages"]) == 4

    def test_creates_directory_if_missing(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path / "deep" / "nested" / "dir")
        messages = [HumanMessage(content="hello"), AIMessage(content="hi")]

        save_conversation(history_dir, "s1", messages, "gpt-4o-mini")

        matches = glob.glob(os.path.join(history_dir, "*_s1.json"))
        assert len(matches) == 1

    def test_swallows_errors(self, tmp_path: Any) -> None:
        """save_conversation must never raise — errors are logged only."""
        messages = [HumanMessage(content="hello")]

        # Pass a file path (not dir) as history_dir to force an OS error
        bad_path = str(tmp_path / "not-a-dir")
        with open(bad_path, "w") as f:
            f.write("block")

        # Should not raise
        save_conversation(bad_path, "s1", messages, "gpt-4o-mini")

    def test_filters_non_message_items(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        messages: list[Any] = [
            HumanMessage(content="hello"),
            "not a message",
            42,
            AIMessage(content="hi"),
            None,
        ]

        save_conversation(history_dir, "filter-test", messages, "gpt-4o-mini")

        filepath = _find_session_file(history_dir, "filter-test")
        with open(filepath) as f:
            data: dict[str, Any] = json.load(f)

        assert len(data["messages"]) == 2

    def test_skips_empty_messages(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        messages: list[Any] = ["not a message", 42]

        save_conversation(history_dir, "empty-test", messages, "gpt-4o-mini")

        # No file should be created when there are no valid messages
        matches = glob.glob(os.path.join(history_dir, "*_empty-test.json"))
        assert len(matches) == 0

    def test_handles_corrupted_existing_file(self, tmp_path: Any) -> None:
        """If the existing JSON is corrupted, overwrite with fresh created_at."""
        history_dir = str(tmp_path)
        # Create a corrupted file with the datetime_session format
        filepath = os.path.join(history_dir, "2026-01-01_000000_corrupt.json")
        os.makedirs(history_dir, exist_ok=True)
        with open(filepath, "w") as f:
            f.write("not valid json{{{")

        messages = [HumanMessage(content="hello"), AIMessage(content="hi")]
        save_conversation(history_dir, "corrupt", messages, "gpt-4o-mini")

        with open(filepath) as f:
            data: dict[str, Any] = json.load(f)

        assert data["session_id"] == "corrupt"
        assert data["created_at"] == data["updated_at"]  # Fresh timestamp


class TestInvokeAgentHistorySaving:
    """Integration tests verifying invoke_agent calls save_conversation."""

    @pytest.mark.integration
    async def test_saves_history_when_dir_configured(self, mock_settings: Any) -> None:
        from src.agent.agent import _invoke_langgraph_agent

        mock_settings.conversation_history_dir = "/tmp/test-history"  # type: ignore[attr-defined]

        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {
            "messages": [
                HumanMessage(content="hello"),
                AIMessage(content="hi there"),
            ]
        }

        with patch("src.agent.agent.save_conversation") as mock_save:
            result = await _invoke_langgraph_agent(mock_agent, "hello", session_id="s1")

        assert result == "hi there"
        mock_save.assert_called_once_with(
            "/tmp/test-history",
            "s1",
            [HumanMessage(content="hello"), AIMessage(content="hi there")],
            "gpt-4o-mini",
        )

    @pytest.mark.integration
    async def test_skips_history_when_dir_empty(self, mock_settings: Any) -> None:
        from src.agent.agent import _invoke_langgraph_agent

        mock_settings.conversation_history_dir = ""  # type: ignore[attr-defined]

        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {"messages": [AIMessage(content="response")]}

        with patch("src.agent.agent.save_conversation") as mock_save:
            await _invoke_langgraph_agent(mock_agent, "hello", session_id="s1")

        mock_save.assert_not_called()

    @pytest.mark.integration
    async def test_uses_fresh_session_id_on_recovery(self, mock_settings: Any) -> None:
        from src.agent.agent import _invoke_langgraph_agent

        mock_settings.conversation_history_dir = "/tmp/test-history"  # type: ignore[attr-defined]

        tool_call_error = Exception(
            "An assistant message with 'tool_calls' must be followed by "
            "tool messages responding to each 'tool_call_id'."
        )

        mock_agent = AsyncMock()
        mock_agent.ainvoke.side_effect = [
            tool_call_error,
            {"messages": [AIMessage(content="recovered")]},
        ]

        with patch("src.agent.agent.save_conversation") as mock_save:
            result = await _invoke_langgraph_agent(mock_agent, "hello", session_id="broken")

        assert result == "recovered"
        # The saved session_id should be the fresh one, not "broken"
        saved_session_id = mock_save.call_args[0][1]
        assert saved_session_id.startswith("broken-")
        assert saved_session_id != "broken"
