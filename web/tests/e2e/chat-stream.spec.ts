import { test, expect } from '@playwright/test'
import { mockBackend, buildSseBody } from './fixtures'

test.describe('chat streaming', () => {
  test('sends a question and renders the streamed answer', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await input.fill('What is the current CPU usage?')
    await page.getByRole('button', { name: 'Send' }).click()

    // User bubble
    await expect(
      page.getByText('What is the current CPU usage?'),
    ).toBeVisible()

    // Assistant answer (markdown-rendered: **42%** -> <strong>)
    await expect(page.getByText('CPU is at')).toBeVisible()
    await expect(page.locator('strong').filter({ hasText: '42%' })).toBeVisible()

    // Input cleared + Send button reappears after stream completes
    await expect(input).toHaveValue('')
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible()
  })

  test('renders an ErrorBubble when the stream emits an error event', async ({
    page,
  }) => {
    const errorBody = buildSseBody([
      { type: 'status', content: 'Thinking...' },
      { type: 'error', content: 'Prometheus connection refused' },
    ])
    await mockBackend(page, { streamBody: errorBody })
    await page.goto('/')

    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('query something')
    await page.getByRole('button', { name: 'Send' }).click()

    // ErrorBubble is rendered with role="alert" and a distinct heading.
    // The full failure-class matrix lives in error-resilience.spec.ts;
    // this test is the smoke check that the SSE error event still produces
    // a user-visible bubble with a Retry affordance.
    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible()
    await expect(alert).toContainText('Agent reported an error')
    await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible()
  })

  test('Enter behaviour follows Slack-style platform rules', async ({
    page,
  }, testInfo) => {
    await mockBackend(page)
    await page.goto('/')

    const input = page.getByPlaceholder('Ask about your infrastructure…')

    // The component branches on `(pointer: coarse)`. Playwright's mobile
    // device descriptors set `hasTouch: true` which makes that media query
    // match, putting us on the "Enter = newline" path. Desktop projects
    // get "Enter = submit, Shift+Enter = newline".
    const isMobile = testInfo.project.name.startsWith('mobile-')

    if (isMobile) {
      // Plain Enter inserts a newline (no submit).
      await input.fill('line one')
      await input.press('Enter')
      await input.pressSequentially('line two')
      await expect(input).toHaveValue('line one\nline two')
      // Send button submits.
      await page.getByRole('button', { name: 'Send' }).click()
      await expect(page.getByText('line one')).toBeVisible()
      await expect(input).toHaveValue('')
    } else {
      // Shift+Enter adds newline, plain Enter submits.
      await input.fill('line one')
      await input.press('Shift+Enter')
      await input.pressSequentially('line two')
      await expect(input).toHaveValue('line one\nline two')
      await input.press('Enter')
      await expect(page.getByText('line one')).toBeVisible()
      await expect(input).toHaveValue('')
    }
  })

  test('Cmd/Ctrl+Enter submits on every platform', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')
    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await input.fill('cmd-enter test')
    await input.press('ControlOrMeta+Enter')
    await expect(page.getByText('cmd-enter test')).toBeVisible()
    await expect(input).toHaveValue('')
  })

  test('textarea has enterkeyhint=send (relabels iOS Return key)', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')
    const hint = await page
      .getByPlaceholder('Ask about your infrastructure…')
      .getAttribute('enterkeyhint')
    expect(hint).toBe('send')
  })

  test('Send button disabled for empty input', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const sendBtn = page.getByRole('button', { name: 'Send' })
    await expect(sendBtn).toBeDisabled()

    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('hello')
    await expect(sendBtn).toBeEnabled()
  })
})
