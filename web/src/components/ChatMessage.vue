<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../lib/markdown'
import type { ChatMessage } from '../stores/chat'
import { useViewMode } from '../composables/useViewMode'
import ErrorBubble from './ErrorBubble.vue'

const props = defineProps<{
  message: ChatMessage
  retryDisabled?: boolean
}>()

const emit = defineEmits<{
  retry: []
}>()

const { effectiveViewMode } = useViewMode()

const html = computed(() => renderMarkdown(props.message.content))
const isUser = computed(() => props.message.role === 'user')
const isError = computed(
  () => props.message.kind === 'error' && props.message.error !== undefined,
)
</script>

<template>
  <!-- Errors render as a distinct alert in BOTH modes — keeping a single
       error visual language avoids confusing a "view mode" toggle with
       "how should I render mistakes?". -->
  <ErrorBubble
    v-if="isError"
    :error="message.error!"
    :retry-disabled="retryDisabled"
    @retry="emit('retry')"
  />

  <!-- Conversation mode: the existing chat-bubble layout. Right-aligned
       blue bubble for the user, left-aligned grey bubble for the agent.
       This is the only mode rendered below the `lg:` breakpoint
       regardless of viewMode (the toggle that sets `document` is hidden
       there, but a viewer landing on a phone with `document` previously
       saved in localStorage still gets bubbles for legibility). -->
  <div
    v-else-if="effectiveViewMode === 'conversation'"
    class="flex w-full"
    :class="isUser ? 'justify-end' : 'justify-start'"
  >
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

  <!-- Document mode: full-width role-tinted blocks. No `max-w-[85%]`
       budget — the markdown content gets the entire 5xl content column
       (matches the chat scroll area's wrapper). User vs agent is signaled
       by background tint and a small role label rather than alignment.
       Better for wide tables and prose-heavy responses; less "chat-like"
       feel. Only reachable when the user has explicitly toggled into it
       on a lg+ viewport via the chat-area toggle button. -->
  <div v-else class="document-block flex w-full flex-col">
    <span
      class="document-role mb-1 text-xs font-semibold tracking-wide uppercase"
      :class="
        isUser
          ? 'text-blue-700 dark:text-blue-300'
          : 'text-gray-500 dark:text-gray-400'
      "
    >
      {{ isUser ? 'You' : 'Agent' }}
    </span>
    <div
      class="min-w-0 rounded-lg px-4 py-3 text-base"
      :class="
        isUser
          ? 'bg-blue-50 text-gray-900 dark:bg-blue-950/40 dark:text-gray-100'
          : 'bg-gray-50 text-gray-900 dark:bg-gray-900/60 dark:text-gray-100'
      "
    >
      <!-- eslint-disable-next-line vue/no-v-html -->
      <div class="markdown" v-html="html" />
    </div>
  </div>
</template>
