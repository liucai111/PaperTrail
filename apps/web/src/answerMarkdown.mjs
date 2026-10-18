function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character])
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>')
    .replace(/(?<!\\)\$([^$\n]+)\$/g, '<span class="math-inline">$1</span>')
    .replace(/\\\((.+?)\\\)/g, '<span class="math-inline">$1</span>')
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

function tableCells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())
}

/** Render a safe, deliberately small Markdown subset. Raw HTML and link targets stay escaped. */
export function renderAnswerMarkdown(markdown) {
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n')
  const blocks = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index += 1; continue }

    if (/^\s*```/.test(line)) {
      const code = []
      index += 1
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++])
      if (index < lines.length) index += 1
      blocks.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
      continue
    }

    if (/^\s*\$\$\s*$/.test(line)) {
      const formula = []
      index += 1
      while (index < lines.length && !/^\s*\$\$\s*$/.test(lines[index])) formula.push(lines[index++])
      if (index < lines.length) index += 1
      blocks.push(`<div class="math-display">${escapeHtml(formula.join('\n'))}</div>`)
      continue
    }

    if (index + 1 < lines.length && line.includes('|') && isTableSeparator(lines[index + 1])) {
      const headers = tableCells(line)
      index += 2
      const rows = []
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(tableCells(lines[index++]))
      blocks.push(`<div class="answer-table-scroll"><table><thead><tr>${headers.map(cell => `<th>${inlineMarkdown(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${headers.map((_, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`)
      continue
    }

    const heading = line.match(/^\s*(#{1,4})\s+(.+)$/)
    if (heading) {
      const level = Math.min(4, heading[1].length + 1)
      blocks.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`)
      index += 1
      continue
    }

    const listMatch = line.match(/^\s*([-*]|\d+\.)\s+(.+)$/)
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1])
      const tag = ordered ? 'ol' : 'ul'
      const items = []
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-*]|\d+\.)\s+(.+)$/)
        if (!item || /^\d/.test(item[1]) !== ordered) break
        items.push(`<li>${inlineMarkdown(item[2])}</li>`)
        index += 1
      }
      blocks.push(`<${tag}>${items.join('')}</${tag}>`)
      continue
    }

    const paragraph = [line.trim()]
    index += 1
    while (index < lines.length && lines[index].trim() && !/^\s*(#{1,4}\s|```|[-*]\s|\d+\.\s)/.test(lines[index])) {
      if (index + 1 < lines.length && lines[index].includes('|') && isTableSeparator(lines[index + 1])) break
      paragraph.push(lines[index++].trim())
    }
    blocks.push(`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`)
  }
  return blocks.join('')
}
