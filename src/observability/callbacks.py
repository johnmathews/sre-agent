"""LangChain callback handler that records Prometheus metrics.

Create a fresh ``MetricsCallbackHandler`` per request and pass it via
``config["callbacks"]``.  The handler writes to module-level metric
singletons defined in :mod:`src.observability.metrics`.

All callback methods are wrapped in try/except — metrics collection must
never crash a request.

Each finished tool call goes through ``observe_tool_call`` with ``source="agent", client="agent"``, so
the LangGraph agent writes the same ``tool_call`` log line and tool metrics as the SDK agent and ``/mcp``.
A tool error that ``handle_tool_error=True`` handled reaches ``on_tool_end`` as a ``ToolMessage`` with
``status="error"`` and is recorded as outcome ``error``.
"""

import logging
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from src.observability.metrics import (
    COST_PER_TOKEN,
    DEFAULT_COST_PER_TOKEN,
    LLM_CALLS_TOTAL,
    LLM_ESTIMATED_COST,
    LLM_TOKEN_USAGE,
)
from src.observability.tool_calls import classify_tool_error, observe_tool_call

logger = logging.getLogger(__name__)

# LangChain's handle_tool_error catches only ToolException, so a ToolMessage with status="error" came from one.
HANDLED_TOOL_ERROR_CLASS = "ToolException"


class MetricsCallbackHandler(BaseCallbackHandler):
    """Captures tool-call and LLM metrics inside LangGraph's execution loop.

    Per-request instance — ``_start_times`` is request-scoped, but all
    counter/histogram writes go to the module-level Prometheus singletons.
    """

    def __init__(self) -> None:
        super().__init__()
        # run_id → (start_time, tool_name, arguments)
        self._start_times: dict[UUID, tuple[float, str, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            tool_name: str = serialized.get("name", "unknown")
            arguments = inputs if isinstance(inputs, dict) else {"input": input_str}
            self._start_times[run_id] = (time.monotonic(), tool_name, arguments)
        except Exception:
            logger.debug("metrics: on_tool_start failed", exc_info=True)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            entry = self._start_times.pop(run_id, None)
            if entry is not None:
                start_time, tool_name, arguments = entry
                content = getattr(output, "content", output)
                text = content if isinstance(content, str) else str(content)
                duration_s = time.monotonic() - start_time
                if getattr(output, "status", None) == "error":
                    # handle_tool_error=True turned the tool's ToolException into a ToolMessage with
                    # status="error": a failed call, whose message is the content (observe_tool_call caps it).
                    observe_tool_call(
                        tool=tool_name,
                        source="agent",
                        client="agent",
                        outcome="error",
                        error_class=HANDLED_TOOL_ERROR_CLASS,
                        error=text,
                        duration_s=duration_s,
                        result_chars=0,
                        arguments=arguments,
                    )
                    return
                observe_tool_call(
                    tool=tool_name,
                    source="agent",
                    client="agent",
                    outcome="success",
                    error_class=None,
                    error=None,
                    duration_s=duration_s,
                    result_chars=len(text),
                    arguments=arguments,
                )
        except Exception:
            logger.debug("metrics: on_tool_end failed", exc_info=True)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            entry = self._start_times.pop(run_id, None)
            if entry is not None:
                start_time, tool_name, arguments = entry
                outcome, decided_by = classify_tool_error(error)
                observe_tool_call(
                    tool=tool_name,
                    source="agent",
                    client="agent",
                    outcome=outcome,
                    error_class=type(decided_by).__name__,
                    error=decided_by,
                    duration_s=time.monotonic() - start_time,
                    result_chars=0,
                    arguments=arguments,
                )
        except Exception:
            logger.debug("metrics: on_tool_error failed", exc_info=True)

    # ------------------------------------------------------------------
    # LLM callbacks
    # ------------------------------------------------------------------

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            LLM_CALLS_TOTAL.labels(status="success").inc()

            llm_output = response.llm_output
            if not llm_output:
                return

            token_usage: dict[str, int] | None = llm_output.get("token_usage")
            if not token_usage:
                return

            prompt_tokens = token_usage.get("prompt_tokens", 0)
            completion_tokens = token_usage.get("completion_tokens", 0)

            LLM_TOKEN_USAGE.labels(type="prompt").inc(prompt_tokens)
            LLM_TOKEN_USAGE.labels(type="completion").inc(completion_tokens)

            # Cost estimation
            model_name: str = llm_output.get("model_name", "")
            pricing = DEFAULT_COST_PER_TOKEN
            for prefix, costs in COST_PER_TOKEN.items():
                if model_name.startswith(prefix):
                    pricing = costs
                    break

            cost = (prompt_tokens * pricing["prompt"]) + (completion_tokens * pricing["completion"])
            LLM_ESTIMATED_COST.inc(cost)
        except Exception:
            logger.debug("metrics: on_llm_end failed", exc_info=True)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            LLM_CALLS_TOTAL.labels(status="error").inc()
        except Exception:
            logger.debug("metrics: on_llm_error failed", exc_info=True)
