# Architecture

`sre-webapp` is a single-page Vue 3 application that talks to the FastAPI
backend at [johnmathews/homelab-sre](https://github.com/johnmathews/homelab-sre).
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
                            │ FastAPI (homelab-sre)      │
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

- **`useChatStore`** — the active conversation: `sessionId`, `messages`,
  streaming state (`isStreaming`, `currentStatus`, `completedTools`,
  `streamError`). Orchestrates the SSE stream via `streamAsk()`.
- **`useConversationsStore`** — the sidebar list of past conversations:
  `items`, `loading`, `error`, plus `refresh / rename / remove` actions.
- **`useHealthStore`** — the health panel snapshot; polled every 30s.

Cross-store coordination is minimal: when a conversation is deleted and it
was the active one, the conversation list store emits no signal — the
`ConversationList` component checks `chat.sessionId` and calls
`chat.startNewConversation()` directly. Keeps each store ignorant of the
others.

## Streaming protocol

The core interaction is the SSE stream from `POST /ask/stream`. The backend
sends events like:

```
data: {"type": "tool_start", "content": "prometheus_query"}\n\n
data: {"type": "tool_end", "content": "prometheus_query"}\n\n
data: {"type": "answer", "content": "CPU is at 73%..."}\n\n
```

See [api-integration.md](./api-integration.md) for the event-type contract and
the reasoning behind using `fetch` + `ReadableStream` instead of
`EventSource`.

## Styling

Tailwind CSS v4 (CSS-first, no config file). Dark theme by default, hand-coded
colors for the MVP — no design tokens yet.

The `.markdown` class in `src/style.css` handles LLM-rendered chat content
(headings, lists, code blocks, tables). This is the one place where we
style HTML we did not author.
