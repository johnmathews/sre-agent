# 2026-05-05 — Desktop ergonomics: taller composer + view-mode toggle

Two small UX additions on top of today's mobile UX overhaul. The
mobile work optimised for the small-screen primary form factor; this
follow-up addresses two things the user wanted on desktop / large
screens specifically.

## 1. Taller composer above `md:`

The empty textarea was a single line everywhere. On desktop that
visually under-promises — long-form questions feel discouraged when
the input box is the height of a button. Slack, Linear, and ChatGPT
all use a 2-3-line empty composer on desktop for the same reason.

Implementation: replaced the inline `style="min-height: 2.5rem; …"` on
the textarea with Tailwind classes — `min-h-10` mobile,
`md:min-h-24` (≈3 lines + padding) at and above `md:` (768px). The
auto-grow handler from Unit 3 still drives the height beyond that on
input, capped at `max-h-[50vh]`.

Mobile portrait stays at one row to preserve vertical space above the
on-screen keyboard.

## 2. View-mode toggle (`lg:` 1024px+ only)

Some agent responses — long prose, multi-column tables — benefit from
the full content width rather than the conversation-mode 85%-of-bubble
budget. Added a second layout the user can opt into.

`src/composables/useViewMode.ts` is a singleton composable:

- `viewMode: Ref<'conversation' | 'document'>` — user preference,
  persisted to `localStorage` under `chat-view-mode`.
- `isLargeViewport: Ref<boolean>` — driven by a module-level
  `matchMedia('(min-width: 1024px)')` listener.
- `effectiveViewMode: ComputedRef<ViewMode>` — what components actually
  render. Clamps to `conversation` whenever `isLargeViewport` is false,
  so a desktop user who toggled into document mode doesn't end up with
  full-width blocks on a phone.

Why a clamp instead of refusing to set `document` below `lg:`: the
toggle button is only rendered at `lg:`, but a user could change their
viewport (resize window, rotate iPad). Clamping in the renderer keeps
the rendered layout sensible without coupling the composable's
state-management to viewport changes.

`ChatMessage.vue` branches on `effectiveViewMode`:

- `conversation` — existing bubble layout.
- `document` — full-width block (within `max-w-5xl` content column),
  with a small uppercase role label above ("You" / "Agent") and a
  subtle background tint distinguishing user (`bg-blue-50` /
  `dark:bg-blue-950/40`) from agent (`bg-gray-50` /
  `dark:bg-gray-900/60`). Markdown rendering (tables, lists, code) is
  unchanged.

`ErrorBubble.vue` renders identically in both modes. Errors should not
masquerade as either layout — they have their own visual language.

The toggle is an icon-pair button group at the top-right of the chat
area, visible only above `lg:`. Each button has `aria-pressed`,
`aria-label`, and a `title`. The currently-active mode shows a raised
white pill against the group's gray track (Notion-style), and clicking
the inactive button switches to it.

## Tests

`tests/e2e/view-mode.spec.ts` — runs on all four Playwright projects.
12 of 16 pass per project (4 properly skipped via project-scope
guards):

1. composer min-height per viewport — mobile compact, desktop ≥80px
2. toggle hidden on mobile, visible on desktop
3. toggling switches DOM (`.document-block` appears), persists to
   `localStorage`, survives reload, and toggling back to
   `conversation` removes the document layout
4. `chat-view-mode=document` pre-seeded into a mobile viewport's
   localStorage still renders bubbles — proves the `effectiveViewMode`
   clamp

Full suite: 206 passed, 8 skipped (was 194 before — +12 view-mode +4
project-skips).

## Out of scope

1. Theming the document-mode tints — kept restrained (`bg-*-50` light,
   `bg-*-950/40` dark). Tighten in a follow-up if the contrast feels
   off in real use.
2. Per-conversation view-mode (current implementation is global per
   user). Could imagine "this conversation is naturally a document"
   metadata, but YAGNI.
3. Animation on toggle — the swap is instant. Could fade-cross if
   it feels jarring.
