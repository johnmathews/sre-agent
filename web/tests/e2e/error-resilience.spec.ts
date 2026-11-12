// End-to-end tests for the error / retry / recovery contract documented in
// docs/api-integration.md § "User-visible error UX".
//
// These run on every project (desktop chromium + webkit, mobile safari +
// chrome) so we catch engine-specific quirks — the original triage was an
// iOS-Safari-only failure that desktop testing missed entirely.

import { test, expect, type Page } from '@playwright/test'
import {
  buildSseBody,
  installRecoveryRoute,
  mockBackend,
  overrideAskStream,
  resetRecoveryFixtures,
} from './fixtures'

test.beforeEach(() => resetRecoveryFixtures())

async function send(page: Page, question = 'check the homelab'): Promise<void> {
  await page
    .getByPlaceholder('Ask about your infrastructure…')
    .fill(question)
  await page.getByRole('button', { name: 'Send' }).click()
}

test.describe('error resilience and recovery', () => {
  test('SSE error event renders an ErrorBubble (not a normal grey bubble)', async ({
    page,
  }) => {
    await mockBackend(page, {
      streamBody: buildSseBody([
        { type: 'status', content: 'Thinking...' },
        { type: 'error', content: 'Prometheus connection refused' },
      ]),
    })
    await page.goto('/')
    await send(page)

    // ErrorBubble has role="alert"; assert it shows up with the right copy.
    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible()
    await expect(alert).toContainText('Agent reported an error')

    // Distinct red styling, not a regular grey bubble.
    await expect(alert).toHaveClass(/error-bubble/)

    // Retry + Details controls are present.
    await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible()
    await expect(alert.getByRole('button', { name: 'Details' })).toBeVisible()

    // Original cause is hidden behind the disclosure.
    await expect(
      alert.getByText('Prometheus connection refused'),
    ).not.toBeVisible()
    await alert.getByRole('button', { name: 'Details' }).click()
    await expect(
      alert.getByText('Prometheus connection refused'),
    ).toBeVisible()
  })

  test('mid-stream failure → auto-retry → success (no error visible)', async ({
    page,
  }) => {
    await mockBackend(page) // happy path on the second attempt
    await overrideAskStream(page, { kind: 'midstream-abort', times: 1 })
    await page.goto('/')
    await send(page, 'mid-stream test')

    // The retry should have produced the canned successful answer.
    await expect(page.locator('strong').filter({ hasText: '42%' })).toBeVisible({
      timeout: 10_000,
    })
    // No error bubble surfaced.
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('mid-stream failure → recovery via GET /conversations finds persisted answer', async ({
    page,
  }) => {
    // Both /ask/stream attempts fail mid-stream; the server already persisted
    // the answer. The recovery layer should surface it without surfacing an
    // error to the user.
    await mockBackend(page)
    await overrideAskStream(page, { kind: 'midstream-abort', times: 5 })
    await installRecoveryRoute(page, {
      matchAny: {
        userQuestion: 'recovered question',
        assistantAnswer: 'Recovered: agent finished but client missed the bytes.',
      },
    })

    await page.goto('/')
    await send(page, 'recovered question')

    await expect(
      page.getByText(
        /Recovered: agent finished but client missed the bytes/,
      ),
    ).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('mid-stream failure with no persisted answer → ErrorBubble + retry button', async ({
    page,
  }) => {
    await mockBackend(page)
    // Both attempts fail; recovery returns 404 (mockBackend's default since
    // the session id isn't in sampleConversations).
    await overrideAskStream(page, { kind: 'midstream-abort', times: 5 })
    await page.goto('/')
    await send(page)

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible({ timeout: 10_000 })
    await expect(alert).toContainText('Connection dropped mid-reply')
    await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible()
  })

  test('HTTP 503 → auto-retry → second 503 → ErrorBubble shows status', async ({
    page,
  }) => {
    await mockBackend(page)
    await overrideAskStream(page, { kind: 'http-503', times: 5 })
    await page.goto('/')
    await send(page)

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible({ timeout: 10_000 })
    await expect(alert).toContainText(/HTTP 503/)
    await alert.getByRole('button', { name: 'Details' }).click()
    await expect(alert).toContainText('http-5xx')
    await expect(alert).toContainText('503')
  })

  test('HTTP 401 → no auto-retry → session-expired ErrorBubble', async ({
    page,
  }) => {
    // Order matters: install mockBackend first, then register the 401 route
    // so it wins (Playwright route precedence: last registered wins).
    await mockBackend(page)
    let askCount = 0
    await page.route('**/api/ask/stream', async (route) => {
      askCount += 1
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Session expired' }),
      })
    })
    await page.goto('/')
    await send(page)

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible({ timeout: 10_000 })
    await expect(alert).toContainText(/Session expired/)
    // 4xx must not auto-retry — exactly one /ask/stream request sent.
    expect(askCount).toBe(1)
  })

  test('Retry button re-issues the question and clears the error', async ({
    page,
  }) => {
    await mockBackend(page) // happy path resumes after the override drains
    await overrideAskStream(page, { kind: 'http-503', times: 5 })
    await page.goto('/')
    await send(page, 'flaky question')

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible({ timeout: 10_000 })

    // Drain the override so the retry hits the happy path.
    await page.unroute('**/api/ask/stream')
    await mockBackend(page) // re-install happy path

    await alert.getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('strong').filter({ hasText: '42%' })).toBeVisible({
      timeout: 10_000,
    })
    // Old error bubble removed.
    await expect(page.getByRole('alert')).toHaveCount(0)
    // The user message appears exactly once (retry deduped the old one).
    await expect(page.getByText('flaky question')).toHaveCount(1)
  })

  test('retry only removes the immediately-preceding user bubble', async ({
    page,
  }) => {
    // Regression: retryMessage previously walked the message list backwards
    // looking for the first user bubble whose content matched the failed
    // question. If the user had already asked the same question earlier in
    // the session, that walk would land on the *historical* user bubble and
    // splice everything between it and the error — wiping intermediate
    // turns. Retry must remove only the adjacent [user, error] pair.
    await mockBackend(page) // happy path; we'll override per send
    await page.goto('/')

    // Turn 1: ask the question; succeeds.
    await send(page, 'duplicate question')
    await expect(
      page.locator('strong').filter({ hasText: '42%' }),
    ).toBeVisible({ timeout: 10_000 })

    // Turn 2: a different question; succeeds. (Two assistant bubbles now.)
    await send(page, 'something else')
    await expect(page.getByText('something else')).toBeVisible()

    // Turn 3: ask the *same* question as turn 1 again, but force it to fail.
    await overrideAskStream(page, { kind: 'http-503', times: 5 })
    await send(page, 'duplicate question')
    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible({ timeout: 10_000 })

    // Drain the override so retry hits the happy path.
    await page.unroute('**/api/ask/stream')
    await mockBackend(page)

    // Hit Retry. The bug we're guarding against: the previous backward
    // scan would find the *earlier* matching user bubble (from turn 1) and
    // splice everything between it and the error bubble — wiping turn 2.
    // The fix only checks the immediately-preceding bubble. After retry
    // we expect three successful answers (turns 1, 2, and the retried 3),
    // turn 2's question still present, and no error bubble remaining.
    await alert.getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('alert')).toHaveCount(0, { timeout: 10_000 })
    await expect(
      page.locator('strong').filter({ hasText: '42%' }),
    ).toHaveCount(3, { timeout: 10_000 })
    await expect(page.getByText('something else')).toBeVisible() // mid-history kept
    // The "duplicate question" appears twice (turns 1 and the retried 3).
    await expect(page.getByText('duplicate question')).toHaveCount(2)
  })

  test('structured auth error renders reason-specific copy', async ({
    page,
  }) => {
    await mockBackend(page, {
      streamBody: buildSseBody([
        { type: 'status', content: 'Thinking...' },
        {
          type: 'error',
          content:
            "The agent can't authenticate to the LLM provider — its credential was rejected. An operator needs to renew it on the host and restart the agent.",
          reason: 'llm_auth_failed',
          detail: 'refresh token rejected by Anthropic (invalid_grant)',
        },
      ]),
    })
    await page.goto('/')
    await send(page)

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible()
    // Reason-specific heading.
    await expect(alert).toContainText("Agent can't reach the LLM")
    // Operator-facing message shown directly, NOT the canned generic text.
    await expect(alert).toContainText('renew it on the host')
    await expect(alert).not.toContainText('often a tool error')
    // Raw cause + reason live behind the Details disclosure.
    await expect(alert.getByText('invalid_grant')).not.toBeVisible()
    await alert.getByRole('button', { name: 'Details' }).click()
    await expect(alert.getByText('invalid_grant')).toBeVisible()
    await expect(alert).toContainText('llm_auth_failed')
  })

  test('successful happy path still works', async ({ page }) => {
    // Sanity: error-resilience plumbing didn't break the basic flow.
    await mockBackend(page)
    await page.goto('/')
    await send(page, 'happy path')
    await expect(page.locator('strong').filter({ hasText: '42%' })).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByRole('alert')).toHaveCount(0)
  })
})
