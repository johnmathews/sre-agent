// Markdown → safe HTML. The LLM output is untrusted, so we sanitize with
// DOMPurify after rendering. Kept in its own module so components just import
// `renderMarkdown`.
//
// Tables get an extra post-process pass:
//   1. Each <table> is wrapped in `<div class="md-table-wrap" role="region"
//      tabindex="0" aria-label="Table" data-cols="N">`. The wrap clips
//      overflow inside the message bubble (otherwise wide tables burst
//      through the bubble's background — see desktop screenshot in
//      .engineering-team/evaluation-report.md) and provides horizontal
//      scroll for tables wider than the bubble.
//   2. Each <td> gets `data-label="<header text>"` so CSS can render
//      stacked-row layout on narrow viewports for ≤2-column tables, where
//      a normal table is illegible. The reflow rules live in style.css
//      under `@media (max-width: 480px)`.

import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({
  gfm: true,
  breaks: true,
})

export function renderMarkdown(src: string): string {
  const html = marked.parse(src, { async: false }) as string
  return DOMPurify.sanitize(wrapTables(html), {
    // Keep our wrap div's a11y attributes through sanitisation.
    ADD_ATTR: ['target', 'data-label', 'data-cols'],
  })
}

function wrapTables(html: string): string {
  return html.replace(
    /<table\b([^>]*)>([\s\S]*?)<\/table>/g,
    (_match, attrs, inner) => {
      const headers = extractHeaderTexts(inner)
      const innerWithLabels = injectCellLabels(inner, headers)
      const cols = headers.length
      return (
        `<div class="md-table-wrap" role="region" aria-label="Table" ` +
        `tabindex="0" data-cols="${cols}">` +
        `<table${attrs}>${innerWithLabels}</table>` +
        `</div>`
      )
    },
  )
}

function extractHeaderTexts(tableInner: string): string[] {
  const theadMatch = tableInner.match(/<thead\b[^>]*>([\s\S]*?)<\/thead>/)
  if (!theadMatch) return []
  const headers: string[] = []
  for (const m of theadMatch[1].matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/g)) {
    headers.push(stripTags(m[1]).trim())
  }
  return headers
}

function injectCellLabels(tableInner: string, headers: string[]): string {
  if (headers.length === 0) return tableInner
  // Only inject on cells that don't already carry a data-label (idempotent).
  // Reset the column counter at every <tr> so each row maps cells back to
  // the correct header.
  return tableInner.replace(
    /(<tr\b[^>]*>)([\s\S]*?)(<\/tr>)/g,
    (_full, openTr, body, closeTr) => {
      let col = 0
      const labelled = body.replace(
        /<td\b([^>]*)>/g,
        (_cell: string, cellAttrs: string) => {
          const label = headers[col] ?? ''
          col += 1
          if (/\bdata-label\s*=/.test(cellAttrs)) {
            return `<td${cellAttrs}>`
          }
          return `<td${cellAttrs} data-label="${escapeAttr(label)}">`
        },
      )
      return `${openTr}${labelled}${closeTr}`
    },
  )
}

function stripTags(s: string): string {
  return s.replace(/<[^>]*>/g, '')
}

function escapeAttr(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}
