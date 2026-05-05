// Desktop ergonomics: taller composer above md, view-mode toggle on lg+.
//
// Both behaviours are viewport-conditional, so the test asserts on each
// project: desktop projects (1280px) get the taller composer and the
// document-mode toggle; mobile projects (≤412px) get the compact composer
// and the toggle is hidden + mode is forced to conversation regardless of
// the localStorage preference.

import { test, expect, type Page } from '@playwright/test'
import { mockBackend, buildSseBody } from './fixtures'

const wideTableAnswer = `Here you go:

| Time | Path | Group | Endpoint | Response |
|------|------|-------|----------|----------|
| 09:43 | /api/x | Infra | alerts | 404 |
| 09:44 | /api/y | Charts | stats | 200 |

Done.`

async function send(page: Page, q = 'show me a table'): Promise<void> {
  await page.getByPlaceholder('Ask about your infrastructure…').fill(q)
  await page.getByRole('button', { name: 'Send' }).click()
}

async function readPxHeight(loc: ReturnType<Page['locator']>): Promise<number> {
  return await loc.evaluate(
    (el) => parseFloat(getComputedStyle(el).height),
  )
}

test.describe('composer min-height', () => {
  test('grows on md+ viewports, stays compact on mobile', async ({
    page,
  }, testInfo) => {
    await mockBackend(page)
    await page.goto('/')

    const textarea = page.getByPlaceholder('Ask about your infrastructure…')
    await expect(textarea).toBeVisible()
    const h = await readPxHeight(textarea)

    if (testInfo.project.name.startsWith('mobile-')) {
      // Mobile: ~2.5rem (40px) min-height, allowing some browser variance.
      expect(h).toBeGreaterThanOrEqual(36)
      expect(h).toBeLessThanOrEqual(56)
    } else {
      // md+ (640+): the inviting 3-line min-height kicks in. Both desktop
      // projects are 1280×720, well above md, so we get min-h-24 ≈ 96px.
      expect(h).toBeGreaterThanOrEqual(80)
    }
  })
})

test.describe('view-mode toggle (lg+ only)', () => {
  test('toggle is hidden on mobile, visible on desktop', async ({
    page,
  }, testInfo) => {
    await mockBackend(page)
    await page.goto('/')

    const conversationBtn = page.getByRole('button', {
      name: /conversation view/i,
    })
    const documentBtn = page.getByRole('button', { name: /document view/i })

    if (testInfo.project.name.startsWith('mobile-')) {
      // 393–412px viewports — below lg (1024). Toggle hidden via Tailwind's
      // `hidden lg:flex`.
      await expect(conversationBtn).toBeHidden()
      await expect(documentBtn).toBeHidden()
    } else {
      await expect(conversationBtn).toBeVisible()
      await expect(documentBtn).toBeVisible()
    }
  })

  test('toggling document mode switches layout and persists across reload', async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name.startsWith('mobile-'),
      'document mode only renders on lg+ viewports',
    )

    await mockBackend(page, {
      streamBody: buildSseBody([
        { type: 'answer', content: wideTableAnswer, session_id: 'doc00001' },
      ]),
    })
    await page.goto('/')
    await send(page)
    // Wait for the agent reply to render so a message bubble exists to
    // re-render when we flip the mode.
    await expect(page.getByText('Done.').last()).toBeVisible({
      timeout: 10_000,
    })

    // Default mode is conversation — bubbles use max-w-[85%].
    await expect(page.locator('.document-block')).toHaveCount(0)

    // Click "Document view".
    await page.getByRole('button', { name: /document view/i }).click()

    // Document mode shows role labels + .document-block container.
    await expect(page.locator('.document-block').first()).toBeVisible()
    await expect(page.getByText('You', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Agent', { exact: true }).first()).toBeVisible()

    // localStorage was updated.
    const stored = await page.evaluate(() =>
      window.localStorage.getItem('chat-view-mode'),
    )
    expect(stored).toBe('document')

    // Reload and confirm the mode persists.
    await page.reload()
    await mockBackend(page) // re-register routes after reload
    await send(page, 'second question')
    await expect(page.locator('.document-block').first()).toBeVisible({
      timeout: 10_000,
    })

    // Switch back to conversation; bubbles return.
    await page.getByRole('button', { name: /conversation view/i }).click()
    await expect(page.locator('.document-block')).toHaveCount(0)
  })

  test('document mode in localStorage does NOT apply on a small viewport', async ({
    page,
  }, testInfo) => {
    test.skip(
      !testInfo.project.name.startsWith('mobile-'),
      'mobile-only — verifying the lg-clamp on effectiveViewMode',
    )

    // Pre-seed localStorage so it looks like the user enabled document mode
    // on desktop and is now viewing on a phone.
    await page.addInitScript(() => {
      window.localStorage.setItem('chat-view-mode', 'document')
    })

    await mockBackend(page, {
      streamBody: buildSseBody([
        { type: 'answer', content: 'Hello there.', session_id: 'mob00001' },
      ]),
    })
    await page.goto('/')
    await send(page)
    await expect(page.getByText('Hello there.')).toBeVisible({
      timeout: 10_000,
    })

    // Bubbles render — document layout is clamped on small viewports
    // regardless of the stored preference.
    await expect(page.locator('.document-block')).toHaveCount(0)
  })
})
