"""Claude Agent SDK integration for the Anthropic provider path.

Uses ``claude-agent-sdk``'s ``query()`` function to run the SRE agent via
the Claude Code CLI subprocess, which handles OAuth token authentication
automatically.  The LangChain agent path is preserved for OpenAI.
"""

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    Message,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from src.agent.history import (
    format_history_as_prompt,
    load_sdk_history,
    save_sdk_conversation,
)
from src.agent.mcp_tools import build_mcp_server
from src.config import Settings, get_settings
from src.observability.sdk_metrics import extract_tool_names, record_sdk_metrics

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
_SYSTEM_PROMPT_TEMPLATE = _PROMPT_PATH.read_text()

# Complete list of Claude Code built-in tools to block.
# The SRE agent must only use our MCP tools — no file/shell/web access.
_BLOCKED_BUILTINS = [
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoRead",
    "TodoWrite",
    "NotebookRead",
    "NotebookEdit",
    "LS",
    "Task",
    "Agent",
    "computer_use",
    "ToolSearch",
]

# Tool name prefix added by the SDK for MCP server tools
_MCP_PREFIX = "mcp__sre__"

# Regex to match short tool names in the system prompt (word boundary)
_TOOL_NAME_PATTERN = re.compile(
    r"\b("
    r"prometheus_search_metrics|prometheus_instant_query|prometheus_range_query"
    r"|grafana_get_alerts|grafana_get_alert_rules|grafana_get_dashboard|grafana_search_dashboards"
    r"|proxmox_list_guests|proxmox_get_guest_config|proxmox_node_status|proxmox_list_tasks"
    r"|truenas_pool_status|truenas_list_shares|truenas_snapshots|truenas_system_status|truenas_apps"
    r"|hdd_power_status"
    r"|loki_query_logs|loki_metric_query|loki_list_label_values|loki_correlate_changes"
    r"|pbs_datastore_status|pbs_list_backups|pbs_list_tasks"
    r"|runbook_search"
    r"|memory_search_incidents|memory_record_incident|memory_get_previous_report|memory_check_baseline"
    r")\b"
)


def _prefix_tool_names(prompt: str) -> str:
    """Add mcp__sre__ prefix to tool names in the system prompt for SDK path.

    Only transforms tool names that appear as backtick-delimited references,
    not arbitrary word occurrences (e.g. in description text).
    """
    return _TOOL_NAME_PATTERN.sub(lambda m: _MCP_PREFIX + m.group(0), prompt)


def _get_memory_context() -> str:
    """Load dynamic context from memory store for the system prompt."""
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
        logger.debug("Failed to load memory context for SDK system prompt", exc_info=True)
        return ""


def _build_system_prompt() -> str:
    """Build a fresh system prompt with current timestamps and SDK tool name prefixes."""
    now = datetime.now(UTC)
    prompt = (
        _SYSTEM_PROMPT_TEMPLATE.replace("{current_time}", now.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("{current_date}", now.strftime("%Y-%m-%d"))
        .replace("{retention_cutoff}", (now - timedelta(days=90)).strftime("%Y-%m-%d"))
    )
    # Add MCP tool name prefixes for the SDK path
    prompt = _prefix_tool_names(prompt)
    # Inject dynamic memory context
    prompt += _get_memory_context()
    return prompt


def build_sdk_options(
    settings: Settings | None = None,
    model_override: str | None = None,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for the SDK agent.

    The system prompt is built fresh each call with current timestamps.
    """
    if settings is None:
        settings = get_settings()

    model = model_override or settings.anthropic_model
    system_prompt = _build_system_prompt()
    mcp_server = build_mcp_server(settings)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        mcp_servers={"sre": mcp_server},
        allowed_tools=[f"{_MCP_PREFIX}*"],
        disallowed_tools=_BLOCKED_BUILTINS,
        permission_mode="bypassPermissions",
        max_turns=10,
        # Strip ANTHROPIC_API_KEY from the CLI subprocess environment.
        # The app's Settings validator requires this env var, but the CLI
        # must NOT see it: the CLI treats ANTHROPIC_API_KEY as an X-Api-Key
        # header (auth precedence item 3), which fails when the value is an
        # OAuth token (sk-ant-oat*).  The CLI should fall through to OAuth
        # credentials in .credentials.json (auth precedence item 5).
        env={"ANTHROPIC_API_KEY": ""},
    )
    return options


def _post_response_actions(tool_names: list[str], question: str, response_text: str) -> str:
    """Run post-response actions: save query pattern, detect incident suggestion.

    Returns any text to append to the response, or empty string. Never raises.
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

        try:
            conn = get_initialized_connection()
            try:
                save_query_pattern(conn, question=question, tool_names=",".join(tool_names))
                cleanup_old_query_patterns(conn, keep=100)
            finally:
                conn.close()
        except Exception:
            logger.debug("Failed to save query pattern", exc_info=True)

        return detect_incident_suggestion(tool_names, response_text)
    except Exception:
        logger.debug("Post-response actions failed", exc_info=True)
        return ""


async def invoke_sdk_agent(
    options: ClaudeAgentOptions,
    message: str,
    session_id: str = "default",
) -> str:
    """Send a message to the SDK agent and return the text response.

    Each call is stateless from the SDK's perspective. Conversation
    continuity is achieved by injecting prior turns into the prompt.
    """
    from src.agent.oauth_refresh import ensure_valid_token

    ensure_valid_token()
    settings = get_settings()

    # Rebuild system prompt with fresh timestamps each call
    options = ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        model=options.model,
        mcp_servers=options.mcp_servers,
        allowed_tools=options.allowed_tools,
        disallowed_tools=options.disallowed_tools,
        permission_mode=options.permission_mode,
        max_turns=options.max_turns,
    )

    # Load conversation history and build prompt with context
    if settings.conversation_history_dir:
        history = load_sdk_history(settings.conversation_history_dir, session_id)
        full_prompt = format_history_as_prompt(history, message)
    else:
        full_prompt = message

    # Call the SDK
    all_messages: list[Message] = []
    result_msg: ResultMessage | None = None
    response_text = "No response generated."

    async for msg in query(prompt=full_prompt, options=options):
        all_messages.append(msg)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    response_text = block.text
        elif isinstance(msg, ResultMessage):
            result_msg = msg
            if msg.is_error:
                logger.warning("SDK query returned error: %s", msg.result)

    # Record observability metrics
    record_sdk_metrics(all_messages, result_msg)

    # Extract tool names for post-response actions
    tool_names = extract_tool_names(all_messages)

    # Post-response actions (memory patterns, incident suggestion)
    suggestion = _post_response_actions(tool_names, message, response_text)
    if suggestion:
        response_text += suggestion

    # Save conversation history
    if settings.conversation_history_dir:
        model_name = options.model or settings.anthropic_model
        save_sdk_conversation(
            settings.conversation_history_dir,
            session_id,
            message,
            response_text,
            model_name,
            all_messages,
        )

    return response_text


# ---------------------------------------------------------------------------
# Streaming invocation (SSE-friendly)
# ---------------------------------------------------------------------------

# Human-readable labels for tools (keep in sync with agent.py _TOOL_LABELS)
_TOOL_LABELS: dict[str, str] = {
    "prometheus_search_metrics": "Searching Prometheus metrics",
    "prometheus_instant_query": "Querying Prometheus",
    "prometheus_range_query": "Querying Prometheus (range)",
    "grafana_get_alerts": "Checking Grafana alerts",
    "grafana_get_alert_rules": "Fetching Grafana alert rules",
    "grafana_get_dashboard": "Loading Grafana dashboard",
    "grafana_search_dashboards": "Searching Grafana dashboards",
    "proxmox_list_guests": "Listing Proxmox VMs/CTs",
    "proxmox_get_guest_config": "Fetching guest config",
    "proxmox_node_status": "Checking Proxmox node status",
    "proxmox_list_tasks": "Listing Proxmox tasks",
    "truenas_pool_status": "Checking TrueNAS pools",
    "truenas_list_shares": "Listing NFS/SMB shares",
    "truenas_snapshots": "Listing TrueNAS snapshots",
    "truenas_system_status": "Checking TrueNAS system status",
    "truenas_apps": "Listing TrueNAS apps",
    "hdd_power_status": "Checking HDD power states",
    "loki_query_logs": "Querying Loki logs",
    "loki_metric_query": "Running Loki metric query",
    "loki_list_label_values": "Listing Loki label values",
    "loki_correlate_changes": "Correlating log changes",
    "pbs_datastore_status": "Checking PBS datastore",
    "pbs_list_backups": "Listing PBS backups",
    "pbs_list_tasks": "Listing PBS tasks",
    "runbook_search": "Searching runbooks",
}


def _tool_display_name(name: str) -> str:
    """Strip mcp__sre__ prefix for display purposes."""
    if name.startswith(_MCP_PREFIX):
        return name[len(_MCP_PREFIX) :]
    return name


async def stream_sdk_agent(
    options: ClaudeAgentOptions,
    message: str,
    session_id: str = "default",
) -> AsyncIterator[dict[str, str]]:
    """Stream SDK agent events as dicts suitable for SSE.

    Yields dicts with keys:
      - type: "status" | "tool_start" | "answer" | "error"
      - content: human-readable text
      - session_id (only on "answer"): the session ID
    """
    from src.agent.oauth_refresh import ensure_valid_token

    ensure_valid_token()
    settings = get_settings()

    # Rebuild system prompt with fresh timestamps
    options = ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        model=options.model,
        mcp_servers=options.mcp_servers,
        allowed_tools=options.allowed_tools,
        disallowed_tools=options.disallowed_tools,
        permission_mode=options.permission_mode,
        max_turns=options.max_turns,
    )

    # Load history
    if settings.conversation_history_dir:
        history = load_sdk_history(settings.conversation_history_dir, session_id)
        full_prompt = format_history_as_prompt(history, message)
    else:
        full_prompt = message

    yield {"type": "status", "content": "Thinking..."}

    all_messages: list[Message] = []
    result_msg: ResultMessage | None = None
    response_text = "No response generated."

    try:
        async for msg in query(prompt=full_prompt, options=options):
            all_messages.append(msg)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        short_name = _tool_display_name(block.name)
                        label = _TOOL_LABELS.get(short_name, f"Running {short_name}")
                        yield {"type": "tool_start", "content": label, "tool_name": short_name}
                    elif isinstance(block, TextBlock) and block.text:
                        response_text = block.text
            elif isinstance(msg, ResultMessage):
                result_msg = msg
    except Exception as exc:
        logger.exception("SDK streaming failed")
        yield {"type": "error", "content": f"Agent error: {exc}"}
        return

    # Record metrics
    record_sdk_metrics(all_messages, result_msg)

    # Post-response actions
    tool_names = extract_tool_names(all_messages)
    suggestion = _post_response_actions(tool_names, message, response_text)
    if suggestion:
        response_text += suggestion

    # Save conversation
    if settings.conversation_history_dir:
        model_name = options.model or settings.anthropic_model
        save_sdk_conversation(
            settings.conversation_history_dir,
            session_id,
            message,
            response_text,
            model_name,
            all_messages,
        )

    yield {"type": "answer", "content": response_text, "session_id": session_id}
