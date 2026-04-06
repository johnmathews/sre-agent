import { test, expect } from '@playwright/test'
import { mockBackend, degradedHealth } from './fixtures'

test.describe('health panel', () => {
  test('shows healthy status collapsed by default', async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')

    // Wait for health to load
    await expect(
      page.getByText(/healthy \(\d+\/\d+\)/),
    ).toBeVisible()

    // Details collapsed: component list not shown
    await expect(page.getByText('prometheus:')).not.toBeVisible()

    // Expand
    await page.locator('aside').getByRole('button').filter({ hasText: 'Health' }).click()
    await expect(page.getByText('prometheus:').first()).toBeVisible()
    await expect(page.getByText('grafana:').first()).toBeVisible()
  })

  test('stays collapsed when health is degraded, expands on click', async ({ page }) => {
    await mockBackend(page, { health: degradedHealth })
    await page.goto('/')

    await expect(page.getByText(/degraded \(\d+\/\d+\)/)).toBeVisible()
    // Details collapsed by default even when degraded
    await expect(page.getByText('grafana:')).not.toBeVisible()

    // Click to expand
    await page.locator('aside').getByRole('button').filter({ hasText: 'Health' }).click()
    await expect(page.getByText('grafana:').first()).toBeVisible()
    await expect(page.getByText('HTTP 503')).toBeVisible()
  })

  test('shows error when backend is unreachable', async ({ page }) => {
    await page.route('**/api/health', (route) =>
      route.fulfill({ status: 500, body: 'boom' }),
    )
    await page.route('**/api/conversations', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
    )
    await page.goto('/')

    await expect(
      page.getByText('Cannot reach API server.'),
    ).toBeVisible()
  })
})
