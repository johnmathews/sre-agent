// Table rendering — covers both bugs the user flagged:
//   1. Wide tables on desktop bursting through the bubble's background
//      (the Grafana 404 screenshot — columns 4 & 5 sat on white, not gray).
//   2. Narrow viewports producing illegible tables.
// Plus the list-marker fix (Tailwind v4 reset removed default bullets).

import { test, expect } from '@playwright/test'
import { buildSseBody, mockBackend } from './fixtures'

const wideTable = `Here is the data:

| Time (UTC) | Request Path | Group | Endpoint | Response |
|------------|--------------|-------|----------|----------|
| 09:43:01 | \`/api/services/proxy?group=Infra&service=Grafana&index=0&endpoint=alerts\` | Infra | alerts | 404 (118 bytes) |
| 09:43:08 | \`/api/services/proxy?group=Charts&service=Grafana&index=0&endpoint=alerts\` | Charts | alerts | 404 (118 bytes) |

Done.`

const twoColTable = `Compact pair:

| Container | Image |
|-----------|-------|
| documentation-server | \`ghcr.io/johnmathews/unified-documentation-server:latest\` |
| mkdocs | \`squidfunk/mkdocs-material:9.6.12\` |

Done.`

const bulletList = `Steps:

- one
- two
- three

Numbered:

1. first
2. second
3. third
`

async function sendAndWait(
  page: import('@playwright/test').Page,
  body: string,
): Promise<void> {
  await mockBackend(page, {
    streamBody: buildSseBody([
      { type: 'answer', content: body, session_id: 'test0001' },
    ]),
  })
  await page.goto('/')
  await page
    .getByPlaceholder('Ask about your infrastructure…')
    .fill('table please')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.locator('.md-table-wrap, .markdown ul').first()).toBeVisible({
    timeout: 5000,
  })
}

test.describe('table rendering — bubble containment', () => {
  test('wide table stays inside the bubble (no burst)', async ({ page }) => {
    await sendAndWait(page, wideTable)

    const bubble = page.locator('.markdown').last()
    const wrap = page.locator('.md-table-wrap').last()

    const [bubbleBox, wrapBox] = await Promise.all([
      bubble.boundingBox(),
      wrap.boundingBox(),
    ])
    expect(bubbleBox).not.toBeNull()
    expect(wrapBox).not.toBeNull()

    // The wrap div must stay inside the bubble's horizontal bounds (allow 1px
    // for sub-pixel rounding). This is the regression check for the desktop
    // bug — columns 4 and 5 escaping the gray bubble.
    expect(wrapBox!.x).toBeGreaterThanOrEqual(bubbleBox!.x - 1)
    expect(wrapBox!.x + wrapBox!.width).toBeLessThanOrEqual(
      bubbleBox!.x + bubbleBox!.width + 1,
    )
  })

  test('wide table is horizontally scrollable inside the wrap', async ({
    page,
  }) => {
    await sendAndWait(page, wideTable)
    const wrap = page.locator('.md-table-wrap').last()
    const overflow = await wrap.evaluate((el) => {
      const cs = getComputedStyle(el)
      return {
        overflowX: cs.overflowX,
        scrollable: el.scrollWidth > el.clientWidth,
      }
    })
    expect(overflow.overflowX).toMatch(/auto|scroll/)
    // On a narrow viewport the table won't fit; on a wide viewport it will.
    // We just assert the overflow strategy is in place — `scrollable` may
    // legitimately be false when the bubble is wide enough to fit the table.
  })

  test('table region is keyboard-focusable for a11y', async ({ page }) => {
    await sendAndWait(page, wideTable)
    const wrap = page.locator('.md-table-wrap').last()
    await expect(wrap).toHaveAttribute('role', 'region')
    await expect(wrap).toHaveAttribute('tabindex', '0')
    await expect(wrap).toHaveAttribute('aria-label', /table/i)
  })
})

test.describe('table rendering — narrow viewport reflow', () => {
  test('2-column table stacks rows on a narrow viewport (mobile)', async ({
    page,
  }, testInfo) => {
    test.skip(
      !testInfo.project.name.startsWith('mobile-'),
      'narrow-viewport reflow only triggers on the mobile projects',
    )
    await sendAndWait(page, twoColTable)

    const wrap = page.locator('.md-table-wrap').last()
    await expect(wrap).toHaveAttribute('data-cols', '2')

    // Each <td> should be `display: block` and show its label as a ::before.
    const firstCell = wrap.locator('td').first()
    const display = await firstCell.evaluate(
      (el) => getComputedStyle(el).display,
    )
    expect(display).toBe('block')

    // The visible label text comes from data-label via ::before. Pseudo-element
    // content isn't directly addressable as DOM text, so we read the
    // `content` of the ::before via getComputedStyle.
    const label = await firstCell.evaluate((el) => {
      return getComputedStyle(el, '::before').content
    })
    // computed `content` typically wraps the value in quotes, e.g. '"Container"'.
    expect(label).toMatch(/Container/)
  })

  test('2-column table renders normally on desktop (no reflow above 480px)', async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name.startsWith('mobile-'),
      'desktop-only assertion',
    )
    await sendAndWait(page, twoColTable)
    const firstCell = page.locator('.md-table-wrap td').first()
    const display = await firstCell.evaluate(
      (el) => getComputedStyle(el).display,
    )
    expect(display).toBe('table-cell')
  })
})

test.describe('markdown lists — bullet markers restored', () => {
  test('unordered lists render disc bullets', async ({ page }) => {
    await sendAndWait(page, bulletList)
    const ul = page.locator('.markdown ul').first()
    const listStyle = await ul.evaluate(
      (el) => getComputedStyle(el).listStyleType,
    )
    expect(listStyle).toBe('disc')
  })

  test('ordered lists render decimal markers', async ({ page }) => {
    await sendAndWait(page, bulletList)
    const ol = page.locator('.markdown ol').first()
    const listStyle = await ol.evaluate(
      (el) => getComputedStyle(el).listStyleType,
    )
    expect(listStyle).toBe('decimal')
  })
})
