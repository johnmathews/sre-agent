<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ChatMessageError } from '../stores/chat'

const props = defineProps<{
  error: ChatMessageError
  /** True while a follow-up `retryMessage` is streaming, so the button can
   * disable itself and avoid double-issuing the question. */
  retryDisabled?: boolean
}>()

const emit = defineEmits<{
  retry: []
}>()

const showDetails = ref(false)

const heading = computed(() => {
  switch (props.error.category) {
    case 'network-before-headers':
      return "Couldn't reach the agent"
    case 'network-midstream':
      return 'Connection dropped mid-reply'
    case 'http-4xx':
      if (props.error.status === 401)
        return 'Session expired — sign in again'
      if (props.error.status === 429) return 'Rate limited'
      return `Request rejected (HTTP ${props.error.status ?? '4xx'})`
    case 'http-5xx':
      return `Agent unavailable (HTTP ${props.error.status ?? '5xx'})`
    case 'sse-error-event':
      return 'Agent reported an error'
    case 'no-body':
      return 'Empty response from agent'
    case 'aborted':
      return 'Stopped'
    default:
      return 'Something went wrong'
  }
})

const explanation = computed(() => {
  switch (props.error.category) {
    case 'network-before-headers':
      return 'The request never reached the server. Network may be offline or the agent is down.'
    case 'network-midstream':
      return 'The streaming reply was interrupted (likely a network handoff or timeout). The reply may have been saved on the server — retrying will check.'
    case 'http-4xx':
      if (props.error.status === 401)
        return 'Your authenticated session is no longer valid. Reload the page to re-authenticate.'
      if (props.error.status === 429)
        return 'Too many requests in a short window. Wait a few seconds and retry.'
      return 'The agent rejected the request. Retrying without changes is unlikely to help — check the details.'
    case 'http-5xx':
      return 'The agent backend hit an internal error. Usually transient — retry in a moment.'
    case 'sse-error-event':
      return 'The agent processed your question but failed to produce an answer (often a tool error or LLM provider issue). Retrying is safe.'
    case 'no-body':
      return 'The server returned a 200 OK with no body — likely a proxy misconfiguration.'
    case 'aborted':
      return 'You stopped the request before it finished.'
    default:
      return ''
  }
})

const showRetry = computed(() => props.error.category !== 'aborted')
</script>

<template>
  <div class="flex w-full justify-start">
    <div
      class="error-bubble max-w-[85%] rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-700/60 dark:bg-red-950/40 dark:text-red-100"
      role="alert"
    >
      <div class="flex items-start gap-2">
        <!-- Error icon -->
        <svg
          class="mt-0.5 h-4 w-4 flex-shrink-0 text-red-500 dark:text-red-400"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
          />
        </svg>
        <div class="min-w-0 flex-1">
          <p class="font-semibold">{{ heading }}</p>
          <p v-if="explanation" class="mt-1 text-red-800 dark:text-red-200">
            {{ explanation }}
          </p>

          <div class="mt-3 flex flex-wrap items-center gap-2">
            <button
              v-if="showRetry"
              type="button"
              class="cursor-pointer rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400 disabled:cursor-not-allowed disabled:opacity-50"
              :disabled="retryDisabled"
              @click="emit('retry')"
            >
              Retry
            </button>
            <button
              type="button"
              class="cursor-pointer rounded-md border border-red-300 px-3 py-1.5 text-sm font-medium text-red-800 hover:bg-red-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400 dark:border-red-700/60 dark:text-red-200 dark:hover:bg-red-900/40"
              :aria-expanded="showDetails"
              @click="showDetails = !showDetails"
            >
              {{ showDetails ? 'Hide details' : 'Details' }}
            </button>
          </div>

          <div
            v-if="showDetails"
            class="mt-3 rounded border border-red-200 bg-red-100/60 p-2 font-mono text-xs text-red-900 dark:border-red-800/60 dark:bg-red-950/60 dark:text-red-100"
          >
            <div><span class="font-semibold">Category:</span> {{ error.category }}</div>
            <div v-if="error.status">
              <span class="font-semibold">Status:</span> {{ error.status }}
            </div>
            <div v-if="error.causeMessage" class="mt-1 break-all">
              <span class="font-semibold">Cause:</span> {{ error.causeMessage }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
