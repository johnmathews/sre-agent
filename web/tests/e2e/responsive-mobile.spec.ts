import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

// iPhone 12 viewport
test.use({ viewport: { width: 390, height: 844 } })

test.describe('responsive mobile layout', () => {
  test('sidebar is hidden by default on mobile', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    // Hamburger menu is visible
    await expect(page.getByRole('button', { name: 'Open sidebar' })).toBeVisible()

    // No backdrop visible (sidebar is closed)
    await expect(page.locator('.bg-black\\/40')).not.toBeVisible()
  })

  test('hamburger opens sidebar overlay', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByRole('button', { name: 'Open sidebar' }).click()

    // Sidebar is now visible
    await expect(page.getByText('+ New conversation')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Close sidebar' })).toBeVisible()
    await expect(page.getByPlaceholder('Search conversations...')).toBeVisible()
  })

  test('close button hides sidebar', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByRole('button', { name: 'Open sidebar' }).click()
    await expect(page.getByRole('button', { name: 'Close sidebar' })).toBeVisible()

    await page.getByRole('button', { name: 'Close sidebar' }).click()

    // Backdrop should be gone
    await expect(page.locator('.bg-black\\/40')).not.toBeVisible()
  })

  test('selecting a conversation closes sidebar on mobile', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByRole('button', { name: 'Open sidebar' }).click()

    // Click a conversation
    await page.getByText('CPU spike investigation').click()

    // Sidebar should close, conversation loaded
    await expect(page.getByText('CPU usage is at')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Open sidebar' })).toBeVisible()
  })

  test('new conversation closes sidebar on mobile', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByRole('button', { name: 'Open sidebar' }).click()
    await page.getByText('+ New conversation').click()

    // Sidebar should close
    await expect(page.getByRole('button', { name: 'Open sidebar' })).toBeVisible()
    await expect(
      page.getByRole('heading', { name: 'Ask about your infrastructure' }),
    ).toBeVisible()
  })

  test('input area is usable on mobile', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await expect(input).toBeVisible()
    await expect(input).toBeEnabled()

    // Can type in it
    await input.fill('Check CPU usage')
    await expect(input).toHaveValue('Check CPU usage')
    await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  test('session ID is hidden on mobile sidebar', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByRole('button', { name: 'Open sidebar' }).click()

    // Session ID should not be visible (hidden on mobile to save space)
    await expect(page.getByText('Session:')).not.toBeVisible()
  })
})
