# iOS viewport fix completed

Completed and shipped a previously-WIP fix for two iOS layout bugs reported on
real iPhones:

1. **Send button clipped by the iPhone's rounded-corner curve.** The composer
   bar's bottom-right Send button sat too close to the screen edge — close
   enough that the corner radius (~47pt on modern iPhones) cut off the
   button's bottom-right pixels. The user could not fully see or reliably tap
   it.
2. **Viewport felt zoomed-in with no horizontal margin.** Tight `px-3` (12px)
   padding on the chat scroll area combined with iOS Safari's auto-zoom on
   focused inputs (textarea inherited a sub-16px size from the form-control
   reset) made the page feel cramped.

## What was already in place

A prior session had landed the bulk of the fix on `main` but left it
uncommitted. That work added:

- `viewport-fit=cover` to the viewport meta tag (required for
  `env(safe-area-inset-*)` to return non-zero on iPhone)
- `.composer-area` and `.app-header-mobile` classes in `style.css` using
  `max(<floor>, env(safe-area-inset-*))` for safe-area-aware padding
- `font-size: max(16px, 1rem)` on `input/textarea/select` to suppress iOS
  auto-zoom
- A `--vvh` CSS variable driven from `window.visualViewport` so the root
  container height tracks the actual visual viewport (keeps the composer
  above the keyboard)

## What this session added

The previous floor of `0.75rem` (12px) for horizontal safe-area padding was
not enough to clear the iPhone corner curve in portrait, where
`safe-area-inset-right` is 0 and only the floor applies. Bumped to `1rem`
(16px) on `.composer-area` and `.app-header-mobile`, and bumped the composer
bottom floor from `0.5rem` to `0.75rem` for symmetry.

Added `.chat-scroll-area` with the same safe-area-aware horizontal padding so
the message list matches the composer/header insets — important in landscape,
where `safe-area-inset-left/right` become non-zero. Replaced hardcoded
`px-3 py-3 sm:px-6 sm:py-4` on the scroll container with the new class.

Wrote `tests/e2e/ios-viewport.spec.ts` (7 specs) covering: viewport meta tag,
input font-size ≥16px, padding floors on composer / header / scroll area,
`--vvh` set from `visualViewport`, and Send button distance from the right
viewport edge. Headless Chromium reports `env(safe-area-inset-*)` as 0, so
these tests validate the *floor* values; the safe-area-inset branch is
exercised on real iOS only.

Documented the mobile/iOS layout in `docs/architecture.md` (new "Mobile / iOS
layout" section) and added `/.playwright-mcp/` to `.gitignore`.

## Verification

- `npm test`: 44/44 pass (37 existing + 7 new)
- `npm run typecheck`: clean
- Manual Playwright at 390×844: composer/header/scroll padding all 16px,
  textarea font-size 16px, `--vvh` set to viewport height, Send button has
  visible margin from the right edge

A real-device check on the user's iPhone is still the final word — headless
Chromium cannot validate the `env(safe-area-inset-*)` branch.
