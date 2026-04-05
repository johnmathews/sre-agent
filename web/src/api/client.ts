// Base API client. All frontend fetches go through /api/* which Vite (dev)
// proxies to the backend; in production nginx/Traefik handles the same rewrite.
// Keep this file tiny — it's the only place that knows about the base URL.

export const API_BASE = '/api'

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

type JsonBody = Record<string, unknown> | unknown[] | null

type JsonRequestInit = Omit<RequestInit, 'body'> & { body?: JsonBody }

export async function apiJson<T>(
  path: string,
  init: JsonRequestInit = {},
): Promise<T> {
  const { body, headers, ...rest } = init
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

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
