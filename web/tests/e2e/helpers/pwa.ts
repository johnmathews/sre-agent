// Helpers for simulating an iOS PWA's safe-area insets in tests.
//
// Real iOS Safari in standalone mode populates `env(safe-area-inset-*)` from
// the device's notch / home indicator. Headless engines (including Playwright
// WebKit on Linux) report 0 because there's no device chrome. Our CSS
// safe-area helpers fall through to the `<floor>` value in
// `max(<floor>, env(safe-area-inset-*))`, so we never exercise the
// notch-clearing branch.
//
// To make the safe-area branch testable without a real device, the production
// CSS reads `var(--safe-area-inset-*, env(safe-area-inset-*))`. In a real PWA
// the variable is unset, so `env()` wins. In tests this helper sets the
// variables to simulated device values.
//
// On a real iPhone 15 Pro: top = 59px, bottom = 34px, left/right = 0 in
// portrait. iPhone SE (2nd gen): top = 20px, bottom = 0.

import type { Page } from '@playwright/test'

export interface SimulatedInsets {
  top?: number
  bottom?: number
  left?: number
  right?: number
}

export const IPHONE_15_PRO_INSETS: SimulatedInsets = {
  top: 59,
  bottom: 34,
  left: 0,
  right: 0,
}

/**
 * Inject simulated safe-area inset CSS variables for the duration of the
 * page's lifetime. Call before `page.goto('/')` so the styles apply on first
 * paint. Adds an `addInitScript` that runs in every navigation.
 */
export async function simulatePwaInsets(
  page: Page,
  insets: SimulatedInsets,
): Promise<void> {
  const { top = 0, bottom = 0, left = 0, right = 0 } = insets
  await page.addInitScript(
    ({ top, bottom, left, right }) => {
      const apply = () => {
        const root = document.documentElement
        if (!root) return
        root.style.setProperty('--safe-area-inset-top', `${top}px`)
        root.style.setProperty('--safe-area-inset-bottom', `${bottom}px`)
        root.style.setProperty('--safe-area-inset-left', `${left}px`)
        root.style.setProperty('--safe-area-inset-right', `${right}px`)
      }
      if (document.documentElement) apply()
      else document.addEventListener('DOMContentLoaded', apply, { once: true })
    },
    { top, bottom, left, right },
  )
}

/**
 * Simulate the iOS soft keyboard appearing by shrinking `window.visualViewport`
 * to a smaller height. Triggers a `resize` event so any visualViewport
 * listener responds. Pass `keyboardHeight` in CSS pixels; iOS keyboard is
 * roughly 290–340px depending on autocomplete bar.
 */
export async function simulateKeyboard(
  page: Page,
  keyboardHeight: number,
): Promise<void> {
  await page.evaluate((kh: number) => {
    const vv = window.visualViewport
    if (!vv) return
    const originalHeight = vv.height
    Object.defineProperty(vv, 'height', {
      configurable: true,
      get: () => originalHeight - kh,
    })
    vv.dispatchEvent(new Event('resize'))
  }, keyboardHeight)
}
