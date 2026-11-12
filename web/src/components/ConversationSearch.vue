<script setup lang="ts">
import { ref, watch } from 'vue'
import { searchConversations, type ConversationSearchResult } from '../api/conversations'
import { useChatStore } from '../stores/chat'

const emit = defineEmits<{
  selected: []
}>()

const chat = useChatStore()

const query = ref('')
const results = ref<ConversationSearchResult[]>([])
const loading = ref(false)
const hasSearched = ref(false)

let debounceTimer: ReturnType<typeof setTimeout> | null = null

watch(query, (val) => {
  if (debounceTimer) clearTimeout(debounceTimer)
  const q = val.trim()
  if (!q) {
    results.value = []
    hasSearched.value = false
    return
  }
  debounceTimer = setTimeout(async () => {
    loading.value = true
    hasSearched.value = true
    try {
      results.value = await searchConversations(q)
    } catch {
      results.value = []
    } finally {
      loading.value = false
    }
  }, 300)
})

async function selectResult(sessionId: string) {
  await chat.loadConversation(sessionId)
  query.value = ''
  emit('selected')
}

function highlightSnippet(snippet: string): string {
  const q = query.value.trim()
  if (!q) return escapeHtml(snippet)
  const escaped = escapeHtml(snippet)
  const escapedQuery = escapeHtml(q)
  const re = new RegExp(`(${escapedQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
  return escaped.replace(re, '<mark class="bg-yellow-200 dark:bg-yellow-700/50 rounded px-0.5">$1</mark>')
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function roleLabel(role: string): string {
  if (role === 'title') return 'Title'
  if (role === 'user') return 'You'
  if (role === 'assistant') return 'Agent'
  return role
}
</script>

<template>
  <div>
    <div class="relative">
      <input
        v-model="query"
        type="text"
        placeholder="Search conversations..."
        class="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 pl-8 text-sm text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500"
      />
      <svg
        class="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
      >
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
        />
      </svg>
      <button
        v-if="query"
        class="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
        title="Clear search"
        @click="query = ''"
      >
        &times;
      </button>
    </div>

    <!-- Search results -->
    <div v-if="loading" class="mt-2 text-center text-xs text-gray-500">
      Searching...
    </div>
    <div
      v-else-if="hasSearched && results.length === 0"
      class="mt-2 text-center text-xs text-gray-500 italic"
    >
      No matches found.
    </div>
    <div v-else-if="results.length > 0" class="mt-2 space-y-1">
      <button
        v-for="result in results"
        :key="result.session_id"
        class="w-full cursor-pointer rounded px-2 py-1.5 text-left hover:bg-gray-200/60 dark:hover:bg-gray-700/40"
        :class="{ 'bg-gray-200/50 dark:bg-gray-700/30': result.session_id === chat.sessionId }"
        @click="selectResult(result.session_id)"
      >
        <div class="truncate text-sm font-medium text-gray-800 dark:text-gray-200">
          {{ result.title || `(${result.session_id})` }}
        </div>
        <div
          v-for="(match, i) in result.matches.slice(0, 2)"
          :key="i"
          class="mt-0.5 text-xs text-gray-500 dark:text-gray-400"
        >
          <span class="font-medium text-gray-600 dark:text-gray-300">{{ roleLabel(match.role) }}:</span>
          <span class="ml-1" v-html="highlightSnippet(match.snippet)"></span>
        </div>
      </button>
    </div>
  </div>
</template>
