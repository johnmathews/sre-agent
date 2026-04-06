import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

test.describe('initial load', () => {
  test('renders the sidebar and empty chat state', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await expect(
      page.getByRole('heading', { name: 'SRE Agent', level: 1 }),
    ).toBeVisible()

    await expect(
      page.getByRole('button', { name: '+ New conversation' }),
    ).toBeVisible()

    await expect(
      page.getByText('Ask about your infrastructure.', { exact: false }),
    ).toBeVisible()

    // Input area is present and empty
    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await expect(input).toBeVisible()
    await expect(input).toHaveValue('')
  })

  test('displays the session id', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    // Session ID is an 8-char hex string rendered inside a <code> element
    const sessionCode = page.locator('code').filter({ hasText: /^[a-f0-9]{8}$/ })
    await expect(sessionCode).toBeVisible()
  })
})
