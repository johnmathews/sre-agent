"""Metrics extraction for the Claude Agent SDK path.

Translates SDK message types into the same Prometheus counters used
by the LangChain callback handler, keeping dashboards unified.
"""

import logging
from typing import Any

from claude_agent_sdk.types import AssistantMessage, ResultMessage, ToolUseBlock

from src.observability.metrics import (
    LLM_CALLS_TOTAL,
    LLM_ESTIMATED_COST,
    LLM_TOKEN_USAGE,
    TOOL_CALL_DURATION,
    TOOL_CALLS_TOTAL,
)

logger = logging.getLogger(__name__)

# Type alias for the union of SDK message types
type SdkMessage = Any


def record_sdk_metrics(
    messages: list[SdkMessage],
    result: ResultMessage | None,
    tool_durations: list[tuple[str, float]] | None = None,
) -> None:
    """Record Prometheus metrics from SDK messages.

    Called once per ``invoke_sdk_agent()`` call. Extracts tool call counts,
    token usage, cost, and per-tool duration from the SDK's message stream.

    ``tool_durations`` is a list of ``(tool_name, seconds)`` pairs measured
    by timestamping the gap between SDK message yields. This is approximate
    (includes network overhead) but sufficient for the Grafana dashboard.
    """
    try:
        _record_sdk_metrics_inner(messages, result, tool_durations)
    except Exception:
        logger.debug("Failed to record SDK metrics", exc_info=True)


def _record_sdk_metrics_inner(
    messages: list[SdkMessage],
    result: ResultMessage | None,
    tool_durations: list[tuple[str, float]] | None = None,
) -> None:
    # Count tool calls from AssistantMessage content blocks
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    # Strip mcp__sre__ prefix for metric labels
                    tool_name = block.name
                    if tool_name.startswith("mcp__sre__"):
                        tool_name = tool_name[len("mcp__sre__") :]
                    TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="success").inc()

    # LLM call count — one per request (SDK abstraction)
    LLM_CALLS_TOTAL.labels(status="success").inc()

    # Per-tool duration (approximate, from message-yield timestamps)
    if tool_durations:
        for tool_name, duration in tool_durations:
            TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(duration)

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
