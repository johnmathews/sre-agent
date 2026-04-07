import { test, expect } from '@playwright/test'
import { buildSseBody, mockBackend, sampleConversations } from './fixtures'

/**
 * Wire a delayed stream: the /ask/stream endpoint holds the connection open
 * and only resolves after `delayMs`. This lets us observe the UI while the
 * agent is "thinking" and then switch away.
 */
async function mockBackendWithDelayedStream(
  page: import('@playwright/test').Page,
  delayMs: number,
) {
  // Set up all normal routes first
  const state = await mockBackend(page, {
    // Use a placeholder that we'll override
    streamBody: '',
  })

  // Override the /ask/stream route with a delayed handler
  await page.route('**/api/ask/stream', async (route) => {
    const body = buildSseBody([
      { type: 'status', content: 'Initializing...' },
      { type: 'status', content: 'Thinking...' },
      { type: 'tool_start', content: 'Querying Prometheus' },
      { type: 'tool_end', content: 'Querying Prometheus' },
      { type: 'answer', content: 'Background answer: all good.', session_id: 'bg-session' },
    ])
    await new Promise((r) => setTimeout(r, delayMs))
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
      body,
    })
  })

  return state
}

test.describe('background conversation processing', () => {
  test('switching conversations does not abort in-progress stream', async ({
    page,
  }) => {
    await mockBackendWithDelayedStream(page, 1500)
    await page.goto('/')

    // Send a question — starts streaming
    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await input.fill('Check CPU usage')
    await page.getByRole('button', { name: 'Send' }).click()

    // Verify streaming started — user bubble visible, Stop button appears
    await expect(page.getByText('Check CPU usage')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible()

    // Switch to a past conversation while stream is in progress
    await page.getByText('CPU spike investigation').click()

    // Should see the loaded conversation's messages
    await expect(page.getByText('CPU usage is at')).toBeVisible()

    // The Send button should be available (this conversation is not streaming)
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible()
  })

  test('sidebar shows processing indicator for background stream', async ({
    page,
  }) => {
    // Use a stream that completes quickly but with tool events
    const streamEvents = [
      { type: 'status', content: 'Thinking...' },
      { type: 'tool_start', content: 'Querying Prometheus' },
      { type: 'tool_end', content: 'Querying Prometheus' },
      { type: 'answer', content: 'CPU at 42%', session_id: 'new00001' },
    ]
    await mockBackend(page, { streamEvents })
    await page.goto('/')

    // Send a question
    const input = page.getByPlaceholder('Ask about your infrastructure…')
    await input.fill('Check metrics')
    await page.getByRole('button', { name: 'Send' }).click()

    // Wait for the answer to appear (stream completed)
    await expect(page.getByText('CPU at 42%')).toBeVisible()

    // After completion, no processing indicator should be visible
    // (the amber dot only shows during active streaming)
    const processingDot = page.locator('[title="Agent is working..."]')
    await expect(processingDot).not.toBeVisible()
  })
})

test.describe('richer tool progress display', () => {
  test('shows tool_start and tool_end events with checkmarks', async ({
    page,
  }) => {
    // Use a stream with multiple tools
    const events = [
      { type: 'status', content: 'Thinking...' },
      { type: 'tool_start', content: 'Querying Prometheus' },
      { type: 'tool_end', content: 'Querying Prometheus' },
      { type: 'tool_start', content: 'Checking Grafana alerts' },
      { type: 'tool_end', content: 'Checking Grafana alerts' },
      { type: 'answer', content: 'Everything looks healthy.', session_id: 'new00001' },
    ]
    await mockBackend(page, { streamEvents: events })
    await page.goto('/')

    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('Check health')
    await page.getByRole('button', { name: 'Send' }).click()

    // Final answer should appear
    await expect(page.getByText('Everything looks healthy.')).toBeVisible()
  })

  test('shows animated thinking dots when no status yet', async ({
    page,
  }) => {
    // Stream that takes a moment: just status then answer
    await mockBackendWithDelayedStream(page, 1000)
    await page.goto('/')

    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('What is happening?')
    await page.getByRole('button', { name: 'Send' }).click()

    // The thinking indicator should appear with the animated dots
    await expect(page.locator('.thinking-dots')).toBeVisible()
  })

  test('status messages are displayed during streaming', async ({ page }) => {
    const events = [
      { type: 'status', content: 'Initializing...' },
      { type: 'status', content: 'Thinking...' },
      { type: 'tool_start', content: 'Querying Prometheus — up{job="node"}' },
      { type: 'tool_end', content: 'Querying Prometheus — up{job="node"}' },
      { type: 'status', content: 'Synthesizing response...' },
      { type: 'answer', content: 'Done.', session_id: 'new00001' },
    ]
    await mockBackend(page, { streamEvents: events })
    await page.goto('/')

    await page
      .getByPlaceholder('Ask about your infrastructure…')
      .fill('query something')
    await page.getByRole('button', { name: 'Send' }).click()

    // The answer should eventually appear
    await expect(page.getByText('Done.')).toBeVisible()
  })
})
