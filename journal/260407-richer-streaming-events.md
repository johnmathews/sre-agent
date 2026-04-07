# Richer Streaming Events for Anthropic Path

**Date:** 2026-04-07

## Problem

The Anthropic streaming path (`stream_sdk_agent`) emitted a single "Thinking..." status
event at the start and then went silent until the final answer — sometimes 10-15 minutes
later. Users had no way to tell if the agent was making progress or had crashed. The
OpenAI/LangGraph path already had `tool_end` events and richer status; the Anthropic path
was missing these.

## Changes

### tool_end events

Added `tool_end` event emission for every completed tool call. Previously only `tool_start`
was emitted. Both the mid-loop batch (when a new `AssistantMessage` arrives, indicating
prior tools completed) and the final batch (after the SDK loop exits) now yield
`tool_end` events with `tool_name` and human-readable labels.

### Richer intermediate status events

Filled the four silent gaps in the streaming pipeline:

1. **"Initializing..."** — emitted before OAuth token refresh (was previously silent)
2. **Intermediate reasoning** — Claude's mid-loop `TextBlock` content (e.g., "Let me check
   disk status next...") is now truncated to 120 chars and emitted as a `status` event
3. **"Synthesizing response..."** — emitted after all tools complete, before final answer
4. Existing "Thinking..." retained after initialization

### Complete tool labels

- Added 4 missing memory tool labels (`memory_search_incidents`, etc.)
- Updated `_tool_display_name` to strip `mcp__docs__` prefix (docs MCP server tools)
- Added `_summarize_sdk_tool_input()` to extract query/expression parameters for display
  (e.g., "Querying Prometheus — up{job='node'}")

### Tests

- 13 new unit tests for `_tool_display_name` (docs prefix) and `_summarize_sdk_tool_input`
  (various input patterns, truncation, priority order, edge cases)
- All 888 tests pass

## Decisions

- Parameter summaries truncated to 80 chars to keep sidebar/progress display clean
- Intermediate TextBlock reasoning limited to first line, max 120 chars — full reasoning
  still only appears in the final answer
- `tool_end` labels reuse `_TOOL_LABELS` for consistency with `tool_start`
