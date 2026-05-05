// Chat view mode — `conversation` (chat-bubble layout, default) or
// `document` (full-width role-tinted blocks, opt-in on lg+ viewports).
//
// `document` mode is for prose-heavy and table-heavy responses where the
// `max-w-[85%]` bubble layout wastes horizontal real estate. Toggled via
// the icon-pair button at the top-right of the chat area; only rendered
// at `lg:` (≥1024px) — below that the toggle stays hidden and the mode
// is forced to `conversation` because document layout has no room to
// pay off on a phone.
//
// Shared singleton ref so a toggle in `ChatWindow` and consumers in
// `ChatMessage` see the same state without prop drilling. Persisted to
// localStorage under `STORAGE_KEY` so the user's choice survives reload.

import { computed, ref, watch } from 'vue'

export type ViewMode = 'conversation' | 'document'

const STORAGE_KEY = 'chat-view-mode'
// Tailwind's `lg:` breakpoint. Document mode only takes effect at or
// above this width — below it we render bubbles regardless of stored
// preference, because the document layout has no horizontal budget to
// pay off on a phone or in tablet portrait.
const LARGE_VIEWPORT_QUERY = '(min-width: 1024px)'

function loadInitial(): ViewMode {
  // SSR / first-render safety: if `window` or localStorage is unavailable,
  // default to conversation. Same fallback if the stored value has been
  // tampered with (browser dev tools, sync from another origin, etc.).
  if (typeof window === 'undefined') return 'conversation'
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    if (v === 'document' || v === 'conversation') return v
  } catch {
    // localStorage may throw in private mode on older Safari; ignore.
  }
  return 'conversation'
}

const viewMode = ref<ViewMode>(loadInitial())
const isLargeViewport = ref(false)

if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  const mq = window.matchMedia(LARGE_VIEWPORT_QUERY)
  isLargeViewport.value = mq.matches
  // The reactivity ref is module-singleton, so the listener lives for
  // the page lifetime. No teardown needed — the page going away takes
  // it with it.
  mq.addEventListener('change', (e) => {
    isLargeViewport.value = e.matches
  })
}

watch(viewMode, (v) => {
  try {
    window.localStorage.setItem(STORAGE_KEY, v)
  } catch {
    // Same private-mode fallback — failing the write is silent because
    // the in-memory ref still drives the UI for the current session.
  }
})

// What the renderer should actually use. `viewMode` is the user's stored
// preference; `effectiveViewMode` clamps it to `conversation` below the
// lg breakpoint so a desktop user who toggled into document mode and
// later opens the PWA on their phone still sees a sensible layout.
const effectiveViewMode = computed<ViewMode>(() =>
  isLargeViewport.value && viewMode.value === 'document'
    ? 'document'
    : 'conversation',
)

export function useViewMode() {
  return {
    viewMode,
    effectiveViewMode,
    isLargeViewport,
    toggle(): void {
      viewMode.value =
        viewMode.value === 'conversation' ? 'document' : 'conversation'
    },
    set(mode: ViewMode): void {
      viewMode.value = mode
    },
  }
}
