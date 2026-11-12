# API integration

The frontend talks to the FastAPI backend
([johnmathews/sre-agent](https://github.com/johnmathews/sre-agent))
through the `/api/*` prefix. In development Vite proxies `/api` to
`http://localhost:8000`; in production, nginx/Traefik does the same rewrite.
Frontend code only ever calls relative URLs — it never knows the backend's
real origin.

## Endpoints used

| Method | Path                           | Purpose                         |
|--------|--------------------------------|---------------------------------|
| GET    | `/health`                      | Health panel                    |
| POST   | `/ask/stream`                  | Chat (Server-Sent Events)       |
| GET    | `/conversations`               | Sidebar list                    |
| GET    | `/conversations/{id}`          | Load a past conversation        |
| PATCH  | `/conversations/{id}`          | Rename                          |
| DELETE | `/conversations/{id}`          | Delete                          |
| GET    | `/conversations/search?q=...`  | Full-text search                |

All JSON payloads match the Pydantic models in
`src/api/main.py` of the backend repo. See `src/api/*.ts` for the
corresponding TypeScript types.

## Streaming (SSE over POST)

The chat interaction is Server-Sent Events. The protocol on the wire:

```
Content-Type: text/event-stream

data: {"type": "status", "content": "Thinking..."}\n\n
data: {"type": "tool_start", "content": "prometheus_query"}\n\n
data: {"type": "tool_end", "content": "prometheus_query"}\n\n
data: {"type": "answer", "content": "CPU is at 73%...", "session_id": "abc12345"}\n\n
```

Each event is `data: <json>\n\n` — two newlines terminate the event. The
server holds the connection open until it finishes or errors.

### Event types

| Type         | Content                              | UI effect                          |
|--------------|--------------------------------------|------------------------------------|
| `heartbeat`  | empty                                | ignored (keep-alive)               |
| `status`     | transient status line                | shown in `ToolProgress` as ⏳      |
| `tool_start` | tool name                            | shown in `ToolProgress` as ⏳      |
| `tool_end`   | tool name                            | appended to completed list (✓)     |
| `answer`     | final answer text (may include `session_id`) | terminal: appended to messages |
| `error`      | error message                        | terminal: appended as error bubble |

### Error event fields

An `error` event may carry, in addition to `content`:

- `reason` — machine-readable failure code. Known values: `llm_auth_failed`
  (LLM credential rejected — operator must re-authenticate), `agent_no_answer`
  (generic; retry is usually safe). Unknown/absent ⇒ frontend shows generic copy.
- `detail` — raw underlying cause, shown in the ErrorBubble "Details" disclosure.

When `reason` is present, `content` is the operator-facing message and is shown
as the bubble explanation. When absent (older backend), `content` is treated as
the raw cause and shown behind Details, preserving prior behavior.

### Why `fetch` + `ReadableStream` instead of `EventSource`?

`EventSource` (the browser's built-in SSE client) is **GET-only**. The backend
accepts `POST /ask/stream` with a JSON body (`question`, optional
`session_id`, and optional `user_timezone` — see "Device timezone" below),
so `EventSource` is not usable. Instead we:

1. Open a `fetch` POST request.
2. Read `response.body.getReader()` — a `ReadableStream` of `Uint8Array` chunks.
3. Decode to text and buffer across chunks.
4. Split on `\n\n` (event delimiter).
5. For each event block, find `data: ` lines and `JSON.parse` the payload.

This is implemented as an async generator in `src/api/stream.ts`:

```ts
for await (const event of streamAsk({ question, session_id }, signal)) {
  // react to event.type
}
```

The caller passes an `AbortSignal` to cancel the stream (the "Stop" button
in `ChatWindow.vue` calls `chat.abort()`, which aborts the underlying fetch).

### Buffering behavior

The parser handles three cases:

- **Multiple events per read** — the read returns several events glued
  together. We loop while `buffer.indexOf('\n\n') !== -1`.
- **Event split across reads** — the delimiter is partway through a chunk.
  The remaining bytes stay in `buffer` until the next read.
- **Heartbeats** — the backend emits `{type: "heartbeat"}` every 15 s so
  Cloudflare's 100 s idle timeout doesn't close the connection. The chat
  store's `handleEvent` switches on `heartbeat` and returns immediately.

## Device timezone

`streamAsk` automatically reads the device's IANA timezone via
`Intl.DateTimeFormat().resolvedOptions().timeZone` (helper in
`src/api/timezone.ts`) and includes it in the request body as
`user_timezone`. The backend uses this to render "now" — and the
`get_current_time` tool — in the user's local clock.

The value is read **fresh on every request**, not cached, so a user who
changes their device's timezone (Settings → General → Date & Time on iOS;
the OS picks the IANA name from "Time Zone Automatically" if enabled) gets
the new zone on their next message without needing a page reload.

If the browser does not expose `Intl` (vanishingly rare on modern devices),
the field is omitted and the backend falls back to `USER_TIMEZONE` env var.

The user can also pass `user_timezone` explicitly to `streamAsk` to override
the device tz — useful in tests, but unused in production code paths today.

## Error handling

The frontend distinguishes failure modes so it can pick the right UX
(silent retry, conversation recovery, or surface to the user with a Retry
button). All classifications live in `src/api/stream.ts` as
`StreamErrorCategory`.

### Error categories and contracts

| Category | Trigger | Auto-retry? | Recovery? | UX |
|---|---|---|---|---|
| `network-before-headers` | `fetch()` rejects (DNS, TLS, offline) | 1× with 1s backoff | — | If still failing: ErrorBubble "Couldn't reach the agent" + Retry |
| `network-midstream` | `reader.read()` rejects after some bytes; clean stream close with no `answer` event | 1× **after** persisted-answer recovery attempt | Yes — `GET /conversations/:session_id` and surface persisted assistant turn if found | ErrorBubble "Connection dropped mid-reply" + Retry |
| `http-4xx` | Status 400-499 from `/ask/stream` | No | — | ErrorBubble with status; 401 → "Session expired"; 429 → "Rate limited" |
| `http-5xx` | Status 500+ from `/ask/stream` | 1× with 1s backoff | — | ErrorBubble "Agent unavailable (HTTP 5xx)" + Retry |
| `sse-error-event` | Backend emits `{type: "error"}` | No | — | ErrorBubble "Agent reported an error" + Retry; cause shown under Details |
| `no-body` | 200 OK but `res.body` is null (proxy bug) | No | — | ErrorBubble "Empty response from agent" |
| `aborted` | User pressed Stop | No | — | No bubble; turn left in place |

The classification distinguishes Safari's `TypeError: Load failed` between
`network-before-headers` and `network-midstream` based on whether any bytes
were received before the failure — they have different retry strategies
(plain retry vs. persisted-answer-recovery-then-retry).

### Resilience contract

After any transient failure, the client MUST attempt
`GET /conversations/:session_id` before showing an error to the user. The
backend persists the assistant turn at the end of the stream's
server-side processing, regardless of whether the client received the
bytes. Recovering a persisted answer keeps the conversation coherent
across iOS network handoffs, Cloudflare 100s idle timeouts, and similar
mid-flight interruptions.

### Layered resilience model — why it's designed this way

A worked example: the iOS network handoff that motivated this layer.
The user's iPhone switched IPv6 prefixes mid-stream during a 63-second
agent reply. The backend completed and persisted the 6,285-byte answer;
the client's `fetch()` socket died and surfaced
`TypeError: Load failed`. The layered response below catches that case
silently — and degrades gracefully to user-actionable failure for
anything it can't recover.

1. **Classification, not generalisation.** `streamAsk` distinguishes
   `network-before-headers` (fetch rejected before any byte) from
   `network-midstream` (rejected after some bytes) by tracking
   `receivedAnyBytes` across the reader loop. They have different
   recovery strategies — only midstream is worth checking the
   conversations endpoint for, because the server only starts
   responding after it has accepted the request. Mixing them would
   waste a recovery call on every offline-tap.

2. **Recovery before retry.** On `network-midstream` the wrapper
   *first* calls `GET /conversations/:session_id` and looks for an
   assistant turn whose preceding user turn matches the question we
   just asked. If found, we synthesise an `answer` event from the
   persisted text and surface it as if the stream had completed
   normally. This is what makes the iOS-handoff case end silently
   instead of with an error — the answer was already on the server,
   we just needed to fetch it. Crucially, recovery is *cheaper* than
   retry: no LLM call, no token spend, no agent rerun.

3. **Retry as fallback.** If recovery turns up no match (the
   disconnect happened before the backend finished persisting), we
   retry the question once with a 1s backoff. `network-*` and
   `http-5xx` are retried; `http-4xx`, `aborted`, and `sse-error-event`
   are not — those are deterministic outcomes where retrying without
   changes is wasteful.

4. **Actionable failure surface.** When everything above is exhausted,
   we render `ErrorBubble.vue` instead of a fake assistant bubble.
   Category-specific heading, copy explaining what's likely wrong, an
   inline Retry button that re-issues the same question (and removes
   the failed bubble + duplicate user bubble so the conversation
   reads cleanly post-recovery), and a Details disclosure for the
   technical user.

### Trade-offs deliberately accepted

1. **Transport is still POST + `ReadableStream`.** A truly
   handoff-proof design would use SSE with `Last-Event-ID` resumption
   or chunked downloads with `Range` headers — both require backend
   changes beyond the resilience layer's scope and a bigger protocol
   commitment than the value justified.

2. **`navigator.onLine` is not wired up.** It's unreliable on iOS
   PWAs across Wi-Fi↔cellular handoffs (the failure mode we care
   most about). Recovery + retry covers the same ground more
   reliably than acting on a flaky online/offline event.

3. **The retry path can double-charge if the backend hadn't
   persisted yet.** If the disconnect lands between "agent finished
   computing" and "answer written to DB", recovery returns no match
   and the retry re-issues the question, costing a second LLM call.
   There's no way around this without the backend signalling
   "answer-ready" before closing the socket. Acceptable trade for a
   rare narrow window.

4. **Same-question deduplication is positional, not content-based.**
   `retryMessage` removes the immediately-preceding user bubble, not
   "the most recent user bubble whose content matches" — the latter
   could splice intermediate history if the same question was asked
   earlier. Tested by `tests/e2e/error-resilience.spec.ts` ›
   "retry only removes the immediately-preceding user bubble".

### Implementation locations

- `src/api/stream.ts` — `StreamError` class, low-level `streamAsk` generator.
- `src/api/streamWithRecovery.ts` — wraps `streamAsk` with retry + recovery.
- `src/stores/chat.ts` — converts errors into `ChatMessage{kind:'error'}`
  entries with `originalQuestion` so the inline Retry button can re-issue.
- `src/components/ErrorBubble.vue` — distinct red-bordered alert with
  Retry, Details disclosure, and category-specific copy.
- `src/api/client.ts` — `apiJson()` adds a 30s default timeout and wraps
  network `TypeError`s as `NetworkError`. Pass `timeoutMs: null` to
  disable the timeout for long-running calls (the streaming layer manages
  its own).
