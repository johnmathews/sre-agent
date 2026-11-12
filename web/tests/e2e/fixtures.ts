// Shared test helpers for mocking the FastAPI backend.
//
// All Playwright specs use these to intercept /api/* and return canned
// payloads that match the Pydantic models in homelab-sre/src/api/main.py.

import type { Page, Route } from '@playwright/test'

// ---- canned payloads ----

export const sampleHealth = {
  status: 'healthy' as const,
  model: 'claude-sonnet-4-5',
  components: [
    { name: 'prometheus', status: 'healthy', detail: null },
    { name: 'grafana', status: 'healthy', detail: null },
    { name: 'loki', status: 'healthy', detail: null },
    { name: 'vector_store', status: 'healthy', detail: null },
  ],
}

export const degradedHealth = {
  status: 'degraded' as const,
  model: 'claude-sonnet-4-5',
  components: [
    { name: 'prometheus', status: 'healthy', detail: null },
    { name: 'grafana', status: 'unhealthy', detail: 'HTTP 503' },
    { name: 'loki', status: 'healthy', detail: null },
  ],
}

export const sampleConversations = [
  {
    session_id: 'abc12345',
    title: 'CPU spike investigation',
    created_at: '2026-04-04T10:00:00+00:00',
    updated_at: '2026-04-04T10:05:00+00:00',
    turn_count: 3,
    model: 'claude-sonnet-4-5',
    provider: 'anthropic',
  },
  {
    session_id: 'def67890',
    title: 'Disk usage check',
    created_at: '2026-04-03T14:00:00+00:00',
    updated_at: '2026-04-03T14:02:00+00:00',
    turn_count: 2,
    model: 'claude-sonnet-4-5',
    provider: 'anthropic',
  },
]

export function sampleConversationDetail(sessionId: string) {
  const summary = sampleConversations.find((c) => c.session_id === sessionId)
  if (!summary) {
    throw new Error(`no fixture for ${sessionId}`)
  }
  return {
    ...summary,
    turns: [
      { role: 'user', content: 'What is the current CPU usage?' },
      { role: 'assistant', content: 'CPU usage is at **73%** — normal for this hour.' },
    ],
  }
}

// ---- SSE helpers ----

export interface SseEvent {
  type: string
  content: string
  session_id?: string
  reason?: string
  detail?: string
}

/** Build an SSE body from a list of events. */
export function buildSseBody(events: SseEvent[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('')
}

/** A canned successful streaming response. */
export const sampleStreamEvents: SseEvent[] = [
  { type: 'status', content: 'Thinking...' },
  { type: 'tool_start', content: 'prometheus_query' },
  { type: 'tool_end', content: 'prometheus_query' },
  { type: 'answer', content: 'CPU is at **42%**.', session_id: 'new00001' },
]

// ---- wiring ----

interface MockState {
  conversations: typeof sampleConversations
  deleted: Set<string>
  renamed: Map<string, string>
}

interface MockOptions {
  health?: typeof sampleHealth | typeof degradedHealth
  conversations?: typeof sampleConversations
  streamEvents?: SseEvent[]
  streamBody?: string // escape hatch — takes precedence over streamEvents
}

/**
 * Install `page.route` handlers that cover every endpoint the frontend calls.
 * Returns the underlying mutable state so tests can assert on it (e.g.,
 * verify a DELETE was received).
 */
export async function mockBackend(
  page: Page,
  options: MockOptions = {},
): Promise<MockState> {
  const state: MockState = {
    conversations: [...(options.conversations ?? sampleConversations)],
    deleted: new Set(),
    renamed: new Map(),
  }

  const healthPayload = options.health ?? sampleHealth
  const streamBody =
    options.streamBody ??
    buildSseBody(options.streamEvents ?? sampleStreamEvents)

  await page.route('**/api/health', (route) => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(healthPayload),
    })
  })

  await page.route('**/api/conversations', (route) => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        state.conversations.filter((c) => !state.deleted.has(c.session_id)),
      ),
    })
  })

  await page.route('**/api/conversations/*', (route: Route) => {
    const url = new URL(route.request().url())
    const sessionId = decodeURIComponent(url.pathname.split('/').pop() ?? '')
    const method = route.request().method()

    if (method === 'GET') {
      if (state.deleted.has(sessionId)) {
        return route.fulfill({ status: 404, body: 'Not found' })
      }
      try {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(sampleConversationDetail(sessionId)),
        })
      } catch {
        return route.fulfill({ status: 404, body: 'Not found' })
      }
    }

    if (method === 'DELETE') {
      state.deleted.add(sessionId)
      return route.fulfill({ status: 204, body: '' })
    }

    if (method === 'PATCH') {
      const body = route.request().postDataJSON() as { title: string }
      state.renamed.set(sessionId, body.title)
      const original = state.conversations.find(
        (c) => c.session_id === sessionId,
      )
      if (!original) {
        return route.fulfill({ status: 404, body: 'Not found' })
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...original,
          title: body.title,
          turns: sampleConversationDetail(sessionId).turns,
        }),
      })
    }

    return route.fulfill({ status: 405, body: 'Method not allowed' })
  })

  // Register search AFTER conversations/* so it takes priority (Playwright: last match wins)
  await page.route('**/api/conversations/search*', (route: Route) => {
    const url = new URL(route.request().url())
    const q = (url.searchParams.get('q') ?? '').toLowerCase()
    const matches = state.conversations
      .filter((c) => !state.deleted.has(c.session_id))
      .filter((c) => c.title.toLowerCase().includes(q))
      .map((c) => ({
        ...c,
        matches: [{ role: 'title', snippet: c.title }],
      }))
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(matches),
    })
  })

  await page.route('**/api/ask/stream', (route) => {
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: {
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
      body: streamBody,
    })
  })

  return state
}

// ---- per-route stream-failure overrides ----
//
// `mockBackend` installs a single happy-path stream handler. These helpers
// re-register the `/api/ask/stream` route with a specific failure mode for a
// fixed number of requests. The browser-level override has higher priority
// than the one installed by mockBackend (Playwright: last route registered
// wins). Combine with `appendBackendConversation` to recreate the
// "server persisted but client missed" recovery case.

export type StreamFailureKind =
  | 'network-before-headers' // fetch rejects before any byte (DNS / TLS / abort('failed'))
  | 'http-500'
  | 'http-503'
  | 'http-401'
  | 'midstream-abort' // some events delivered, then connection closed
  | 'corrupt-json' // SSE chunk with malformed payload, no answer event
  | 'sse-error-event' // backend emits {type:'error'} cleanly

export interface StreamFailureOptions {
  kind: StreamFailureKind
  /** Events to send before the midstream-abort kicks in. Ignored for other kinds. */
  preAbortEvents?: SseEvent[]
  /** Custom message for sse-error-event. */
  errorMessage?: string
  /**
   * Apply this failure to the next N requests, then fall through to the
   * happy-path body installed by `mockBackend`. Default: 1.
   */
  times?: number
}

/**
 * Override `/api/ask/stream` for the next `times` requests with a specific
 * failure mode. After that the original `mockBackend` handler resumes.
 */
export async function overrideAskStream(
  page: Page,
  options: StreamFailureOptions,
): Promise<void> {
  const limit = options.times ?? 1
  let count = 0
  await page.route('**/api/ask/stream', async (route) => {
    if (count >= limit) {
      // fall through to the next-registered handler (the happy-path one)
      return route.fallback()
    }
    count += 1
    switch (options.kind) {
      case 'network-before-headers':
        return route.abort('failed')
      case 'http-500':
        return route.fulfill({ status: 500, body: 'Internal Server Error' })
      case 'http-503':
        return route.fulfill({ status: 503, body: 'Service Unavailable' })
      case 'http-401':
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Session expired' }),
        })
      case 'midstream-abort': {
        // Send some events, then close the body without a final \n\n.
        // Playwright's route.fulfill closes cleanly, so we use an explicit
        // truncated body — the SSE parser will be mid-frame when the read
        // ends, which the client must treat as a transient failure.
        const events = options.preAbortEvents ?? [
          { type: 'status', content: 'Thinking...' },
        ]
        const partial = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('')
        // Append a half-written event (no trailing \n\n) so the parser
        // discards it and sees no answer event.
        return route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: partial + 'data: {"type":"answ',
        })
      }
      case 'corrupt-json':
        return route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: 'data: {bad json}\n\ndata: {"type":"status","content":"x"\n\n',
        })
      case 'sse-error-event':
        return route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody([
            { type: 'status', content: 'Thinking...' },
            {
              type: 'error',
              content: options.errorMessage ?? 'Backend exploded',
            },
          ]),
        })
    }
  })
}

/**
 * Pretend the backend persisted a successful conversation that the client
 * missed mid-stream — used by recovery tests. After registering the override,
 * the recovery path's `GET /conversations/:session_id` lookup will see the
 * answer the client never received.
 */
export function appendBackendConversation(
  state: MockState,
  args: {
    session_id: string
    title: string
    userQuestion: string
    assistantAnswer: string
  },
): void {
  state.conversations.unshift({
    session_id: args.session_id,
    title: args.title,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    turn_count: 2,
    model: 'claude-sonnet-4-5',
    provider: 'anthropic',
  })
  // The mockBackend GET handler reads from `sampleConversationDetail`. Patch
  // it for this session id.
  recoveryDetail.set(args.session_id, {
    user: args.userQuestion,
    assistant: args.assistantAnswer,
  })
}

const recoveryDetail = new Map<string, { user: string; assistant: string }>()

export interface RecoveryRouteOptions {
  /**
   * If set, ANY GET /api/conversations/:id returns a fabricated conversation
   * with these turns. Useful when the client generates a random session id
   * the test doesn't know in advance — the recovery path will find this
   * persisted "answer" for whatever id it queries.
   */
  matchAny?: { userQuestion: string; assistantAnswer: string }
}

/**
 * Augment `mockBackend`'s GET /api/conversations/:id handler with
 * recovery-mode fixtures registered via `appendBackendConversation`, OR — if
 * `matchAny` is set — return a fabricated answer for every session id.
 *
 * Call this AFTER `mockBackend` so it registers later and wins (Playwright:
 * last route registered wins). The fallback still flows through to the
 * mockBackend handler when this one declines.
 */
export async function installRecoveryRoute(
  page: Page,
  options: RecoveryRouteOptions = {},
): Promise<void> {
  await page.route('**/api/conversations/*', (route) => {
    const url = new URL(route.request().url())
    const sessionId = decodeURIComponent(url.pathname.split('/').pop() ?? '')
    if (route.request().method() !== 'GET') {
      return route.fallback()
    }
    if (options.matchAny) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: sessionId,
          title: options.matchAny.userQuestion.slice(0, 40),
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          turn_count: 2,
          model: 'claude-sonnet-4-5',
          provider: 'anthropic',
          turns: [
            { role: 'user', content: options.matchAny.userQuestion },
            { role: 'assistant', content: options.matchAny.assistantAnswer },
          ],
        }),
      })
    }
    const detail = recoveryDetail.get(sessionId)
    if (!detail) {
      return route.fallback()
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: sessionId,
        title: detail.user.slice(0, 40),
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        turn_count: 2,
        model: 'claude-sonnet-4-5',
        provider: 'anthropic',
        turns: [
          { role: 'user', content: detail.user },
          { role: 'assistant', content: detail.assistant },
        ],
      }),
    })
  })
}

/** Reset cross-test recovery state. Call in `test.beforeEach`. */
export function resetRecoveryFixtures(): void {
  recoveryDetail.clear()
}
