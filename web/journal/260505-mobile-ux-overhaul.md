# 2026-05-05 — Mobile UX overhaul (PWA, error UX, tables, multi-line, cross-browser tests)

User report (iPhone PWA, daily-driver use):

1. The "SRE Agent" title overlaps the iOS status bar.
2. Markdown tables overflow the message bubble's gray background — both on
   mobile *and* desktop (a screenshot showed columns 4 and 5 sitting on
   the page's white background).
3. A streaming reply died mid-flight with "Error: Load failed" and no
   retry. Sending "Try again" recovered it. Error message gives the user
   nothing actionable.
4. Multi-line input doesn't work: every Enter submits, and iOS soft
   keyboards have no Shift modifier for `<textarea>`, so bullet lists
   are impossible to type.

## Triage — root cause of "Error: Load failed"

Pulled production logs via the SRE-agent MCP. The user's session is
`9dade85f`. Cross-referenced with Loki:

- Turn 4 (`POST /api/ask/stream`) at 07:36:23 UTC succeeded server-side:
  200 OK, 6,285-byte response, 63s duration. The conversation API
  returns the answer, so the backend persisted it.
- The IPv6 source prefix in traefik / sre-webapp access logs changed
  between turn 4 (`2a09:bac2:4d85:c8::14:2f2`) and the retry at 07:37:41
  (`2a09:bac2:4d80:2719::3e5:22`). Different /48 prefixes ⇒ a Wi-Fi
  reconnect or Wi-Fi↔cellular handoff mid-stream.

So the streaming `fetch()`'s socket was killed by the network change.
iOS Safari surfaces every `fetch` failure (DNS, TLS, abort, mid-stream
drop) as `TypeError: Load failed`. The previous chat store assigned
`err.message` directly to the message list as a fake assistant bubble:

```ts
s.messages.push({ role: 'assistant', content: '**Error:** Load failed' })
```

Indistinguishable from a real reply. No retry. No recovery via the
already-persisted server turn.

## What landed

One bundled PR off `main` → branch `eng-mobile-ux-overhaul`:

### Test infrastructure (Unit 0)
- `playwright.config.ts` now runs four projects: `desktop-chromium`,
  `desktop-webkit`, `mobile-safari` (iPhone 15 Pro), `mobile-chrome`
  (Pixel 7). Specs scoped via `MOBILE_ONLY` / `DESKTOP_ONLY` lists.
- `tests/e2e/helpers/pwa.ts` simulates iOS safe-area insets by setting
  CSS variables. Production CSS reads
  `var(--safe-area-inset-*, env(safe-area-inset-*))` — production
  resolves through to `env()`, tests can override.
- `tests/e2e/fixtures.ts` gained `overrideAskStream` (network-before-
  headers / midstream / http-4xx / http-5xx / sse-error) and
  `installRecoveryRoute` (with `matchAny` mode for the random session
  id case) to drive the new resilience layer deterministically.
- CI workflow installs both `chromium` and `webkit` browsers; cache key
  bumped.

### Error UX + recovery (Unit 1+6)
- `src/api/stream.ts` — `StreamError` class with category enum
  (`network-before-headers`, `network-midstream`, `http-4xx`, `http-5xx`,
  `no-body`, `aborted`). `StreamError.from` distinguishes the two
  network categories via a `receivedAnyBytes` flag tracked across the
  reader loop.
- `src/api/streamWithRecovery.ts` (new) wraps `streamAsk`:
  - Retries `network-*` and `http-5xx` once with 1s backoff.
  - On `network-midstream`, calls `getConversation(session_id)` first.
    If the persisted conversation contains an assistant turn whose
    preceding user turn matches our question, synthesise an `answer`
    event and exit cleanly — recovers the iOS-handoff case the user hit
    without burning a second LLM call.
  - Permanent categories (`http-4xx`, `aborted`) skip retry.
- `src/api/client.ts` — `apiJson` got a default 30s timeout via
  `AbortSignal.timeout` + `AbortSignal.any` (with manual fallback for
  older runtimes). Network `TypeError` wrapped as `NetworkError`.
- `src/stores/chat.ts` — `ChatMessage` gains `kind: 'normal' | 'error'`
  and an `error: { category, status, causeMessage, originalQuestion }`
  payload. The fake-bubble path is gone. `retryMessage(index)` removes
  the [user, error] pair (only the immediately preceding user bubble —
  not a backward scan, which the code review caught as a regression
  hazard) and re-issues the question.
- `src/components/ErrorBubble.vue` (new) — distinct red-bordered alert
  with a category-specific heading, plain (non-markdown) message,
  Retry button, and Details disclosure showing category, status, and
  cause for the technical user.
- `src/components/ChatMessage.vue` branches on `message.kind`.

### PWA shell (Unit 2)
- `index.html` got the manifest link, `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-status-bar-style="black-translucent"` (the only
  value that lets the page extend under the status bar — required for
  `safe-area-inset-top > 0`), `apple-touch-icon`, and `theme-color`
  (light + dark).
- `public/manifest.webmanifest` declares display=standalone, start_url,
  icons.
- `public/icon-192.png`, `icon-512.png`, `apple-touch-icon-180.png` —
  placeholder PNGs (dark navy with white "SRE" wordmark in a 5×7
  bitmap font), generated reproducibly by
  `scripts/gen-placeholder-icons.mjs` using only Node's built-in
  `zlib`. Swap in real artwork by replacing the files.
- `src/style.css` safe-area helpers refactored to read CSS-variable
  insets with `env()` fallback (enables Playwright simulation).

### Composer multi-line (Unit 3)
- `src/components/ChatWindow.vue` `handleKeydown` is now Slack-style:
  - `(pointer: coarse)` → Enter inserts newline; Send button submits.
  - `(pointer: fine)` → Enter submits, Shift+Enter inserts newline.
  - Cmd/Ctrl+Enter submits on every platform.
- `enterkeyhint="send"` on the textarea relabels the iOS Return key.

### Markdown table strategy (Unit 4)
- `src/lib/markdown.ts` post-processes the rendered HTML before
  DOMPurify: every `<table>` is wrapped in
  `<div class="md-table-wrap" role="region" tabindex="0" aria-label="Table" data-cols="N">`,
  and every `<td>` gets `data-label="<header text>"` for the stacked-
  row reflow. DOMPurify config allows `data-cols` and `data-label`.
- `src/style.css`:
  - `.markdown { min-width: 0 }` is the actual fix for the bubble-
    bursting bug. Flex children default to `min-width: auto` (intrinsic
    content width); a wide table then forces the bubble past
    `max-w-[85%]`. Setting `min-width: 0` caps the bubble at its width
    budget; overflow ends up *inside* the markdown container where
    `.md-table-wrap` and `.markdown pre` handle it cleanly.
  - `.md-table-wrap` provides `overflow-x: auto`, focus ring, and
    `width: max-content` on the inner table so long-URL columns scroll
    horizontally instead of squeezing every other cell.
  - `@media (max-width: 480px)` reflow for `data-cols="1"` and
    `data-cols="2"`: stacked rows with `::before { content: attr(data-label) }`
    labels, thead visually hidden but accessible.
  - List markers restored (`list-style: disc / decimal`) — Tailwind v4's
    preflight had silently stripped them. Visible on both desktop and
    mobile in past LLM responses.

### Layout simplification (Unit 5)
- `src/App.vue` — dropped the `--vvh` JS observer that drove a CSS
  variable from `window.visualViewport`. Replaced with `100dvh` on the
  root container. `dvh` reached Baseline Widely Available in 2025 and
  shrinks correctly when the iOS keyboard opens.

### Tooling (Unit 7, partial)
- `dompurify` bumped 3.3.3 → 3.4.x (the most common minor in
  `npm install`'s registry resolution at the time).
- ESLint flat-config setup is **deferred** — the diff is large enough
  already, and adding a linter would force whole-file restyling that
  obscures the intent of this PR. Filed mentally as the next small PR.

### Docs (Unit 8)
- `README.md` — Primary use case (installed iOS PWA in portrait) and
  Install on iPhone steps.
- `CLAUDE.md` — overhauled the iOS / mobile section: PWA shell rules,
  CSS-variable safe-area indirection, dropped `--vvh`, composer key
  rules, `min-width: 0` rule, table wrap.
- `docs/architecture.md` — PWA shell, Mobile / iOS layout, cross-engine
  testing, streaming resilience, markdown rendering on narrow viewports.
- `docs/api-integration.md` — full error category table, resilience
  contract, implementation locations.
- `docs/development.md` — cross-browser projects, composer key handling
  table, mobile testing checklist for real-device verification.

## Code-review pass

Independent review caught two real issues, both fixed:

1. **`retryMessage` backward scan** — the old loop walked from
   `messageIndex - 1` to 0 looking for the first matching user bubble.
   If the user had previously asked the same question, the scan would
   find that historical occurrence and splice everything between it and
   the error, wiping intermediate turns. Replaced with a direct check on
   `messageIndex - 1`. Added a regression test
   (`error-resilience.spec.ts › retry only removes the immediately-
   preceding user bubble`) covering the same-question-twice case.
2. **`role="alert"` + `aria-live="polite"`** on `ErrorBubble.vue` was
   contradictory. `role="alert"` already implies
   `aria-live="assertive"`. Removed the explicit `aria-live`.

Status-bar style choice (`black-translucent`) flagged as worth verifying
on a real iPhone in dark mode. Comment in `index.html` documents the
trade-off — the alternatives reserve a strip and break safe-area, which
is the worse failure mode.

## Test results

`npx playwright test` — **194 passed, 4 skipped** across
`desktop-chromium`, `desktop-webkit`, `mobile-safari`, `mobile-chrome`.
Production build clean (`vue-tsc -b && vite build`).

## Real-device verification needed

Some bugs only manifest on a real iPhone PWA install. Before declaring
the user-reported issues fixed:

1. Add to Home Screen on the user's iPhone, launch the home-screen
   icon, confirm standalone mode (no Safari chrome).
2. Verify the title clears the status bar / Dynamic Island.
3. Type a multi-line bullet list in the composer; confirm Enter inserts
   newlines and the Send button delivers.
4. Ask the agent for a wide table (e.g. `Show recent backup tasks`);
   confirm the table stays inside the bubble's gray background and
   scrolls horizontally.
5. Ask for a 2-column comparison; confirm it reflows into stacked rows.
6. Send a long question and switch from Wi-Fi to cellular while it's
   streaming; confirm either silent recovery (answer arrives anyway)
   or a `Connection dropped mid-reply` ErrorBubble with a Retry that
   recovers cleanly.
7. Check status bar icon legibility in light + dark theme.

Checklist also documented in `docs/development.md` § "Mobile testing
checklist".

## Out of scope (filed for follow-up)

- ESLint flat config + eslint-plugin-vue.
- Real PWA icons (artwork to replace the placeholder 5×7 wordmark).
- `marked` 17 → 18 bump (renderer API breaking change; not worth
  bundling here).
- `postcss` advisory — transitive dev-only dep flagged by `npm audit`;
  resolves with `npm audit fix` but worth doing under its own PR.
