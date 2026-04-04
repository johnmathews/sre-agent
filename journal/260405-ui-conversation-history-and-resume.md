# 2026-04-05 — UI conversation history sidebar + cross-provider resume

## Context

The Streamlit UI had no way to browse past conversations or resume them. History
persistence already existed, but in two incompatible formats (LangChain-serialized
`messages` for the OpenAI path, turn-based `turns` for the Anthropic/SDK path), and
the LangGraph path had no cold-start resume logic at all — once the process
restarted, its conversations were effectively unreadable.

Goal: unified storage, sidebar listing, click-to-resume, delete, rename. All shipped
as part of an engineering-team run (evaluate → plan → develop).

## What changed

### `src/agent/history.py` — rewritten around a unified turn-based format

Old `save_conversation` (LangGraph) and `save_sdk_conversation` (SDK) are gone,
replaced by a single `save_turn(history_dir, session_id, role, content, model,
provider)` that both paths call twice per invocation (once for the user message,
once for the final assistant response). Tool calls are intentionally NOT
persisted — both paths already dropped them or flattened them in practice, and the
sidebar only needs `{role, content}` to render. Checkpointer state (LangGraph) and
SDK in-memory traces remain the source of truth for tool-level detail.

New public surface:
- `save_turn`, `load_turns` (replaces `load_sdk_history`)
- `load_turns_as_langchain_messages` — converts saved turns back to
  `HumanMessage`/`AIMessage` for LangGraph injection
- `list_conversations` / `get_conversation` / `delete_conversation` /
  `rename_conversation` — feed the new API endpoints
- `migrate_history_files` — one-shot idempotent converter from old formats, gated
  by a `.unified-format-v1` marker file
- `_derive_title` — truncation-based title (first user message, 60 chars, ellipsis)
- `_validate_session_id_path_safe` — regex guard against path traversal

Unified JSON schema: `{session_id, title, created_at, updated_at, turn_count, model,
provider, turns[]}` where `provider ∈ {"openai", "anthropic"}`.

### `src/agent/agent.py` — LangGraph cold-start resume

Before each `agent.ainvoke()` / `astream_events()`, inspect the `MemorySaver`
checkpointer via `aget_state(config)`. If it has messages, trust in-process
continuity and skip injection. If it's empty (cold start after restart), load prior
turns from the file and prepend them as `HumanMessage`/`AIMessage` to the input
list. This resurrects cross-restart conversation continuity for the OpenAI path,
which previously didn't exist. Subtle but tested: the injection happens only on
the first turn of a resumed session within a process, so turns aren't duplicated
by repeated file-reads during a single process's lifetime.

### `src/agent/sdk_agent.py` — caller rewire

`load_sdk_history` → `load_turns`, `save_sdk_conversation` → two `save_turn` calls.
The existing `format_history_as_prompt` prompt-stuffing approach is unchanged.

### `src/api/main.py` — 4 new endpoints + migration-on-startup

- `GET /conversations` — metadata list, sorted by `updated_at` DESC
- `GET /conversations/{session_id}` — full conversation payload
- `DELETE /conversations/{session_id}` — 204 on success
- `PATCH /conversations/{session_id}` — rename; 422 on empty title
- Migration runs once at FastAPI startup via `migrate_history_files`
- All endpoints return 503 when `CONVERSATION_HISTORY_DIR` is empty
- Session IDs validated against `^[A-Za-z0-9_-]{1,64}$` at both the endpoint
  layer and inside the history module (defense in depth)

### `src/ui/app.py` — sidebar rewrite

- "Past conversations" section lists all saved conversations, most-recent-first,
  with a `▶` prefix marking the active one
- Click the title button → fetch `GET /conversations/{id}`, populate
  `st.session_state.messages`, rerun. Session ID flips to the resumed conversation.
- Per-row `⋯` menu toggles an inline rename text input + Save/Delete/Cancel
- Delete opens a native `@st.dialog` confirmation (Streamlit modal). On confirm,
  calls `DELETE /conversations/{id}`, clears the cache, resets session if active.
- `@st.cache_data(ttl=5)` around the conversation list, `ttl=30` around `/health`
  to avoid hammering endpoints on rerun
- After sending a new message, cache is cleared so the updated conversation
  floats to the top on next render

## Design decisions

1. **Two-formats → one format.** The old LangGraph format preserved full tool-call
   granularity via `messages_to_dict()`, but the SDK path had already discarded
   that. Unifying on the simpler `turns[]` shape means the UI can render both
   providers uniformly with zero conditional logic. Tool-call inspection remains
   possible via the in-process state (checkpointer or SDK trace) for debugging,
   just not from the saved file.
2. **Truncation titles over LLM-generated titles.** Free, instant, deterministic,
   and first-60-chars of the question is surprisingly good ("Why did prometheus
   alert fire at 3am last night?" is a perfect title). LLM summaries can be added
   later as a toggle — the API already supports arbitrary titles via PATCH.
3. **Cold-start injection, not always-inject.** The LangGraph `MemorySaver` is a
   real checkpointer with per-thread state — re-injecting prior turns when it's
   already populated would duplicate context. The `aget_state()` check keeps
   resume cheap during a single process's lifetime and correct across restarts.
4. **Migration is idempotent + self-gating.** A `.unified-format-v1` marker
   prevents re-running on every FastAPI startup. Per-file failures are logged
   and skipped, never blocking startup.
5. **Path traversal validated everywhere.** Both the API endpoint and the
   history module validate session_id against the same strict regex — the
   history module is still callable directly (from agent paths) without the API
   layer enforcing it, so it has to defend itself.

## Testing

- 870 tests passing (+55 new)
- 93% coverage
- Full visual verification via Playwright: seeded 3 conversations, verified
  list/load/rename/delete/dialog/send/new-conversation flows against a minimal
  smoke backend. Zero browser console errors.

## Risks and follow-ups

1. **Unbounded file growth.** No retention policy — a single session can grow
   arbitrarily. `format_history_as_prompt` trims for prompt context (20 turns),
   but the file keeps everything. Not a bug today, latent issue for later.
2. **LangGraph resume assumes tool-free prior turns.** When injecting
   HumanMessage/AIMessage pairs, LangGraph doesn't see the original tool calls
   that generated those AI responses. For a pure Q&A flow this is fine. If the
   agent ever needs to reference what tool it called on turn 3, it can't — but
   it could re-run the tool.
3. **Rename bumps `updated_at`**, so renamed conversations float to the top of
   the sidebar. Minor UX quirk; acceptable since rename is infrequent.
4. **UI cache clearing is manual.** Each mutation clears
   `_fetch_conversations.clear()` explicitly. If a new mutation path is added
   and the cache isn't cleared, the sidebar will show stale data for up to 5s.
