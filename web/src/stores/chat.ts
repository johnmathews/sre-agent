import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { streamAsk, type StreamEvent } from '../api/stream'
import { getConversation } from '../api/conversations'

export type MessageRole = 'user' | 'assistant'

export interface ChatMessage {
  role: MessageRole
  content: string
}

function newSessionId(): string {
  // Matches the backend's uuid4().hex[:8] style
  return Math.random().toString(16).slice(2, 10)
}

export const useChatStore = defineStore('chat', () => {
  // ---------- persistent state ----------
  const sessionId = ref<string>(newSessionId())
  const messages = ref<ChatMessage[]>([])

  // ---------- streaming state (ephemeral, per-request) ----------
  const isStreaming = ref(false)
  const currentStatus = ref<string>('') // transient "status" / "tool_start" event
  const completedTools = ref<string[]>([]) // finished tool names
  const streamError = ref<string | null>(null)

  let abortController: AbortController | null = null

  // ---------- getters ----------
  const hasMessages = computed(() => messages.value.length > 0)

  // ---------- actions ----------
  function startNewConversation(): void {
    abort()
    sessionId.value = newSessionId()
    messages.value = []
    resetStreamingState()
  }

  async function loadConversation(id: string): Promise<void> {
    abort()
    const detail = await getConversation(id)
    sessionId.value = detail.session_id
    messages.value = detail.turns.map((t) => ({
      role: (t.role === 'user' ? 'user' : 'assistant') as MessageRole,
      content: t.content,
    }))
    resetStreamingState()
  }

  function resetStreamingState(): void {
    isStreaming.value = false
    currentStatus.value = ''
    completedTools.value = []
    streamError.value = null
  }

  function abort(): void {
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    isStreaming.value = false
  }

  async function sendMessage(question: string): Promise<void> {
    const q = question.trim()
    if (!q || isStreaming.value) return

    messages.value.push({ role: 'user', content: q })
    resetStreamingState()
    isStreaming.value = true

    abortController = new AbortController()
    let answer: string | null = null

    try {
      for await (const event of streamAsk(
        { question: q, session_id: sessionId.value },
        abortController.signal,
      )) {
        handleEvent(event)
        if (event.type === 'answer') {
          answer = event.content
          if (event.session_id) sessionId.value = event.session_id
        } else if (event.type === 'error') {
          streamError.value = event.content || 'Unknown error'
        }
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        // User cancelled; silently stop.
        return
      }
      streamError.value =
        err instanceof Error ? err.message : 'Streaming failed'
    } finally {
      abortController = null
      isStreaming.value = false
      currentStatus.value = ''
    }

    if (answer !== null) {
      messages.value.push({ role: 'assistant', content: answer })
    } else if (streamError.value) {
      messages.value.push({
        role: 'assistant',
        content: `**Error:** ${streamError.value}`,
      })
    }
  }

  function handleEvent(event: StreamEvent): void {
    switch (event.type) {
      case 'heartbeat':
        return
      case 'status':
      case 'tool_start':
        currentStatus.value = event.content
        return
      case 'tool_end':
        completedTools.value.push(event.content)
        currentStatus.value = ''
        return
      case 'answer':
      case 'error':
        // Terminal events handled by the caller
        return
    }
  }

  return {
    // state
    sessionId,
    messages,
    isStreaming,
    currentStatus,
    completedTools,
    streamError,
    // getters
    hasMessages,
    // actions
    startNewConversation,
    loadConversation,
    sendMessage,
    abort,
  }
})
