// Markdown → safe HTML. The LLM output is untrusted, so we sanitize with
// DOMPurify after rendering. Kept in its own module so components just import
// `renderMarkdown`.

import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({
  gfm: true,
  breaks: true,
})

export function renderMarkdown(src: string): string {
  const html = marked.parse(src, { async: false }) as string
  return DOMPurify.sanitize(html)
}
