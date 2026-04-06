<script setup lang="ts">
import { useChatStore } from '../stores/chat'
import { useTheme } from '../composables/useTheme'
import HealthPanel from './HealthPanel.vue'
import ConversationList from './ConversationList.vue'

const chat = useChatStore()
const { theme, toggle } = useTheme()

function handleNewConversation() {
  chat.startNewConversation()
}
</script>

<template>
  <aside
    class="flex h-full flex-col gap-4 bg-gray-100 p-4 text-gray-800 dark:bg-gray-900 dark:text-gray-200"
  >
    <div class="flex items-center justify-between">
      <h1 class="text-xl font-semibold">SRE Assistant</h1>
      <button
        class="cursor-pointer rounded p-1.5 text-gray-500 hover:bg-gray-200 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
        :title="theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'"
        @click="toggle"
      >
        <svg v-if="theme === 'dark'" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
        </svg>
        <svg v-else class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
        </svg>
      </button>
    </div>

    <button
      class="w-full cursor-pointer rounded border border-gray-300 px-3 py-2 text-base text-gray-700 hover:border-gray-400 hover:bg-gray-200 dark:border-gray-700 dark:text-gray-200 dark:hover:border-gray-500 dark:hover:bg-gray-800"
      @click="handleNewConversation"
    >
      + New conversation
    </button>

    <div class="text-sm text-gray-500">
      Session: <code class="rounded bg-gray-200 px-1 py-0.5 text-gray-600 dark:bg-gray-800 dark:text-gray-400">{{ chat.sessionId }}</code>
    </div>

    <hr class="border-gray-200 dark:border-gray-800" />

    <HealthPanel />

    <hr class="border-gray-200 dark:border-gray-800" />

    <div class="min-h-0 flex-1 overflow-y-auto">
      <ConversationList />
    </div>
  </aside>
</template>
