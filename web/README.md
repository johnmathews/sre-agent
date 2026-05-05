# sre-webapp

Vue 3 SPA frontend for the [HomeLab SRE Agent](https://github.com/johnmathews/sre-agent).
Streams chat responses from a FastAPI backend over Server-Sent Events.

## Primary use case

The app is a single-user tool deployed behind Cloudflare Access. The most
common form factor is an **installed iOS PWA on iPhone in portrait** — the
mobile layout, safe-area handling, multi-line input, and stream-resilience
behaviour are tuned for that target. Desktop browsers work too and have
full feature parity, but expect mobile-first behaviour where they
diverge.

## Install on iPhone (PWA)

1. Open the app's URL in iOS Safari.
2. Tap the Share icon → **Add to Home Screen**.
3. Tap the new icon. The app launches in standalone mode (no Safari
   chrome) — this is what populates `env(safe-area-inset-*)` so the
   header clears the status bar and the composer clears the home
   indicator. If you launch the URL inside a regular Safari tab the
   safe-area branch never runs and the title overlaps the status bar.

The PWA shell (`manifest.webmanifest`, `apple-mobile-web-app-capable`,
status-bar style, theme-color) is wired up in `index.html`. See
[`docs/architecture.md`](./docs/architecture.md) §"PWA shell" for why
each tag matters.

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

## Docker

A multi-arch image is published to GHCR on every push to `main`:

```sh
docker pull ghcr.io/johnmathews/sre-webapp:latest
docker run --rm -p 8080:80 \
  -e API_UPSTREAM=http://sre-api:8000 \
  ghcr.io/johnmathews/sre-webapp:latest
```

`API_UPSTREAM` defaults to `http://sre-api:8000` — the expected service name
when the frontend and backend run in the same docker-compose network.

See [`docs/deployment.md`](./docs/deployment.md) for the full compose stack,
or [`docker-compose.demo.yml`](./docker-compose.demo.yml) for a ready-to-run
stack (webapp + sre-agent backend) pulled straight from GHCR.

## Related repos

- **Backend (FastAPI + LangChain agent):** [johnmathews/sre-agent](https://github.com/johnmathews/sre-agent)
