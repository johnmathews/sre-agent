"""Metrics extraction for the Claude Agent SDK path.

Translates SDK message types into the same LLM counters used by the LangChain callback handler, keeping
dashboards unified. Tool calls are not counted here: the SDK tool server's handler (``src/agent/mcp_tools.py``)
records each call it runs through ``observe_tool_call``, with its real duration.
"""

import logging
from typing import Any

from claude_agent_sdk.types import AssistantMessage, ResultMessage, ToolUseBlock

from src.observability.metrics import (
    LLM_CALLS_TOTAL,
    LLM_ESTIMATED_COST,
    LLM_TOKEN_USAGE,
)

logger = logging.getLogger(__name__)

# Type alias for the union of SDK message types
type SdkMessage = Any


def record_sdk_metrics(messages: list[SdkMessage], result: ResultMessage | None) -> None:
    """Record the LLM call, token usage and cost of one ``invoke_sdk_agent()`` / ``stream_sdk_agent()`` call.

    The request counts as one LLM call: ``error`` when the SDK returned no ``ResultMessage`` or one with
    ``is_error``, otherwise ``success``. A request whose query raised never reaches this function; the caller counts
    it with ``record_sdk_llm_error()``.

    Tool calls are not recorded here: the SDK tool server's handler records each one, so counting the
    ``ToolUseBlock``s in ``messages`` too would count each call twice.
    """
    try:
        _record_sdk_metrics_inner(messages, result)
    except Exception:
        logger.warning("Failed to record SDK metrics", exc_info=True)


def record_sdk_llm_error() -> None:
    """Count one failed LLM call for an SDK request whose query raised. Never raises."""
    try:
        LLM_CALLS_TOTAL.labels(status="error").inc()
    except Exception:
        logger.warning("Failed to record SDK LLM error", exc_info=True)


def _record_sdk_metrics_inner(messages: list[SdkMessage], result: ResultMessage | None) -> None:
    # LLM call count — one per request (SDK abstraction)
    failed = result is None or result.is_error
    LLM_CALLS_TOTAL.labels(status="error" if failed else "success").inc()

    if result is None:
        return

    # Cost
    if result.total_cost_usd is not None:
        LLM_ESTIMATED_COST.inc(result.total_cost_usd)

    # Token usage
    usage = result.usage
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)

        if input_tokens:
            LLM_TOKEN_USAGE.labels(type="prompt").inc(input_tokens)
        if output_tokens:
            LLM_TOKEN_USAGE.labels(type="completion").inc(output_tokens)
        if cache_read:
            LLM_TOKEN_USAGE.labels(type="cache_read").inc(cache_read)
        if cache_creation:
            LLM_TOKEN_USAGE.labels(type="cache_creation").inc(cache_creation)


def extract_tool_names(messages: list[SdkMessage]) -> list[str]:
    """Extract tool names from SDK messages for post-response actions.

    Returns short names (without mcp__sre__ prefix).
    """
    tool_names: list[str] = []
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    name = block.name
                    if name.startswith("mcp__sre__"):
                        name = name[len("mcp__sre__") :]
                    tool_names.append(name)
    return tool_names
