// Resilience layer on top of `streamAsk`. Handles three things the raw
// generator does not:
//
//   1. Auto-retry on transient network failures (one retry, 1s backoff).
//   2. Conversation recovery — when a stream dies mid-flight, the backend
//      may have already persisted the answer. Before surfacing an error,
//      we GET /conversations/:session_id and check whether our question
//      was answered.
//   3. "Stream ended without an answer" detection — a clean close with no
//      `answer` event is treated as transient (retry once).
//
// The triage that motivated this layer: an iOS network handoff killed the
// streaming `fetch()` mid-response. The server had already persisted the
// answer; the client showed Safari's opaque "Load failed". This layer
// recovers that case automatically.

import { streamAsk, StreamError, type StreamEvent } from './stream'
import { getConversation } from './conversations'

export type RecoveryStatus = 'reconnecting' | 'recovering'

export interface StreamWithRecoveryRequest {
  question: string
  /** Required — the recovery path needs the session id to query for persisted state. */
  session_id: string
  user_timezone?: string
}

const RETRY_BACKOFF_MS = 1000

/**
 * Wraps `streamAsk` with retry + conversation recovery. Yields the same
 * `StreamEvent`s the underlying generator would, plus the wrapper resolves
 * the "missed answer" case by synthesising an `answer` event from the
 * persisted conversation.
 *
 * Throws `StreamError` only on permanent failures (after retry + recovery
 * have been exhausted, or for categories we never retry — `aborted`,
 * `http-4xx`, `sse-error-event` from the upstream).
 *
 * `onStatus` is called when the wrapper enters a transient recovery state
 * so the UI can show "Reconnecting…" or "Checking saved answer…" — keeps
 * the user oriented during the silent retry.
 */
export async function* streamWithRecovery(
  req: StreamWithRecoveryRequest,
  signal: AbortSignal,
  onStatus?: (status: RecoveryStatus) => void,
): AsyncGenerator<StreamEvent, void, void> {
  let attempt = 0
  // 2 attempts total: original + 1 retry. Mid-stream failures get a
  // recovery check before the retry so we don't double-charge the LLM if
  // the backend already finished.
  const maxAttempts = 2

  while (attempt < maxAttempts) {
    attempt += 1
    let yieldedAnswer = false
    let yieldedError = false
    try {
      for await (const event of streamAsk(req, signal)) {
        yield event
        if (event.type === 'answer') yieldedAnswer = true
        // The agent emitted a clean error event — terminal, but the upstream
        // chat store is responsible for surfacing it as an ErrorBubble. Don't
        // confuse this with a mid-stream failure.
        if (event.type === 'error') yieldedError = true
      }
      if (yieldedAnswer || yieldedError) return
      // Stream closed cleanly with neither an answer nor an error event.
      // Treat as transient and let the catch handler decide whether to
      // retry / recover / give up.
      throw new StreamError(
        'network-midstream',
        'Stream ended without an answer event',
      )
    } catch (err) {
      // `streamAsk` always throws `StreamError` already (with the correct
      // `receivedAnyBytes`-based classification baked in by
      // `StreamError.from` inside the reader loop), so the `instanceof`
      // branch is the path that matters in practice. The fallback
      // `StreamError.from(...)` here only fires if some non-StreamError
      // value bubbles up unexpectedly; we conservatively treat it as
      // "could be midstream" to keep the recovery path reachable.
      const se =
        err instanceof StreamError
          ? err
          : StreamError.from(err, { receivedAnyBytes: true })

      // Permanent — surface immediately.
      if (se.category === 'aborted' || se.category === 'http-4xx' || se.category === 'no-body') {
        throw se
      }

      // Mid-stream: try recovery before burning another LLM call.
      if (se.category === 'network-midstream') {
        onStatus?.('recovering')
        const recovered = await tryRecoverPersistedAnswer(req, signal)
        if (recovered) {
          yield recovered
          return
        }
      }

      // Network or 5xx: one retry with backoff.
      if (attempt < maxAttempts) {
        onStatus?.('reconnecting')
        await delay(RETRY_BACKOFF_MS, signal)
        continue
      }
      throw se
    }
  }
}

async function tryRecoverPersistedAnswer(
  req: StreamWithRecoveryRequest,
  signal: AbortSignal,
): Promise<StreamEvent | null> {
  try {
    const detail = await getConversation(req.session_id, signal)
    // Walk the persisted turns from the end. Find the most recent assistant
    // turn whose preceding user turn matches our question — that's the
    // answer the client missed.
    for (let i = detail.turns.length - 1; i >= 0; i--) {
      const t = detail.turns[i]
      if (t.role !== 'assistant') continue
      const prev = i > 0 ? detail.turns[i - 1] : null
      if (prev && prev.role === 'user' && prev.content === req.question) {
        return {
          type: 'answer',
          content: t.content,
          session_id: req.session_id,
        }
      }
    }
    return null
  } catch {
    // Recovery itself failed (404, network, abort) — caller will retry.
    return null
  }
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

export { StreamError } from './stream'
export type { StreamEvent } from './stream'
