# Conversation Search Endpoint

**Date:** 2026-04-07

## Feature

`GET /conversations/search?q=<query>` — full-text search across all stored conversations.

## Implementation

`search_conversations()` in `history.py` scans all JSON conversation files, performing
case-insensitive matching against titles and turn content. Returns `SearchResult` typed
dicts with metadata + up to 5 matching snippets per conversation, each with 60 chars
of surrounding context.

The endpoint is registered before `/conversations/{session_id}` in FastAPI to avoid
the path parameter capturing "search" as a session ID.

## Tests

- 8 unit tests in `test_history.py` covering: title search, content search,
  case-insensitivity, no results, empty query, multiple conversations, snippet
  context extraction, and max_results limiting
- 3 API integration tests in `test_api_integration.py`
