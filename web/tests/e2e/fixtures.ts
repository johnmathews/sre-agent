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
