<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()

// Elapsed-time ticker for the current active tool/status
const elapsed = ref(0)
let tickInterval: ReturnType<typeof setInterval> | null = null

function startTicker() {
  stopTicker()
  elapsed.value = 0
  tickInterval = setInterval(() => {
    elapsed.value++
  }, 1000)
}

function stopTicker() {
  if (tickInterval) {
    clearInterval(tickInterval)
    tickInterval = null
  }
  elapsed.value = 0
}

// Restart ticker when currentStatus changes (new tool or status message)
watch(
  () => chat.currentStatus,
  (val) => {
    if (val) {
      startTicker()
    } else {
      stopTicker()
    }
  },
)

// Stop ticker when streaming ends
watch(
  () => chat.isStreaming,
  (streaming) => {
    if (!streaming) stopTicker()
  },
)

onUnmounted(() => stopTicker())

function formatDuration(ms: number): string {
  if (ms < 1000) return '<1s'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rem = s % 60
  return `${m}m ${rem}s`
}

/** Detect whether a status string looks like a tool event or prose status. */
function isToolEvent(status: string): boolean {
  // Tool events come from tool_start which typically start with a verb + service name
  // Status events are prose like "Thinking...", "Initializing...", "Synthesizing response..."
  // Simple heuristic: tool events have — separator (parameter summary) or match known patterns
  return status.includes(' — ') || /^(Querying|Checking|Searching|Listing|Fetching|Loading|Running|Correlating)\b/.test(status)
}
</script>

<template>
  <div
    v-if="chat.isStreaming"
    class="flex w-full justify-start"
  >
    <div class="max-w-[85%] rounded-lg bg-gray-100/80 px-4 py-3 text-base text-gray-600 dark:bg-gray-800/60 dark:text-gray-300">
      <ul class="space-y-1.5">
        <!-- Completed tools with duration -->
        <li
          v-for="(tool, i) in chat.completedTools"
          :key="`done-${i}-${tool.name}`"
          class="flex items-center gap-2"
        >
          <span class="text-green-500 dark:text-green-400">&#10003;</span>
          <code class="text-sm text-gray-500 dark:text-gray-400">{{ tool.name }}</code>
          <span
            v-if="tool.duration > 0"
            class="text-xs text-gray-400 dark:text-gray-500"
          >({{ formatDuration(tool.duration) }})</span>
        </li>

        <!-- Current active status (tool or prose) -->
        <li
          v-if="chat.currentStatus && isToolEvent(chat.currentStatus)"
          class="flex items-center gap-2"
        >
          <span class="animate-pulse text-amber-500 dark:text-amber-400">&#8987;</span>
          <code class="text-sm text-gray-600 dark:text-gray-300">{{ chat.currentStatus }}</code>
          <span
            v-if="elapsed > 0"
            class="text-xs text-gray-400 dark:text-gray-500"
          >({{ elapsed }}s)</span>
        </li>
        <li
          v-else-if="chat.currentStatus"
          class="flex items-center gap-2"
        >
          <span class="animate-pulse text-blue-400 dark:text-blue-500">&#9679;</span>
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ chat.currentStatus }}</span>
          <span
            v-if="elapsed > 2"
            class="text-xs text-gray-400 dark:text-gray-500"
          >({{ elapsed }}s)</span>
        </li>

        <!-- Thinking fallback with animated dots -->
        <li
          v-if="!chat.currentStatus && chat.completedTools.length === 0"
          class="flex items-center gap-2 text-gray-500 dark:text-gray-400"
        >
          <span class="thinking-dots">
            <span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>
          </span>
          <span class="text-sm">Thinking</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.thinking-dots .dot {
  animation: blink 1.4s infinite;
  font-weight: bold;
  font-size: 1.25rem;
  line-height: 1;
}
.thinking-dots .dot:nth-child(2) {
  animation-delay: 0.2s;
}
.thinking-dots .dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes blink {
  0%, 20% { opacity: 0.2; }
  50% { opacity: 1; }
  80%, 100% { opacity: 0.2; }
}
</style>
