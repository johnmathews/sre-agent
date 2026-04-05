<script setup lang="ts">
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
</script>

<template>
  <div
    v-if="chat.isStreaming"
    class="flex w-full justify-start"
  >
    <div class="max-w-[85%] rounded-lg bg-gray-800/60 px-4 py-3 text-sm text-gray-300">
      <ul class="space-y-1">
        <li
          v-for="(tool, i) in chat.completedTools"
          :key="`done-${i}-${tool}`"
          class="flex items-center gap-2"
        >
          <span class="text-green-400">✓</span>
          <code class="text-xs text-gray-400">{{ tool }}</code>
        </li>
        <li
          v-if="chat.currentStatus"
          class="flex items-center gap-2"
        >
          <span class="animate-pulse text-amber-400">⏳</span>
          <code class="text-xs text-gray-300">{{ chat.currentStatus }}</code>
        </li>
        <li
          v-if="
            !chat.currentStatus && chat.completedTools.length === 0
          "
          class="flex items-center gap-2 text-gray-400"
        >
          <span class="animate-pulse">●</span>
          <span class="text-xs">Thinking…</span>
        </li>
      </ul>
    </div>
  </div>
</template>
