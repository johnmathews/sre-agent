# sre-webapp — Claude Code instructions

Vue 3 SPA frontend for the SRE Agent. The companion backend lives in the sibling
repo `sre-agent/` (FastAPI + LangChain). This repo contains *only* the frontend.

## Stack

- Vue 3 (Composition API, `<script setup>`) + TypeScript
- Vite 8 (dev server + production build)
- Pinia 3 (state)
- Tailwind CSS v4 (styling, configured via `@tailwindcss/vite`)
- `marked` + `DOMPurify` (markdown rendering of LLM output)
- Playwright (E2E tests, with a mocked backend)

Node 22+ is required.

## Repo layout

```
src/
  api/          fetch wrappers; SSE parser in stream.ts
  stores/       Pinia: chat (messages + streaming), conversations, health
  components/   Sidebar, ChatWindow, ChatMessage, ToolProgress, ConversationList/Row/Search, HealthPanel
  composables/  useTheme (light/dark)
  lib/          markdown renderer
  App.vue       root layout (sidebar + chat), mobile breakpoint, visualViewport tracking
  style.css     Tailwind import + global resets, safe-area helpers, markdown styles
docker/         nginx.conf.template (used at container start via envsubst)
docs/           architecture, api-integration, deployment, development
journal/        dated entries — YYMMDD-descriptive-name.md
tests/e2e/      Playwright specs
```

## Commands

```sh
npm run dev        # Vite on :5173, proxies /api/* → http://localhost:8000
npm run build      # vue-tsc + vite build → dist/
npm run typecheck  # vue-tsc -b
npm run preview    # serve the built dist/
npm test           # playwright test (mocked backend; no real API needed)
npm run test:headed
npm run test:ui
```

Override the proxy target: `VITE_API_PROXY_TARGET=http://other-host:8000 npm run dev`.

## API contract

The frontend never embeds the backend origin — it always calls `/api/*` and
relies on either Vite's proxy (dev) or nginx's `API_UPSTREAM` upstream (prod).

- `GET /health` — health badge
- `GET /conversations`, `GET /conversations/:session_id`, `DELETE /conversations/:session_id`
- `POST /ask/stream` — Server-Sent Events; chunks of assistant text plus
  tool-progress events parsed in `src/api/stream.ts`

When changing endpoints, update both the backend (in `sre-agent/`) **and**
`docs/api-integration.md` so the contract stays documented.

## Deployment

Multi-stage Dockerfile builds the SPA with Node, then serves `dist/` via nginx.
Image is published to `ghcr.io/johnmathews/sre-webapp:latest` on every push to
`main` (workflow: `.github/workflows/ci.yml`).

The nginx config is a `.template` that envsubsts `API_UPSTREAM` at container
start (default `http://sre-api:8000`). Do not hardcode upstream hosts — set
them through the env var.

`docker-compose.demo.yml` is the canonical "pull from GHCR and run" stack
(webapp + backend). Production deploy lives on the `infra` VM (see the
parent-repo `CLAUDE.md`).

## iOS / mobile considerations

The app is used heavily on iPhone in portrait. Several pieces of CSS and JS
exist specifically to keep the iOS experience clean — be careful when touching
these:

1. **`index.html`** has `viewport-fit=cover` — required for `env(safe-area-inset-*)`
   to return non-zero values on iPhones with a home indicator / Dynamic Island.
2. **`src/style.css`** defines `.composer-area` and `.app-header-mobile`. They
   apply `max(<base>, env(safe-area-inset-*))` so the input bar clears the home
   indicator and the header clears the status bar. If you restyle these regions
   with raw Tailwind padding, you will re-introduce the curved-corner clip.
3. **`src/style.css`** forces `font-size: max(16px, 1rem)` on `input/textarea/select`.
   This prevents iOS Safari's auto-zoom on focus. Don't lower input font-size
   below 16px.
4. **`src/App.vue`** drives a `--vvh` CSS variable from `window.visualViewport`
   and sets the root container height to `var(--vvh, 100dvh)`. This is what
   keeps the composer above the iOS keyboard. Plain `100vh`/`h-screen` will
   break this — keep using the var.

If you change the mobile layout, verify on a real iPhone (or at least at
390×844 in DevTools with iOS device emulation). Headless Chromium reports
`env(safe-area-inset-*)` as 0, so Playwright cannot fully validate insets.

## Conventions

- Composition API with `<script setup lang="ts">` only — no Options API.
- Pinia stores are the source of truth for chat state; components stay thin.
- API calls live in `src/api/`; components/stores import from there, never
  `fetch` directly.
- Markdown from the LLM is **always** sanitised through `src/lib/markdown.ts`
  (marked → DOMPurify). Never `v-html` raw model output.
- Use Tailwind utilities in templates; reach for `style.css` only for things
  that don't fit utilities (markdown styles, safe-area helpers, dynamic vars).
- Dark mode is toggled by the `dark` class on `<html>` (custom variant in
  `style.css`); use `dark:` prefix in templates.

## Testing

Playwright specs live in `tests/e2e/` and run against a mocked backend so
they don't require the real FastAPI service. Add tests when:

- Adding a new user-visible flow (conversation switching, search, etc.)
- Fixing a regression that wasn't caught (write the failing test first)
- Touching streaming or SSE parsing — these are the most fragile areas

Run `npm test` before pushing UI changes. CI runs typecheck + Playwright on
every PR.

## Related

- Backend: `../sre-agent/` (FastAPI + LangChain agent, separate git repo)
- Parent repo `CLAUDE.md`: `../CLAUDE.md` (covers the MCP tools available
  for querying live homelab infrastructure)
