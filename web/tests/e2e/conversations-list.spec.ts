import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

test.describe('conversations list', () => {
  test('renders past conversations from the API', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await expect(page.getByText('CPU spike investigation')).toBeVisible()
    await expect(page.getByText('Disk usage check')).toBeVisible()
  })

  test('shows empty state when no conversations exist', async ({ page }) => {
    await mockBackend(page, { conversations: [] })
    await page.goto('/')

    await expect(page.getByText('No past conversations yet.')).toBeVisible()
  })

  test('loading a conversation populates the chat window', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByText('CPU spike investigation').click()

    // Messages from the canned detail payload
    await expect(
      page.getByText('What is the current CPU usage?'),
    ).toBeVisible()
    await expect(page.getByText('CPU usage is at')).toBeVisible()

    // The loaded session id is now shown
    await expect(
      page.locator('code').filter({ hasText: 'abc12345' }),
    ).toBeVisible()
  })

  test('clicking "+ New conversation" clears messages and generates new session', async ({
    page,
  }) => {
    await mockBackend(page)
    await page.goto('/')

    // Load a past conversation first
    await page.getByText('CPU spike investigation').click()
    await expect(page.getByText('What is the current CPU usage?')).toBeVisible()

    // Start fresh
    await page.getByRole('button', { name: '+ New conversation' }).click()

    await expect(page.getByText('What is the current CPU usage?')).not.toBeVisible()
    await expect(
      page.getByText('Ask about your infrastructure.', { exact: false }),
    ).toBeVisible()
  })
})
