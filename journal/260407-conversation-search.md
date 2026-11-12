# Conversation Search

**Date:** 2026-04-07

## Feature

Full-text search across all past conversations, accessible from the sidebar.

## Backend (sre-agent)

Added `GET /conversations/search?q=<query>` endpoint that performs case-insensitive
search across all conversation titles and turn content. Returns matching conversations
with context snippets (60 chars surrounding each match). Route registered before the
`/conversations/{session_id}` catch-all to avoid FastAPI route capture conflicts.

Implementation is simple in-memory file scanning — suitable for a homelab with hundreds
of conversations, not thousands. Each search reads all JSON files from disk and filters
in-memory. No indexing or caching.

## Frontend (sre-webapp)

- `ConversationSearch` component in the sidebar with a search input
- 300ms debounce to avoid firing on every keystroke
- Results show conversation title + up to 2 matching snippets per conversation
- Snippets have highlighted query terms via `<mark>` tags
- Role labels ("You", "Agent", "Title") identify where the match was found
- Clicking a result loads the conversation and clears the search
- Clear button (x) to dismiss results

## Tests

- 8 unit tests + 3 API integration tests in sre-agent (899 total)
- 5 e2e tests in sre-webapp (27 total)
- Mock search route registered last in Playwright fixtures to take priority over
  the `**/api/conversations/*` catch-all (Playwright: last-registered route wins)
