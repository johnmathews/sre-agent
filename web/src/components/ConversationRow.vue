<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ConversationSummary } from '../api/conversations'

const props = defineProps<{
  conv: ConversationSummary
  isActive: boolean
}>()

const emit = defineEmits<{
  select: [sessionId: string]
  rename: [sessionId: string, title: string]
  delete: [sessionId: string, title: string]
}>()

type Mode = 'idle' | 'menu' | 'rename' | 'confirm-delete'
const mode = ref<Mode>('idle')
const renameInput = ref('')
const inputRef = ref<HTMLInputElement | null>(null)

function openMenu(e: Event) {
  e.stopPropagation()
  mode.value = mode.value === 'menu' ? 'idle' : 'menu'
}

function startRename(e: Event) {
  e.stopPropagation()
  renameInput.value = props.conv.title || props.conv.session_id
  mode.value = 'rename'
  void nextTick(() => inputRef.value?.focus())
}

function startDelete(e: Event) {
  e.stopPropagation()
  mode.value = 'confirm-delete'
}

function commitRename() {
  const title = renameInput.value.trim()
  if (!title) return
  emit('rename', props.conv.session_id, title)
  mode.value = 'idle'
}

function commitDelete() {
  emit('delete', props.conv.session_id, props.conv.title || props.conv.session_id)
  mode.value = 'idle'
}

function cancel() {
  mode.value = 'idle'
}

// Close the menu if this row becomes inactive during another row's interaction.
watch(
  () => props.conv.session_id,
  () => {
    mode.value = 'idle'
  },
)

const titleLabel = () => props.conv.title || `(${props.conv.session_id})`
</script>

<template>
  <div class="group">
    <!-- Main row: title + hover-only ⋯ menu button -->
    <div
      class="flex items-center gap-1 rounded px-2 py-1.5 text-sm hover:bg-gray-700/40"
      :class="{ 'bg-gray-700/30': isActive }"
    >
      <button
        class="flex-1 cursor-pointer truncate text-left"
        :title="`${conv.turn_count} turn${conv.turn_count !== 1 ? 's' : ''} — ${conv.provider}`"
        @click="emit('select', conv.session_id)"
      >
        <span v-if="isActive" class="mr-1 text-gray-400">▶</span>
        {{ titleLabel() }}
      </button>
      <button
        class="cursor-pointer rounded px-1.5 py-0.5 text-gray-400 opacity-0 hover:bg-gray-600/60 hover:text-gray-100 group-hover:opacity-100"
        :class="{ 'opacity-100': mode !== 'idle' }"
        title="Rename or delete"
        aria-label="Open menu"
        @click="openMenu"
      >
        ⋯
      </button>
    </div>

    <!-- Inline menu -->
    <div
      v-if="mode === 'menu'"
      class="mt-1 flex gap-1 pl-2 text-xs"
      @click.stop
    >
      <button
        class="cursor-pointer rounded bg-gray-700/60 px-2 py-1 hover:bg-gray-600"
        @click="startRename"
      >
        Rename
      </button>
      <button
        class="cursor-pointer rounded bg-gray-700/60 px-2 py-1 hover:bg-red-900/80"
        @click="startDelete"
      >
        Delete
      </button>
      <button
        class="cursor-pointer rounded px-2 py-1 text-gray-400 hover:text-gray-100"
        @click="cancel"
      >
        Cancel
      </button>
    </div>

    <!-- Rename input -->
    <div
      v-else-if="mode === 'rename'"
      class="mt-1 flex gap-1 pl-2"
      @click.stop
    >
      <input
        ref="inputRef"
        v-model="renameInput"
        type="text"
        class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 text-xs text-gray-100 focus:border-blue-500 focus:outline-none"
        @keydown.enter="commitRename"
        @keydown.escape="cancel"
      />
      <button
        class="cursor-pointer rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-500"
        @click="commitRename"
      >
        Save
      </button>
      <button
        class="cursor-pointer rounded px-2 py-1 text-xs text-gray-400 hover:text-gray-100"
        @click="cancel"
      >
        Cancel
      </button>
    </div>

    <!-- Delete confirmation -->
    <div
      v-else-if="mode === 'confirm-delete'"
      class="mt-1 pl-2 text-xs"
      @click.stop
    >
      <p class="mb-1 text-gray-300">Delete “{{ titleLabel() }}”?</p>
      <div class="flex gap-1">
        <button
          class="cursor-pointer rounded bg-red-600 px-2 py-1 text-white hover:bg-red-500"
          @click="commitDelete"
        >
          Delete
        </button>
        <button
          class="cursor-pointer rounded px-2 py-1 text-gray-400 hover:text-gray-100"
          @click="cancel"
        >
          Cancel
        </button>
      </div>
    </div>
  </div>
</template>
