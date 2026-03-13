"""Unit tests for agent assembly — system prompt, tool wiring, invocation."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from src.agent.agent import (
    _TOOL_LABELS,
    SYSTEM_PROMPT_TEMPLATE,
    _extract_ai_text,
    _get_tools,
    _is_tool_call_pairing_error,
    _summarize_tool_input,
    build_agent,
    invoke_agent,
    stream_agent,
)


class TestSystemPrompt:
    def test_mentions_all_tools(self) -> None:
        assert "prometheus_instant_query" in SYSTEM_PROMPT_TEMPLATE
        assert "prometheus_range_query" in SYSTEM_PROMPT_TEMPLATE
        assert "grafana_get_alerts" in SYSTEM_PROMPT_TEMPLATE
        assert "grafana_get_alert_rules" in SYSTEM_PROMPT_TEMPLATE
        assert "runbook_search" in SYSTEM_PROMPT_TEMPLATE

    def test_mentions_proxmox_tools(self) -> None:
        assert "proxmox_list_guests" in SYSTEM_PROMPT_TEMPLATE
        assert "proxmox_get_guest_config" in SYSTEM_PROMPT_TEMPLATE
        assert "proxmox_node_status" in SYSTEM_PROMPT_TEMPLATE
        assert "proxmox_list_tasks" in SYSTEM_PROMPT_TEMPLATE

    def test_mentions_pbs_tools(self) -> None:
        assert "pbs_datastore_status" in SYSTEM_PROMPT_TEMPLATE
        assert "pbs_list_backups" in SYSTEM_PROMPT_TEMPLATE
        assert "pbs_list_tasks" in SYSTEM_PROMPT_TEMPLATE

    def test_has_proxmox_vs_prometheus_guidance(self) -> None:
        assert "Proxmox API vs Prometheus" in SYSTEM_PROMPT_TEMPLATE

    def test_has_promql_patterns(self) -> None:
        assert "Common PromQL Patterns" in SYSTEM_PROMPT_TEMPLATE
        assert "topk" in SYSTEM_PROMPT_TEMPLATE
        assert "avg_over_time" in SYSTEM_PROMPT_TEMPLATE
        assert "rate(" in SYSTEM_PROMPT_TEMPLATE

    def test_has_tool_selection_guide(self) -> None:
        assert "Tool Selection Guide" in SYSTEM_PROMPT_TEMPLATE

    def test_advises_metrics_first(self) -> None:
        assert "query metrics first" in SYSTEM_PROMPT_TEMPLATE

    def test_warns_against_fabrication(self) -> None:
        assert "Never fabricate" in SYSTEM_PROMPT_TEMPLATE

    def test_has_power_consumption_guidance(self) -> None:
        assert "homeassistant_sensor_power_w" in SYSTEM_PROMPT_TEMPLATE
        assert "node_hwmon_power_watt" in SYSTEM_PROMPT_TEMPLATE


class TestGetTools:
    def test_includes_prometheus_tools(self, mock_settings: object) -> None:
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "prometheus_instant_query" in tool_names
        assert "prometheus_range_query" in tool_names

    def test_includes_grafana_tools(self, mock_settings: object) -> None:
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "grafana_get_alerts" in tool_names
        assert "grafana_get_alert_rules" in tool_names

    def test_includes_proxmox_tools_when_configured(self, mock_settings: object) -> None:
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "proxmox_list_guests" in tool_names
        assert "proxmox_get_guest_config" in tool_names
        assert "proxmox_node_status" in tool_names
        assert "proxmox_list_tasks" in tool_names

    def test_excludes_proxmox_tools_when_not_configured(self, mock_settings: object) -> None:
        mock_settings.proxmox_url = ""  # type: ignore[attr-defined]
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "proxmox_list_guests" not in tool_names
        assert "proxmox_get_guest_config" not in tool_names

    def test_includes_pbs_tools_when_configured(self, mock_settings: object) -> None:
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "pbs_datastore_status" in tool_names
        assert "pbs_list_backups" in tool_names
        assert "pbs_list_tasks" in tool_names

    def test_excludes_pbs_tools_when_not_configured(self, mock_settings: object) -> None:
        mock_settings.pbs_url = ""  # type: ignore[attr-defined]
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "pbs_datastore_status" not in tool_names
        assert "pbs_list_backups" not in tool_names

    def test_includes_runbook_search(self, mock_settings: object) -> None:
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "runbook_search" in tool_names

    def test_gracefully_handles_missing_runbook_tool(self, mock_settings: object) -> None:
        with patch(
            "src.agent.retrieval.runbooks.load_vector_store",
            side_effect=Exception("no vector store"),
        ):
            # Import still works but tool would fail at runtime;
            # _get_tools should still include it since import succeeds
            tools = _get_tools()
            assert len(tools) >= 4


class TestBuildAgent:
    def test_builds_without_error(self, mock_settings: object) -> None:
        agent = build_agent()
        assert agent is not None
        assert hasattr(agent, "invoke")

    def test_custom_model_name(self, mock_settings: object) -> None:
        agent = build_agent(model_name="gpt-4o")
        assert agent is not None

    def test_system_prompt_contains_current_date(self, mock_settings: object) -> None:
        """build_agent should inject today's date into the system prompt."""
        with patch("src.agent.agent.create_agent") as mock_create:
            mock_create.return_value = AsyncMock()
            build_agent()

            call_kwargs = mock_create.call_args
            prompt: str = call_kwargs.kwargs.get("system_prompt") or call_kwargs.args[2]
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            assert today in prompt
            assert "retains data" in prompt.lower()

    def test_system_prompt_has_aggregation_guidance(self, mock_settings: object) -> None:
        """The prompt template should include instant-query aggregation guidance."""
        assert "Single-value aggregation" in SYSTEM_PROMPT_TEMPLATE
        assert "prometheus_instant_query" in SYSTEM_PROMPT_TEMPLATE
        assert "*_over_time" in SYSTEM_PROMPT_TEMPLATE


class TestIsToolCallPairingError:
    """Tests for the tool_call pairing error detection helper."""

    def test_detects_openai_tool_call_error(self) -> None:
        exc = Exception(
            "Error code: 400 - {'error': {'message': \"An assistant message "
            "with 'tool_calls' must be followed by tool messages responding "
            "to each 'tool_call_id'.\"}}"
        )
        assert _is_tool_call_pairing_error(exc) is True

    def test_ignores_unrelated_errors(self) -> None:
        assert _is_tool_call_pairing_error(Exception("Connection refused")) is False
        assert _is_tool_call_pairing_error(Exception("rate limit exceeded")) is False
        assert _is_tool_call_pairing_error(TimeoutError("timed out")) is False

    def test_ignores_partial_match(self) -> None:
        # Must have BOTH "tool_calls" AND "tool messages" to match
        assert _is_tool_call_pairing_error(Exception("tool_calls not found")) is False
        assert _is_tool_call_pairing_error(Exception("tool messages missing")) is False


class TestInvokeAgent:
    """Tests for invoke_agent error handling and session recovery."""

    @pytest.mark.integration
    async def test_returns_ai_message_content(self, mock_settings: object) -> None:
        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {"messages": [AIMessage(content="CPU is at 42%.")]}

        result = await invoke_agent(mock_agent, "What is CPU?", session_id="s1")
        assert result == "CPU is at 42%."

    @pytest.mark.integration
    async def test_returns_fallback_when_no_ai_message(self, mock_settings: object) -> None:
        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {"messages": []}

        result = await invoke_agent(mock_agent, "hello", session_id="s1")
        assert result == "No response generated."

    @pytest.mark.integration
    async def test_recovers_from_corrupted_tool_call_history(self, mock_settings: object) -> None:
        """When session history has orphaned tool_calls, invoke_agent retries
        with a fresh session instead of permanently failing."""
        tool_call_error = Exception(
            "Error code: 400 - {'error': {'message': \"An assistant message "
            "with 'tool_calls' must be followed by tool messages responding "
            "to each 'tool_call_id'. The following tool_call_ids did not have "
            'response messages: call_abc123"}}'
        )

        mock_agent = AsyncMock()
        # First call with original session: corrupted history → error
        # Second call with fresh session: succeeds
        mock_agent.ainvoke.side_effect = [
            tool_call_error,
            {"messages": [AIMessage(content="Recovered response.")]},
        ]

        result = await invoke_agent(mock_agent, "hello?", session_id="broken-sess")

        assert result == "Recovered response."
        assert mock_agent.ainvoke.call_count == 2

        # Verify the retry used a different thread_id (config passed as kwarg)
        first_thread = mock_agent.ainvoke.call_args_list[0].kwargs["config"]["configurable"]["thread_id"]
        second_thread = mock_agent.ainvoke.call_args_list[1].kwargs["config"]["configurable"]["thread_id"]
        assert first_thread != second_thread
        assert second_thread.startswith("broken-sess-")

    @pytest.mark.integration
    async def test_raises_non_tool_call_errors(self, mock_settings: object) -> None:
        """Errors unrelated to tool_call pairing still propagate."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke.side_effect = RuntimeError("LLM exploded")

        with pytest.raises(RuntimeError, match="LLM exploded"):
            await invoke_agent(mock_agent, "boom", session_id="s1")

    @pytest.mark.integration
    async def test_timeout_error_propagates(self, mock_settings: object) -> None:
        """A generic timeout from ainvoke propagates (not a tool_call pairing issue)."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke.side_effect = TimeoutError("timed out")

        with pytest.raises(TimeoutError, match="timed out"):
            await invoke_agent(mock_agent, "slow query", session_id="s1")

    @pytest.mark.integration
    async def test_recovery_failure_propagates(self, mock_settings: object) -> None:
        """If the fresh-session retry also fails, that error propagates."""
        tool_call_error = Exception(
            "An assistant message with 'tool_calls' must be followed by "
            "tool messages responding to each 'tool_call_id'."
        )

        mock_agent = AsyncMock()
        mock_agent.ainvoke.side_effect = [
            tool_call_error,
            RuntimeError("LLM still broken"),
        ]

        with pytest.raises(RuntimeError, match="LLM still broken"):
            await invoke_agent(mock_agent, "hello", session_id="s1")


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestToolLabels:
    """Tool label dict covers all registered tools."""

    def test_core_tools_have_labels(self) -> None:
        core = [
            "prometheus_instant_query",
            "prometheus_range_query",
            "prometheus_search_metrics",
            "grafana_get_alerts",
            "grafana_get_alert_rules",
            "runbook_search",
        ]
        for name in core:
            assert name in _TOOL_LABELS, f"Missing label for {name}"

    def test_labels_are_human_readable(self) -> None:
        for name, label in _TOOL_LABELS.items():
            assert len(label) > 5, f"Label for {name} too short"
            assert label[0].isupper(), f"Label for {name} should start uppercase"


class TestSummarizeToolInput:
    """_summarize_tool_input produces concise descriptions."""

    def test_query_field(self) -> None:
        result = _summarize_tool_input("prometheus_instant_query", {"query": "up{job='node'}"})
        assert result == "`up{job='node'}`"

    def test_long_query_truncated(self) -> None:
        long_query = "a" * 200
        result = _summarize_tool_input("prometheus_instant_query", {"query": long_query})
        assert result.endswith("...`")
        assert len(result) <= 125

    def test_search_term_field(self) -> None:
        result = _summarize_tool_input("prometheus_search_metrics", {"search_term": "cpu"})
        assert result == "`cpu`"

    def test_uid_field(self) -> None:
        result = _summarize_tool_input("grafana_get_dashboard", {"uid": "abc123"})
        assert result == "uid=abc123"

    def test_vmid_field(self) -> None:
        result = _summarize_tool_input("proxmox_get_guest_config", {"vmid": "100"})
        assert result == "vmid=100"

    def test_empty_for_unknown_fields(self) -> None:
        result = _summarize_tool_input("some_tool", {"foo": "bar"})
        assert result == ""

    def test_non_dict_returns_empty(self) -> None:
        result = _summarize_tool_input("some_tool", "not a dict")
        assert result == ""

    def test_empty_query_returns_empty(self) -> None:
        """Empty string values must not produce empty backticks like ``."""
        assert _summarize_tool_input("x", {"query": ""}) == ""
        assert _summarize_tool_input("x", {"search_term": ""}) == ""
        assert _summarize_tool_input("x", {"pattern": ""}) == ""
        assert _summarize_tool_input("x", {"uid": ""}) == ""
        assert _summarize_tool_input("x", {"vmid": ""}) == ""


class TestStreamAgent:
    """Tests for stream_agent async generator.

    The answer is always extracted from checkpoint state (aget_state) after
    streaming completes — the stream itself only yields tool progress events.
    """

    @pytest.mark.integration
    async def test_emits_status_and_answer(self, mock_settings: object) -> None:
        """Basic flow: status → answer extracted from checkpoint."""
        mock_agent = AsyncMock()

        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"event": "on_chain_end", "name": "agent", "data": {}}

        mock_agent.astream_events = fake_stream
        mock_agent.aget_state = AsyncMock(
            return_value=AsyncMock(values={"messages": [AIMessage(content="CPU is at 42%.")]})
        )

        events = [e async for e in stream_agent(mock_agent, "What is CPU?", session_id="s1")]

        types = [e["type"] for e in events]
        assert "status" in types
        assert "answer" in types

        answer_event = next(e for e in events if e["type"] == "answer")
        assert answer_event["content"] == "CPU is at 42%."
        assert answer_event["session_id"] == "s1"

    @pytest.mark.integration
    async def test_emits_tool_start_and_end(self, mock_settings: object) -> None:
        """Tool events are yielded during streaming."""
        mock_agent = AsyncMock()

        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {
                "event": "on_tool_start",
                "name": "prometheus_instant_query",
                "data": {"input": {"query": "up{job='node'}"}},
            }
            yield {
                "event": "on_tool_end",
                "name": "prometheus_instant_query",
                "data": {"output": "up=1"},
            }

        mock_agent.astream_events = fake_stream
        mock_agent.aget_state = AsyncMock(
            return_value=AsyncMock(values={"messages": [AIMessage(content="All nodes are up.")]})
        )

        events = [e async for e in stream_agent(mock_agent, "Are nodes up?", session_id="s1")]

        tool_start = next(e for e in events if e["type"] == "tool_start")
        assert "prometheus_instant_query" in tool_start["tool_name"]
        assert "`up{job='node'}`" in tool_start["content"]

        tool_end = next(e for e in events if e["type"] == "tool_end")
        assert tool_end["tool_name"] == "prometheus_instant_query"

        # Answer comes from checkpoint, not stream events
        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "All nodes are up."

    @pytest.mark.integration
    async def test_answer_from_checkpoint_not_stream(self, mock_settings: object) -> None:
        """The answer is extracted from checkpoint state, not stream events."""
        mock_agent = AsyncMock()

        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            # Stream only has tool events — no chat_model events needed
            yield {
                "event": "on_tool_start",
                "name": "runbook_search",
                "data": {"input": {"query": "disk"}},
            }
            yield {
                "event": "on_tool_end",
                "name": "runbook_search",
                "data": {"output": "some runbook content"},
            }

        # Checkpoint has the final answer (as it would in real LangGraph)
        mock_agent.astream_events = fake_stream
        mock_agent.aget_state = AsyncMock(
            return_value=AsyncMock(
                values={
                    "messages": [
                        AIMessage(content="Let me check..."),  # intermediate
                        AIMessage(content="The disk is healthy."),  # final
                    ]
                }
            )
        )

        events = [e async for e in stream_agent(mock_agent, "disk status?", session_id="s1")]

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "The disk is healthy."

    @pytest.mark.integration
    async def test_error_during_streaming(self, mock_settings: object) -> None:
        """Non-recoverable errors yield an error event."""
        mock_agent = AsyncMock()

        async def failing_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise RuntimeError("LLM exploded")
            yield  # noqa: RET503 — make this an async generator

        mock_agent.astream_events = failing_stream

        events = [e async for e in stream_agent(mock_agent, "boom", session_id="s1")]

        error_event = next(e for e in events if e["type"] == "error")
        assert "LLM exploded" in error_event["content"]

    @pytest.mark.integration
    async def test_recovers_from_corrupted_session(self, mock_settings: object) -> None:
        """Tool-call pairing errors trigger fallback to ainvoke."""
        mock_agent = AsyncMock()

        async def failing_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise Exception(
                "An assistant message with 'tool_calls' must be followed by "
                "tool messages responding to each 'tool_call_id'."
            )
            yield  # noqa: RET503

        mock_agent.astream_events = failing_stream
        mock_agent.ainvoke.return_value = {"messages": [AIMessage(content="Recovered.")]}

        events = [e async for e in stream_agent(mock_agent, "hello", session_id="broken")]

        types = [e["type"] for e in events]
        assert "status" in types  # "Retrying with fresh session..."
        assert "answer" in types

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "Recovered."
        # Session ID should be different from original
        assert answer["session_id"].startswith("broken-")

    @pytest.mark.integration
    async def test_fallback_response_text(self, mock_settings: object) -> None:
        """When checkpoint has no AI messages, the fallback text is used."""
        mock_agent = AsyncMock()

        async def empty_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"event": "on_chain_end", "name": "agent", "data": {}}

        mock_agent.astream_events = empty_stream
        mock_agent.aget_state = AsyncMock(return_value=AsyncMock(values={"messages": []}))

        events = [e async for e in stream_agent(mock_agent, "hello", session_id="s1")]

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "No response generated."


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestStreamAgentRegressions:
    """Regression tests for bugs found in the streaming UI."""

    def test_empty_backticks_never_produced(self) -> None:
        """Regression: _summarize_tool_input returned `` for empty/None values,
        which rendered as visible empty backticks in the Streamlit UI."""
        # Empty strings
        for field in ("query", "search_term", "pattern"):
            result = _summarize_tool_input("any_tool", {field: ""})
            assert "``" not in result, f"Empty backticks for {field}=''"
            assert result == ""

        # None values
        for field in ("query", "search_term", "pattern", "uid", "vmid"):
            result = _summarize_tool_input("any_tool", {field: None})
            assert "``" not in result, f"Empty backticks for {field}=None"
            assert result == ""

    def test_tool_start_content_no_trailing_empty_summary(self) -> None:
        """Regression: tool_start content showed 'Label: ' with trailing colon+space
        when the summary was empty, because the format string was always applied."""
        label = _TOOL_LABELS.get("grafana_get_alerts", "Checking Grafana alerts")
        summary = _summarize_tool_input("grafana_get_alerts", {})
        # This mirrors the logic in stream_agent
        content = f"{label}: {summary}" if summary else label
        assert not content.endswith(": "), "Trailing ': ' when summary is empty"
        assert content == label

    @pytest.mark.integration
    async def test_answer_not_no_response_generated(self, mock_settings: object) -> None:
        """Regression: stream_agent always returned 'No response generated.' because
        it tried to extract the answer from on_chat_model_end stream events, where
        the output type (AIMessageChunk) didn't match isinstance(output, AIMessage).

        Fix: answer is now extracted from checkpoint state via aget_state(), which
        always contains proper AIMessage objects."""
        mock_agent = AsyncMock()

        # Simulate a stream with NO chat_model events at all — this is what
        # happens when include_types=["tool"] filters them out
        async def tool_only_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {
                "event": "on_tool_start",
                "name": "prometheus_instant_query",
                "data": {"input": {"query": "up"}},
            }
            yield {
                "event": "on_tool_end",
                "name": "prometheus_instant_query",
                "data": {"output": "up=1"},
            }

        mock_agent.astream_events = tool_only_stream
        # Checkpoint has the real answer
        mock_agent.aget_state = AsyncMock(
            return_value=AsyncMock(values={"messages": [AIMessage(content="All 3 nodes are up and healthy.")]})
        )

        events = [e async for e in stream_agent(mock_agent, "are nodes up?", session_id="s1")]

        answer = next(e for e in events if e["type"] == "answer")
        # Must NOT be the fallback text
        assert answer["content"] != "No response generated."
        assert answer["content"] == "All 3 nodes are up and healthy."

    @pytest.mark.integration
    async def test_recovery_path_also_returns_answer(self, mock_settings: object) -> None:
        """Regression: after corrupted-session recovery via ainvoke fallback, the code
        fell through to aget_state() with the original (corrupted) config, which
        overwrote the already-extracted answer with empty messages.

        Fix: recovery path uses try/except/else so aget_state only runs on the
        normal (non-exception) path."""
        mock_agent = AsyncMock()

        async def failing_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise Exception(
                "An assistant message with 'tool_calls' must be followed by "
                "tool messages responding to each 'tool_call_id'."
            )
            yield  # noqa: RET503

        mock_agent.astream_events = failing_stream
        # ainvoke fallback returns a real answer
        mock_agent.ainvoke.return_value = {"messages": [AIMessage(content="Recovered successfully.")]}
        # aget_state on the ORIGINAL config would return garbage — but it
        # should NOT be called in the recovery path
        mock_agent.aget_state = AsyncMock(return_value=AsyncMock(values={"messages": []}))

        events = [e async for e in stream_agent(mock_agent, "test", session_id="bad")]

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "Recovered successfully."
        assert answer["content"] != "No response generated."
        # aget_state should NOT have been called (recovery path skips it)
        mock_agent.aget_state.assert_not_called()


class TestExtractAiText:
    """Tests for _extract_ai_text helper."""

    def test_string_content(self) -> None:
        msg = AIMessage(content="Hello world")
        assert _extract_ai_text(msg) == "Hello world"

    def test_empty_string(self) -> None:
        msg = AIMessage(content="")
        assert _extract_ai_text(msg) == ""

    def test_list_content_single_text_block(self) -> None:
        """Anthropic returns content as a list of typed blocks."""
        msg = AIMessage(content=[{"type": "text", "text": "Hello world"}])
        assert _extract_ai_text(msg) == "Hello world"

    def test_list_content_multiple_text_blocks(self) -> None:
        msg = AIMessage(
            content=[
                {"type": "text", "text": "First part. "},
                {"type": "text", "text": "Second part."},
            ]
        )
        assert _extract_ai_text(msg) == "First part. Second part."

    def test_list_content_with_non_text_blocks(self) -> None:
        """Non-text blocks (e.g., tool_use) should be skipped."""
        msg = AIMessage(
            content=[
                {"type": "tool_use", "id": "1", "name": "foo", "input": {}},
                {"type": "text", "text": "The answer is 42."},
            ]
        )
        assert _extract_ai_text(msg) == "The answer is 42."

    def test_list_content_only_tool_use(self) -> None:
        """If only tool_use blocks, no text should be extracted."""
        msg = AIMessage(content=[{"type": "tool_use", "id": "1", "name": "foo", "input": {}}])
        assert _extract_ai_text(msg) == ""

    def test_list_content_plain_strings(self) -> None:
        """Some providers return plain strings in the list."""
        msg = AIMessage(content=["Hello ", "world"])
        assert _extract_ai_text(msg) == "Hello world"

    def test_empty_list(self) -> None:
        msg = AIMessage(content=[])
        assert _extract_ai_text(msg) == ""


class TestAnthropicContentFormatRegressions:
    """Regression: Anthropic returns AIMessage.content as a list of content blocks
    (e.g., [{"type": "text", "text": "..."}]) instead of a plain string.
    The isinstance(msg.content, str) check silently skipped every AIMessage,
    causing 'No response generated.' for all queries when using Anthropic."""

    @pytest.mark.integration
    async def test_stream_agent_handles_list_content(self, mock_settings: object) -> None:
        """stream_agent must extract text from list-format AIMessage content."""
        mock_agent = AsyncMock()

        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"event": "on_chain_end", "name": "agent", "data": {}}

        mock_agent.astream_events = fake_stream
        # Anthropic-style content: list of typed blocks
        mock_agent.aget_state = AsyncMock(
            return_value=AsyncMock(
                values={"messages": [AIMessage(content=[{"type": "text", "text": "No alerts are currently firing."}])]}
            )
        )

        events = [e async for e in stream_agent(mock_agent, "any alerts?", session_id="s1")]

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "No alerts are currently firing."
        assert answer["content"] != "No response generated."

    @pytest.mark.integration
    async def test_invoke_agent_handles_list_content(self, mock_settings: object) -> None:
        """invoke_agent must also handle list-format content from Anthropic."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {
            "messages": [AIMessage(content=[{"type": "text", "text": "CPU is at 42%."}])]
        }

        result = await invoke_agent(mock_agent, "What is CPU?", session_id="s1")
        assert result == "CPU is at 42%."
        assert result != "No response generated."

    @pytest.mark.integration
    async def test_stream_agent_skips_tool_use_only_messages(self, mock_settings: object) -> None:
        """AIMessages with only tool_use blocks (no text) should be skipped."""
        mock_agent = AsyncMock()

        async def fake_stream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            yield {"event": "on_chain_end", "name": "agent", "data": {}}

        mock_agent.astream_events = fake_stream
        mock_agent.aget_state = AsyncMock(
            return_value=AsyncMock(
                values={
                    "messages": [
                        # First message: tool_use only (intermediate)
                        AIMessage(content=[{"type": "tool_use", "id": "1", "name": "prom", "input": {}}]),
                        # Second message: actual answer
                        AIMessage(content=[{"type": "text", "text": "All systems operational."}]),
                    ]
                }
            )
        )

        events = [e async for e in stream_agent(mock_agent, "status?", session_id="s1")]

        answer = next(e for e in events if e["type"] == "answer")
        assert answer["content"] == "All systems operational."
