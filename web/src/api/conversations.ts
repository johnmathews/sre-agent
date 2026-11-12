import { apiJson } from './client'

export interface ConversationSummary {
  session_id: string
  title: string
  created_at: string
  updated_at: string
  turn_count: number
  model: string
  provider: string
}

export interface ConversationTurn {
  role: string
  content: string
}

export interface ConversationDetail extends ConversationSummary {
  turns: ConversationTurn[]
}

export interface SearchMatch {
  role: string
  snippet: string
}

export interface ConversationSearchResult extends ConversationSummary {
  matches: SearchMatch[]
}

export async function listConversations(
  signal?: AbortSignal,
): Promise<ConversationSummary[]> {
  return apiJson<ConversationSummary[]>('/conversations', { signal })
}

export async function searchConversations(
  query: string,
  signal?: AbortSignal,
): Promise<ConversationSearchResult[]> {
  return apiJson<ConversationSearchResult[]>(
    `/conversations/search?q=${encodeURIComponent(query)}`,
    { signal },
  )
}

export async function getConversation(
  sessionId: string,
  signal?: AbortSignal,
): Promise<ConversationDetail> {
  return apiJson<ConversationDetail>(
    `/conversations/${encodeURIComponent(sessionId)}`,
    { signal },
  )
}

export async function deleteConversation(
  sessionId: string,
  signal?: AbortSignal,
): Promise<void> {
  return apiJson<void>(`/conversations/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    signal,
  })
}

export async function renameConversation(
  sessionId: string,
  title: string,
  signal?: AbortSignal,
): Promise<ConversationDetail> {
  return apiJson<ConversationDetail>(
    `/conversations/${encodeURIComponent(sessionId)}`,
    {
      method: 'PATCH',
      body: { title },
      signal,
    },
  )
}
