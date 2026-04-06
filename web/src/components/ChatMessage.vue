<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../lib/markdown'
import type { ChatMessage } from '../stores/chat'

const props = defineProps<{
  message: ChatMessage
}>()

const html = computed(() => renderMarkdown(props.message.content))
const isUser = computed(() => props.message.role === 'user')
</script>

<template>
  <div class="flex w-full" :class="isUser ? 'justify-end' : 'justify-start'">
    <div
      class="max-w-[85%] rounded-lg px-4 py-3 text-base"
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
