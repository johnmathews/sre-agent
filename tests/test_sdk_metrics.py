"""Tests for SDK observability metrics extraction."""

from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from src.observability.metrics import TOOL_CALL_DURATION
from src.observability.sdk_metrics import extract_tool_names, record_sdk_metrics


class TestExtractToolNames:
    """Test tool name extraction from SDK messages."""

    def test_extracts_tool_names_from_assistant_messages(self) -> None:
        messages = [
            AssistantMessage(
                content=[
                    ToolUseBlock(id="1", name="mcp__sre__prometheus_instant_query", input={}),
                    TextBlock(text="Result..."),
                ],
                model="claude-opus-4-6",
            )
        ]
        names = extract_tool_names(messages)
        assert names == ["prometheus_instant_query"]

    def test_strips_mcp_prefix(self) -> None:
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="1", name="mcp__sre__grafana_get_alerts", input={})],
                model="claude-opus-4-6",
            )
        ]
        names = extract_tool_names(messages)
        assert names == ["grafana_get_alerts"]

    def test_handles_no_tool_calls(self) -> None:
        messages = [
            AssistantMessage(
                content=[TextBlock(text="Just text, no tools.")],
                model="claude-opus-4-6",
            )
        ]
        names = extract_tool_names(messages)
        assert names == []

    def test_handles_empty_messages(self) -> None:
        assert extract_tool_names([]) == []

    def test_handles_multiple_tool_calls(self) -> None:
        messages = [
            AssistantMessage(
                content=[
                    ToolUseBlock(id="1", name="mcp__sre__prometheus_search_metrics", input={}),
                    ToolUseBlock(id="2", name="mcp__sre__prometheus_instant_query", input={}),
                ],
                model="claude-opus-4-6",
            )
        ]
        names = extract_tool_names(messages)
        assert names == ["prometheus_search_metrics", "prometheus_instant_query"]


class TestRecordSdkMetrics:
    """Test Prometheus metrics recording from SDK messages."""

    def test_records_without_error(self) -> None:
        """record_sdk_metrics should never raise."""
        messages = [
            AssistantMessage(
                content=[TextBlock(text="Hello")],
                model="claude-opus-4-6",
            )
        ]
        result = ResultMessage(
            subtype="success",
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=1,
            session_id="test",
            total_cost_usd=0.01,
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        # Should not raise
        record_sdk_metrics(messages, result)

    def test_handles_none_result(self) -> None:
        """record_sdk_metrics with no result message should not raise."""
        messages = [
            AssistantMessage(
                content=[TextBlock(text="Hello")],
                model="claude-opus-4-6",
            )
        ]
        record_sdk_metrics(messages, None)

    def test_handles_empty_messages(self) -> None:
        record_sdk_metrics([], None)

    def test_records_tool_durations(self) -> None:
        """Tool durations passed from the message loop are recorded in the histogram."""
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="1", name="mcp__sre__prometheus_instant_query", input={})],
                model="claude-opus-4-6",
            )
        ]
        # Get the sample count before
        before = TOOL_CALL_DURATION.labels(tool_name="prometheus_instant_query")._sum.get()

        tool_durations = [("prometheus_instant_query", 2.5)]
        record_sdk_metrics(messages, None, tool_durations)

        after = TOOL_CALL_DURATION.labels(tool_name="prometheus_instant_query")._sum.get()
        assert after - before == 2.5

    def test_records_multiple_tool_durations(self) -> None:
        """Multiple tool durations from a single request are all recorded."""
        messages = [
            AssistantMessage(
                content=[
                    ToolUseBlock(id="1", name="mcp__sre__grafana_get_alerts", input={}),
                    ToolUseBlock(id="2", name="mcp__sre__runbook_search", input={}),
                ],
                model="claude-opus-4-6",
            )
        ]
        before_grafana = TOOL_CALL_DURATION.labels(tool_name="grafana_get_alerts")._sum.get()
        before_runbook = TOOL_CALL_DURATION.labels(tool_name="runbook_search")._sum.get()

        tool_durations = [("grafana_get_alerts", 1.0), ("runbook_search", 0.5)]
        record_sdk_metrics(messages, None, tool_durations)

        after_grafana = TOOL_CALL_DURATION.labels(tool_name="grafana_get_alerts")._sum.get()
        after_runbook = TOOL_CALL_DURATION.labels(tool_name="runbook_search")._sum.get()
        assert after_grafana - before_grafana == 1.0
        assert after_runbook - before_runbook == 0.5

    def test_no_durations_is_safe(self) -> None:
        """Passing None or empty durations does not error."""
        record_sdk_metrics([], None, None)
        record_sdk_metrics([], None, [])
