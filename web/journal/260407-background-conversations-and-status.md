# Background Conversation Processing and Richer Status Display

**Date:** 2026-04-07

## Problem

Two UX issues with the SRE webapp:

1. **Switching conversations killed in-progress streams.** The chat store had a single
   global `isStreaming` boolean, one `AbortController`, and one `messages` array.
   `loadConversation()` called `abort()` unconditionally, canceling any in-flight agent
   response. Users couldn't check another conversation while waiting for a long response.

2. **Static "Thinking..." with no progress feedback.** While the agent worked (sometimes
   10-15 minutes), the only visible indicator was a static "Thinking..." message. No tool
   activity, no elapsed time, no intermediate status — users couldn't tell if the agent
   had crashed.

## Changes

### Per-session streaming state (chat store refactor)

Replaced all singular globals with a `Map<sessionId, SessionStreamState>`:

- Each session independently tracks `messages`, `isStreaming`, `currentStatus`,
  `completedTools`, `streamError`, `abortController`, and `toolStartTime`
- `activeSessionId` ref determines which session is displayed in the chat window
- Computed properties expose the active session's state — backward-compatible API,
  components needed minimal changes
- `loadConversation()` and `startNewConversation()` no longer call `abort()`
- Multiple conversations can stream simultaneously
- New `streamingSessions` computed exposes which sessions have active streams

### Enhanced ToolProgress component

- Elapsed time ticker: shows running seconds for the current active tool/status
- Completed tools show their duration (e.g., "Querying Prometheus (3s)")
- Visual distinction between tool events (code style, hourglass) and status messages
  (dot style, lighter text)
- Animated "Thinking..." dots (CSS keyframe animation) replace static text
- `CompletedTool` type with `{ name, duration }` replaces bare `string[]`

### Sidebar processing indicators

- `ConversationRow` accepts new `isProcessing` prop
- Non-active streaming conversations show a pulsing amber dot next to the title
- Active conversations rely on ToolProgress in the main chat area (no redundant dot)

### Tests

- 5 new e2e tests covering background streaming, processing indicators, tool progress
  rendering, animated thinking dots, and status message display
- All 22 tests pass (17 existing + 5 new)

## Architecture decision

Used `ref(new Map())` with `triggerRef()` for manual reactivity triggering. Vue's `ref`
doesn't track Map mutations automatically, so every mutation is followed by `triggerRef(sessions)`.
This is explicit but reliable. Alternative was `reactive()` which has proxy limitations with Maps.

Non-active completed sessions clean up streaming artifacts after 60 seconds to prevent memory growth.

### Local sidebar sessions

After deploying, discovered that new conversations didn't appear in the sidebar until
the stream completed (backend only persists after the answer). Added `localSessions`
computed that synthesizes temporary `ConversationSummary` entries from in-progress
sessions. Merged at the top of the sidebar list. Excludes the active session (already
visible in chat). Cleaned up from session Map on deletion.

### Input UX improvements

- Textarea auto-focuses when switching conversations or clicking "+ New conversation"
  (watch on `sessionId` + `nextTick`)
- Textarea auto-grows with content (`@input` handler resets height then sets to
  `scrollHeight`), manually resizable via `resize-y`, capped at `50vh`
- Clicking anywhere in the chat window focuses the textarea

## Decisions

- No limit on concurrent streams — browser connection limit (6 per domain) provides natural backpressure
- Tool duration is measured client-side (time between tool_start and tool_end events) which is slightly
  longer than server-side tool execution due to network latency, but accurate enough for UX purposes
- Status messages show elapsed time only after 2 seconds to avoid flickering on fast transitions
- Local sidebar sessions use the first user message (truncated to 60 chars) as the title
