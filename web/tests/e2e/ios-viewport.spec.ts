import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

// iPhone 12 viewport. Note: headless Chromium reports env(safe-area-inset-*)
// as 0, so these tests verify the *floor* values from `max(<floor>, env(...))`
// — i.e. the padding the layout falls back to when no insets exist. The
// safe-area branch is exercised on real iOS only (see CLAUDE.md).
test.use({ viewport: { width: 390, height: 844 } })

test.describe('iOS viewport / safe-area handling', () => {
  test('viewport meta tag enables safe-area insets', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const content = await page
      .locator('meta[name="viewport"]')
      .getAttribute('content')
    expect(content).toContain('viewport-fit=cover')
    expect(content).toContain('width=device-width')
  })

  test('input font-size is at least 16px (prevents iOS auto-zoom)', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')

    const textarea = page.getByPlaceholder('Ask about your infrastructure…')
    const fontSize = await textarea.evaluate(
      (el) => parseFloat(getComputedStyle(el).fontSize),
    )
    expect(fontSize).toBeGreaterThanOrEqual(16)
  })

  test('composer area has corner-clear horizontal padding', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const composer = page.locator('.composer-area')
    await expect(composer).toBeVisible()

    const padding = await composer.evaluate((el) => {
      const cs = getComputedStyle(el)
      return {
        left: parseFloat(cs.paddingLeft),
        right: parseFloat(cs.paddingRight),
        bottom: parseFloat(cs.paddingBottom),
      }
    })
    // 1rem floor on each side keeps the Send button clear of iPhone's
    // rounded-corner curve in portrait, where safe-area-inset-right is 0.
    expect(padding.left).toBeGreaterThanOrEqual(16)
    expect(padding.right).toBeGreaterThanOrEqual(16)
    // Bottom floor is 0.75rem; on real iOS this expands to safe-area-inset-bottom.
    expect(padding.bottom).toBeGreaterThanOrEqual(12)
  })

  test('mobile header has safe-area padding for status bar', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const header = page.locator('.app-header-mobile')
    await expect(header).toBeVisible()

    const padding = await header.evaluate((el) => {
      const cs = getComputedStyle(el)
      return {
        left: parseFloat(cs.paddingLeft),
        right: parseFloat(cs.paddingRight),
        top: parseFloat(cs.paddingTop),
      }
    })
    expect(padding.left).toBeGreaterThanOrEqual(16)
    expect(padding.right).toBeGreaterThanOrEqual(16)
    expect(padding.top).toBeGreaterThanOrEqual(8)
  })

  test('chat scroll area has safe-area padding (matches composer)', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')

    const scroll = page.locator('.chat-scroll-area')
    await expect(scroll).toBeVisible()

    const padding = await scroll.evaluate((el) => {
      const cs = getComputedStyle(el)
      return {
        left: parseFloat(cs.paddingLeft),
        right: parseFloat(cs.paddingRight),
      }
    })
    expect(padding.left).toBeGreaterThanOrEqual(16)
    expect(padding.right).toBeGreaterThanOrEqual(16)
  })

  test('root container uses 100dvh for keyboard-aware sizing', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')

    // We replaced the JS visualViewport observer with `100dvh`. Assert the
    // computed height resolves to a px value in the dynamic-viewport range.
    // `dvh` shrinks when the iOS keyboard opens; without the keyboard it
    // should equal the viewport height (allow ~50px of browser-chrome slack).
    const root = page.locator('.flex.w-screen.overflow-hidden').first()
    const height = await root.evaluate(
      (el) => parseFloat(getComputedStyle(el).height),
    )
    expect(height).toBeGreaterThan(500)
    expect(height).toBeLessThanOrEqual(844)
  })

  test('Send button is positioned inward from the right viewport edge', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')

    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await input.fill('test')

    const button = page.getByRole('button', { name: 'Send' })
    const box = await button.boundingBox()
    expect(box).not.toBeNull()
    // The button's right edge should be at least 16px from the viewport edge
    // — that's the floor that keeps it clear of iPhone's corner curve.
    const rightGap = 390 - (box!.x + box!.width)
    expect(rightGap).toBeGreaterThanOrEqual(16)
  })
})
