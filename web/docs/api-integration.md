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
