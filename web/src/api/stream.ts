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
//
// Errors are surfaced as `StreamError` so callers can classify and route
// them — see `streamWithRecovery.ts` for the retry / recovery logic that
// builds on this generator.

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
 * Categories the recovery layer cares about. Each maps to a different UX
 * decision (auto-retry, conversation-recovery, or surface to the user).
 */
export type StreamErrorCategory =
  | 'network-before-headers' // fetch rejected before any byte received
  | 'network-midstream' // reader rejected after at least one chunk
  | 'http-4xx'
  | 'http-5xx'
  | 'no-body' // 200 OK but res.body was null
  | 'aborted' // user-initiated abort

export interface StreamErrorOptions {
  status?: number
  detail?: unknown
  cause?: unknown
}

/**
 * Classified error thrown by `streamAsk`. The recovery wrapper inspects
 * `category` to decide whether to retry, attempt conversation recovery, or
 * surface to the UI.
 */
export class StreamError extends Error {
  readonly category: StreamErrorCategory
  readonly status?: number
  readonly detail?: unknown

  constructor(
    category: StreamErrorCategory,
    message: string,
    options: StreamErrorOptions = {},
  ) {
    super(
      message,
      options.cause !== undefined ? { cause: options.cause } : undefined,
    )
    this.name = 'StreamError'
    this.category = category
    this.status = options.status
    this.detail = options.detail
  }

  /**
   * Map an arbitrary thrown value into a StreamError. `receivedAnyBytes`
   * disambiguates Safari's `TypeError: Load failed` between "fetch failed
   * before headers" (network-before-headers) and "stream interrupted
   * mid-flight" (network-midstream) — they differ in retry strategy.
   */
  static from(err: unknown, ctx: { receivedAnyBytes: boolean }): StreamError {
    if (err instanceof StreamError) return err
    if (err instanceof DOMException && err.name === 'AbortError') {
      return new StreamError('aborted', 'Request aborted', { cause: err })
    }
    // Safari surfaces all fetch failures (DNS, TLS, mid-stream socket close,
    // CORS) as `TypeError: Load failed`. Chromium uses `TypeError: Failed to
    // fetch`. Both land here.
    if (err instanceof TypeError) {
      return new StreamError(
        ctx.receivedAnyBytes ? 'network-midstream' : 'network-before-headers',
        err.message || 'Network error',
        { cause: err },
      )
    }
    return new StreamError(
      ctx.receivedAnyBytes ? 'network-midstream' : 'network-before-headers',
      err instanceof Error ? err.message : String(err),
      { cause: err },
    )
  }
}

/**
 * POST /ask/stream and yield each decoded SSE event as it arrives.
 *
 * Throws `StreamError` (with a category) on any failure. The caller passes
 * an AbortSignal to cancel the stream (e.g. user pressed Stop or a new
 * question started). The device's IANA timezone is attached automatically
 * unless the caller supplies one.
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

  let res: Response
  try {
    res = await fetch(`${API_BASE}/ask/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    })
  } catch (err) {
    throw StreamError.from(err, { receivedAnyBytes: false })
  }

  if (!res.ok) {
    let detail: unknown
    try {
      const text = await res.text()
      try {
        detail = text ? JSON.parse(text) : undefined
      } catch {
        detail = text
      }
    } catch {
      // body unreadable — leave detail undefined
    }
    throw new StreamError(
      res.status >= 500 ? 'http-5xx' : 'http-4xx',
      `HTTP ${res.status} on /ask/stream`,
      { status: res.status, detail },
    )
  }
  if (!res.body) {
    throw new StreamError('no-body', 'Response has no body')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let receivedAnyBytes = false

  try {
    while (true) {
      let chunk: ReadableStreamReadResult<Uint8Array>
      try {
        chunk = await reader.read()
      } catch (err) {
        throw StreamError.from(err, { receivedAnyBytes })
      }
      if (chunk.done) break
      receivedAnyBytes = true
      buffer += decoder.decode(chunk.value, { stream: true })

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
 *
 * Malformed JSON is silently dropped here. The recovery layer detects the
 * higher-level "stream ended without an answer" condition and treats it as
 * transient — that's the right place to escalate, since a single bad chunk
 * in an otherwise-healthy stream shouldn't break the user's session.
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
