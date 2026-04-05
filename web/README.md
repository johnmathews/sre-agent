# sre-webapp

Vue 3 SPA frontend for the [homelab SRE assistant](https://github.com/johnmathews/homelab-sre).
Streams chat responses from a FastAPI backend over Server-Sent Events.

## Stack

- **Vue 3** (Composition API, `<script setup>`) + **TypeScript**
- **Vite 8** (dev server + build tool)
- **Pinia 3** (state management)
- **Tailwind CSS v4** (styling)
- **marked** + **DOMPurify** (safe markdown rendering of LLM output)
- **Playwright** (E2E tests)

## Development

Requires Node 22+ and npm 10+.

```sh
npm install
npm run dev        # Vite dev server on :5173
npm run build      # type-check + bundle to dist/
npm run preview    # serve the built bundle
npm test           # Playwright E2E (mocked backend)
```

The dev server proxies `/api/*` → `http://localhost:8000` (the FastAPI backend).
Override with `VITE_API_PROXY_TARGET=http://other-host:8000 npm run dev`.

## Architecture

See [`docs/architecture.md`](./docs/architecture.md) for a full walkthrough.

```
src/
├── api/          fetch wrappers; SSE parser (streamAsk)
├── stores/       Pinia: chat (messages + stream), conversations, health
├── components/   Sidebar + sub-components, ChatWindow, ChatMessage, ToolProgress
├── lib/          markdown renderer
└── App.vue
```

## Related repos

- **Backend + old Streamlit UI:** [johnmathews/homelab-sre](https://github.com/johnmathews/homelab-sre)
