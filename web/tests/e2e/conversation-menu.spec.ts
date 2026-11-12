import { test, expect } from '@playwright/test'
import { mockBackend } from './fixtures'

test.describe('conversation row menu', () => {
  test('renames a conversation through the inline flow', async ({ page }) => {
    const state = await mockBackend(page)
    await page.goto('/')

    const row = page
      .locator('.group')
      .filter({ hasText: 'CPU spike investigation' })

    await row.getByLabel('Open menu').click()
    await row.getByRole('button', { name: 'Rename' }).click()

    const input = row.locator('input[type="text"]')
    await expect(input).toBeFocused()
    await input.fill('Renamed title')
    await row.getByRole('button', { name: 'Save' }).click()

    // Backend received the PATCH
    await expect.poll(() => state.renamed.get('abc12345')).toBe('Renamed title')
    // And the list reflects the new title
    await expect(page.getByText('Renamed title')).toBeVisible()
  })

  test('delete requires confirmation and removes the row', async ({ page }) => {
    const state = await mockBackend(page)
    await page.goto('/')

    const row = page
      .locator('.group')
      .filter({ hasText: 'Disk usage check' })

    await row.getByLabel('Open menu').click()
    await row.getByRole('button', { name: 'Delete' }).click()

    // Confirmation prompt visible, row button still present
    await expect(row.getByText(/Delete .Disk usage check.\?/)).toBeVisible()
    await expect(
      row.getByRole('button', { name: 'Disk usage check' }),
    ).toBeVisible()

    // Confirm
    await row.getByRole('button', { name: 'Delete' }).click()

    await expect.poll(() => state.deleted.has('def67890')).toBe(true)
    await expect(
      page.getByRole('button', { name: 'Disk usage check' }),
    ).not.toBeVisible()
  })

  test('cancel closes the menu without changes', async ({ page }) => {
    const state = await mockBackend(page)
    await page.goto('/')

    const row = page
      .locator('.group')
      .filter({ hasText: 'CPU spike investigation' })

    await row.getByLabel('Open menu').click()
    await row.getByRole('button', { name: 'Rename' }).click()

    const input = row.locator('input[type="text"]')
    await input.fill('Should not save')
    await row.getByRole('button', { name: 'Cancel' }).click()

    await expect(input).not.toBeVisible()
    await expect(page.getByText('CPU spike investigation')).toBeVisible()
    expect(state.renamed.size).toBe(0)
  })

  test('deleting the active conversation resets to a new one', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    // Load CPU spike as active
    await page.getByText('CPU spike investigation').click()
    await expect(page.getByText('What is the current CPU usage?')).toBeVisible()

    const activeSessionCode = page.locator('code').filter({ hasText: 'abc12345' })
    await expect(activeSessionCode).toBeVisible()

    // Delete it
    const row = page
      .locator('.group')
      .filter({ hasText: 'CPU spike investigation' })
    await row.getByLabel('Open menu').click()
    await row.getByRole('button', { name: 'Delete' }).click()
    await row.getByRole('button', { name: 'Delete' }).click()

    // Chat reset and session id changed
    await expect(page.getByText('What is the current CPU usage?')).not.toBeVisible()
    await expect(activeSessionCode).not.toBeVisible()
  })
})
