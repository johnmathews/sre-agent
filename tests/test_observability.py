"""Unit tests for observability metrics and callback handler."""

import logging
from uuid import uuid4

import pytest
from langchain_core.tools import ToolException
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
