import { defineStore } from 'pinia'
import { computed, ref, triggerRef } from 'vue'
import {
  streamWithRecovery,
  type StreamEvent,
  StreamError,
} from '../api/streamWithRecovery'
import type { StreamErrorCategory } from '../api/stream'
import { getConversation, type ConversationSummary } from '../api/conversations'

export type MessageRole = 'user' | 'assistant'
export type MessageKind = 'normal' | 'error'

export interface ChatMessageError {
  /** What broke — drives the heading and copy in `ErrorBubble`. */
  category: StreamErrorCategory | 'sse-error-event'
  /** HTTP status if the failure had one. */
  status?: number
  /** Raw upstream message — shown inside the "Details" disclosure. */
  causeMessage?: string
  /** Machine-readable failure code from a structured SSE error event. */
  reason?: string
  /** Operator-facing message from the backend (structured error events). */
  backendMessage?: string
  /** The user's question — used by the inline Retry button. */
  originalQuestion: string
}

export interface ChatMessage {
  role: MessageRole
  content: string
  /** Defaults to 'normal'. Errors render via `ErrorBubble`. */
  kind?: MessageKind
  /** Populated when `kind === 'error'`. */
  error?: ChatMessageError
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
      kind: 'normal',
    }))
    // Reset streaming state for a freshly loaded conversation
    s.isStreaming = false
    s.currentStatus = ''
    s.completedTools = []
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

    s.messages.push({ role: 'user', content: q, kind: 'normal' })

    // Reset streaming state for this session
    s.isStreaming = true
    s.currentStatus = ''
    s.completedTools = []
    s.toolStartTime = 0
    triggerRef(sessions)

    const controller = new AbortController()
    s.abortController = controller
    let answer: string | null = null
    let sseError: { content: string; reason?: string; detail?: string } | null =
      null

    try {
      for await (const event of streamWithRecovery(
        { question: q, session_id: id },
        controller.signal,
        (status) => {
          // Surface recovery state via the same channel that drives the
          // ToolProgress spinner — keeps the user oriented during the
          // silent retry / persisted-answer lookup.
          s.currentStatus =
            status === 'recovering'
              ? 'Connection lost — checking for a saved answer…'
              : 'Reconnecting…'
          triggerRef(sessions)
        },
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
          sseError = {
            content: event.content || 'Backend reported an error',
            reason: event.reason,
            detail: event.detail,
          }
        }
        triggerRef(sessions)
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }
      const se = err instanceof StreamError ? err : null
      // Keep the user bubble — the user can see what they asked. The error
      // bubble appears below with a Retry that re-issues the question.
      s.messages.push({
        role: 'assistant',
        content: '',
        kind: 'error',
        error: {
          category: se?.category ?? 'network-before-headers',
          status: se?.status,
          causeMessage:
            err instanceof Error ? err.message : String(err),
          originalQuestion: q,
        },
      })
      triggerRef(sessions)
      return
    } finally {
      s.abortController = null
      s.isStreaming = false
      s.currentStatus = ''
      triggerRef(sessions)
    }

    if (answer !== null) {
      s.messages.push({ role: 'assistant', content: answer, kind: 'normal' })
    } else if (sseError) {
      // The agent itself reported an error (clean SSE error event). Render
      // as a proper ErrorBubble with retry, not as a fake assistant reply.
      s.messages.push({
        role: 'assistant',
        content: '',
        kind: 'error',
        error: {
          category: 'sse-error-event',
          reason: sseError.reason,
          backendMessage: sseError.reason ? sseError.content : undefined,
          causeMessage: sseError.reason ? sseError.detail : sseError.content,
          originalQuestion: q,
        },
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
          triggerRef(sessions)
        }
      }, 60_000)
    }
  }

  /**
   * Re-issue the question that produced an error message. Removes both the
   * error bubble and the original user bubble (sendMessage will re-push the
   * user message), so the conversation reads cleanly after a recovery.
   */
  async function retryMessage(messageIndex: number): Promise<void> {
    const id = activeSessionId.value
    const s = sessions.value.get(id)
    if (!s) return
    const target = s.messages[messageIndex]
    if (!target || target.kind !== 'error' || !target.error) return
    const question = target.error.originalQuestion

    // The error bubble is always appended directly after the user bubble
    // that produced it — see sendMessage's push order. Look only at the
    // immediately preceding entry; a backward scan would mis-match if the
    // user has asked the same question earlier in the session and wipe out
    // a chunk of intermediate history.
    const prev = s.messages[messageIndex - 1]
    if (prev && prev.role === 'user' && prev.content === question) {
      s.messages.splice(messageIndex - 1, 2)
    } else {
      // Defensive: just remove the error bubble.
      s.messages.splice(messageIndex, 1)
    }
    triggerRef(sessions)
    await sendMessage(question)
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
    streamingSessions,
    localSessions,
    sessions,
    // getters
    hasMessages,
    // actions
    startNewConversation,
    loadConversation,
    sendMessage,
    retryMessage,
    abort,
    removeSession,
  }
})
