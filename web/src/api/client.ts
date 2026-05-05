// Base API client. All frontend fetches go through /api/* which Vite (dev)
// proxies to the backend; in production nginx/Traefik handles the same rewrite.
// Keep this file tiny — it's the only place that knows about the base URL.

export const API_BASE = '/api'

/** Default timeout for one-shot JSON requests. Streaming uses its own. */
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000

export class ApiError extends Error {
  status: number
  body?: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/**
 * Network-layer failure (DNS, TLS, connection drop, abort) before any HTTP
 * status was received. Distinct from `ApiError` so callers can decide between
 * retry-and-pray (network) and fix-the-request (HTTP).
 */
export class NetworkError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(
      message,
      options?.cause !== undefined ? { cause: options.cause } : undefined,
    )
    this.name = 'NetworkError'
  }
}

type JsonBody = Record<string, unknown> | unknown[] | null

type JsonRequestInit = Omit<RequestInit, 'body'> & {
  body?: JsonBody
  /**
   * Override the default 30s timeout. Pass `null` to disable timeout
   * entirely (used by long-running streams that manage their own lifecycle).
   */
  timeoutMs?: number | null
}

export async function apiJson<T>(
  path: string,
  init: JsonRequestInit = {},
): Promise<T> {
  const { body, headers, signal: callerSignal, timeoutMs, ...rest } = init

  const timeout = timeoutMs === undefined ? DEFAULT_REQUEST_TIMEOUT_MS : timeoutMs
  const signal = mergeAbortSignals(callerSignal ?? null, timeout)

  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      // Re-throw — caller distinguishes user abort from timeout via signal state
      throw err
    }
    // TypeError from fetch = network-layer failure. Wrap it so callers can
    // pattern-match against NetworkError instead of guessing at TypeError.
    if (err instanceof TypeError) {
      throw new NetworkError(err.message || 'Network error', { cause: err })
    }
    throw err
  }

  if (!res.ok) {
    let detail: unknown = undefined
    try {
      detail = await res.json()
    } catch {
      // body wasn't JSON
    }
    throw new ApiError(res.status, `HTTP ${res.status} on ${path}`, detail)
  }

  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

/**
 * Combine the caller's optional AbortSignal with a timeout-driven signal so
 * either can cancel the fetch. Uses `AbortSignal.any` when the runtime
 * supports it (Chrome 116+, Safari 17.4+, Node 20+) and falls back to a
 * manual implementation for older environments.
 */
function mergeAbortSignals(
  caller: AbortSignal | null,
  timeoutMs: number | null,
): AbortSignal | undefined {
  if (timeoutMs === null && caller === null) return undefined
  const signals: AbortSignal[] = []
  if (caller) signals.push(caller)
  if (timeoutMs !== null) signals.push(AbortSignal.timeout(timeoutMs))
  if (signals.length === 1) return signals[0]
  type AnyFn = (signals: AbortSignal[]) => AbortSignal
  const any = (AbortSignal as unknown as { any?: AnyFn }).any
  if (typeof any === 'function') {
    return any(signals)
  }
  const ac = new AbortController()
  for (const s of signals) {
    if (s.aborted) {
      ac.abort(s.reason)
      break
    }
    s.addEventListener('abort', () => ac.abort(s.reason), { once: true })
  }
  return ac.signal
}
