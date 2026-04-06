<script setup lang="ts">
import { ref, onUnmounted } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatWindow from './components/ChatWindow.vue'
import { useTheme } from './composables/useTheme'

useTheme()

const SIDEBAR_MIN = 200
const SIDEBAR_MAX = 600
const SIDEBAR_STORAGE_KEY = 'sidebar-width'

function getInitialWidth(): number {
  const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY)
  if (stored) {
    const n = parseInt(stored, 10)
    if (!isNaN(n) && n >= SIDEBAR_MIN && n <= SIDEBAR_MAX) return n
  }
  return 288
}

const sidebarWidth = ref(getInitialWidth())
const isDragging = ref(false)

function startDrag(e: MouseEvent) {
  e.preventDefault()
  isDragging.value = true
  document.addEventListener('mousemove', onDrag)
  document.addEventListener('mouseup', stopDrag)
}

function onDrag(e: MouseEvent) {
  sidebarWidth.value = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, e.clientX))
}

function stopDrag() {
  isDragging.value = false
  localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarWidth.value))
  document.removeEventListener('mousemove', onDrag)
  document.removeEventListener('mouseup', stopDrag)
}

onUnmounted(() => {
  document.removeEventListener('mousemove', onDrag)
  document.removeEventListener('mouseup', stopDrag)
})
</script>

<template>
  <div class="flex h-screen w-screen overflow-hidden" :class="{ 'select-none': isDragging }">
    <div :style="{ width: sidebarWidth + 'px', flexShrink: 0 }">
      <Sidebar />
    </div>
    <div
      class="w-1 shrink-0 cursor-col-resize bg-gray-200 transition-colors hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500"
      @mousedown="startDrag"
    />
    <main class="min-w-0 flex-1">
      <ChatWindow />
    </main>
  </div>
</template>
