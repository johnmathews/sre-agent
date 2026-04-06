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

onMounted(() => {
  textareaRef.value?.focus()
})

async function scrollToBottom() {
  await nextTick()
  const el = scrollContainer.value
  if (el) el.scrollTop = el.scrollHeight
}

// Auto-scroll on new message or tool progress
watch(
  () => [
    chat.messages.length,
    chat.completedTools.length,
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
        class="flex h-full items-center justify-center text-center text-gray-400 dark:text-gray-500"
      >
        <div>
          <p class="mb-2 text-xl">Ask about your infrastructure.</p>
          <p class="text-base text-gray-500 dark:text-gray-400">
            The assistant can query Prometheus, Grafana, Loki, and more.
          </p>
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
