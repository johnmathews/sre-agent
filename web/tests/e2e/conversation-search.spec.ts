import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

test.describe('conversation search', () => {
  test('search input is visible in sidebar', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await expect(
      page.getByPlaceholder('Search conversations...'),
    ).toBeVisible()
  })

  test('typing a query shows matching results', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const searchInput = page.getByPlaceholder('Search conversations...')
    await searchInput.fill('CPU')

    // Search results show snippet with role label
    await expect(page.getByText('Title:')).toBeVisible()
  })

  test('shows no matches message for unmatched query', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    await page.getByPlaceholder('Search conversations...').fill('kubernetes')

    await expect(page.getByText('No matches found.')).toBeVisible()
  })

  test('clicking a result loads the conversation', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const searchInput = page.getByPlaceholder('Search conversations...')
    await searchInput.fill('Disk')

    // Wait for search results with role label
    await expect(page.getByText('Title:').first()).toBeVisible()

    // Click the search result (the one with "Disk usage check" title div)
    const resultButton = page.locator('button').filter({ has: page.locator('div', { hasText: 'Disk usage check' }) }).first()
    await resultButton.click()

    // Conversation should be loaded — session id visible
    await expect(
      page.locator('code').filter({ hasText: 'def67890' }),
    ).toBeVisible()

    // Search should be cleared
    await expect(searchInput).toHaveValue('')
  })

  test('clearing search hides results', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    const searchInput = page.getByPlaceholder('Search conversations...')
    await searchInput.fill('CPU')

    // Wait for results
    await expect(page.getByText('Title:').first()).toBeVisible()

    // Clear via the X button
    await page.locator('[title="Clear search"]').click()

    await expect(searchInput).toHaveValue('')
    // Results should be gone
    await expect(page.getByText('Title:')).not.toBeVisible()
  })
})
