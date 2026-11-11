"""Unit tests for observability metrics and callback handler."""

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from prometheus_client import REGISTRY

from src.observability.callbacks import MetricsCallbackHandler
from src.observability.metrics import (
    COMPONENT_HEALTHY,
    DEFAULT_COST_PER_TOKEN,
    LLM_CALLS_TOTAL,
    LLM_ESTIMATED_COST,
    LLM_TOKEN_USAGE,
    REQUEST_DURATION,
    REQUESTS_IN_PROGRESS,
    REQUESTS_TOTAL,
    TOOL_CALL_DURATION,
    TOOL_CALLS_TOTAL,
)
from src.observability.tool_calls import MAX_ERROR_CHARS
from tests.test_tool_calls import LOGGER as TOOL_CALL_LOGGER
from tests.test_tool_calls import fields, tool_call_records

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample(metric_name: str, labels: dict[str, str] | None = None) -> float | None:
    """Read current value from the default registry."""
    return REGISTRY.get_sample_value(metric_name, labels or {})


# ---------------------------------------------------------------------------
# Metric definition tests
# ---------------------------------------------------------------------------


class TestMetricDefinitions:
    """Verify all expected metrics are registered with correct types."""

    def test_request_duration_is_histogram(self) -> None:
        assert REQUEST_DURATION._type == "histogram"

    def test_requests_total_is_counter(self) -> None:
        assert REQUESTS_TOTAL._type == "counter"

    def test_requests_in_progress_is_gauge(self) -> None:
        assert REQUESTS_IN_PROGRESS._type == "gauge"

    def test_tool_call_duration_is_histogram(self) -> None:
        assert TOOL_CALL_DURATION._type == "histogram"

    def test_tool_calls_total_is_counter(self) -> None:
        assert TOOL_CALLS_TOTAL._type == "counter"

    def test_llm_calls_total_is_counter(self) -> None:
        assert LLM_CALLS_TOTAL._type == "counter"

    def test_llm_token_usage_is_counter(self) -> None:
        assert LLM_TOKEN_USAGE._type == "counter"

    def test_llm_estimated_cost_is_counter(self) -> None:
        assert LLM_ESTIMATED_COST._type == "counter"

    def test_component_healthy_is_gauge(self) -> None:
        assert COMPONENT_HEALTHY._type == "gauge"


# ---------------------------------------------------------------------------
# Callback handler tests
# ---------------------------------------------------------------------------


class TestMetricsCallbackHandler:
    """Test the LangChain callback handler records metrics correctly."""

    def test_tool_success_records_counter_and_histogram(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()
        labels = {"tool_name": "test_tool", "status": "success", "source": "agent", "client": "agent"}

        # Snapshot before
        before = _sample("sre_assistant_tool_calls_total", labels)
        hist_before = _sample(
            "sre_assistant_tool_call_duration_seconds_count", {"tool_name": "test_tool", "source": "agent"}
        )

        with caplog.at_level(logging.INFO, logger=TOOL_CALL_LOGGER):
            handler.on_tool_start(
                serialized={"name": "test_tool"},
                input_str="{'query': 'up'}",
                run_id=run_id,
                inputs={"query": "up"},
            )
            handler.on_tool_end(output="result", run_id=run_id)

        after = _sample("sre_assistant_tool_calls_total", labels)
        assert after is not None
        assert after - (before or 0.0) == 1.0
        hist_after = _sample(
            "sre_assistant_tool_call_duration_seconds_count", {"tool_name": "test_tool", "source": "agent"}
        )
        assert (hist_after or 0.0) - (hist_before or 0.0) == 1.0
        [record] = tool_call_records(caplog)
        parsed = fields(record)
        assert parsed["source"] == "agent"
        assert parsed["client"] == "agent"
        assert parsed["outcome"] == "success"
        assert parsed["result_chars"] == "6"
        assert parsed["args"] == "query='up'"

    def test_tool_error_records_error_counter(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()
        labels = {"tool_name": "failing_tool", "status": "error", "source": "agent", "client": "agent"}

        before = _sample("sre_assistant_tool_calls_total", labels)

        with caplog.at_level(logging.INFO, logger=TOOL_CALL_LOGGER):
            handler.on_tool_start(
                serialized={"name": "failing_tool"},
                input_str="query",
                run_id=run_id,
            )
            handler.on_tool_error(error=ToolException("upstream said no"), run_id=run_id)

        after = _sample("sre_assistant_tool_calls_total", labels)
        assert after is not None
        assert after - (before or 0.0) == 1.0
        [record] = tool_call_records(caplog)
        parsed = fields(record)
        assert parsed["outcome"] == "error"
        assert parsed["error_class"] == "ToolException"
        assert parsed["args"] == "input='query'"

    def test_unexpected_tool_error_is_internal(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()
        labels = {"tool_name": "crashing_tool", "status": "internal", "source": "agent", "client": "agent"}
        before = _sample("sre_assistant_tool_calls_total", labels)

        with caplog.at_level(logging.INFO, logger=TOOL_CALL_LOGGER):
            handler.on_tool_start(serialized={"name": "crashing_tool"}, input_str="", run_id=run_id)
            handler.on_tool_error(error=RuntimeError("boom"), run_id=run_id)

        assert (_sample("sre_assistant_tool_calls_total", labels) or 0.0) - (before or 0.0) == 1.0
        [record] = tool_call_records(caplog)
        assert record.levelno == logging.ERROR
        assert fields(record)["outcome"] == "internal"

    def test_handled_tool_error_counts_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A tool with handle_tool_error=True reports its ToolException through on_tool_end as a ToolMessage with
        status="error"; that call is a failure, not a success (finding N1)."""
        handler = MetricsCallbackHandler()
        error_labels = {"tool_name": "handled_failing_tool", "status": "error", "source": "agent", "client": "agent"}
        success_labels = {**error_labels, "status": "success"}
        errors_before = _sample("sre_assistant_tool_calls_total", error_labels) or 0.0
        successes_before = _sample("sre_assistant_tool_calls_total", success_labels) or 0.0
        message = "upstream said no " + "x" * 300

        def _fail(query: str) -> str:
            raise ToolException(message)

        tool = StructuredTool.from_function(
            func=_fail, name="handled_failing_tool", description="Always fails.", handle_tool_error=True
        )
        with caplog.at_level(logging.INFO, logger=TOOL_CALL_LOGGER):
            output = tool.invoke(
                {"type": "tool_call", "id": "call-1", "name": "handled_failing_tool", "args": {"query": "up"}},
                config={"callbacks": [handler]},
            )

        assert isinstance(output, ToolMessage) and output.status == "error"
        assert (_sample("sre_assistant_tool_calls_total", error_labels) or 0.0) - errors_before == 1.0
        assert (_sample("sre_assistant_tool_calls_total", success_labels) or 0.0) - successes_before == 0.0
        [record] = tool_call_records(caplog)
        assert record.levelno == logging.INFO
        parsed = fields(record)
        assert parsed["outcome"] == "error"
        assert parsed["error_class"] == "ToolException"
        assert parsed["error"] == message[:MAX_ERROR_CHARS]
        assert parsed["result_chars"] == "0"
        assert parsed["args"] == "query='up'"

    def test_tool_message_success_counts_as_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """Pin: a ToolMessage with the default status="success" still counts as a success."""
        handler = MetricsCallbackHandler()
        labels = {"tool_name": "handled_ok_tool", "status": "success", "source": "agent", "client": "agent"}
        before = _sample("sre_assistant_tool_calls_total", labels) or 0.0

        def _ok(query: str) -> str:
            return f"ok {query}"

        tool = StructuredTool.from_function(
            func=_ok, name="handled_ok_tool", description="Always works.", handle_tool_error=True
        )
        with caplog.at_level(logging.INFO, logger=TOOL_CALL_LOGGER):
            output = tool.invoke(
                {"type": "tool_call", "id": "call-2", "name": "handled_ok_tool", "args": {"query": "up"}},
                config={"callbacks": [handler]},
            )

        assert isinstance(output, ToolMessage) and output.status == "success"
        assert (_sample("sre_assistant_tool_calls_total", labels) or 0.0) - before == 1.0
        [record] = tool_call_records(caplog)
        parsed = fields(record)
        assert parsed["outcome"] == "success"
        assert parsed["result_chars"] == "5"
        assert "error" not in parsed

    def test_tool_end_without_start_is_safe(self) -> None:
        """on_tool_end with unknown run_id should not raise."""
        handler = MetricsCallbackHandler()
        handler.on_tool_end(output="result", run_id=uuid4())

    def test_tool_error_without_start_is_safe(self) -> None:
        """on_tool_error with unknown run_id should not raise."""
        handler = MetricsCallbackHandler()
        handler.on_tool_error(error=RuntimeError("boom"), run_id=uuid4())

    def test_llm_success_increments_counter(self) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()

        before = _sample("sre_assistant_llm_calls_total", {"status": "success"})

        # Minimal LLMResult with no token usage
        from langchain_core.outputs import LLMResult

        result = LLMResult(generations=[], llm_output=None)
        handler.on_llm_end(response=result, run_id=run_id)

        after = _sample("sre_assistant_llm_calls_total", {"status": "success"})
        assert after is not None
        assert after - (before or 0.0) == 1.0

    def test_llm_end_records_token_usage(self) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()

        before_prompt = _sample("sre_assistant_llm_token_usage_total", {"type": "prompt"})
        before_completion = _sample("sre_assistant_llm_token_usage_total", {"type": "completion"})

        from langchain_core.outputs import LLMResult

        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "model_name": "gpt-4o-mini",
            },
        )
        handler.on_llm_end(response=result, run_id=run_id)

        after_prompt = _sample("sre_assistant_llm_token_usage_total", {"type": "prompt"})
        after_completion = _sample("sre_assistant_llm_token_usage_total", {"type": "completion"})

        assert after_prompt is not None
        assert after_prompt - (before_prompt or 0.0) == 100.0
        assert after_completion is not None
        assert after_completion - (before_completion or 0.0) == 50.0

    def test_llm_end_records_cost(self) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()

        before = _sample("sre_assistant_llm_estimated_cost_dollars_total")

        from langchain_core.outputs import LLMResult

        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                "model_name": "gpt-4o-mini",
            },
        )
        handler.on_llm_end(response=result, run_id=run_id)

        after = _sample("sre_assistant_llm_estimated_cost_dollars_total")
        assert after is not None

        from src.observability.metrics import COST_PER_TOKEN

        pricing = COST_PER_TOKEN["gpt-4o-mini"]
        expected_cost = (1000 * pricing["prompt"]) + (500 * pricing["completion"])
        assert pytest.approx(after - (before or 0.0)) == expected_cost

    def test_llm_end_unknown_model_uses_default_pricing(self) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()

        before = _sample("sre_assistant_llm_estimated_cost_dollars_total")

        from langchain_core.outputs import LLMResult

        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "model_name": "some-future-model",
            },
        )
        handler.on_llm_end(response=result, run_id=run_id)

        after = _sample("sre_assistant_llm_estimated_cost_dollars_total")
        assert after is not None

        expected = (100 * DEFAULT_COST_PER_TOKEN["prompt"]) + (50 * DEFAULT_COST_PER_TOKEN["completion"])
        assert pytest.approx(after - (before or 0.0)) == expected

    def test_llm_error_increments_error_counter(self) -> None:
        handler = MetricsCallbackHandler()
        run_id = uuid4()

        before = _sample("sre_assistant_llm_calls_total", {"status": "error"})
        handler.on_llm_error(error=RuntimeError("api down"), run_id=run_id)

        after = _sample("sre_assistant_llm_calls_total", {"status": "error"})
        assert after is not None
        assert after - (before or 0.0) == 1.0

    def test_llm_end_missing_token_usage_is_safe(self) -> None:
        """on_llm_end with llm_output but no token_usage should not raise."""
        handler = MetricsCallbackHandler()

        from langchain_core.outputs import LLMResult

        result = LLMResult(generations=[], llm_output={"model_name": "gpt-4o"})
        handler.on_llm_end(response=result, run_id=uuid4())


# ---------------------------------------------------------------------------
# SLI dashboard
# ---------------------------------------------------------------------------

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboards" / "sre-assistant-sli.json"
CLIENT_ERRORS = "invalid_args|unknown_tool"


def _panels() -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = json.loads(DASHBOARD.read_text())["panels"]
    return panels


def _exprs(panel: dict[str, Any]) -> list[str]:
    return [str(target["expr"]) for target in panel.get("targets", [])]


class TestSliDashboard:
    """The SLI dashboard's queries count failures the way the SLIs define them."""

    def test_tool_success_excludes_client_errors(self) -> None:
        [overview] = [p for p in _panels() if p["title"] == "SLO Overview"]
        [target] = [t for t in overview["targets"] if str(t["legendFormat"]).startswith("Tool Success")]
        assert target["expr"] == (
            'sum(rate(sre_assistant_tool_calls_total{status="success"}[1h]))'
            f' / sum(rate(sre_assistant_tool_calls_total{{status!~"{CLIENT_ERRORS}"}}[1h]))'
        )

    def test_no_tool_panel_counts_only_status_error_as_failure(self) -> None:
        """timeout and internal are failures too; a panel reading status="error" alone would hide them."""
        exprs = [e for p in _panels() for e in _exprs(p)]
        assert not [e for e in exprs if 'sre_assistant_tool_calls_total{status="error"}' in e]

    def test_tool_calls_by_source_and_by_client(self) -> None:
        exprs = [e for p in _panels() for e in _exprs(p) if "sre_assistant_tool_calls_total" in e]
        assert any("sum by (source)" in e for e in exprs)
        assert any("sum by (client)" in e for e in exprs)

    def test_mcp_http_requests_panel_says_it_counts_every_request(self) -> None:
        [panel] = [p for p in _panels() if any("sre_assistant_mcp_http_requests_total" in e for e in _exprs(p))]
        assert all("sum by (access)" in e for e in _exprs(panel))
        text = f"{panel['title']} {panel.get('description', '')}"
        assert "every HTTP request to /mcp" in text
        assert "not only tool calls" in text

    def test_panel_ids_unique(self) -> None:
        ids = [p["id"] for p in _panels()]
        assert len(ids) == len(set(ids))
