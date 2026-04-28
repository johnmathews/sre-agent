// SSE (Server-Sent Events) parser over a POST request.
//
// Why fetch + manual parse instead of EventSource?
//   EventSource only supports GET. The backend uses POST /ask/stream so that
//   the question + session_id travel in the request body. We therefore open a
//   POST fetch, read the response body as a ReadableStream, and hand-parse the
//   SSE wire format: each event is `data: <json>\n\n` (two newlines end it).
//
// Backend event types (see src/api/main.py in sre-agent):
//   - heartbeat   — keep-alive, ignore
//   - status      — transient status message
//   - tool_start  — tool name that is about to run
//   - tool_end    — tool name that just finished
//   - answer      — final answer text (may include a session_id)
//   - error       — error message

import { API_BASE } from './client'
import { getDeviceTimezone } from './timezone'

export type StreamEventType =
  | 'heartbeat'
  | 'status'
  | 'tool_start'
  | 'tool_end'
  | 'answer'
  | 'error'

export interface StreamEvent {
  type: StreamEventType
  content: string
  session_id?: string
}

export interface StreamRequest {
  question: string
  session_id?: string
  /**
   * Override the device timezone used for this request. Normally the helper
   * fills this in automatically from `Intl.DateTimeFormat()`; tests pass
   * explicit values to assert the wire payload.
   */
  user_timezone?: string
}

/**
 * POST /ask/stream and yield each decoded SSE event as it arrives.
 *
 * The caller supplies an AbortSignal to cancel the stream (e.g. if the user
 * navigates away or starts a new question). The device's IANA timezone is
 * attached automatically (read fresh per request so a travelling user gets
 * answers in the zone they're currently in) unless the caller passed one
 * explicitly.
 */
export async function* streamAsk(
  req: StreamRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent, void, void> {
  const body: StreamRequest = {
    ...req,
    user_timezone: req.user_timezone ?? getDeviceTimezone(),
  }
  // Don't send the field at all when it's undefined — keeps the wire shape
  // identical to today for clients that have no Intl support.
  if (body.user_timezone === undefined) {
    delete body.user_timezone
  }

  const res = await fetch(`${API_BASE}/ask/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })

  if (!res.ok) {
    let detail = ''
    try {
      detail = await res.text()
    } catch {
      // ignore
    }
    throw new Error(`HTTP ${res.status} on /ask/stream: ${detail}`)
  }
  if (!res.body) {
    throw new Error('Response has no body')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE events are terminated by a blank line (\n\n). There may be
      // multiple events in one read, or a single event may span reads.
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const rawEvent = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)

        const event = parseSseBlock(rawEvent)
        if (event) yield event

        sep = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/**
 * Parse a single SSE event block. An SSE event is one or more lines;
 * lines starting with `data: ` carry the payload. Multi-line data fields
 * should be concatenated with `\n`, but our backend only emits single-line
 * JSON payloads so we handle both cases defensively.
 */
function parseSseBlock(block: string): StreamEvent | null {
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('data:')) {
      // Strip "data:" and at most one leading space
      const v = line.slice(5)
      dataLines.push(v.startsWith(' ') ? v.slice(1) : v)
    }
    // Comment lines (starting with :) and unknown fields are ignored.
  }
  if (dataLines.length === 0) return null

  const payload = dataLines.join('\n')
  try {
    const parsed = JSON.parse(payload) as StreamEvent
    return parsed
  } catch {
    return null
  }
}
