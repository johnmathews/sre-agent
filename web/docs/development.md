# Development

## Prerequisites

- Node 22+ and npm 10+
- The backend ([johnmathews/homelab-sre](https://github.com/johnmathews/homelab-sre))
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
