<script setup lang="ts">
import { onMounted } from 'vue'
import { useConversationsStore } from '../stores/conversations'
import { useChatStore } from '../stores/chat'
import ConversationRow from './ConversationRow.vue'

const conversations = useConversationsStore()
const chat = useChatStore()

onMounted(() => {
  void conversations.refresh()
})

async function handleSelect(sessionId: string) {
  try {
    await chat.loadConversation(sessionId)
  } catch (err) {
    console.error('Failed to load conversation', err)
  }
}

async function handleRename(sessionId: string, title: string) {
  await conversations.rename(sessionId, title)
}

async function handleDelete(sessionId: string) {
  const wasActive = chat.sessionId === sessionId
  const ok = await conversations.remove(sessionId)
  if (ok && wasActive) {
    chat.startNewConversation()
  }
}
</script>

<template>
  <div>
    <div class="mb-2 flex items-center justify-between">
      <h2 class="text-base font-semibold text-gray-600 dark:text-gray-300">Past conversations</h2>
      <button
        class="cursor-pointer text-sm text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
        title="Refresh"
        @click="conversations.refresh()"
      >
        ↻
      </button>
    </div>
    <div v-if="conversations.loading && conversations.items.length === 0" class="text-sm text-gray-500">
      Loading...
    </div>
    <div
      v-else-if="conversations.items.length === 0"
      class="text-sm text-gray-500 italic"
    >
      No past conversations yet.
    </div>
    <div v-else class="space-y-0.5">
      <ConversationRow
        v-for="conv in conversations.items"
        :key="conv.session_id"
        :conv="conv"
        :is-active="conv.session_id === chat.sessionId"
        @select="handleSelect"
        @rename="handleRename"
        @delete="handleDelete"
      />
    </div>
  </div>
</template>
