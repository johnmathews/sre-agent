"""Streamlit chat UI for the SRE Assistant.

Talks to the FastAPI backend via httpx. Run with: make ui
"""

import json
import os
from typing import Any
from uuid import uuid4

import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="SRE Assistant", layout="wide")


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []

if "session_id" not in st.session_state:
    st.session_state.session_id = uuid4().hex[:8]

if "renaming" not in st.session_state:
    st.session_state.renaming = None  # session_id currently being renamed


# ---------------------------------------------------------------------------
# API helpers (cached where appropriate)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def _fetch_health() -> dict[str, Any] | None:
    """Fetch health status, cached for 30s to avoid hammering on reruns."""
    try:
        resp = httpx.get(f"{API_URL}/health", timeout=5.0)
        data: dict[str, Any] = resp.json()
        return data
    except Exception:
        return None


@st.cache_data(ttl=5)
def _fetch_conversations() -> list[dict[str, Any]]:
    """Fetch the conversation list, short TTL so it updates after each turn."""
    try:
        resp = httpx.get(f"{API_URL}/conversations", timeout=5.0)
        if resp.status_code != 200:
            return []
        items: list[dict[str, Any]] = resp.json()
        return items
    except Exception:
        return []


def _fetch_conversation_detail(session_id: str) -> dict[str, Any] | None:
    """Fetch a single conversation's full turns."""
    try:
        resp = httpx.get(f"{API_URL}/conversations/{session_id}", timeout=5.0)
        if resp.status_code != 200:
            return None
        data: dict[str, Any] = resp.json()
        return data
    except Exception:
        return None


def _delete_conversation_api(session_id: str) -> bool:
    try:
        resp = httpx.delete(f"{API_URL}/conversations/{session_id}", timeout=5.0)
        return resp.status_code == 204
    except Exception:
        return False


def _rename_conversation_api(session_id: str, title: str) -> bool:
    try:
        resp = httpx.patch(
            f"{API_URL}/conversations/{session_id}",
            json={"title": title},
            timeout=5.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _load_conversation(session_id: str) -> None:
    """Load a past conversation into session_state and rerun."""
    detail = _fetch_conversation_detail(session_id)
    if detail is None:
        st.error(f"Could not load conversation {session_id}")
        return
    st.session_state.session_id = session_id
    st.session_state.messages = [{"role": t["role"], "content": t["content"]} for t in detail.get("turns", [])]
    st.session_state.renaming = None
    st.rerun()


def _start_new_conversation() -> None:
    st.session_state.messages = []
    st.session_state.session_id = uuid4().hex[:8]
    st.session_state.renaming = None


# ---------------------------------------------------------------------------
# Delete confirmation dialog
# ---------------------------------------------------------------------------


@st.dialog("Delete conversation?")
def _confirm_delete_dialog(session_id: str, title: str) -> None:
    st.write(f"Are you sure you want to delete **{title or session_id}**?")
    st.caption("This cannot be undone.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Delete", type="primary", use_container_width=True):
            if _delete_conversation_api(session_id):
                _fetch_conversations.clear()
                if st.session_state.session_id == session_id:
                    _start_new_conversation()
                st.rerun()
            else:
                st.error("Delete failed")
    with col2:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

_SIDEBAR_CSS = """
<style>
/* Compact buttons inside the sidebar */
section[data-testid="stSidebar"] div[data-testid="stButton"] > button {
    padding: 0.3rem 0.6rem;
    min-height: 0;
    font-size: 0.85rem;
    line-height: 1.25;
}
/* Hide the overflow (⋯) menu button until its row is hovered.
   The selector matches the 2nd column only when it is also the last child,
   so 3-column action rows (Save/Delete/Cancel) stay visible. */
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"]
    > div[data-testid="stColumn"]:nth-child(2):nth-last-child(1)
    div[data-testid="stButton"] > button {
    opacity: 0;
    transition: opacity 0.15s ease-in-out;
    padding: 0.3rem 0.2rem;
}
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"]:hover
    > div[data-testid="stColumn"]:nth-child(2):nth-last-child(1)
    div[data-testid="stButton"] > button {
    opacity: 1;
}
</style>
"""


with st.sidebar:
    st.markdown(_SIDEBAR_CSS, unsafe_allow_html=True)
    st.title("SRE Assistant")

    if st.button("+ New conversation", use_container_width=True):
        _start_new_conversation()
        st.rerun()

    st.caption(f"Session: `{st.session_state.session_id}`")
    st.divider()

    # --- Infrastructure health (compact) ---
    health_data = _fetch_health()
    if health_data is None:
        st.error("Cannot reach API server. Is `make serve` running?")
    else:
        overall = health_data.get("status", "unknown")
        components_raw = health_data.get("components", [])
        components = [c for c in components_raw if isinstance(c, dict)] if isinstance(components_raw, list) else []
        healthy_count = sum(1 for c in components if c.get("status") == "healthy")
        total_count = len(components)

        if overall == "healthy":
            badge = ":green[●]"
        elif overall == "degraded":
            badge = ":orange[●]"
        else:
            badge = ":red[●]"

        st.markdown(f"**Health** {badge} {overall} ({healthy_count}/{total_count})")

        with st.expander("Details", expanded=(overall != "healthy")):
            model_name = health_data.get("model")
            if isinstance(model_name, str) and model_name:
                st.caption(f"LLM: `{model_name}`")
            for comp in components:
                name = comp.get("name", "unknown")
                status = comp.get("status", "unknown")
                detail = comp.get("detail")
                icon = ":white_check_mark:" if status == "healthy" else ":x:"
                label = f"{icon} {name}: {status}"
                if detail:
                    label += f" — {detail}"
                st.markdown(label)

    st.divider()

    # --- Past conversations list ---
    st.subheader("Past conversations")
    convos = _fetch_conversations()
    if not convos:
        st.caption("_No past conversations yet._")
    else:
        active_id = st.session_state.session_id
        for conv in convos:
            sid = conv["session_id"]
            title = conv.get("title") or f"({sid})"
            turns = conv.get("turn_count", 0)
            is_active = sid == active_id
            prefix = "▶ " if is_active else ""

            display_label = f"{prefix}{title}"
            col_main, col_menu = st.columns([10, 1], gap="small")
            with col_main:
                if st.button(
                    display_label,
                    key=f"load_{sid}",
                    use_container_width=True,
                    help=f"{turns} turn{'s' if turns != 1 else ''} — {conv.get('provider', '')}",
                ):
                    _load_conversation(sid)
            with col_menu:
                if st.button("⋯", key=f"menu_{sid}", help="Rename or delete"):
                    st.session_state.renaming = sid if st.session_state.renaming != sid else None
                    st.rerun()

            if st.session_state.renaming == sid:
                new_title = st.text_input(
                    "Rename to:",
                    value=title,
                    key=f"rename_input_{sid}",
                    label_visibility="collapsed",
                )
                action_col1, action_col2, action_col3 = st.columns(3)
                with action_col1:
                    if st.button("Save", key=f"save_{sid}", use_container_width=True) and (
                        new_title.strip() and _rename_conversation_api(sid, new_title)
                    ):
                        _fetch_conversations.clear()
                        st.session_state.renaming = None
                        st.rerun()
                with action_col2:
                    if st.button("Delete", key=f"delete_{sid}", use_container_width=True):
                        _confirm_delete_dialog(sid, title)
                with action_col3:
                    if st.button("Cancel", key=f"cancel_{sid}", use_container_width=True):
                        st.session_state.renaming = None
                        st.rerun()


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


# ---------------------------------------------------------------------------
# User input
# ---------------------------------------------------------------------------

if prompt := st.chat_input("Ask about your infrastructure..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status_area = st.empty()
        answer_area = st.empty()
        answer = ""
        active_tools: list[str] = []

        def _render_status(tools: list[str], current: str = "") -> None:
            """Render the tool progress display."""
            lines: list[str] = list(tools)
            if current:
                lines.append(f":hourglass_flowing_sand: {current}")
            if lines:
                status_area.markdown("  \n".join(lines))

        try:
            with httpx.stream(
                "POST",
                f"{API_URL}/ask/stream",
                json={"question": prompt, "session_id": st.session_state.session_id},
                timeout=120.0,
            ) as resp:
                _ = resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event: dict[str, str] = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "heartbeat":
                        continue

                    elif event_type == "status":
                        _render_status(active_tools, event.get("content", ""))

                    elif event_type == "tool_start":
                        content = event.get("content", "")
                        _render_status(active_tools, content)

                    elif event_type == "tool_end":
                        content = event.get("content", "")
                        active_tools.append(f":white_check_mark: {content}")
                        _render_status(active_tools)

                    elif event_type == "answer":
                        answer = event.get("content", "No response received.")
                        returned_sid = event.get("session_id")
                        if returned_sid:
                            st.session_state.session_id = returned_sid

                    elif event_type == "error":
                        answer = f"Error: {event.get('content', 'Unknown error')}"

        except httpx.ConnectError:
            answer = "Cannot reach the API server. Make sure `make serve` is running."
        except httpx.HTTPStatusError as exc:
            answer = f"API error (HTTP {exc.response.status_code}): {exc.response.text}"
        except Exception as exc:
            answer = f"Unexpected error: {exc}"

        status_area.empty()
        answer_area.markdown(answer)
        st.session_state.messages.append({"role": "assistant", "content": answer})

    # Conversation list is stale after a new turn
    _fetch_conversations.clear()
    st.rerun()
