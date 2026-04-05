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

export async function listConversations(): Promise<ConversationSummary[]> {
  return apiJson<ConversationSummary[]>('/conversations')
}

export async function getConversation(
  sessionId: string,
): Promise<ConversationDetail> {
  return apiJson<ConversationDetail>(
    `/conversations/${encodeURIComponent(sessionId)}`,
  )
}

export async function deleteConversation(sessionId: string): Promise<void> {
  return apiJson<void>(`/conversations/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

export async function renameConversation(
  sessionId: string,
  title: string,
): Promise<ConversationDetail> {
  return apiJson<ConversationDetail>(
    `/conversations/${encodeURIComponent(sessionId)}`,
    {
      method: 'PATCH',
      body: { title },
    },
  )
}
