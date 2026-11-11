"""Tests for the Claude Agent SDK agent path."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, ToolUseBlock
from prometheus_client import REGISTRY

from src.agent.sdk_agent import (
    _BLOCKED_BUILTINS,
    _STREAM_CLOSE_TIMEOUT_MS,
    _build_system_prompt,
    _prefix_tool_names,
    _summarize_sdk_tool_input,
    _tool_display_name,
    build_sdk_options,
    invoke_sdk_agent,
    stream_sdk_agent,
)
from src.agent.tool_registry import TOOL_SPECS, input_schema

AGENT_SPECS = [s for s in TOOL_SPECS if "agent" in s.surfaces]


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

    def test_tool_name_pattern_covers_registry(self) -> None:
        """Every tool on the SDK server is rewritten to its mcp__sre__ name; /mcp-only tools are not on it."""
        for spec in TOOL_SPECS:
            text = f"Call `{spec.name}` first."
            expected = f"Call `mcp__sre__{spec.name}` first." if "agent" in spec.surfaces else text
            assert _prefix_tool_names(text) == expected, spec.name


class TestBuildSystemPrompt:
    """Test system prompt construction."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, mock_settings: object) -> None:
        pass

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

    def test_contains_weekday(self) -> None:
        from datetime import UTC, datetime

        prompt = _build_system_prompt()
        today_weekday = datetime.now(UTC).strftime("%A")
        assert today_weekday in prompt

    def test_contains_get_current_time_tool_with_mcp_prefix(self) -> None:
        prompt = _build_system_prompt()
        assert "mcp__sre__get_current_time" in prompt

    def test_contains_elapsed_time_guidance(self) -> None:
        prompt = _build_system_prompt()
        assert "Computing Elapsed Time" in prompt
        assert "epoch" in prompt.lower()

    def test_no_unfilled_placeholders(self) -> None:
        prompt = _build_system_prompt()
        # Catch leftover {placeholder} patterns from the prompt template
        import re

        leftover = re.findall(r"\{[a-z_]+\}", prompt)
        assert leftover == [], f"unfilled placeholders: {leftover}"


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

    def test_exclude_reaches_the_sdk_server(self, mock_settings: Any) -> None:
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            build_sdk_options(exclude={"runbook_search"})
        mock_mcp.assert_called_once_with(mock_settings, exclude={"runbook_search"})

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

    def test_extracts_search_term_param(self) -> None:
        assert _summarize_sdk_tool_input({"search_term": "disk"}) == "disk"

    def test_extracts_dashboard_param(self) -> None:
        assert _summarize_sdk_tool_input({"dashboard": "node-exporter"}) == "node-exporter"

    def test_summary_keys_are_real_parameters(self) -> None:
        """Each key is a parameter of some tool the SDK agent can call."""
        from src.agent.sdk_agent import _SUMMARY_KEYS

        params = {name for spec in AGENT_SPECS for name in input_schema(spec)["properties"]}
        assert [k for k in _SUMMARY_KEYS if k not in params] == []

    def test_summary_covers_every_required_string_parameter(self) -> None:
        """A tool that takes a required string shows something in its tool_start line."""
        for spec in AGENT_SPECS:
            schema = input_schema(spec)
            strings = [n for n in schema.get("required", []) if schema["properties"][n].get("type") == "string"]
            if strings:
                assert _summarize_sdk_tool_input({n: "x" for n in strings}) == "x", spec.name

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
        result = _summarize_sdk_tool_input({"label": "container", "query": "up"})
        # 'query' comes before 'label' in the priority list
        assert result == "up"


class TestStreamLabels:
    @pytest.mark.integration
    async def test_every_tool_has_label_on_sdk_stream(self, mock_settings: Any) -> None:
        """Each tool's tool_start and tool_end events carry its registry label."""
        blocks = [ToolUseBlock(id=f"t{i}", name=f"mcp__sre__{s.name}", input={}) for i, s in enumerate(AGENT_SPECS)]

        async def fake_query(**kwargs: object):  # type: ignore[no-untyped-def]
            yield AssistantMessage(content=list(blocks), model="test-model")

        options = MagicMock()
        options.model = "test-model"
        with (
            patch("src.agent.sdk_agent.query", side_effect=fake_query),
            patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock),
            patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"),
            patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options),
            patch("src.agent.sdk_agent.record_sdk_metrics"),
            patch("src.agent.sdk_agent._post_response_actions", return_value=""),
        ):
            events = [ev async for ev in stream_sdk_agent(options, "hi")]

        expected = [(s.name, s.label) for s in AGENT_SPECS]
        starts = [(e["tool_name"], e["content"]) for e in events if e["type"] == "tool_start"]
        ends = [(e["tool_name"], e["content"]) for e in events if e["type"] == "tool_end"]
        assert starts == expected
        assert ends == expected


# ---------------------------------------------------------------------------
# LLM call status on the SDK path: one count per request, error when it fails
# ---------------------------------------------------------------------------


def _llm_calls(status: str) -> float:
    return REGISTRY.get_sample_value("sre_assistant_llm_calls_total", {"status": status}) or 0.0


async def _failing_query(**kwargs: object):  # type: ignore[no-untyped-def]
    raise RuntimeError("Command failed with exit code 1")
    yield  # pragma: no cover — makes this an async generator


async def _error_result_query(**kwargs: object):  # type: ignore[no-untyped-def]
    yield ResultMessage(
        subtype="error_max_turns",
        duration_ms=10,
        duration_api_ms=5,
        is_error=True,
        num_turns=1,
        session_id="test",
        result="Reached max turns",
    )


def _sdk_patches(query_fn: Any, options: MagicMock) -> Any:
    """Patch everything around ``query`` except the metrics, so the real counters move."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("src.agent.sdk_agent.query", side_effect=query_fn))
    stack.enter_context(patch("src.agent.oauth_refresh.ensure_valid_token", new_callable=AsyncMock))
    stack.enter_context(patch("src.agent.sdk_agent._build_system_prompt", return_value="test prompt"))
    stack.enter_context(patch("src.agent.sdk_agent.ClaudeAgentOptions", return_value=options))
    stack.enter_context(patch("src.agent.sdk_agent._post_response_actions", return_value=""))
    stack.enter_context(patch("src.agent.agent_errors.get_token_health", return_value=("healthy", None)))
    return stack


def _options() -> MagicMock:
    options = MagicMock()
    options.model = "test-model"
    options.env = {}
    return options


class TestSdkLlmCallStatus:
    @pytest.mark.integration
    async def test_stream_exception_counts_llm_error(self, mock_settings: Any) -> None:
        options = _options()
        errors, successes = _llm_calls("error"), _llm_calls("success")

        with _sdk_patches(_failing_query, options):
            events = [ev async for ev in stream_sdk_agent(options, "hi")]

        assert [ev["type"] for ev in events if ev["type"] == "error"] == ["error"]
        assert _llm_calls("error") - errors == 1.0
        assert _llm_calls("success") - successes == 0.0

    @pytest.mark.integration
    async def test_invoke_exception_counts_llm_error(self, mock_settings: Any) -> None:
        options = _options()
        errors, successes = _llm_calls("error"), _llm_calls("success")

        with _sdk_patches(_failing_query, options), pytest.raises(RuntimeError, match="exit code 1"):
            await invoke_sdk_agent(options, "hi")

        assert _llm_calls("error") - errors == 1.0
        assert _llm_calls("success") - successes == 0.0

    @pytest.mark.integration
    @pytest.mark.parametrize("path", ["stream", "invoke"])
    async def test_error_result_counts_llm_error_once(self, mock_settings: Any, path: str) -> None:
        """A ResultMessage with is_error goes through record_sdk_metrics once: one error, no success."""
        options = _options()
        errors, successes = _llm_calls("error"), _llm_calls("success")

        with _sdk_patches(_error_result_query, options):
            if path == "stream":
                _ = [ev async for ev in stream_sdk_agent(options, "hi")]
            else:
                _ = await invoke_sdk_agent(options, "hi")

        assert _llm_calls("error") - errors == 1.0
        assert _llm_calls("success") - successes == 0.0
