<script setup lang="ts">
import { useChatStore } from '../stores/chat'
import HealthPanel from './HealthPanel.vue'
import ConversationList from './ConversationList.vue'

const chat = useChatStore()

function handleNewConversation() {
  chat.startNewConversation()
}
</script>

<template>
  <aside
    class="flex h-full w-72 flex-col gap-4 bg-gray-900 p-4 text-gray-200"
  >
    <h1 class="text-lg font-semibold">SRE Assistant</h1>

    <button
      class="w-full cursor-pointer rounded border border-gray-700 px-3 py-2 text-sm text-gray-200 hover:border-gray-500 hover:bg-gray-800"
      @click="handleNewConversation"
    >
      + New conversation
    </button>

    <div class="text-xs text-gray-500">
      Session: <code class="rounded bg-gray-800 px-1 py-0.5 text-gray-400">{{ chat.sessionId }}</code>
    </div>

    <hr class="border-gray-800" />

    <HealthPanel />

    <hr class="border-gray-800" />

    <div class="min-h-0 flex-1 overflow-y-auto">
      <ConversationList />
    </div>
  </aside>
</template>
