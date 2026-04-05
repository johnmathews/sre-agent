# 2026-04-05 — Initial scaffold

Spun up `sre-webapp` as the Vue 3 replacement for the Streamlit UI that
shipped with `homelab-sre`. Keeping the backend repo (API + old UI) separate
and having this frontend live in its own repo.

## Why Vue + SPA

Streamlit's tradeoffs finally bit on a CSS task (had to ship a
`:nth-child(2):nth-last-child(1)` structural selector hack to hide a hover
menu). Python-rendered frontends fight you as soon as you want real
interaction patterns (popovers, keyboard shortcuts, virtualized lists).

Picked Vue as an experiment — I've used Svelte before, wanted to see what
`<script setup>` + Composition API look like in practice on a non-trivial
app.

SPA over SSR because:

- LAN-only, behind Cloudflare Access, one user — no SEO, no cold-start
  latency concerns.
- SPA is just static files (one Dockerfile, one nginx, zero Node at
  runtime). SSR means a Node process in prod for no real benefit.

## Stack decisions

| Choice             | Reason                                                     |
|--------------------|------------------------------------------------------------|
| Vue 3 + Pinia      | Official state lib; Composition API ergonomics             |
| TypeScript         | Catch type drift vs. backend Pydantic models               |
| Vite 8             | Default Vue toolchain; great DX                            |
| Tailwind CSS v4    | Iterate fast on styling without per-component CSS          |
| marked + DOMPurify | LLM output is untrusted — sanitize rendered HTML           |
| No Vue Router      | Single-view app; matches Streamlit's lack of routing       |
| Playwright E2E     | Browser-level confidence; mocks `/api/*` so no backend needed |

## SSE implementation notes

The chat stream is the one risky piece. The backend uses
`POST /ask/stream`, which rules out `EventSource` (GET-only). Implemented
as a `fetch` + `ReadableStream` async generator in `src/api/stream.ts`:
buffer chunks across reads, split on `\n\n`, parse each `data: <json>` block.

Pushed the streaming orchestration into `src/stores/chat.ts` rather than a
separate `useChatStream` composable — the store already owns the message
list and session ID, and having two layers (composable + store) added a
handoff with no real decoupling benefit. Single place to look.

The "Stop" button calls `chat.abort()`, which aborts the underlying fetch
via an `AbortController`. Catching `AbortError` in the stream loop keeps
cancellation silent (no error bubble added).

## UI parity with Streamlit

Matched the old sidebar: new-conversation button, session ID display,
health panel (collapsible, auto-expands when not healthy), past
conversations list with a hover-reveal `⋯` menu for inline rename /
delete-confirm. The nasty CSS hack from Streamlit became a trivial
`opacity-0 group-hover:opacity-100` Tailwind pair.

## Repo layout

Monorepo-by-proximity (both in `~/projects/homelab-sre/`) but each is a
standalone repo: `homelab-sre` (backend + old UI) and `sre-webapp`
(new frontend).

## Open questions / next

- Dockerfile (multi-stage: node build → nginx serve) + compose service
- GitHub Actions workflow (build + push to `ghcr.io/johnmathews/sre-webapp`)
- Deep-linking (`/conversations/:id`) — deferred; add Vue Router when
  bookmarking conversations becomes useful
- Whether to add a dark-mode toggle or leave it as `prefers-color-scheme`
