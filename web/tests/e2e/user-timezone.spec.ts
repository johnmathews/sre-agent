// Verify the webapp forwards the device timezone on every /ask/stream
// request. We stub Intl.DateTimeFormat directly rather than driving it
// through the browser context — that way the test pins the contract
// of our getDeviceTimezone() helper rather than relying on Playwright's
// emulation.

import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

/** Replace Intl.DateTimeFormat().resolvedOptions().timeZone with `tz`. */
async function stubIntlTimezone(
  page: import('@playwright/test').Page,
  tz: string,
): Promise<void> {
  await page.addInitScript((zone: string) => {
    const fake = (() => ({
      resolvedOptions: () => ({ timeZone: zone }),
    })) as unknown as typeof Intl.DateTimeFormat
    Intl.DateTimeFormat = fake
  }, tz)
}

async function captureNextStreamBody(
  page: import('@playwright/test').Page,
): Promise<Record<string, unknown>> {
  return await new Promise((resolve) => {
    page.route('**/api/ask/stream', (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<
        string,
        unknown
      >
      resolve(body)
      void route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: 'data: {"type":"answer","content":"ok","session_id":"s1"}\n\n',
      })
    })
  })
}

test.describe('user_timezone forwarding', () => {
  test('sends the device timezone with every /ask/stream request', async ({
    page,
  }) => {
    await stubIntlTimezone(page, 'Asia/Seoul')
    await mockBackend(page)
    await page.goto('/')

    const bodyPromise = captureNextStreamBody(page)
    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('what time is it?')
    await page.getByRole('button', { name: 'Send' }).click()

    const body = await bodyPromise
    expect(body.user_timezone).toBe('Asia/Seoul')
    expect(body.question).toBe('what time is it?')
  })

  test('sends a different zone for a different device', async ({ page }) => {
    await stubIntlTimezone(page, 'Europe/Amsterdam')
    await mockBackend(page)
    await page.goto('/')

    const bodyPromise = captureNextStreamBody(page)
    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('and now from home')
    await page.getByRole('button', { name: 'Send' }).click()

    const body = await bodyPromise
    expect(body.user_timezone).toBe('Europe/Amsterdam')
  })

  test('omits user_timezone when Intl returns an empty timezone', async ({
    page,
  }) => {
    await stubIntlTimezone(page, '')
    await mockBackend(page)
    await page.goto('/')

    const bodyPromise = captureNextStreamBody(page)
    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('no intl')
    await page.getByRole('button', { name: 'Send' }).click()

    const body = await bodyPromise
    expect(body).not.toHaveProperty('user_timezone')
    expect(body.question).toBe('no intl')
  })
})
