<script setup lang="ts">
import { ref, onUnmounted, onMounted, watch } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatWindow from './components/ChatWindow.vue'
import { useTheme } from './composables/useTheme'

useTheme()

const SIDEBAR_MIN = 200
const SIDEBAR_MAX = 600
const MOBILE_BREAKPOINT = 768
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
const isMobile = ref(false)
const sidebarOpen = ref(false)

function checkMobile() {
  isMobile.value = window.innerWidth < MOBILE_BREAKPOINT
  if (!isMobile.value) {
    sidebarOpen.value = false // Reset overlay state on desktop
  }
}

// Layout sizing: we now use `100dvh` directly on the root container instead
// of driving a `--vvh` CSS variable from `window.visualViewport`.
//
// `dvh` ("dynamic viewport height") reached Baseline Widely Available in
// June 2025 — Safari 15.4+, Chrome 108+, Firefox 101+. Modern Safari
// honours dvh by shrinking its reported height when the keyboard opens,
// which is what the JS observer used to do manually. Dropping the JS
// observer also avoids the iOS 26 visualViewport regression where
// `visualViewport.height` doesn't fully revert after the keyboard
// dismisses, leaving a persistent dead-space gap above the composer.
//
// If a future iOS version regresses dvh handling, restore the observer
// behind a feature check rather than reverting wholesale.

onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
})

onUnmounted(() => {
  window.removeEventListener('resize', checkMobile)
  document.removeEventListener('mousemove', onDrag)
  document.removeEventListener('mouseup', stopDrag)
})

function toggleSidebar() {
  sidebarOpen.value = !sidebarOpen.value
}

function closeSidebar() {
  sidebarOpen.value = false
}

// Close sidebar when a conversation is loaded on mobile
watch(sidebarOpen, (open) => {
  if (open) {
    document.body.style.overflow = isMobile.value ? 'hidden' : ''
  } else {
    document.body.style.overflow = ''
  }
})

function startDrag(e: MouseEvent) {
  if (isMobile.value) return
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
</script>

<template>
  <div
    class="flex w-screen overflow-hidden"
    :class="{ 'select-none': isDragging }"
    style="height: 100dvh; min-height: 100dvh"
  >
    <!-- Mobile overlay backdrop -->
    <div
      v-if="isMobile && sidebarOpen"
      class="fixed inset-0 z-30 bg-black/40"
      @click="closeSidebar"
    />

    <!-- Sidebar -->
    <div
      v-if="!isMobile"
      :style="{ width: sidebarWidth + 'px', flexShrink: 0 }"
    >
      <Sidebar />
    </div>
    <div
      v-else
      class="fixed inset-y-0 left-0 z-40 w-[85vw] max-w-[320px] transform transition-transform duration-200 ease-in-out"
      :class="sidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <Sidebar :is-mobile="true" @close="closeSidebar" />
    </div>

    <!-- Drag handle (desktop only) -->
    <div
      v-if="!isMobile"
      class="w-1 shrink-0 cursor-col-resize bg-gray-200 transition-colors hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500"
      @mousedown="startDrag"
    />

    <!-- Main content -->
    <main class="min-w-0 flex-1">
      <ChatWindow :is-mobile="isMobile" @open-sidebar="toggleSidebar" />
    </main>
  </div>
</template>
