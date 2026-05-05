<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { useConversationsStore } from '../stores/conversations'
import { useViewMode } from '../composables/useViewMode'
import ChatMessage from './ChatMessage.vue'
import ToolProgress from './ToolProgress.vue'

defineProps<{
  isMobile?: boolean
}>()

const emit = defineEmits<{
  'open-sidebar': []
}>()

const chat = useChatStore()
const conversations = useConversationsStore()
const { viewMode, effectiveViewMode, set: setViewMode } = useViewMode()

const input = ref('')
const scrollContainer = ref<HTMLDivElement | null>(null)
const textareaRef = ref<HTMLTextAreaElement | null>(null)

const exampleQuestions = [
  'What VMs and containers are running on Proxmox?',
  'Are any Grafana alerts firing right now?',
  'How has CPU usage changed over the last 6 hours?',
  'Show me errors from traefik in the last hour',
  'Is the ZFS tank pool healthy? How much space is left?',
  'When was VM 100 last backed up?',
  'Which services experienced crashes today?',
  'What disks are currently spun up?',
]

onMounted(() => {
  textareaRef.value?.focus()
})

// Re-focus textarea when switching conversations or starting a new one
watch(
  () => chat.sessionId,
  async () => {
    await nextTick()
    textareaRef.value?.focus()
  },
)

async function scrollToBottom() {
  await nextTick()
  const el = scrollContainer.value
  if (el) el.scrollTop = el.scrollHeight
}

// Auto-scroll on new message or tool progress
watch(
  () => [
    chat.messages.length,
    chat.completedTools?.length ?? 0,
    chat.currentStatus,
  ],
  () => {
    void scrollToBottom()
  },
)

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

async function handleSubmit() {
  const q = input.value.trim()
  if (!q || chat.isStreaming) return
  input.value = ''
  await nextTick()
  autoResize()
  await chat.sendMessage(q)
  // Refresh conversation list after each turn (new convo appears, title updates)
  void conversations.refresh()
}

function useExample(question: string) {
  input.value = question
  textareaRef.value?.focus()
  void nextTick(() => autoResize())
}

function focusIfNoSelection() {
  // Don't steal focus when the user is selecting/copying text from messages
  const selection = window.getSelection()
  if (selection && selection.toString().length > 0) return
  textareaRef.value?.focus()
}

function handleKeydown(e: KeyboardEvent) {
  // Slack-style key handling. On touch devices the iOS / Android soft
  // keyboard has no Shift modifier when typing into a <textarea>, so if
  // Enter submitted on mobile the user could never enter a newline (or
  // a bullet list, or a multi-paragraph reply). The chosen rules:
  //   - Cmd/Ctrl+Enter:   always submits (Slack / Mac convention).
  //   - Plain Enter on a *fine-pointer* device (mouse/trackpad): submits.
  //     Shift+Enter on the same device: newline.
  //   - Plain Enter on a *coarse-pointer* device (touch): newline. Submit
  //     is via the Send button only. Tap Send to deliver.
  //
  // We use `pointer: coarse` rather than UA sniffing because it's a
  // pointer-quality signal — iPad with a hardware keyboard reports
  // `pointer: fine` and gets the desktop behaviour, which is what we want.
  if (e.key !== 'Enter') return
  if (e.metaKey || e.ctrlKey) {
    e.preventDefault()
    void handleSubmit()
    return
  }
  const coarsePointer =
    typeof window !== 'undefined' &&
    window.matchMedia &&
    window.matchMedia('(pointer: coarse)').matches
  if (!coarsePointer && !e.shiftKey) {
    e.preventDefault()
    void handleSubmit()
  }
  // Otherwise: let the browser insert a newline natively.
}
</script>

<template>
  <div
    class="flex h-full flex-col bg-white text-gray-900 dark:bg-gray-950 dark:text-gray-100"
    @click="focusIfNoSelection"
  >
    <!-- Mobile header bar -->
    <div
      v-if="isMobile"
      class="app-header-mobile flex items-center border-b border-gray-200 dark:border-gray-800"
    >
      <button
        class="cursor-pointer rounded p-1.5 text-gray-500 hover:bg-gray-200 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
        title="Open sidebar"
        aria-label="Open sidebar"
        @click.stop="emit('open-sidebar')"
      >
        <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>
      <span class="ml-2 text-sm font-medium text-gray-600 dark:text-gray-300">SRE Agent</span>
    </div>

    <!-- View-mode toggle (lg+ only). Switches between bubble-style
         conversation layout and full-width document layout. Hidden on
         mobile / tablet portrait — those viewports have no horizontal
         budget for document mode to pay off, and the bubble layout
         already feels right at small widths. -->
    <div
      class="hidden lg:flex items-center justify-end gap-1 border-b border-gray-200 bg-white px-6 py-1.5 dark:border-gray-800 dark:bg-gray-950"
    >
      <div
        class="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5 dark:border-gray-700 dark:bg-gray-900"
        role="group"
        aria-label="View mode"
      >
        <button
          type="button"
          class="cursor-pointer rounded px-2 py-1 transition-colors"
          :class="
            viewMode === 'conversation'
              ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-gray-100'
              : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
          "
          :aria-pressed="viewMode === 'conversation'"
          aria-label="Conversation view (chat bubbles)"
          title="Conversation view"
          @click.stop="setViewMode('conversation')"
        >
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" d="M2.25 12.76c0 1.6 1.123 2.994 2.707 3.227 1.068.157 2.148.279 3.238.364.466.037.893.281 1.153.671L12 21l2.652-3.978c.26-.39.687-.634 1.153-.67 1.09-.086 2.17-.208 3.238-.365 1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0 0 12 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z"/>
          </svg>
        </button>
        <button
          type="button"
          class="cursor-pointer rounded px-2 py-1 transition-colors"
          :class="
            viewMode === 'document'
              ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-gray-100'
              : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
          "
          :aria-pressed="viewMode === 'document'"
          aria-label="Document view (full-width blocks)"
          title="Document view"
          @click.stop="setViewMode('document')"
        >
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9z"/>
            <path stroke-linecap="round" stroke-linejoin="round" d="M9 13.5h6M9 17.25h6"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Scrollable message area -->
    <div
      ref="scrollContainer"
      class="chat-scroll-area flex-1 overflow-y-auto"
    >
      <div
        v-if="!chat.hasMessages && !chat.isStreaming"
        class="mx-auto flex h-full max-w-2xl flex-col items-center justify-center"
      >
        <h2 class="mb-3 text-2xl font-semibold text-gray-800 dark:text-gray-100">
          Ask about your infrastructure
        </h2>
        <p class="mb-6 text-center text-base text-gray-500 dark:text-gray-400">
          Query metrics, inspect logs, check backups, and troubleshoot your homelab.
        </p>

        <!-- Tool grid — hidden on very small screens -->
        <div class="mb-6 hidden w-full grid-cols-2 gap-x-6 gap-y-1 text-sm text-gray-500 dark:text-gray-400 sm:grid lg:grid-cols-3">
          <div class="flex items-center gap-1.5">
            <span class="text-blue-500">&#9679;</span> Prometheus metrics
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-orange-500">&#9679;</span> Grafana alerts &amp; dashboards
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-yellow-500">&#9679;</span> Loki logs
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-green-500">&#9679;</span> Proxmox VMs &amp; containers
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-cyan-500">&#9679;</span> TrueNAS storage &amp; shares
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-purple-500">&#9679;</span> Proxmox Backup Server
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-rose-500">&#9679;</span> HDD power status
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-teal-500">&#9679;</span> Runbook search
          </div>
        </div>

        <!-- Example questions -->
        <div class="w-full">
          <p class="mb-2 text-center text-sm font-medium text-gray-400 dark:text-gray-500">
            Try asking
          </p>
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button
              v-for="q in exampleQuestions"
              :key="q"
              class="cursor-pointer rounded-lg border border-gray-200 px-3 py-2 text-left text-sm text-gray-600 transition-colors hover:border-blue-300 hover:bg-blue-50 dark:border-gray-700 dark:text-gray-400 dark:hover:border-blue-600 dark:hover:bg-blue-950"
              @click="useExample(q)"
            >
              {{ q }}
            </button>
          </div>
        </div>
      </div>
      <div
        v-else
        class="mx-auto flex min-w-0 flex-col gap-4"
        :class="effectiveViewMode === 'document' ? 'max-w-5xl' : 'max-w-3xl'"
      >
        <ChatMessage
          v-for="(msg, i) in chat.messages"
          :key="i"
          :message="msg"
          :retry-disabled="chat.isStreaming"
          @retry="chat.retryMessage(i)"
        />
        <ToolProgress />
      </div>
    </div>

    <!-- Input area -->
    <div class="composer-area border-t border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900">
      <form
        class="mx-auto flex max-w-3xl items-end gap-2"
        @submit.prevent="handleSubmit"
      >
        <textarea
          ref="textareaRef"
          v-model="input"
          autofocus
          rows="1"
          enterkeyhint="send"
          placeholder="Ask about your infrastructure…"
          class="max-h-[50vh] min-h-10 flex-1 resize-y overflow-y-auto rounded-lg border border-gray-300 bg-white px-3 py-2 text-base text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:outline-none disabled:opacity-50 md:min-h-24 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500"
          :disabled="chat.isStreaming"
          @input="autoResize"
          @keydown="handleKeydown"
        />
        <button
          v-if="!chat.isStreaming"
          type="submit"
          :disabled="!input.trim()"
          class="cursor-pointer rounded-lg bg-blue-600 px-4 py-2 text-base font-medium text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Send
        </button>
        <button
          v-else
          type="button"
          class="cursor-pointer rounded-lg bg-red-600 px-4 py-2 text-base font-medium text-white hover:bg-red-500"
          @click="chat.abort()"
        >
          Stop
        </button>
      </form>
    </div>
  </div>
</template>
