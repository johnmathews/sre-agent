<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../lib/markdown'
import type { ChatMessage } from '../stores/chat'
import ErrorBubble from './ErrorBubble.vue'

const props = defineProps<{
  message: ChatMessage
  retryDisabled?: boolean
}>()

const emit = defineEmits<{
  retry: []
}>()

const html = computed(() => renderMarkdown(props.message.content))
const isUser = computed(() => props.message.role === 'user')
const isError = computed(
  () => props.message.kind === 'error' && props.message.error !== undefined,
)
</script>

<template>
  <ErrorBubble
    v-if="isError"
    :error="message.error!"
    :retry-disabled="retryDisabled"
    @retry="emit('retry')"
  />
  <div v-else class="flex w-full" :class="isUser ? 'justify-end' : 'justify-start'">
    <div
      class="max-w-[85%] min-w-0 rounded-lg px-4 py-3 text-base"
      :class="
        isUser
          ? 'bg-blue-600 text-white'
          : 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100'
      "
    >
      <!-- eslint-disable-next-line vue/no-v-html -->
      <div class="markdown" v-html="html" />
    </div>
  </div>
</template>
