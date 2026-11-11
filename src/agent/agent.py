"""LangChain agent assembly — wires tools, system prompt, and memory together.

Supports two backend paths:
- ``LLM_PROVIDER=openai`` — LangGraph agent with LangChain tools
- ``LLM_PROVIDER=anthropic`` — Claude Agent SDK with MCP tools (via CLI subprocess)
"""

import logging
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver

from src.agent.history import load_turns_as_langchain_messages, save_turn
from src.agent.llm import create_llm
from src.agent.tool_registry import enabled_specs, tool_label
from src.config import get_settings
from src.observability.callbacks import MetricsCallbackHandler

logger = logging.getLogger(__name__)

# LangGraph has no public type stubs — the compiled agent type is opaque to
# static analysers.  Using Any avoids cascading "partially unknown" warnings
# in every module that imports build_agent / invoke_agent.
type AgentGraph = Any

_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT_TEMPLATE = _PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# SREAgent — unified wrapper for both backend paths
# ---------------------------------------------------------------------------


@dataclass
class SREAgent:
    """Unified agent handle supporting both OpenAI (LangGraph) and Anthropic (SDK) paths."""

    provider: str  # "anthropic" | "openai"
    langgraph_agent: AgentGraph | None = None
    sdk_options: Any = field(default=None)  # ClaudeAgentOptions (lazy import)


def _get_memory_context() -> str:
    """Load dynamic context from memory store for the system prompt.

    Returns a string to append to the system prompt, or empty string if
    memory is not configured or on any error.
    """
    try:
        from src.memory.context import get_open_incidents_context, get_recent_patterns_context

        parts: list[str] = []
        incidents_ctx = get_open_incidents_context()
        if incidents_ctx:
            parts.append(incidents_ctx)
        patterns_ctx = get_recent_patterns_context()
        if patterns_ctx:
            parts.append(patterns_ctx)
        return "\n".join(parts)
    except Exception:
        logger.debug("Failed to load memory context for system prompt", exc_info=True)
        return ""


def _extract_ai_text(msg: AIMessage) -> str:
    """Extract text content from an AIMessage.

    Anthropic returns content as a list of content blocks
    (e.g., [{"type": "text", "text": "..."}]) while OpenAI returns a plain
    string.  This helper normalises both forms to a single string.
    """
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_tool_names(messages: list[Any]) -> list[str]:
    """Extract tool names from AIMessage tool_calls in a message list."""
    tool_names: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
            for tc in msg.tool_calls:
                if isinstance(tc, dict) and "name" in tc:
                    tool_names.append(tc["name"])
    return tool_names


def _post_response_actions(messages: list[Any], question: str, response_text: str) -> str:
    """Run post-response actions: save query pattern, detect incident suggestion.

    Returns any text to append to the response (e.g. incident suggestion),
    or empty string. Never raises.
    """
    try:
        from src.memory.context import detect_incident_suggestion
        from src.memory.store import (
            cleanup_old_query_patterns,
            get_initialized_connection,
            is_memory_configured,
            save_query_pattern,
        )

        if not is_memory_configured():
            return ""

        tool_names = _extract_tool_names(messages)

        # Save query pattern
        try:
            conn = get_initialized_connection()
            try:
                save_query_pattern(conn, question=question, tool_names=",".join(tool_names))
                cleanup_old_query_patterns(conn, keep=100)
            finally:
                conn.close()
        except Exception:
            logger.debug("Failed to save query pattern", exc_info=True)

        # Check for incident suggestion
        return detect_incident_suggestion(tool_names, response_text)
    except Exception:
        logger.debug("Post-response actions failed", exc_info=True)
        return ""


def _get_tools(exclude: Collection[str] = ()) -> list[BaseTool]:
    """The registry's ``agent`` surface as the current settings enable it, minus ``exclude``."""
    return [spec.tool for spec in enabled_specs(get_settings(), "agent", exclude)]


def build_agent(
    model_name: str | None = None,
    temperature: float = 0.0,
    exclude: Collection[str] = (),
) -> SREAgent:
    """Build and return the SRE assistant agent.

    For ``LLM_PROVIDER=anthropic``, builds a Claude Agent SDK agent that
    communicates via MCP tools through the CLI subprocess.
    For ``LLM_PROVIDER=openai``, builds the existing LangGraph agent.

    Args:
        model_name: LLM model to use. Defaults to the configured provider's model.
        temperature: LLM temperature (0.0 for deterministic tool-calling).
        exclude: Tool names to leave out on either path (the eval runner leaves out ``runbook_search``).

    Returns:
        An SREAgent wrapping either the SDK options or the LangGraph agent.
    """
    settings = get_settings()

    # --- Anthropic / SDK path ---
    if settings.llm_provider == "anthropic":
        from src.agent.sdk_agent import build_sdk_options

        sdk_options = build_sdk_options(settings, model_override=model_name, exclude=exclude)
        resolved_model = model_name or settings.anthropic_model
        logger.info("Building SDK agent with model=%s", resolved_model)
        return SREAgent(provider="anthropic", sdk_options=sdk_options)

    # --- OpenAI / LangGraph path ---
    llm = create_llm(settings, temperature=temperature, model_override=model_name)

    tools = _get_tools(exclude)
    resolved_model = model_name or settings.openai_model
    logger.info("Building agent with model=%s, %d tools: %s", resolved_model, len(tools), [t.name for t in tools])

    from src.agent.tools.clock import render_prompt_time_fields

    fields = render_prompt_time_fields(settings)
    system_prompt = SYSTEM_PROMPT_TEMPLATE
    for key, value in fields.items():
        system_prompt = system_prompt.replace("{" + key + "}", value)

    # Inject dynamic context from memory store (best-effort, never fails build)
    system_prompt += _get_memory_context()

    checkpointer = MemorySaver()

    langgraph_agent: AgentGraph = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
    )

    return SREAgent(provider="openai", langgraph_agent=langgraph_agent)


def _is_tool_call_pairing_error(exc: BaseException) -> bool:
    """Check if an exception is caused by orphaned tool_calls in conversation history.

    This happens when a previous request saved an AIMessage with tool_calls to the
    checkpoint but failed before the corresponding ToolMessages were added (e.g., due
    to a timeout). The OpenAI API rejects the malformed history on the next request.
    """
    msg = str(exc).lower()
    return "tool_calls" in msg and "tool messages" in msg


async def invoke_agent(
    agent: SREAgent,
    message: str,
    session_id: str = "default",
) -> str:
    """Send a message to the agent and return the text response.

    Dispatches to the SDK path for Anthropic or the LangGraph path for OpenAI.

    Args:
        agent: The SREAgent from build_agent().
        message: User's question.
        session_id: Conversation session ID for memory isolation.

    Returns:
        The agent's text response.
    """
    # --- Anthropic / SDK path ---
    if agent.provider == "anthropic":
        from src.agent.sdk_agent import invoke_sdk_agent

        return await invoke_sdk_agent(agent.sdk_options, message, session_id)

    # --- OpenAI / LangGraph path ---
    assert agent.langgraph_agent is not None
    return await _invoke_langgraph_agent(agent.langgraph_agent, message, session_id)


async def _invoke_langgraph_agent(
    agent: AgentGraph,
    message: str,
    session_id: str = "default",
) -> str:
    """LangGraph invoke implementation (OpenAI path)."""
    settings = get_settings()
    effective_session_id = session_id

    metrics_cb = MetricsCallbackHandler()
    config: RunnableConfig = {
        "configurable": {"thread_id": session_id},
        "callbacks": [metrics_cb],
    }

    # Cold-start resume: if checkpointer is empty for this thread_id,
    # inject prior turns from the history file (if any).
    prior_messages: list[Any] = []
    if settings.conversation_history_dir:
        try:
            snapshot = await agent.aget_state(config)  # pyright: ignore[reportUnknownMemberType]
            checkpoint_empty = not snapshot.values.get("messages")  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        except Exception:
            checkpoint_empty = True
        if checkpoint_empty:
            prior_messages = list(load_turns_as_langchain_messages(settings.conversation_history_dir, session_id))

    input_messages = [*prior_messages, HumanMessage(content=message)]

    try:
        result: dict[str, Any] = await agent.ainvoke(
            {"messages": input_messages},
            config=config,
        )
    except Exception as exc:
        if _is_tool_call_pairing_error(exc):
            fresh_id = f"{session_id}-{uuid4().hex[:6]}"
            effective_session_id = fresh_id
            logger.warning(
                "Session '%s' has corrupted tool-call history; retrying with fresh session '%s'",
                session_id,
                fresh_id,
            )
            fresh_cb = MetricsCallbackHandler()
            fresh_config: RunnableConfig = {
                "configurable": {"thread_id": fresh_id},
                "callbacks": [fresh_cb],
            }
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=message)]},
                config=fresh_config,
            )
        else:
            raise

    messages: list[Any] = result.get("messages", [])

    response_text = "No response generated."
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = _extract_ai_text(msg)
            if text:
                response_text = text
                break

    suggestion = _post_response_actions(messages, message, response_text)
    if suggestion:
        response_text += suggestion

    if settings.conversation_history_dir:
        from src.agent.tools.clock import effective_timezone

        active_model = settings.openai_model
        tz = effective_timezone(settings)
        save_turn(
            settings.conversation_history_dir,
            effective_session_id,
            "user",
            message,
            active_model,
            "openai",
            user_timezone=tz,
        )
        save_turn(
            settings.conversation_history_dir,
            effective_session_id,
            "assistant",
            response_text,
            active_model,
            "openai",
            user_timezone=tz,
        )

    return response_text


# ---------------------------------------------------------------------------
# Streaming invocation (SSE-friendly)
# ---------------------------------------------------------------------------


def _summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """Create a brief human-readable summary of the tool input."""
    if not isinstance(tool_input, dict):
        return ""
    # For prometheus queries, show the PromQL expression
    if "query" in tool_input:
        query = tool_input["query"]
        if isinstance(query, str) and query:
            if len(query) <= 120:
                return f"`{query}`"
            return f"`{query[:117]}...`"
    # For search tools, show the search term
    if "search_term" in tool_input:
        val = tool_input["search_term"]
        if isinstance(val, str) and val:
            return f"`{val}`"
    if "pattern" in tool_input:
        val = tool_input["pattern"]
        if isinstance(val, str) and val:
            return f"`{val}`"
    # For dashboard lookups
    if "uid" in tool_input:
        val = tool_input["uid"]
        if val:
            return f"uid={val}"
    # For guest config
    if "vmid" in tool_input:
        val = tool_input["vmid"]
        if val:
            return f"vmid={val}"
    return ""


async def stream_agent(
    agent: SREAgent,
    message: str,
    session_id: str = "default",
) -> AsyncIterator[dict[str, str]]:
    """Stream agent events as dicts suitable for SSE.

    Dispatches to the SDK streaming path for Anthropic or LangGraph for OpenAI.

    Yields dicts with keys:
      - type: "status" | "tool_start" | "tool_end" | "answer" | "error"
      - content: human-readable text
      - tool_name (optional): raw tool name for tool events
      - session_id (only on "answer"): the effective session ID
      - reason (only on "error"): machine-readable failure code
      - detail (only on "error"): raw underlying cause, for a details view
    """
    # --- Anthropic / SDK path ---
    if agent.provider == "anthropic":
        from src.agent.sdk_agent import stream_sdk_agent

        async for event in stream_sdk_agent(agent.sdk_options, message, session_id):
            yield event
        return

    # --- OpenAI / LangGraph path ---
    assert agent.langgraph_agent is not None
    async for event in _stream_langgraph_agent(agent.langgraph_agent, message, session_id):
        yield event


async def _stream_langgraph_agent(
    agent: AgentGraph,
    message: str,
    session_id: str = "default",
) -> AsyncIterator[dict[str, str]]:
    """LangGraph streaming implementation (OpenAI path)."""
    settings = get_settings()
    effective_session_id = session_id

    metrics_cb = MetricsCallbackHandler()
    config: RunnableConfig = {
        "configurable": {"thread_id": session_id},
        "callbacks": [metrics_cb],
    }

    yield {"type": "status", "content": "Thinking..."}

    # Cold-start resume: inject prior turns if checkpoint is empty
    prior_messages: list[Any] = []
    if settings.conversation_history_dir:
        try:
            snapshot = await agent.aget_state(config)  # pyright: ignore[reportUnknownMemberType]
            checkpoint_empty = not snapshot.values.get("messages")  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        except Exception:
            checkpoint_empty = True
        if checkpoint_empty:
            prior_messages = list(load_turns_as_langchain_messages(settings.conversation_history_dir, session_id))

    input_messages = [*prior_messages, HumanMessage(content=message)]

    all_messages: list[Any] = []
    response_text = "No response generated."

    try:
        async for event in agent.astream_events(  # pyright: ignore[reportUnknownMemberType]
            {"messages": input_messages},
            config=config,
            version="v2",
            include_types=["tool"],
        ):
            event_type: str = event.get("event", "")
            event_name: str = event.get("name", "")
            data: dict[str, Any] = event.get("data", {})

            if event_type == "on_tool_start":
                label = tool_label(event_name)
                summary = _summarize_tool_input(event_name, data.get("input"))
                content = f"{label}: {summary}" if summary else label
                yield {
                    "type": "tool_start",
                    "content": content,
                    "tool_name": event_name,
                }

            elif event_type == "on_tool_end":
                label = tool_label(event_name)
                yield {
                    "type": "tool_end",
                    "content": f"{label} — done",
                    "tool_name": event_name,
                }

    except Exception as exc:
        if _is_tool_call_pairing_error(exc):
            fresh_id = f"{session_id}-{uuid4().hex[:6]}"
            effective_session_id = fresh_id
            logger.warning(
                "Session '%s' has corrupted tool-call history; retrying with fresh session '%s'",
                session_id,
                fresh_id,
            )
            yield {"type": "status", "content": "Retrying with fresh session..."}
            fresh_cb = MetricsCallbackHandler()
            fresh_config: RunnableConfig = {
                "configurable": {"thread_id": fresh_id},
                "callbacks": [fresh_cb],
            }
            result: dict[str, Any] = await agent.ainvoke(
                {"messages": [HumanMessage(content=message)]},
                config=fresh_config,
            )
            all_messages = result.get("messages", [])
            for msg in reversed(all_messages):
                if isinstance(msg, AIMessage):
                    text = _extract_ai_text(msg)
                    if text:
                        response_text = text
                        break
        else:
            yield {"type": "error", "content": f"Agent error: {exc}"}
            return
    else:
        try:
            snapshot = await agent.aget_state(config)  # pyright: ignore[reportUnknownMemberType]
            all_messages = snapshot.values.get("messages", [])  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        except Exception:
            logger.debug("Failed to get state after streaming", exc_info=True)

        for msg in reversed(all_messages):
            if isinstance(msg, AIMessage):
                text = _extract_ai_text(msg)
                if text:
                    response_text = text
                    break

    suggestion = _post_response_actions(all_messages, message, response_text)
    response_text_final = response_text + suggestion if suggestion else response_text

    if settings.conversation_history_dir and all_messages:
        from src.agent.tools.clock import effective_timezone

        active_model = settings.openai_model
        tz = effective_timezone(settings)
        save_turn(
            settings.conversation_history_dir,
            effective_session_id,
            "user",
            message,
            active_model,
            "openai",
            user_timezone=tz,
        )
        save_turn(
            settings.conversation_history_dir,
            effective_session_id,
            "assistant",
            response_text_final,
            active_model,
            "openai",
            user_timezone=tz,
        )

    yield {
        "type": "answer",
        "content": response_text_final,
        "session_id": effective_session_id,
    }
