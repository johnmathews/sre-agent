import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  deleteConversation,
  listConversations,
  renameConversation,
  type ConversationSummary,
} from '../api/conversations'

export const useConversationsStore = defineStore('conversations', () => {
  const items = ref<ConversationSummary[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function refresh(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      items.value = await listConversations()
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Failed to load'
    } finally {
      loading.value = false
    }
  }

  async function remove(sessionId: string): Promise<boolean> {
    try {
      await deleteConversation(sessionId)
      items.value = items.value.filter((c) => c.session_id !== sessionId)
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Delete failed'
      return false
    }
  }

  async function rename(sessionId: string, title: string): Promise<boolean> {
    try {
      const updated = await renameConversation(sessionId, title)
      const idx = items.value.findIndex((c) => c.session_id === sessionId)
      if (idx !== -1) {
        items.value[idx] = {
          session_id: updated.session_id,
          title: updated.title,
          created_at: updated.created_at,
          updated_at: updated.updated_at,
          turn_count: updated.turn_count,
          model: updated.model,
          provider: updated.provider,
        }
      }
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Rename failed'
      return false
    }
  }

  return { items, loading, error, refresh, remove, rename }
})
