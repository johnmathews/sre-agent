<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { useConversationsStore } from '../stores/conversations'
import ChatMessage from './ChatMessage.vue'
import ToolProgress from './ToolProgress.vue'

const chat = useChatStore()
const conversations = useConversationsStore()

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

async function handleSubmit() {
  const q = input.value.trim()
  if (!q || chat.isStreaming) return
  input.value = ''
  await chat.sendMessage(q)
  // Refresh conversation list after each turn (new convo appears, title updates)
  void conversations.refresh()
}

function useExample(question: string) {
  input.value = question
  textareaRef.value?.focus()
}

function handleKeydown(e: KeyboardEvent) {
  // Enter = submit; Shift+Enter = newline
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void handleSubmit()
  }
}
</script>

<template>
  <div class="flex h-full flex-col bg-white text-gray-900 dark:bg-gray-950 dark:text-gray-100">
    <!-- Scrollable message area -->
    <div
      ref="scrollContainer"
      class="flex-1 overflow-y-auto px-6 py-4"
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
      <div v-else class="mx-auto flex max-w-3xl flex-col gap-4">
        <ChatMessage
          v-for="(msg, i) in chat.messages"
          :key="i"
          :message="msg"
        />
        <ToolProgress />
      </div>
    </div>

    <!-- Input area -->
    <div class="border-t border-gray-200 bg-gray-50 px-6 py-3 dark:border-gray-800 dark:bg-gray-900">
      <form
        class="mx-auto flex max-w-3xl items-end gap-2"
        @submit.prevent="handleSubmit"
      >
        <textarea
          ref="textareaRef"
          v-model="input"
          autofocus
          rows="1"
          placeholder="Ask about your infrastructure…"
          class="flex-1 resize-none rounded-lg border border-gray-300 bg-white px-3 py-2 text-base text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:outline-none disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500"
          :disabled="chat.isStreaming"
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
