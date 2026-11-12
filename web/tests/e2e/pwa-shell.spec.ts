// PWA shell tests. The user installs the webapp as a standalone iOS PWA;
// without the manifest + apple meta tags iOS launches it inside Safari
// chrome and `env(safe-area-inset-top)` resolves to 0, leaving the title
// overlapping the device status bar. These tests lock in the shell so a
// future refactor can't quietly remove a meta tag and re-introduce the bug.

import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'
import { simulatePwaInsets, IPHONE_15_PRO_INSETS } from './helpers/pwa'

test.describe('PWA shell', () => {
  test('manifest is linked and parseable', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const href = await page
      .locator('link[rel="manifest"]')
      .getAttribute('href')
    expect(href).toBe('/manifest.webmanifest')

    const res = await page.request.get(href!)
    expect(res.ok()).toBe(true)
    const manifest = await res.json()
    expect(manifest.display).toBe('standalone')
    expect(manifest.start_url).toBe('/')
    expect(manifest.scope).toBe('/')
    expect(manifest.theme_color).toBeTruthy()
    expect(manifest.background_color).toBeTruthy()
    expect(manifest.icons).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ sizes: '192x192' }),
        expect.objectContaining({ sizes: '512x512' }),
      ]),
    )
  })

  test('apple-mobile-web-app meta tags present', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const capable = await page
      .locator('meta[name="apple-mobile-web-app-capable"]')
      .getAttribute('content')
    expect(capable).toBe('yes')

    const statusBar = await page
      .locator('meta[name="apple-mobile-web-app-status-bar-style"]')
      .getAttribute('content')
    // black-translucent draws the status bar over the page; CSS
    // safe-area-inset-top handles clearance. See style.css.
    expect(statusBar).toBe('black-translucent')

    const appleIcon = await page
      .locator('link[rel="apple-touch-icon"]')
      .getAttribute('href')
    expect(appleIcon).toMatch(/apple-touch-icon/)
  })

  test('theme-color meta tags drive OS chrome tinting', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const themeColors = await page
      .locator('meta[name="theme-color"]')
      .all()
    expect(themeColors.length).toBeGreaterThanOrEqual(1)
  })

  test('apple-touch-icon resource is reachable and a real PNG', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')
    const res = await page.request.get('/apple-touch-icon-180.png')
    expect(res.ok()).toBe(true)
    const buf = await res.body()
    // PNG signature
    expect(buf[0]).toBe(0x89)
    expect(buf[1]).toBe(0x50)
    expect(buf[2]).toBe(0x4e)
    expect(buf[3]).toBe(0x47)
  })
})

test.describe('PWA shell — safe-area branch', () => {
  // Simulate iPhone 15 Pro insets (top: 59px, bottom: 34px) so we exercise
  // the inset branch of `max(<floor>, env(...))` that headless engines
  // can't otherwise reach. The title and Send button must clear the system
  // chrome regardless of viewport.
  test.use({ viewport: { width: 393, height: 852 } })

  test('mobile header clears the simulated status bar (top inset 59px)', async ({
    page,
  }) => {
    await simulatePwaInsets(page, IPHONE_15_PRO_INSETS)
    await mockBackend(page)
    await page.goto('/')

    const header = page.locator('.app-header-mobile')
    await expect(header).toBeVisible()
    const top = await header.evaluate(
      (el) => parseFloat(getComputedStyle(el).paddingTop),
    )
    // Floor is 0.5rem = 8px; with simulated 59px inset the rule should
    // expand to 59. If this regresses to 8 the title will sit under the
    // status bar exactly like the user reported.
    expect(top).toBeGreaterThanOrEqual(59)
  })

  test('composer clears the simulated home indicator (bottom inset 34px)', async ({
    page,
  }) => {
    await simulatePwaInsets(page, IPHONE_15_PRO_INSETS)
    await mockBackend(page)
    await page.goto('/')

    const composer = page.locator('.composer-area')
    await expect(composer).toBeVisible()
    const bottom = await composer.evaluate(
      (el) => parseFloat(getComputedStyle(el).paddingBottom),
    )
    expect(bottom).toBeGreaterThanOrEqual(34)
  })
})
