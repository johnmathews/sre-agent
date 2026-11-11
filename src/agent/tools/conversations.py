"""LangChain tools that read the deployed SRE agent's own conversation history.

Registered on ``/mcp`` only, and only when ``CONVERSATION_HISTORY_DIR`` is set (see
``src/agent/tool_registry.py``). Both tools are sync, because they read files; the registry
runs them in a worker thread so the event loop is never blocked.
"""

import json

from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from src.agent.history import get_conversation, list_conversations
from src.agent.tools import HOMELAB_CONTEXT
from src.config import get_settings

SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MAX_LIST_LIMIT = 100


class ListConversationsInput(BaseModel):
    limit: int = Field(
        20,
        ge=1,
        le=MAX_LIST_LIMIT,
        description=f"Maximum number of conversations to return, newest first (1-{MAX_LIST_LIMIT}, default 20).",
    )


class GetConversationInput(BaseModel):
    session_id: str = Field(
        ...,
        pattern=SESSION_ID_PATTERN,
        description=(
            "Session ID as shown by sre_agent_list_conversations: 1-64 letters, digits, '_' or '-'. "
            "Wildcards and paths are rejected."
        ),
    )


_LIST_DESC = HOMELAB_CONTEXT + (
    "List recent conversations the deployed SRE agent has had via the web UI or API. "
    "Returns session IDs, titles (derived from the first user message), timestamps, "
    "turn counts, and which LLM model/provider was used. "
    "Most-recently-updated conversations appear first. "
    "Use this to find a specific past conversation, then call sre_agent_get_conversation "
    "with its session_id to read the full dialogue."
)

_GET_DESC = HOMELAB_CONTEXT + (
    "Retrieve the full dialogue of a specific deployed SRE agent conversation by session ID. "
    "Returns all user and assistant turns with timestamps. "
    "Use sre_agent_list_conversations first to find the session_id you need."
)


def _history_dir() -> str:
    history_dir = get_settings().conversation_history_dir
    if not history_dir:
        raise ToolException("Conversation history is not configured: set CONVERSATION_HISTORY_DIR to enable it")
    return history_dir


@tool("sre_agent_list_conversations", args_schema=ListConversationsInput)
def sre_agent_list_conversations(limit: int = 20) -> str:
    """List recent SRE agent conversations, newest first."""
    items = list_conversations(_history_dir())[:limit]
    if not items:
        return "No conversations found."
    lines = [f"Found {len(items)} conversation(s):\n"]
    for item in items:
        lines.append(
            f"  [{item['session_id']}] {item['title'] or '(untitled)'} "
            f"— {item['turn_count']} turn(s), {item['provider']}/{item['model']}, "
            f"updated {item['updated_at']}"
        )
    return "\n".join(lines)


sre_agent_list_conversations.description = _LIST_DESC
sre_agent_list_conversations.handle_tool_error = True


@tool("sre_agent_get_conversation", args_schema=GetConversationInput)
def sre_agent_get_conversation(session_id: str) -> str:
    """Return one SRE agent conversation as JSON."""
    data = get_conversation(_history_dir(), session_id)
    if data is None:
        return f"Conversation '{session_id}' not found."
    return json.dumps(data, indent=2, default=str)


sre_agent_get_conversation.description = _GET_DESC
sre_agent_get_conversation.handle_tool_error = True
