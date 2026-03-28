"""Tests for the Claude Agent SDK agent path."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.sdk_agent import (
    _BLOCKED_BUILTINS,
    _STREAM_CLOSE_TIMEOUT_MS,
    _build_system_prompt,
    _prefix_tool_names,
    build_sdk_options,
)


class TestPrefixToolNames:
    """Test system prompt tool name prefixing for the SDK path."""

    def test_prefixes_known_tool_names(self) -> None:
        text = "Use `prometheus_instant_query` to check metrics."
        result = _prefix_tool_names(text)
        assert "mcp__sre__prometheus_instant_query" in result

    def test_prefixes_multiple_tools(self) -> None:
        text = "Call grafana_get_alerts and then runbook_search."
        result = _prefix_tool_names(text)
        assert "mcp__sre__grafana_get_alerts" in result
        assert "mcp__sre__runbook_search" in result

    def test_does_not_double_prefix(self) -> None:
        text = "mcp__sre__prometheus_instant_query"
        result = _prefix_tool_names(text)
        # Should not become mcp__sre__mcp__sre__...
        assert "mcp__sre__mcp__sre__" not in result

    def test_leaves_non_tool_names_alone(self) -> None:
        text = "This is a normal sentence about CPU usage."
        result = _prefix_tool_names(text)
        assert result == text


class TestBuildSystemPrompt:
    """Test system prompt construction."""

    def test_contains_current_date(self) -> None:
        from datetime import UTC, datetime

        prompt = _build_system_prompt()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert today in prompt

    def test_contains_tool_references(self) -> None:
        prompt = _build_system_prompt()
        # Should have MCP-prefixed tool names
        assert "mcp__sre__prometheus_instant_query" in prompt

    def test_contains_retention_cutoff(self) -> None:
        prompt = _build_system_prompt()
        assert "retains data" in prompt.lower() or "retention" in prompt.lower()


class TestBuildSdkOptions:
    """Test SDK options construction."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

    def test_builds_options_with_correct_model(self) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options(model_override="claude-sonnet-4-20250514")
            assert options.model == "claude-sonnet-4-20250514"

    def test_has_mcp_servers(self) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            assert "sre" in options.mcp_servers

    def test_allowed_tools_wildcard(self) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            assert "mcp__sre__*" in options.allowed_tools

    def test_disallowed_tools_blocks_builtins(self) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            for tool in _BLOCKED_BUILTINS:
                assert tool in options.disallowed_tools

    def test_permission_mode_bypass(self) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            assert options.permission_mode == "bypassPermissions"

    def test_max_turns_set(self) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            assert options.max_turns == 25

    def test_env_strips_anthropic_api_key(self) -> None:
        """ANTHROPIC_API_KEY must be cleared so the CLI uses OAuth credentials."""
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            assert options.env.get("ANTHROPIC_API_KEY") == ""

    def test_env_sets_stream_close_timeout(self) -> None:
        """CLAUDE_CODE_STREAM_CLOSE_TIMEOUT guards against CLI inactivity timer bug."""
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            options = build_sdk_options()
            assert options.env.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT") == _STREAM_CLOSE_TIMEOUT_MS
            # Must be a large value (at least 10 minutes) to survive long agent loops
            assert int(_STREAM_CLOSE_TIMEOUT_MS) >= 600_000
