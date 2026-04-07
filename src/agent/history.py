"""Persist conversation history to JSON files and load for resume.

Unified turn-based format used by both LangGraph (OpenAI) and SDK (Anthropic)
agent paths. One JSON file per session, keyed by session_id in the filename.

Schema:
    {
      "session_id": "abc12345",
      "title": "Why did prometheus alert fire...",
      "created_at": "ISO8601",
      "updated_at": "ISO8601",
      "turn_count": 3,
      "model": "gpt-4o-mini",
      "provider": "openai" | "anthropic",
      "turns": [
        {"role": "user" | "assistant", "content": "...", "timestamp": "ISO8601"}
      ]
    }

Tool calls are NOT preserved in turns. The sidebar renders role/content only.
"""

import contextlib
import glob
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

# Maximum prior turns injected on resume (user+assistant pairs)
_MAX_HISTORY_TURNS = 20

# Title derived from first user message, truncated to this length
_MAX_TITLE_CHARS = 60

# File sentinel written after one-shot migration
_MIGRATION_MARKER = ".unified-format-v1"


class Turn(TypedDict):
    """Single conversation turn."""

    role: str
    content: str
    timestamp: str


class ConversationMetadata(TypedDict):
    """Metadata summary of a conversation (for list views)."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int
    model: str
    provider: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_existing_file(history_dir: str, session_id: str) -> str | None:
    """Find an existing conversation file for this session ID."""
    matches = glob.glob(os.path.join(history_dir, f"*_{session_id}.json"))
    return matches[0] if matches else None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _derive_title(first_user_message: str, max_chars: int = _MAX_TITLE_CHARS) -> str:
    """Derive a display title from the first user message.

    Collapses whitespace, strips edges, truncates to ``max_chars`` with a
    trailing ellipsis if longer.
    """
    collapsed = re.sub(r"\s+", " ", first_user_message).strip()
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1].rstrip() + "\u2026"


def _atomic_write_json(filepath: str, payload: dict[str, Any]) -> None:
    """Atomically write JSON to disk (write-to-temp, rename)."""
    history_dir = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=history_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, filepath)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _read_conversation_file(filepath: str) -> dict[str, Any] | None:
    """Read and parse a conversation JSON file. Returns None on error."""
    try:
        with open(filepath) as f:
            data: dict[str, Any] = json.load(f)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _validate_session_id_path_safe(session_id: str) -> bool:
    """Guard against path traversal and weird characters."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id))


# ---------------------------------------------------------------------------
# Save: append a single turn
# ---------------------------------------------------------------------------


def save_turn(
    history_dir: str,
    session_id: str,
    role: str,
    content: str,
    model: str,
    provider: str,
    timestamp: str | None = None,
) -> None:
    """Append a single turn to the conversation file. Never raises.

    Creates the file on first call. On subsequent calls, loads existing file,
    appends the turn, bumps updated_at and turn_count, re-writes atomically.
    Sets ``title`` only on the first user turn (or when no title exists yet).

    Args:
        history_dir: Directory containing conversation JSON files.
        session_id: Conversation session ID (8-char hex by convention).
        role: "user" or "assistant".
        content: The text content of the turn.
        model: LLM model name.
        provider: "openai" or "anthropic".
        timestamp: ISO8601 timestamp; defaults to now.
    """
    try:
        _save_turn_inner(history_dir, session_id, role, content, model, provider, timestamp)
    except Exception:
        logger.exception("Failed to save turn for session '%s'", session_id)


def _save_turn_inner(
    history_dir: str,
    session_id: str,
    role: str,
    content: str,
    model: str,
    provider: str,
    timestamp: str | None,
) -> None:
    if not _validate_session_id_path_safe(session_id):
        logger.warning("Rejecting unsafe session_id: %r", session_id)
        return

    os.makedirs(history_dir, exist_ok=True)
    ts = timestamp or _now_iso()
    new_turn: Turn = {"role": role, "content": content, "timestamp": ts}

    existing_path = _find_existing_file(history_dir, session_id)

    if existing_path:
        existing = _read_conversation_file(existing_path)
        if existing is None:
            # Corrupted file -- overwrite with a fresh payload
            existing = {"turns": [], "created_at": ts}
        turns: list[Turn] = existing.get("turns", [])
        turns.append(new_turn)
        title = existing.get("title", "")
        if not title and role == "user":
            title = _derive_title(content)
        payload: dict[str, Any] = {
            "session_id": session_id,
            "title": title,
            "created_at": existing.get("created_at", ts),
            "updated_at": ts,
            "turn_count": sum(1 for t in turns if t.get("role") == "user"),
            "model": model,
            "provider": provider,
            "turns": turns,
        }
        filepath = existing_path
    else:
        title = _derive_title(content) if role == "user" else ""
        payload = {
            "session_id": session_id,
            "title": title,
            "created_at": ts,
            "updated_at": ts,
            "turn_count": 1 if role == "user" else 0,
            "model": model,
            "provider": provider,
            "turns": [new_turn],
        }
        datetime_prefix = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        filepath = os.path.join(history_dir, f"{datetime_prefix}_{session_id}.json")

    _atomic_write_json(filepath, payload)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_turns(history_dir: str, session_id: str) -> list[Turn]:
    """Load all turns for a session. Returns empty list if none or error."""
    try:
        path = _find_existing_file(history_dir, session_id)
        if not path:
            return []
        data = _read_conversation_file(path)
        if data is None:
            return []
        turns: list[Turn] = data.get("turns", [])
        return turns
    except Exception:
        logger.debug("Failed to load turns for session '%s'", session_id, exc_info=True)
        return []


def load_turns_as_langchain_messages(
    history_dir: str,
    session_id: str,
    max_turns: int = _MAX_HISTORY_TURNS,
) -> list[BaseMessage]:
    """Load prior turns and convert to LangChain HumanMessage/AIMessage list.

    Used by the LangGraph path to inject prior context on cold-start resume
    (i.e. after a process restart when MemorySaver checkpointer is empty).
    Keeps the last ``max_turns`` user/assistant pairs.
    """
    turns = load_turns(history_dir, session_id)
    if not turns:
        return []
    recent = turns[-max_turns * 2 :] if len(turns) > max_turns * 2 else turns
    messages: list[BaseMessage] = []
    for turn in recent:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    return messages


def format_history_as_prompt(turns: list[Turn], new_message: str) -> str:
    """Format prior turns + new user message into a single prompt string.

    Used by the SDK path (stateless subprocesses) to stuff prior context
    into each query. Keeps the last ``_MAX_HISTORY_TURNS`` pairs.
    """
    recent = turns[-_MAX_HISTORY_TURNS * 2 :] if len(turns) > _MAX_HISTORY_TURNS * 2 else turns
    if not recent:
        return new_message

    parts: list[str] = ["<conversation_history>"]
    for turn in recent:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        if role == "user":
            parts.append(f"Human: {content}")
        else:
            parts.append(f"Assistant: {content}")
    parts.append("</conversation_history>")
    parts.append("")
    parts.append(f"Human: {new_message}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Conversation management (list, get, delete, rename)
# ---------------------------------------------------------------------------


def list_conversations(history_dir: str) -> list[ConversationMetadata]:
    """Return metadata for all conversations, most-recently-updated first.

    Corrupted files and non-unified files are skipped. Never raises.
    """
    try:
        return _list_conversations_inner(history_dir)
    except Exception:
        logger.exception("Failed to list conversations in %s", history_dir)
        return []


def _list_conversations_inner(history_dir: str) -> list[ConversationMetadata]:
    if not os.path.isdir(history_dir):
        return []
    results: list[ConversationMetadata] = []
    for filepath in glob.glob(os.path.join(history_dir, "*.json")):
        data = _read_conversation_file(filepath)
        if data is None:
            logger.warning("Skipping corrupted conversation file: %s", filepath)
            continue
        if "turns" not in data:
            continue
        metadata: ConversationMetadata = {
            "session_id": data.get("session_id", ""),
            "title": data.get("title", ""),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
            "turn_count": data.get("turn_count", 0),
            "model": data.get("model", ""),
            "provider": data.get("provider", ""),
        }
        results.append(metadata)
    results.sort(key=lambda m: m["updated_at"], reverse=True)
    return results


class SearchResult(TypedDict):
    """A conversation that matched a search query, with matching snippets."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int
    model: str
    provider: str
    matches: list[dict[str, str]]  # [{"role": "user"|"assistant", "snippet": "..."}]


def search_conversations(history_dir: str, query: str, max_results: int = 20) -> list[SearchResult]:
    """Search all conversations for a query string (case-insensitive).

    Searches both titles and turn content. Returns conversations with
    matching snippets, ordered by most-recently-updated first.
    """
    try:
        return _search_conversations_inner(history_dir, query, max_results)
    except Exception:
        logger.exception("Failed to search conversations in %s", history_dir)
        return []


def _search_conversations_inner(history_dir: str, query: str, max_results: int) -> list[SearchResult]:
    if not os.path.isdir(history_dir) or not query.strip():
        return []

    query_lower = query.strip().lower()
    results: list[SearchResult] = []

    for filepath in glob.glob(os.path.join(history_dir, "*.json")):
        data = _read_conversation_file(filepath)
        if data is None or "turns" not in data:
            continue

        matches: list[dict[str, str]] = []

        # Search title
        title = data.get("title", "")
        if query_lower in title.lower():
            matches.append({"role": "title", "snippet": title})

        # Search turn content
        for turn in data.get("turns", []):
            content = turn.get("content", "")
            if query_lower in content.lower():
                # Extract a snippet around the match
                idx = content.lower().index(query_lower)
                start = max(0, idx - 60)
                end = min(len(content), idx + len(query_lower) + 60)
                snippet = content[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(content):
                    snippet = snippet + "..."
                matches.append({"role": turn.get("role", ""), "snippet": snippet})

        if matches:
            results.append(
                {
                    "session_id": data.get("session_id", ""),
                    "title": title,
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "turn_count": data.get("turn_count", 0),
                    "model": data.get("model", ""),
                    "provider": data.get("provider", ""),
                    "matches": matches[:5],  # Limit snippets per conversation
                }
            )

    results.sort(key=lambda r: r["updated_at"], reverse=True)
    return results[:max_results]


def get_conversation(history_dir: str, session_id: str) -> dict[str, Any] | None:
    """Return the full conversation payload, or None if not found."""
    if not _validate_session_id_path_safe(session_id):
        return None
    path = _find_existing_file(history_dir, session_id)
    if not path:
        return None
    return _read_conversation_file(path)


def delete_conversation(history_dir: str, session_id: str) -> bool:
    """Delete the conversation file. Returns whether a file was removed."""
    if not _validate_session_id_path_safe(session_id):
        return False
    path = _find_existing_file(history_dir, session_id)
    if not path:
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        logger.exception("Failed to delete conversation file: %s", path)
        return False


def rename_conversation(history_dir: str, session_id: str, new_title: str) -> bool:
    """Update the title field. Returns whether the session existed."""
    if not _validate_session_id_path_safe(session_id):
        return False
    path = _find_existing_file(history_dir, session_id)
    if not path:
        return False
    data = _read_conversation_file(path)
    if data is None:
        return False
    data["title"] = new_title.strip()
    data["updated_at"] = _now_iso()
    try:
        _atomic_write_json(path, data)
        return True
    except OSError:
        logger.exception("Failed to rename conversation: %s", path)
        return False


# ---------------------------------------------------------------------------
# Migration: old formats -> unified
# ---------------------------------------------------------------------------


def migrate_history_files(history_dir: str) -> dict[str, int]:
    """Convert legacy LangGraph and SDK history files to the unified format.

    Idempotent: writes a marker file after first run and subsequently no-ops.
    Never raises; per-file failures are logged and counted.

    Returns:
        dict with counts: {"migrated": N, "skipped": N, "failed": N}
    """
    counts = {"migrated": 0, "skipped": 0, "failed": 0}
    if not os.path.isdir(history_dir):
        return counts

    marker_path = os.path.join(history_dir, _MIGRATION_MARKER)
    if os.path.exists(marker_path):
        return counts

    for filepath in glob.glob(os.path.join(history_dir, "*.json")):
        try:
            result = _migrate_one_file(filepath)
            counts[result] += 1
        except Exception:
            logger.exception("Migration failed for file: %s", filepath)
            counts["failed"] += 1

    try:
        with open(marker_path, "w") as f:
            _ = f.write(_now_iso())
    except OSError:
        logger.exception("Failed to write migration marker")

    logger.info(
        "History migration complete: migrated=%d skipped=%d failed=%d",
        counts["migrated"],
        counts["skipped"],
        counts["failed"],
    )
    return counts


def _migrate_one_file(filepath: str) -> str:
    """Migrate a single file. Returns 'migrated', 'skipped', or 'failed'."""
    data = _read_conversation_file(filepath)
    if data is None:
        return "failed"

    if "turns" in data and "title" in data and "provider" in data and data.get("provider") != "sdk":
        return "skipped"

    if "turns" in data:
        first_user_turn = next((t for t in data["turns"] if t.get("role") == "user"), None)
        title = _derive_title(first_user_turn.get("content", "")) if first_user_turn else ""
        provider = data.get("provider", "anthropic")
        if provider == "sdk":
            provider = "anthropic"
        unified = {
            "session_id": data.get("session_id", ""),
            "title": title,
            "created_at": data.get("created_at", _now_iso()),
            "updated_at": data.get("updated_at", _now_iso()),
            "turn_count": data.get("turn_count", 0),
            "model": data.get("model", ""),
            "provider": provider,
            "turns": data["turns"],
        }
        _atomic_write_json(filepath, unified)
        return "migrated"

    if "messages" in data:
        turns = _langchain_messages_to_turns(data["messages"], data.get("updated_at", _now_iso()))
        first_user = next((t for t in turns if t["role"] == "user"), None)
        title = _derive_title(first_user["content"]) if first_user else ""
        unified = {
            "session_id": data.get("session_id", ""),
            "title": title,
            "created_at": data.get("created_at", _now_iso()),
            "updated_at": data.get("updated_at", _now_iso()),
            "turn_count": sum(1 for t in turns if t["role"] == "user"),
            "model": data.get("model", ""),
            "provider": "openai",
            "turns": turns,
        }
        _atomic_write_json(filepath, unified)
        return "migrated"

    return "skipped"


def _langchain_messages_to_turns(messages: list[dict[str, Any]], default_ts: str) -> list[Turn]:
    """Extract user/assistant turns from LangChain-serialized messages.

    Skips ToolMessage entries and AIMessage entries with no text content
    (pure tool-call messages).
    """
    turns: list[Turn] = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = ""
        inner = msg.get("data", {})
        if isinstance(inner, dict):
            raw_content = inner.get("content", "")
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                parts = [b.get("text", "") for b in raw_content if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(p for p in parts if p)
        if not content.strip():
            continue
        if msg_type == "human":
            turns.append({"role": "user", "content": content, "timestamp": default_ts})
        elif msg_type == "ai":
            turns.append({"role": "assistant", "content": content, "timestamp": default_ts})
    return turns
