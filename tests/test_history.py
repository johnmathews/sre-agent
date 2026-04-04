"""Tests for unified conversation history persistence."""

import glob
import json
import os
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agent.history import (
    _derive_title,
    _langchain_messages_to_turns,
    delete_conversation,
    format_history_as_prompt,
    get_conversation,
    list_conversations,
    load_turns,
    load_turns_as_langchain_messages,
    migrate_history_files,
    rename_conversation,
    save_turn,
)


def _find_session_file(history_dir: str, session_id: str) -> str:
    """Find the JSON file for a session ID."""
    matches = glob.glob(os.path.join(history_dir, f"*_{session_id}.json"))
    assert len(matches) == 1, f"Expected 1 file for {session_id}, found {len(matches)}"
    return matches[0]


def _read(filepath: str) -> dict[str, Any]:
    with open(filepath) as f:
        data: dict[str, Any] = json.load(f)
    return data


class TestDeriveTitle:
    def test_short_message_unchanged(self) -> None:
        assert _derive_title("hello world") == "hello world"

    def test_long_message_truncated_with_ellipsis(self) -> None:
        msg = "a" * 100
        title = _derive_title(msg, max_chars=10)
        assert len(title) == 10
        assert title.endswith("\u2026")

    def test_collapses_newlines(self) -> None:
        assert _derive_title("hello\n\nworld\tfoo") == "hello world foo"

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert _derive_title("   hi there   ") == "hi there"

    def test_empty_message(self) -> None:
        assert _derive_title("") == ""


class TestSaveTurn:
    def test_creates_new_file_with_title(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "Why did prometheus alert?", "gpt-4o-mini", "openai")

        filepath = _find_session_file(history_dir, "s1")
        data = _read(filepath)
        assert data["session_id"] == "s1"
        assert data["title"] == "Why did prometheus alert?"
        assert data["provider"] == "openai"
        assert data["model"] == "gpt-4o-mini"
        assert data["turn_count"] == 1
        assert len(data["turns"]) == 1
        assert data["turns"][0] == {
            "role": "user",
            "content": "Why did prometheus alert?",
            "timestamp": data["turns"][0]["timestamp"],
        }
        assert data["created_at"] == data["updated_at"]

    def test_appends_to_existing_file(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "question", "m", "openai")
        save_turn(history_dir, "s1", "assistant", "answer", "m", "openai")
        save_turn(history_dir, "s1", "user", "follow up", "m", "openai")

        filepath = _find_session_file(history_dir, "s1")
        data = _read(filepath)
        assert len(data["turns"]) == 3
        assert data["turn_count"] == 2  # 2 user turns
        assert data["turns"][0]["role"] == "user"
        assert data["turns"][1]["role"] == "assistant"
        assert data["turns"][2]["role"] == "user"

    def test_title_set_only_on_first_user_turn(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "first question", "m", "openai")
        save_turn(history_dir, "s1", "assistant", "completely different", "m", "openai")
        save_turn(history_dir, "s1", "user", "follow up question", "m", "openai")

        filepath = _find_session_file(history_dir, "s1")
        data = _read(filepath)
        assert data["title"] == "first question"

    def test_title_empty_when_first_turn_is_assistant(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "assistant", "weird opener", "m", "openai")
        filepath = _find_session_file(history_dir, "s1")
        data = _read(filepath)
        assert data["title"] == ""

    def test_preserves_created_at_across_saves(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "q1", "m", "openai")
        filepath = _find_session_file(history_dir, "s1")
        first = _read(filepath)

        save_turn(history_dir, "s1", "assistant", "a1", "m", "openai")
        second = _read(filepath)

        assert second["created_at"] == first["created_at"]
        assert second["updated_at"] >= first["updated_at"]

    def test_never_raises_on_bad_history_dir(self, tmp_path: Any) -> None:
        bad_path = str(tmp_path / "not-a-dir")
        with open(bad_path, "w") as f:
            _ = f.write("block")
        save_turn(bad_path, "s1", "user", "hi", "m", "openai")

    def test_rejects_unsafe_session_id(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "../etc/passwd", "user", "hi", "m", "openai")
        assert glob.glob(os.path.join(history_dir, "*.json")) == []

    def test_creates_directory_if_missing(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path / "deep" / "nested")
        save_turn(history_dir, "s1", "user", "hi", "m", "openai")
        assert os.path.isdir(history_dir)
        assert glob.glob(os.path.join(history_dir, "*_s1.json"))

    def test_overwrites_corrupted_file(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        os.makedirs(history_dir, exist_ok=True)
        filepath = os.path.join(history_dir, "2026-01-01_000000_s1.json")
        with open(filepath, "w") as f:
            _ = f.write("not valid json{{{")

        save_turn(history_dir, "s1", "user", "hi", "m", "openai")

        data = _read(filepath)
        assert data["session_id"] == "s1"
        assert data["turn_count"] == 1


class TestLoadTurns:
    def test_returns_empty_for_missing_session(self, tmp_path: Any) -> None:
        assert load_turns(str(tmp_path), "missing") == []

    def test_returns_empty_for_missing_dir(self, tmp_path: Any) -> None:
        assert load_turns(str(tmp_path / "nope"), "s1") == []

    def test_returns_turns(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "q", "m", "openai")
        save_turn(history_dir, "s1", "assistant", "a", "m", "openai")
        turns = load_turns(history_dir, "s1")
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[0]["content"] == "q"
        assert turns[1]["role"] == "assistant"

    def test_returns_empty_for_corrupted_file(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        os.makedirs(history_dir, exist_ok=True)
        with open(os.path.join(history_dir, "2026_s1.json"), "w") as f:
            _ = f.write("garbage")
        assert load_turns(history_dir, "s1") == []


class TestLoadTurnsAsLangchainMessages:
    def test_empty_when_no_file(self, tmp_path: Any) -> None:
        assert load_turns_as_langchain_messages(str(tmp_path), "missing") == []

    def test_converts_roles_to_correct_types(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "q1", "m", "openai")
        save_turn(history_dir, "s1", "assistant", "a1", "m", "openai")
        save_turn(history_dir, "s1", "user", "q2", "m", "openai")

        messages = load_turns_as_langchain_messages(history_dir, "s1")
        assert len(messages) == 3
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].content == "q1"
        assert isinstance(messages[1], AIMessage)
        assert messages[1].content == "a1"
        assert isinstance(messages[2], HumanMessage)

    def test_respects_max_turns(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        for i in range(10):
            save_turn(history_dir, "s1", "user", f"q{i}", "m", "openai")
            save_turn(history_dir, "s1", "assistant", f"a{i}", "m", "openai")

        # max_turns=3 means last 6 turns (3 user/assistant pairs)
        messages = load_turns_as_langchain_messages(history_dir, "s1", max_turns=3)
        assert len(messages) == 6
        # Should be the LAST 3 pairs (q7..q9, a7..a9)
        assert messages[0].content == "q7"
        assert messages[-1].content == "a9"


class TestFormatHistoryAsPrompt:
    def test_no_turns_returns_raw_message(self) -> None:
        assert format_history_as_prompt([], "hello") == "hello"

    def test_wraps_history_in_tags(self) -> None:
        turns: list[Any] = [
            {"role": "user", "content": "q1", "timestamp": "t"},
            {"role": "assistant", "content": "a1", "timestamp": "t"},
        ]
        result = format_history_as_prompt(turns, "q2")
        assert "<conversation_history>" in result
        assert "</conversation_history>" in result
        assert "Human: q1" in result
        assert "Assistant: a1" in result
        assert result.endswith("Human: q2")


class TestListConversations:
    def test_empty_dir(self, tmp_path: Any) -> None:
        assert list_conversations(str(tmp_path)) == []

    def test_missing_dir(self, tmp_path: Any) -> None:
        assert list_conversations(str(tmp_path / "nope")) == []

    def test_returns_metadata(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "question 1", "gpt-4o", "openai")
        save_turn(history_dir, "s2", "user", "question 2", "claude-sonnet", "anthropic")

        result = list_conversations(history_dir)
        assert len(result) == 2
        ids = {c["session_id"] for c in result}
        assert ids == {"s1", "s2"}
        providers = {c["provider"] for c in result}
        assert providers == {"openai", "anthropic"}

    def test_sorted_by_updated_at_descending(self, tmp_path: Any) -> None:
        import time

        history_dir = str(tmp_path)
        save_turn(history_dir, "oldest", "user", "old", "m", "openai")
        time.sleep(0.02)
        save_turn(history_dir, "middle", "user", "mid", "m", "openai")
        time.sleep(0.02)
        save_turn(history_dir, "newest", "user", "new", "m", "openai")

        result = list_conversations(history_dir)
        assert [c["session_id"] for c in result] == ["newest", "middle", "oldest"]

    def test_skips_corrupted_files(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "good", "user", "q", "m", "openai")
        os.makedirs(history_dir, exist_ok=True)
        with open(os.path.join(history_dir, "2026_bad.json"), "w") as f:
            _ = f.write("not json")

        result = list_conversations(history_dir)
        assert len(result) == 1
        assert result[0]["session_id"] == "good"


class TestGetConversation:
    def test_returns_none_for_missing(self, tmp_path: Any) -> None:
        assert get_conversation(str(tmp_path), "nope") is None

    def test_returns_full_payload(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "question", "m", "openai")
        save_turn(history_dir, "s1", "assistant", "answer", "m", "openai")

        result = get_conversation(history_dir, "s1")
        assert result is not None
        assert result["session_id"] == "s1"
        assert len(result["turns"]) == 2

    def test_rejects_path_traversal(self, tmp_path: Any) -> None:
        assert get_conversation(str(tmp_path), "../etc/passwd") is None


class TestDeleteConversation:
    def test_removes_file(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "q", "m", "openai")
        assert delete_conversation(history_dir, "s1") is True
        assert load_turns(history_dir, "s1") == []

    def test_returns_false_for_missing(self, tmp_path: Any) -> None:
        assert delete_conversation(str(tmp_path), "nope") is False

    def test_rejects_path_traversal(self, tmp_path: Any) -> None:
        assert delete_conversation(str(tmp_path), "../etc/passwd") is False


class TestRenameConversation:
    def test_updates_title(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "original question", "m", "openai")
        assert rename_conversation(history_dir, "s1", "My custom title") is True

        convo = get_conversation(history_dir, "s1")
        assert convo is not None
        assert convo["title"] == "My custom title"

    def test_strips_whitespace(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "q", "m", "openai")
        _ = rename_conversation(history_dir, "s1", "   spaced   ")

        convo = get_conversation(history_dir, "s1")
        assert convo is not None
        assert convo["title"] == "spaced"

    def test_returns_false_for_missing(self, tmp_path: Any) -> None:
        assert rename_conversation(str(tmp_path), "nope", "title") is False

    def test_rejects_path_traversal(self, tmp_path: Any) -> None:
        assert rename_conversation(str(tmp_path), "../etc", "title") is False


class TestMigrateHistoryFiles:
    def test_empty_dir(self, tmp_path: Any) -> None:
        counts = migrate_history_files(str(tmp_path))
        assert counts == {"migrated": 0, "skipped": 0, "failed": 0}

    def test_missing_dir_noop(self, tmp_path: Any) -> None:
        counts = migrate_history_files(str(tmp_path / "nope"))
        assert counts == {"migrated": 0, "skipped": 0, "failed": 0}

    def test_converts_old_sdk_format(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        os.makedirs(history_dir, exist_ok=True)
        old_file = os.path.join(history_dir, "2026-01-01_000000_sdk1.json")
        with open(old_file, "w") as f:
            json.dump(
                {
                    "session_id": "sdk1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:05:00+00:00",
                    "turn_count": 1,
                    "model": "claude-sonnet-4",
                    "provider": "sdk",
                    "turns": [
                        {"role": "user", "content": "hello claude", "timestamp": "t"},
                        {"role": "assistant", "content": "hi", "timestamp": "t"},
                    ],
                },
                f,
            )

        counts = migrate_history_files(history_dir)
        assert counts["migrated"] == 1

        data = _read(old_file)
        assert data["title"] == "hello claude"
        assert data["provider"] == "anthropic"
        assert len(data["turns"]) == 2

    def test_converts_old_langgraph_format(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        os.makedirs(history_dir, exist_ok=True)
        old_file = os.path.join(history_dir, "2026-01-01_000000_lg1.json")
        with open(old_file, "w") as f:
            json.dump(
                {
                    "session_id": "lg1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:05:00+00:00",
                    "turn_count": 1,
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"type": "human", "data": {"content": "list pods"}},
                        {"type": "ai", "data": {"content": ""}},  # tool-call only, skipped
                        {"type": "tool", "data": {"content": "tool output"}},  # skipped
                        {"type": "ai", "data": {"content": "here are 3 pods"}},
                    ],
                },
                f,
            )

        counts = migrate_history_files(history_dir)
        assert counts["migrated"] == 1

        data = _read(old_file)
        assert "turns" in data
        assert "messages" not in data
        assert data["provider"] == "openai"
        assert data["title"] == "list pods"
        assert len(data["turns"]) == 2
        assert data["turns"][0]["role"] == "user"
        assert data["turns"][0]["content"] == "list pods"
        assert data["turns"][1]["role"] == "assistant"
        assert data["turns"][1]["content"] == "here are 3 pods"

    def test_skips_already_unified(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "hi", "m", "openai")

        counts = migrate_history_files(history_dir)
        assert counts["skipped"] == 1
        assert counts["migrated"] == 0

    def test_writes_marker_file(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        os.makedirs(history_dir, exist_ok=True)
        _ = migrate_history_files(history_dir)
        assert os.path.exists(os.path.join(history_dir, ".unified-format-v1"))

    def test_second_run_noop(self, tmp_path: Any) -> None:
        history_dir = str(tmp_path)
        save_turn(history_dir, "s1", "user", "hi", "m", "openai")
        _ = migrate_history_files(history_dir)

        # Add an old-format file after migration
        old_file = os.path.join(history_dir, "2020_old.json")
        with open(old_file, "w") as f:
            json.dump({"session_id": "old", "turns": [], "provider": "sdk"}, f)

        counts = migrate_history_files(history_dir)
        assert counts == {"migrated": 0, "skipped": 0, "failed": 0}


class TestLangchainMessagesToTurns:
    def test_extracts_human_and_ai_text(self) -> None:
        messages = [
            {"type": "human", "data": {"content": "hello"}},
            {"type": "ai", "data": {"content": "hi back"}},
        ]
        result = _langchain_messages_to_turns(messages, "t1")
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "hello", "timestamp": "t1"}
        assert result[1] == {"role": "assistant", "content": "hi back", "timestamp": "t1"}

    def test_skips_tool_messages(self) -> None:
        messages = [
            {"type": "human", "data": {"content": "hi"}},
            {"type": "tool", "data": {"content": "tool output"}},
            {"type": "ai", "data": {"content": "answer"}},
        ]
        result = _langchain_messages_to_turns(messages, "t")
        assert len(result) == 2

    def test_skips_empty_ai_messages(self) -> None:
        messages = [
            {"type": "human", "data": {"content": "hi"}},
            {"type": "ai", "data": {"content": ""}},
            {"type": "ai", "data": {"content": "real answer"}},
        ]
        result = _langchain_messages_to_turns(messages, "t")
        assert len(result) == 2
        assert result[1]["content"] == "real answer"

    def test_handles_list_content_blocks(self) -> None:
        messages = [
            {
                "type": "ai",
                "data": {
                    "content": [
                        {"type": "text", "text": "answer part 1"},
                        {"type": "text", "text": "part 2"},
                    ]
                },
            },
        ]
        result = _langchain_messages_to_turns(messages, "t")
        assert len(result) == 1
        assert "part 1" in result[0]["content"]
        assert "part 2" in result[0]["content"]


@pytest.mark.integration
class TestAgentIntegration:
    """Verify agent paths call save_turn correctly."""

    async def test_langgraph_invoke_saves_both_turns(self, mock_settings: Any, tmp_path: Any) -> None:
        from unittest.mock import AsyncMock

        from src.agent.agent import _invoke_langgraph_agent

        mock_settings.conversation_history_dir = str(tmp_path)

        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {
            "messages": [
                HumanMessage(content="hello"),
                AIMessage(content="hi there"),
            ]
        }
        mock_agent.aget_state = AsyncMock(return_value=type("S", (), {"values": {"messages": []}})())

        result = await _invoke_langgraph_agent(mock_agent, "hello", session_id="s1")
        assert result == "hi there"

        turns = load_turns(str(tmp_path), "s1")
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[0]["content"] == "hello"
        assert turns[1]["role"] == "assistant"
        assert turns[1]["content"] == "hi there"

    async def test_langgraph_invoke_skips_save_when_dir_empty(self, mock_settings: Any) -> None:
        from unittest.mock import AsyncMock, patch

        from src.agent.agent import _invoke_langgraph_agent

        mock_settings.conversation_history_dir = ""

        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {"messages": [AIMessage(content="response")]}
        mock_agent.aget_state = AsyncMock(return_value=type("S", (), {"values": {"messages": []}})())

        with patch("src.agent.agent.save_turn") as mock_save:
            _ = await _invoke_langgraph_agent(mock_agent, "hello", session_id="s1")

        mock_save.assert_not_called()
