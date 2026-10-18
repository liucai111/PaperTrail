import test from 'node:test'
import assert from 'node:assert/strict'
import { renderAnswerMarkdown } from '../src/answerMarkdown.mjs'

test('answer markdown renders readable blocks and tables', () => {
  const html = renderAnswerMarkdown('# Findings\n\nA **supported** result with `RAG`.\n\n| Metric | Value |\n| --- | --- |\n| Score | 4.4 |')
  assert.match(html, /<h2>Findings<\/h2>/)
  assert.match(html, /<strong>supported<\/strong>/)
  assert.match(html, /<table>/)
  assert.match(html, /<td>4\.4<\/td>/)
})

test('answer markdown escapes raw HTML and unsafe link syntax', () => {
  const html = renderAnswerMarkdown('<img src=x onerror=alert(1)> [click](javascript:alert(1))')
  assert.doesNotMatch(html, /<img|href=/)
  assert.match(html, /&lt;img/)
})

test('code and inline equations remain readable without injecting markup', () => {
  const html = renderAnswerMarkdown('Equation \\(x^2 + y^2\\) and $E=mc^2$.\n\n$$\n\\nabla f=0\n$$\n\n```text\n<a>\n```')
  assert.match(html, /class="math-inline"/)
  assert.match(html, /class="math-display"/)
  assert.match(html, /&lt;a&gt;/)
})
