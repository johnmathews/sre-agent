"""Streamlit chat UI for the SRE Assistant.

Talks to the FastAPI backend via httpx. Run with: make ui
"""

import json
import os
from uuid import uuid4

import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="SRE Assistant", layout="centered")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []

if "session_id" not in st.session_state:
    st.session_state.session_id = uuid4().hex[:8]

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("SRE Assistant")

    if st.button("New conversation"):
        st.session_state.messages = []
        st.session_state.session_id = uuid4().hex[:8]
        st.rerun()

    st.divider()

    st.caption(f"Session: `{st.session_state.session_id}`")

    # Health check
    st.subheader("Infrastructure Health")
    try:
        health_resp = httpx.get(f"{API_URL}/health", timeout=5.0)
        health_data: dict[str, object] = health_resp.json()
        overall = health_data.get("status", "unknown")
        model_name = health_data.get("model")
        if isinstance(model_name, str) and model_name:
            st.caption(f"LLM: `{model_name}`")

        if overall == "healthy":
            st.success(f"Overall: {overall}")
        elif overall == "degraded":
            st.warning(f"Overall: {overall}")
        else:
            st.error(f"Overall: {overall}")

        components = health_data.get("components", [])
        if isinstance(components, list):
            for comp in components:
                if not isinstance(comp, dict):
                    continue
                name = comp.get("name", "unknown")
                status = comp.get("status", "unknown")
                detail = comp.get("detail")
                icon = ":white_check_mark:" if status == "healthy" else ":x:"
                label = f"{icon} {name}: {status}"
                if detail:
                    label += f" — {detail}"
                st.markdown(label)
    except httpx.ConnectError:
        st.error("Cannot reach API server. Is `make serve` running?")
    except Exception as exc:
        st.error(f"Health check failed: {exc}")

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
    # Display user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Call the streaming API
    with st.chat_message("assistant"):
        status_area = st.empty()
        answer_area = st.empty()
        answer = ""
        active_tools: list[str] = []

        def _render_status(tools: list[str], current: str = "") -> None:
            """Render the tool progress display."""
            lines: list[str] = []
            for tool_line in tools:
                lines.append(f"- {tool_line}")
            if current:
                lines.append(f"- :hourglass_flowing_sand: {current}")
            if lines:
                status_area.markdown("\n".join(lines))

        try:
            with httpx.stream(
                "POST",
                f"{API_URL}/ask/stream",
                json={"question": prompt, "session_id": st.session_state.session_id},
                timeout=120.0,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event: dict[str, str] = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "status":
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

        # Clear the status area and show the final answer
        status_area.empty()
        answer_area.markdown(answer)
        st.session_state.messages.append({"role": "assistant", "content": answer})
