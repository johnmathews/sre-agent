# UI Polish: Theme Toggle, Text Sizing, and Sidebar Improvements

## Changes

### Light/Dark Theme Toggle
Added class-based dark mode using Tailwind v4's `@custom-variant dark` directive.
Created `src/composables/useTheme.ts` — a singleton composable that manages the
`dark` class on `<html>` and persists preference to `localStorage`. Defaults to
light theme. Toggle button (sun/moon SVG icons) sits in the sidebar header next
to the title.

All components converted from hard-coded dark colors to light-first utilities
with `dark:` variants. The markdown styles in `style.css` switched from
`@media (prefers-color-scheme: dark)` to `.dark` class selectors.

### Text Size Bump
Bumped all text sizes one Tailwind step: `text-xs` to `text-sm`, `text-sm` to
`text-base`, `text-lg` to `text-xl`. Affects all components.

### Auto-Focus Chat Input
Added `autofocus` HTML attribute and programmatic `onMounted` focus to the chat
textarea. The native attribute proved more reliable in production than the
programmatic approach alone.

### Collapsible Health Panel
Removed the `autoExpand()` behavior that opened the health panel when status was
degraded. Panel now always starts collapsed, showing only the headline summary
(status dot + "healthy/degraded (N/N)"). Click to expand and see individual
component health. Updated the E2E test to match.

### Resizable Sidebar
Replaced the fixed `w-72` sidebar with a draggable resize handle in `App.vue`.
Width is constrained between 200-600px and persisted to `localStorage`. The
handle is a 4px-wide column between the sidebar and chat area that highlights
on hover.

### Rename to "SRE Agent"
Changed "SRE Assistant" to "SRE Agent" in the sidebar heading, page title,
tests, and README.

## Test Fixes
- Restored Unicode ellipsis character in textarea placeholder (tests matched on it)
- Updated health panel test from "auto-expands when degraded" to "stays collapsed,
  expands on click"
