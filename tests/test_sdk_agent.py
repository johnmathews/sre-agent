"""Tests for the Claude Agent SDK agent path."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.sdk_agent import (
    _BLOCKED_BUILTINS,
    _STREAM_CLOSE_TIMEOUT_MS,
    _build_system_prompt,
    _prefix_tool_names,
    _summarize_sdk_tool_input,
    _tool_display_name,
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


class TestToolDisplayName:
    """Test MCP prefix stripping for display."""

    def test_strips_sre_prefix(self) -> None:
        assert _tool_display_name("mcp__sre__prometheus_instant_query") == "prometheus_instant_query"

    def test_strips_docs_prefix(self) -> None:
        assert _tool_display_name("mcp__docs__search_docs") == "search_docs"

    def test_no_prefix_unchanged(self) -> None:
        assert _tool_display_name("runbook_search") == "runbook_search"

    def test_sre_prefix_takes_priority_over_docs(self) -> None:
        # Ensure sre prefix is checked first
        name = "mcp__sre__some_tool"
        result = _tool_display_name(name)
        assert result == "some_tool"


class TestSummarizeSdkToolInput:
    """Test parameter summary extraction for tool_start events."""

    def test_extracts_query_param(self) -> None:
        assert _summarize_sdk_tool_input({"query": "up{job='node'}"}) == "up{job='node'}"

    def test_extracts_expr_param(self) -> None:
        assert _summarize_sdk_tool_input({"expr": "rate(http_requests[5m])"}) == "rate(http_requests[5m])"

    def test_extracts_search_param(self) -> None:
        assert _summarize_sdk_tool_input({"search": "disk failure"}) == "disk failure"

    def test_returns_empty_for_no_matching_keys(self) -> None:
        assert _summarize_sdk_tool_input({"unrelated": "value"}) == ""

    def test_returns_empty_for_none(self) -> None:
        assert _summarize_sdk_tool_input(None) == ""

    def test_returns_empty_for_empty_dict(self) -> None:
        assert _summarize_sdk_tool_input({}) == ""

    def test_truncates_long_values(self) -> None:
        long_query = "x" * 200
        result = _summarize_sdk_tool_input({"query": long_query})
        assert len(result) == 80

    def test_skips_non_string_values(self) -> None:
        assert _summarize_sdk_tool_input({"query": 42}) == ""

    def test_priority_order(self) -> None:
        """First matching key in priority order wins."""
        result = _summarize_sdk_tool_input({"expr": "rate(...)", "query": "up"})
        # 'query' comes before 'expr' in the priority list
        assert result == "up"
