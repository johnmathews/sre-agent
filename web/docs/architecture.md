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

## PWA shell

Primary form factor is an installed iOS PWA. The shell tags in
`index.html` are what make iOS treat the home-screen launch as a real
standalone app — without them the bookmark opens in Safari chrome,
`env(safe-area-inset-*)` resolves to 0, and the title overlaps the
status bar.

| Tag | Why |
|---|---|
| `<link rel="manifest" href="/manifest.webmanifest">` | Declares display=standalone, start_url, icons. |
| `<meta name="apple-mobile-web-app-capable" content="yes">` | Tells iOS to launch in standalone mode (no Safari chrome). |
| `<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">` | Lets iOS draw the status bar **over** the page; CSS `safe-area-inset-top` handles clearance. The other choices either reserve white space (`default`) or paint a black bar that fights the dark theme (`black`). |
| `<link rel="apple-touch-icon" sizes="180x180">` | Home-screen icon. |
| `<meta name="theme-color">` × 2 | OS chrome tinting; light + dark variants. |

The placeholder PNGs (`public/icon-192.png`, `icon-512.png`,
`apple-touch-icon-180.png`) are generated by
`scripts/gen-placeholder-icons.mjs` — replace with real artwork by
swapping the files; the script is reproducible if you ever need a
quick set again.

## Mobile / iOS layout

- **Viewport meta tag** uses `viewport-fit=cover` so
  `env(safe-area-inset-*)` returns non-zero on iPhones with a Dynamic
  Island / home indicator. (Also requires the PWA shell tags above —
  Safari-tab mode reports 0 even with `viewport-fit=cover`.)
- **`.composer-area`, `.app-header-mobile`, `.chat-scroll-area`** in
  `src/style.css` apply
  `max(<floor>, var(--safe-area-inset-*, env(safe-area-inset-*)))` for
  horizontal/vertical padding. The 1rem horizontal floor keeps the
  Send button clear of the iPhone screen's rounded-corner curve in
  portrait. The CSS-variable indirection lets tests inject simulated
  inset values via `tests/e2e/helpers/pwa.ts` — production CSS resolves
  through to `env()` because the variables are unset.
- **Input font-size is forced to ≥16px** on all
  `input`/`textarea`/`select`, which prevents iOS Safari's auto-zoom on
  focus.
- **Root container uses `100dvh`** (no JS observer). `dvh` ("dynamic
  viewport height") shrinks when the iOS keyboard opens, which is what
  used to require the `--vvh` visualViewport observer. `dvh` reached
  Baseline Widely Available in 2025; we deleted the JS observer along
  with this change. If a future iOS regresses, add a JS fallback behind
  a runtime feature check rather than reverting wholesale.

### Cross-engine testing

Headless Chromium reports `env(safe-area-inset-*)` as 0, so Playwright
on Linux can only validate the *floor* values from
`max(<floor>, env(...))` directly. To test the inset branch we use
`simulatePwaInsets()` (in `tests/e2e/helpers/pwa.ts`) which sets the
`--safe-area-inset-*` CSS variables to simulated device values. The
production CSS reads
`var(--safe-area-inset-*, env(safe-area-inset-*))` so tests can drive
the inset branch without a real device.

`playwright.config.ts` runs four projects:

- `desktop-chromium` and `desktop-webkit` — cross-engine desktop
- `mobile-safari` (iPhone 15 Pro) and `mobile-chrome` (Pixel 7) —
  cross-engine mobile

Specs scoped to specific projects via `testIgnore`:

- `MOBILE_ONLY` (`ios-viewport.spec.ts`, `responsive-mobile.spec.ts`)
  hardcode small viewports and only run on the mobile projects.
- `DESKTOP_ONLY` (sidebar / conversation-management specs) assume a
  desktop layout where the sidebar is always visible.

Real-iPhone PWA verification is still required for safe-area, keyboard,
and gesture-driven interactions — Playwright covers a lot of ground but
not the device-specific behaviour iOS adds in standalone mode.

## Streaming resilience

The stream from `POST /ask/stream` can fail mid-flight — the iOS
network handoff that motivated this layer is the canonical case
(server completed, persisted the answer, but the client's `fetch()`
read rejected with `TypeError: Load failed`).

`src/api/streamWithRecovery.ts` wraps `streamAsk` with:

1. **Retry** — one auto-retry with 1s backoff for `network-*` and
   `http-5xx` failures.
2. **Persisted-answer recovery** — on a mid-stream failure, before
   retrying we call `GET /conversations/:session_id` and synthesise an
   `answer` event from the persisted assistant turn if it matches our
   question. This is what recovers the iOS-handoff case silently
   without asking the user to re-press Send.
3. **Classification** — `StreamError.category` distinguishes
   `network-before-headers`, `network-midstream`, `http-4xx`,
   `http-5xx`, `no-body`, and `aborted`. Permanent categories
   (4xx, aborted) skip retry.

The chat store renders failed turns as `ChatMessage{kind:'error'}` →
`ErrorBubble.vue`, which carries an inline Retry button that re-issues
the original question. See [`api-integration.md`](./api-integration.md)
§"Error handling" for the full contract.

## Markdown rendering on narrow viewports

`src/lib/markdown.ts` post-processes the rendered HTML before
DOMPurify sanitisation:

- Every `<table>` is wrapped in a
  `<div class="md-table-wrap" role="region" tabindex="0" aria-label="Table" data-cols="N">`.
  CSS `overflow-x: auto` on the wrap clips overflow inside the message
  bubble (the bug the user reported was wide tables bursting through
  the bubble's gray background).
- Each `<td>` gets `data-label="<header text>"` so CSS can render
  stacked-row layout for ≤2-column tables under
  `@media (max-width: 480px)`. Wider tables (3+ cols) keep their
  tabular shape and rely on the wrap's horizontal scroll instead.
- The `.markdown` div has `min-width: 0` so the flex chain (chat-scroll
  → flex-col → bubble → markdown) can clip overflowing children.
  Without it, flexbox children default to `min-width: auto` ≈ intrinsic
  content width and the bubble grows past `max-w-[85%]`.
