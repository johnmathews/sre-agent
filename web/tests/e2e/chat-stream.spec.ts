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

  test('renders an error bubble when the stream emits an error event', async ({
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

    await expect(page.getByText(/Error.*Prometheus connection refused/)).toBeVisible()
  })

  test('Enter submits, Shift+Enter inserts newline', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const input = page.getByPlaceholder('Ask about your infrastructure…')

    // Shift+Enter adds newline, does not submit
    await input.fill('line one')
    await input.press('Shift+Enter')
    await input.pressSequentially('line two')
    await expect(input).toHaveValue('line one\nline two')

    // Plain Enter submits
    await input.press('Enter')
    await expect(page.getByText('line one')).toBeVisible()
    await expect(input).toHaveValue('')
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
