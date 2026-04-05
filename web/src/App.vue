<script setup lang="ts">
import Sidebar from './components/Sidebar.vue'
import ChatWindow from './components/ChatWindow.vue'
</script>

<template>
  <div class="flex h-screen w-screen overflow-hidden">
    <Sidebar />
    <main class="min-w-0 flex-1">
      <ChatWindow />
    </main>
  </div>
</template>
