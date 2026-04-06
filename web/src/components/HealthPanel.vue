<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useHealthStore } from '../stores/health'

const health = useHealthStore()
const expanded = ref(false)

const healthyCount = computed(
  () => health.data?.components.filter((c) => c.status === 'healthy').length ?? 0,
)
const totalCount = computed(() => health.data?.components.length ?? 0)

const statusColor = computed(() => {
  switch (health.data?.status) {
    case 'healthy':
      return 'bg-green-500'
    case 'degraded':
      return 'bg-orange-500'
    case 'unhealthy':
      return 'bg-red-500'
    default:
      return 'bg-gray-400'
  }
})

let pollId: number | null = null
onMounted(async () => {
  await health.refresh()
  pollId = window.setInterval(() => health.refresh(), 30_000)
})
onUnmounted(() => {
  if (pollId !== null) window.clearInterval(pollId)
})
</script>

<template>
  <div class="text-base">
    <div v-if="health.error" class="text-red-500 dark:text-red-400">
      Cannot reach API server.
    </div>
    <template v-else-if="health.data">
      <button
        class="flex w-full cursor-pointer items-center gap-2 text-left hover:opacity-80"
        @click="expanded = !expanded"
      >
        <span
          class="inline-block h-2.5 w-2.5 rounded-full"
          :class="statusColor"
        />
        <span class="font-medium">Health</span>
        <span class="text-gray-500 dark:text-gray-400">
          {{ health.data.status }} ({{ healthyCount }}/{{ totalCount }})
        </span>
        <span class="ml-auto text-gray-400 dark:text-gray-500">{{ expanded ? '&#9662;' : '&#9656;' }}</span>
      </button>

      <div v-if="expanded" class="mt-2 space-y-1 pl-4 text-sm text-gray-500 dark:text-gray-400">
        <div>
          LLM:
          <code class="rounded bg-gray-200/60 px-1 py-0.5 text-gray-600 dark:bg-gray-700/40 dark:text-gray-300">{{
            health.data.model
          }}</code>
        </div>
        <div
          v-for="comp in health.data.components"
          :key="comp.name"
          class="flex flex-wrap items-baseline gap-x-2"
        >
          <span>
            {{ comp.status === 'healthy' ? '&#10003;' : '&#10007;' }} {{ comp.name }}:
            <span
              :class="
                comp.status === 'healthy' ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'
              "
            >{{ comp.status }}</span>
          </span>
          <span v-if="comp.detail" class="text-gray-400 dark:text-gray-500">
            &mdash; {{ comp.detail }}
          </span>
        </div>
      </div>
    </template>
    <div v-else class="text-gray-500">Loading health...</div>
  </div>
</template>
