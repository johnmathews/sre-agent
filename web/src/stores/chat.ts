import { defineStore } from 'pinia'
import { computed, ref, triggerRef } from 'vue'
import { streamAsk, type StreamEvent } from '../api/stream'
import { getConversation, type ConversationSummary } from '../api/conversations'

export type MessageRole = 'user' | 'assistant'

export interface ChatMessage {
  role: MessageRole
  content: string
}

export interface CompletedTool {
  name: string
  duration: number // ms
}

/** Per-session streaming state. One entry per conversation that has been touched. */
export interface SessionStreamState {
  messages: ChatMessage[]
  isStreaming: boolean
  currentStatus: string
  completedTools: CompletedTool[]
  streamError: string | null
  abortController: AbortController | null
  toolStartTime: number // Date.now() when current tool started
}

function newSessionId(): string {
  // Matches the backend's uuid4().hex[:8] style
  return Math.random().toString(16).slice(2, 10)
}

function createEmptySession(): SessionStreamState {
  return {
    messages: [],
    isStreaming: false,
    currentStatus: '',
    completedTools: [],
    streamError: null,
    abortController: null,
    toolStartTime: 0,
  }
}

export const useChatStore = defineStore('chat', () => {
  // ---------- core state ----------
  // Map from sessionId → per-session streaming state.
  // Using shallowRef-like manual triggering: we call triggerRef(sessions)
  // after mutating Map entries so Vue picks up the change.
  const sessions = ref(new Map<string, SessionStreamState>())
  const activeSessionId = ref<string>(newSessionId())

  // ---------- helpers ----------
  function getOrCreateSession(id: string): SessionStreamState {
    let s = sessions.value.get(id)
    if (!s) {
      s = createEmptySession()
      sessions.value.set(id, s)
      triggerRef(sessions)
    }
    return s
  }

  // ---------- computed (active session) ----------
  const sessionId = computed({
    get: () => activeSessionId.value,
    set: (v: string) => { activeSessionId.value = v },
  })

  const messages = computed<ChatMessage[]>(
    () => getOrCreateSession(activeSessionId.value).messages,
  )
  const isStreaming = computed(
    () => getOrCreateSession(activeSessionId.value).isStreaming,
  )
  const currentStatus = computed(
    () => getOrCreateSession(activeSessionId.value).currentStatus,
  )
  const completedTools = computed<CompletedTool[]>(
    () => getOrCreateSession(activeSessionId.value).completedTools,
  )
  const streamError = computed<string | null>(
    () => getOrCreateSession(activeSessionId.value).streamError,
  )
  const hasMessages = computed(
    () => getOrCreateSession(activeSessionId.value).messages.length > 0,
  )

  /** Session IDs that currently have an active stream running. */
  const streamingSessions = computed<string[]>(() => {
    const result: string[] = []
    for (const [id, s] of sessions.value) {
      if (s.isStreaming) result.push(id)
    }
    return result
  })

  /**
   * Local sessions that have messages but may not yet exist in the backend's
   * conversation list (the backend only persists after the stream completes).
   * The ConversationList merges these with backend items so users can navigate
   * back to in-progress conversations.
   */
  const localSessions = computed<ConversationSummary[]>(() => {
    const result: ConversationSummary[] = []
    for (const [id, s] of sessions.value) {
      if (s.messages.length === 0) continue
      // Skip the active session — the user can already see it in the chat area.
      // It only needs a sidebar entry when the user switches away from it.
      if (id === activeSessionId.value) continue
      const firstUserMsg = s.messages.find((m) => m.role === 'user')
      result.push({
        session_id: id,
        title: firstUserMsg
          ? firstUserMsg.content.slice(0, 60) + (firstUserMsg.content.length > 60 ? '...' : '')
          : `(${id})`,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        turn_count: s.messages.length,
        model: '',
        provider: '',
      })
    }
    return result
  })

  // ---------- actions ----------

  function startNewConversation(): void {
    // Do NOT abort other sessions — they keep streaming in the background.
    activeSessionId.value = newSessionId()
    // The new session is created lazily by getOrCreateSession.
    triggerRef(sessions)
  }

  async function loadConversation(id: string): Promise<void> {
    // Do NOT abort other sessions — they keep streaming in the background.
    // If this session is already streaming, just switch the view to it.
    const existing = sessions.value.get(id)
    if (existing) {
      activeSessionId.value = id
      triggerRef(sessions)
      return
    }

    // Fetch from backend and populate
    const detail = await getConversation(id)
    const s = getOrCreateSession(id)
    s.messages = detail.turns.map((t) => ({
      role: (t.role === 'user' ? 'user' : 'assistant') as MessageRole,
      content: t.content,
    }))
    // Reset streaming state for a freshly loaded conversation
    s.isStreaming = false
    s.currentStatus = ''
    s.completedTools = []
    s.streamError = null
    activeSessionId.value = id
    triggerRef(sessions)
  }

  function abort(targetSessionId?: string): void {
    const id = targetSessionId ?? activeSessionId.value
    const s = sessions.value.get(id)
    if (!s) return
    if (s.abortController) {
      s.abortController.abort()
      s.abortController = null
    }
    s.isStreaming = false
    triggerRef(sessions)
  }

  /** Remove a session from the local Map (e.g., after backend deletion). */
  function removeSession(id: string): void {
    const s = sessions.value.get(id)
    if (s?.abortController) {
      s.abortController.abort()
    }
    sessions.value.delete(id)
    triggerRef(sessions)
  }

  async function sendMessage(question: string): Promise<void> {
    const q = question.trim()
    const id = activeSessionId.value
    const s = getOrCreateSession(id)

    if (!q || s.isStreaming) return

    s.messages.push({ role: 'user', content: q })

    // Reset streaming state for this session
    s.isStreaming = true
    s.currentStatus = ''
    s.completedTools = []
    s.streamError = null
    s.toolStartTime = 0
    triggerRef(sessions)

    const controller = new AbortController()
    s.abortController = controller
    let answer: string | null = null

    try {
      for await (const event of streamAsk(
        { question: q, session_id: id },
        controller.signal,
      )) {
        handleEvent(id, event)
        if (event.type === 'answer') {
          answer = event.content
          if (event.session_id) {
            // Backend may assign a canonical session_id
            const newId = event.session_id
            if (newId !== id) {
              // Re-key the session under the backend's ID
              sessions.value.delete(id)
              sessions.value.set(newId, s)
              if (activeSessionId.value === id) {
                activeSessionId.value = newId
              }
            }
          }
        } else if (event.type === 'error') {
          s.streamError = event.content || 'Unknown error'
        }
        triggerRef(sessions)
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        return
      }
      s.streamError =
        err instanceof Error ? err.message : 'Streaming failed'
    } finally {
      s.abortController = null
      s.isStreaming = false
      s.currentStatus = ''
      triggerRef(sessions)
    }

    if (answer !== null) {
      s.messages.push({ role: 'assistant', content: answer })
    } else if (s.streamError) {
      s.messages.push({
        role: 'assistant',
        content: `**Error:** ${s.streamError}`,
      })
    }
    triggerRef(sessions)

    // Clean up non-active completed sessions after a delay to free memory
    if (id !== activeSessionId.value) {
      setTimeout(() => {
        const session = sessions.value.get(id)
        if (session && !session.isStreaming && id !== activeSessionId.value) {
          // Keep messages but clear streaming artifacts
          session.completedTools = []
          session.currentStatus = ''
          session.streamError = null
          triggerRef(sessions)
        }
      }, 60_000)
    }
  }

  function handleEvent(targetId: string, event: StreamEvent): void {
    const s = sessions.value.get(targetId)
    if (!s) return

    switch (event.type) {
      case 'heartbeat':
        return
      case 'status':
        s.currentStatus = event.content
        return
      case 'tool_start':
        s.currentStatus = event.content
        s.toolStartTime = Date.now()
        return
      case 'tool_end': {
        const duration = s.toolStartTime > 0 ? Date.now() - s.toolStartTime : 0
        s.completedTools.push({ name: event.content, duration })
        s.currentStatus = ''
        s.toolStartTime = 0
        return
      }
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
    streamingSessions,
    localSessions,
    sessions,
    // getters
    hasMessages,
    // actions
    startNewConversation,
    loadConversation,
    sendMessage,
    abort,
    removeSession,
  }
})
