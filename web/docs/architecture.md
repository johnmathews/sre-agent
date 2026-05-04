# Architecture

`sre-webapp` is a single-page Vue 3 application that talks to the FastAPI
backend at [johnmathews/sre-agent](https://github.com/johnmathews/sre-agent).
It replaces the earlier Streamlit UI with a first-class frontend stack.

## Why a SPA?

The app runs on a LAN behind Cloudflare Access for a single user. There is no
SEO need, no cold-start concern, and no benefit to server-side rendering.
A SPA built to static files (served by any HTTP server) is the simplest
deployment that provides full control over the DOM and CSS — which the old
Streamlit UI could not give us.

## Components

```
┌──────────────────────────────────────────────────────┐
│  browser (Vue SPA)                                   │
│                                                      │
│  ┌──────────┐    ┌───────────────────────────┐      │
│  │ Sidebar  │    │ ChatWindow                │      │
│  │ ├ Health │    │ ├ ChatMessage × N         │      │
│  │ │ Panel  │    │ ├ ToolProgress (stream)   │      │
│  │ └ Conv.  │    │ └ textarea + Send/Stop    │      │
│  │   List   │    └───────────────────────────┘      │
│  └──────────┘                                        │
│       │                      │                       │
│       ▼                      ▼                       │
│   Pinia stores       ── streamAsk() ── fetch ──┐    │
│   (chat, conv.,                                 │    │
│    health)                                      │    │
└─────────────────────────────────────────────────┼────┘
                                                  │
                       /api/* proxied by Vite     │
                                                  ▼
                            ┌────────────────────────────┐
                            │ FastAPI (sre-agent)        │
                            │ :8000                      │
                            │ ├ POST /ask/stream (SSE)   │
                            │ ├ GET  /health             │
                            │ ├ GET  /conversations      │
                            │ ├ GET  /conversations/{id} │
                            │ ├ PATCH/conversations/{id} │
                            │ └ DELETE /conversations/{id}│
                            └────────────────────────────┘
```

## Layered structure

| Layer        | Responsibility                           | Files                              |
|--------------|------------------------------------------|------------------------------------|
| `api/`       | Wire protocol: fetch, SSE parse          | `client.ts`, `stream.ts`, etc.     |
| `stores/`    | Reactive state, orchestration            | `chat.ts`, `conversations.ts`, `health.ts` |
| `components/`| DOM + interaction                        | `Sidebar.vue`, `ChatWindow.vue`, etc. |
| `lib/`       | Pure helpers                             | `markdown.ts`                      |

**Rule:** components talk to stores, stores talk to `api/`. Components never
call `fetch` directly; the API layer never touches Vue reactivity.

## State ownership

Each Pinia store owns a slice:

- **`useChatStore`** — all conversation streaming state, keyed by session ID.
  Internally uses a `Map<sessionId, SessionStreamState>` where each entry holds
  `messages`, `isStreaming`, `currentStatus`, `completedTools`, `streamError`,
  and an `AbortController`. An `activeSessionId` ref determines which session
  is displayed; computed properties expose the active session's state for
  backward-compatible component access. Multiple conversations can stream
  simultaneously — switching conversations does not abort background streams.
  Exposes `streamingSessions` (list of session IDs with active streams) for
  sidebar indicators.
- **`useConversationsStore`** — the sidebar list of past conversations:
  `items`, `loading`, `error`, plus `refresh / rename / remove` actions.
- **`useHealthStore`** — the health panel snapshot; polled every 30s.

Cross-store coordination is minimal: when a conversation is deleted and it
was the active one, the conversation list store emits no signal — the
`ConversationList` component checks `chat.sessionId` and calls
`chat.startNewConversation()` directly. Keeps each store ignorant of the
others. The `ConversationList` reads `chat.streamingSessions` to show
processing indicators on sidebar rows.

## Streaming protocol

The core interaction is the SSE stream from `POST /ask/stream`. The backend
sends events like:

```
data: {"type": "status", "content": "Initializing..."}\n\n
data: {"type": "status", "content": "Thinking..."}\n\n
data: {"type": "tool_start", "content": "Querying Prometheus — up{job='node'}"}\n\n
data: {"type": "tool_end", "content": "Querying Prometheus — up{job='node'}"}\n\n
data: {"type": "status", "content": "Synthesizing response..."}\n\n
data: {"type": "answer", "content": "CPU is at 73%..."}\n\n
```

The Anthropic backend path emits richer status events throughout the request
lifecycle: `"Initializing..."` during token refresh, `"Thinking..."` at
startup, intermediate reasoning text, tool start/end events with parameter
summaries, and `"Synthesizing response..."` during final answer generation.
`completedTools` entries are `{ name, duration }` objects, enabling the
`ToolProgress` component to show elapsed times.

See [api-integration.md](./api-integration.md) for the event-type contract and
the reasoning behind using `fetch` + `ReadableStream` instead of
`EventSource`.

## Styling

Tailwind CSS v4 (CSS-first, class-based dark mode via `@custom-variant dark`).
Light theme by default with a toggle button in the sidebar header. Theme
preference is persisted to `localStorage`. Colors use light-first utilities
with `dark:` variants throughout — no design tokens yet.

The `.markdown` class in `src/style.css` handles LLM-rendered chat content
(headings, lists, code blocks, tables). This is the one place where we
style HTML we did not author.

## Mobile / iOS layout

The app is used heavily on iPhone in portrait, so several pieces of the
layout exist specifically to keep the iOS experience clean:

- **Viewport meta tag** uses `viewport-fit=cover` so `env(safe-area-inset-*)`
  returns non-zero on iPhones with a Dynamic Island / home indicator.
- **`.composer-area`, `.app-header-mobile`, `.chat-scroll-area`** in
  `src/style.css` apply `max(<floor>, env(safe-area-inset-*))` for
  horizontal/vertical padding. The 1rem horizontal floor is what keeps the
  Send button (and other corner content) clear of the iPhone screen's
  rounded-corner curve in portrait, where `safe-area-inset-right` is 0.
- **Input font-size is forced to ≥16px** on all `input`/`textarea`/`select`,
  which prevents iOS Safari's auto-zoom on focus.
- **Root container height** binds to a `--vvh` CSS variable that `App.vue`
  drives from `window.visualViewport`. This is what keeps the composer above
  the iOS keyboard when it opens; plain `100vh` / `h-screen` would be hidden
  behind the keyboard.

Headless Chromium reports `env(safe-area-inset-*)` as 0, so the Playwright
spec at `tests/e2e/ios-viewport.spec.ts` only validates the *floor* values
from `max(<floor>, env(...))`. Verify on a real iPhone (or DevTools iOS
device emulation at 390×844) when changing this layout.
