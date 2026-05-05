# Development

## Prerequisites

- Node 22+ and npm 10+
- The backend ([johnmathews/sre-agent](https://github.com/johnmathews/sre-agent))
  running at `http://localhost:8000` (use `make serve` in that repo) — only
  needed for live chat; the E2E test suite mocks the backend.

## Commands

```sh
npm install           # install deps (also installs Playwright browsers via postinstall? no — see below)
npm run dev           # Vite dev server on :5173 (auto-picks :5174 if busy)
npm run build         # vue-tsc --noEmit + vite build -> dist/
npm run preview       # serve the built bundle locally
npm test              # Playwright E2E tests (mocked backend)
npm run test:headed   # same, but with a visible browser
npm run test:ui       # Playwright UI mode for writing/debugging tests
```

## Environment variables

| Variable                  | Default                   | Effect                         |
|---------------------------|---------------------------|--------------------------------|
| `VITE_API_PROXY_TARGET`   | `http://localhost:8000`   | Backend URL for the dev proxy  |

Example:

```sh
VITE_API_PROXY_TARGET=http://homelab.lan:8000 npm run dev
```

## Project layout

```
src/
├── api/              Wire protocol (fetch wrappers, SSE parser)
│   ├── client.ts     apiJson<T>() + ApiError
│   ├── health.ts
│   ├── conversations.ts
│   └── stream.ts     streamAsk() async generator
├── stores/           Pinia stores (reactive state, orchestration)
│   ├── chat.ts
│   ├── conversations.ts
│   └── health.ts
├── components/       Vue SFCs
│   ├── Sidebar.vue
│   ├── HealthPanel.vue
│   ├── ConversationList.vue
│   ├── ConversationRow.vue
│   ├── ChatWindow.vue
│   ├── ChatMessage.vue
│   └── ToolProgress.vue
├── lib/
│   └── markdown.ts   renderMarkdown() — marked + DOMPurify
├── App.vue           top-level layout
├── main.ts           Vue + Pinia bootstrap
└── style.css         Tailwind import + .markdown styles

tests/e2e/            Playwright specs
playwright.config.ts
```

## Rules of thumb

- Components never call `fetch` directly — always go through a store, which
  goes through `src/api/*`.
- `src/api/*.ts` is the single place that knows the URL paths and JSON
  shapes. Keep TypeScript types in sync with the Pydantic models in the
  backend.
- Tailwind utilities live inline on elements; reusable styles (only the
  `.markdown` class today) live in `src/style.css`.

## Adding a new API endpoint

1. Add the TypeScript types + fetch wrapper in `src/api/<topic>.ts`.
2. If it needs reactive state, add it to an existing store or create a new
   one in `src/stores/`.
3. Consume from components via the store's getters/actions.
4. Add a Playwright spec in `tests/e2e/` that mocks the new endpoint.

## Testing

See Playwright docs: <https://playwright.dev/docs/intro>. Tests live in
`tests/e2e/` and mock the `/api/*` endpoints with
`page.route('**/api/**', ...)` so they run without the backend.

The `playwright.config.ts` starts `npm run dev` as a `webServer` before
tests run, so `npm test` is a single command.

## Cross-browser projects

Tests run on four projects in parallel:

- `desktop-chromium` (Desktop Chrome)
- `desktop-webkit` (Desktop Safari) — only place we catch
  Chromium-only assumptions
- `mobile-safari` (iPhone 15 Pro) — closest to the user's PWA target
- `mobile-chrome` (Pixel 7) — Android regression

Specs scoped via `testIgnore` in `playwright.config.ts`:

- `MOBILE_ONLY` (iOS / responsive specs) only run on mobile projects.
- `DESKTOP_ONLY` (sidebar/conversation specs that assume desktop
  layout) only run on desktop projects.
- Everything else (chat-stream, error-resilience, table-rendering,
  pwa-shell, user-timezone) runs on every project.

## Composer key handling

The textarea uses Slack-style platform-aware key handling
(see `src/components/ChatWindow.vue#handleKeydown`):

| Platform | Key | Action |
|---|---|---|
| Desktop (`pointer: fine`) | Enter | Submit |
| Desktop | Shift+Enter | Newline |
| Desktop | Cmd/Ctrl+Enter | Submit (always) |
| Mobile (`pointer: coarse`) | Enter | Newline |
| Mobile | Cmd/Ctrl+Enter | Submit (always) |
| Mobile | Send button tap | Submit |

Why platform-aware: iOS soft keyboard has no Shift modifier when
typing into a `<textarea>`, so if Enter submitted on mobile a user
could never enter a newline (or a bullet list). Detection uses
`window.matchMedia('(pointer: coarse)')` rather than UA sniffing, so
iPad with a hardware keyboard correctly gets desktop behaviour.

`enterkeyhint="send"` is set on the textarea so the iOS keyboard's
Return key shows "Send" — purely visual cue; the actual submit comes
from the Send button on mobile.

## Mobile testing checklist

Some bugs only appear on a real iPhone PWA install. Before merging
mobile-touching changes, verify:

1. **Add to Home Screen** the production URL on a real iPhone.
2. Launch the home-screen icon (must open standalone — no Safari
   chrome).
3. **Header**: "SRE Agent" title clears the status bar / Dynamic
   Island.
4. **Composer**: Send button clears the home indicator with the
   keyboard closed.
5. **Keyboard**: focus the input — composer remains visible above the
   keyboard. Long messages scroll inside the textarea.
6. **Multi-line**: type "List:", Enter, "- one", Enter, "- two" —
   the textarea grows and contains all four lines. Send button
   delivers it.
7. **Wide table**: ask the agent something that returns a 4+ column
   table. The table renders inside the bubble's gray background and
   scrolls horizontally; columns don't escape onto the white
   background.
8. **Narrow table**: ask for a 2-column comparison. On a real iPhone
   the table reflows into stacked key/value pairs.
9. **Network resilience**: send a question, switch from Wi-Fi to
   cellular while the agent is responding. Observe either a silent
   recovery (the answer arrives despite the handoff) or a
   `Connection dropped mid-reply` ErrorBubble with a Retry button —
   not "Error: Load failed" with no retry.
