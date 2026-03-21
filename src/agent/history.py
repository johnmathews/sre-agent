"""Persist conversation history to JSON files for debugging and analysis."""

import contextlib
import dataclasses
import glob
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import BaseMessage, messages_to_dict

logger = logging.getLogger(__name__)

# Maximum number of prior turns to inject into the SDK prompt for continuity
_MAX_HISTORY_TURNS = 20


def _find_existing_file(history_dir: str, session_id: str) -> str | None:
    """Find an existing conversation file for this session ID."""
    matches = glob.glob(os.path.join(history_dir, f"*_{session_id}.json"))
    return matches[0] if matches else None


def save_conversation(
    history_dir: str,
    session_id: str,
    messages: list[Any],
    model: str,
) -> None:
    """Save conversation messages to a JSON file. Never raises.

    Args:
        history_dir: Directory to write JSON files into.
        session_id: Conversation session ID (used as filename).
        messages: List of LangChain message objects from the agent result.
        model: The LLM model name used for this conversation.
    """
    try:
        _save_conversation_inner(history_dir, session_id, messages, model)
    except Exception:
        logger.exception("Failed to save conversation history for session '%s'", session_id)


def _save_conversation_inner(
    history_dir: str,
    session_id: str,
    messages: list[Any],
    model: str,
) -> None:
    """Inner implementation that may raise on I/O or serialization errors."""
    # Filter to only BaseMessage instances (skip any non-message items)
    valid_messages: list[BaseMessage] = [m for m in messages if isinstance(m, BaseMessage)]
    if not valid_messages:
        logger.debug("No messages to save for session '%s'", session_id)
        return

    serialized = messages_to_dict(valid_messages)
    now = datetime.now(UTC).isoformat()

    os.makedirs(history_dir, exist_ok=True)

    # Find existing file for this session, or create a new one with datetime prefix
    existing_path = _find_existing_file(history_dir, session_id)
    if existing_path:
        filepath = existing_path
        created_at = now
        try:
            with open(existing_path) as f:
                existing: dict[str, Any] = json.load(f)
            created_at = existing.get("created_at", now)
        except (json.JSONDecodeError, OSError):
            pass  # Corrupted file — overwrite with new created_at
    else:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        filepath = os.path.join(history_dir, f"{timestamp}_{session_id}.json")
        created_at = now

    payload: dict[str, Any] = {
        "session_id": session_id,
        "created_at": created_at,
        "updated_at": now,
        "turn_count": sum(1 for m in serialized if m.get("type") == "human"),
        "model": model,
        "messages": serialized,
    }

    # Atomic write: write to temp file then rename
    fd, tmp_path = tempfile.mkstemp(dir=history_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, filepath)
    except BaseException:
        # Clean up temp file on any error
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# SDK conversation history (context-stuffing for stateless query() calls)
# ---------------------------------------------------------------------------


def _serialize_sdk_message(msg: Any) -> dict[str, Any]:
    """Serialize a single SDK message to a JSON-safe dict."""
    if dataclasses.is_dataclass(msg) and not isinstance(msg, type):
        return dataclasses.asdict(msg)
    return {"type": type(msg).__name__, "data": str(msg)}


def save_sdk_conversation(
    history_dir: str,
    session_id: str,
    question: str,
    response_text: str,
    model: str,
    sdk_messages: list[Any] | None = None,
) -> None:
    """Append a turn to the SDK conversation history file. Never raises."""
    try:
        _save_sdk_conversation_inner(history_dir, session_id, question, response_text, model, sdk_messages)
    except Exception:
        logger.exception("Failed to save SDK conversation for session '%s'", session_id)


def _save_sdk_conversation_inner(
    history_dir: str,
    session_id: str,
    question: str,
    response_text: str,
    model: str,
    sdk_messages: list[Any] | None = None,
) -> None:
    """Inner implementation — may raise on I/O errors."""
    os.makedirs(history_dir, exist_ok=True)
    now = datetime.now(UTC).isoformat()

    existing_path = _find_existing_file(history_dir, session_id)

    turns: list[dict[str, str]] = []
    created_at = now

    if existing_path:
        filepath = existing_path
        try:
            with open(existing_path) as f:
                existing: dict[str, Any] = json.load(f)
            created_at = existing.get("created_at", now)
            turns = existing.get("turns", [])
        except (json.JSONDecodeError, OSError):
            pass
    else:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        filepath = os.path.join(history_dir, f"{timestamp}_{session_id}.json")

    turns.append({"role": "user", "content": question, "timestamp": now})
    turns.append({"role": "assistant", "content": response_text, "timestamp": now})

    payload: dict[str, Any] = {
        "session_id": session_id,
        "created_at": created_at,
        "updated_at": now,
        "turn_count": sum(1 for t in turns if t.get("role") == "user"),
        "model": model,
        "provider": "sdk",
        "turns": turns,
    }

    # Optionally store raw SDK messages for debugging
    if sdk_messages:
        payload["sdk_messages"] = [_serialize_sdk_message(m) for m in sdk_messages]

    fd, tmp_path = tempfile.mkstemp(dir=history_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, filepath)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def load_sdk_history(history_dir: str, session_id: str) -> list[dict[str, str]]:
    """Load prior conversation turns for a session. Returns empty list if none."""
    try:
        existing_path = _find_existing_file(history_dir, session_id)
        if not existing_path:
            return []
        with open(existing_path) as f:
            data: dict[str, Any] = json.load(f)
        turns: list[dict[str, str]] = data.get("turns", [])
        return turns
    except Exception:
        logger.debug("Failed to load SDK history for session '%s'", session_id, exc_info=True)
        return []


def format_history_as_prompt(turns: list[dict[str, str]], new_message: str) -> str:
    """Format prior conversation turns + new message into a single prompt string.

    Keeps the last ``_MAX_HISTORY_TURNS`` turns (user + assistant pairs) to
    stay within a reasonable context budget.
    """
    # Trim to last N turns
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
