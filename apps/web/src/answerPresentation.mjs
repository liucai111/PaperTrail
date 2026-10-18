export const SUPPORT_EXPLANATION = '核验状态来自 AI 自动核验，未经人工确认；引句匹配原文不等同于语义支持。'

export function stripKnownEvidenceMarkers(answer, citations = []) {
  let text = String(answer ?? '')
  const knownIds = [...new Set(citations
    .map(citation => citation?.evidence_id)
    .filter(id => typeof id === 'string' && /^ev_[A-Za-z0-9_-]+$/.test(id)))]
  for (const id of knownIds) text = text.split(`[${id}]`).join(' ')
  return text
    .replace(/[\t \u00a0]{2,}/g, ' ')
    .replace(/[\t \u00a0]+([,.;:!?，。；：！？、])/g, '$1')
    .replace(/^[\t \u00a0]+|[\t \u00a0]+$/gm, '')
}

export function supportLabel(status) {
  const normalized = String(status || '').toLowerCase()
  if (['supported', 'support'].includes(normalized)) return '有证据支持'
  if (['partially_supported', 'partial', 'partially-supported'].includes(normalized)) return '部分支持'
  if (['unsupported', 'not_supported'].includes(normalized)) return '不支持'
  if (['conflicting', 'conflict'].includes(normalized)) return '证据冲突'
  if (['failed', 'error', 'verification_failed', '核验失败'].includes(normalized)) return '核验失败'
  return '未核实'
}

export function claimSupportLabel(claim) {
  return ['failed', 'error', 'verification_failed'].includes(String(claim?.support_verification || '').toLowerCase())
    ? '核验失败'
    : supportLabel(claim?.support_status)
}

export function claimSourceItems(claim, citations = []) {
  const evidenceIds = Array.isArray(claim?.evidence_ids) ? claim.evidence_ids : []
  return evidenceIds.map(evidenceId => ({
    evidenceId,
    citation: citations.find(item => item?.evidence_id === evidenceId) || null,
  }))
}

function pageList(values) {
  return [...new Set((Array.isArray(values) ? values : [])
    .filter(value => Number.isInteger(value) && value >= 0))].sort((a, b) => a - b)
}

export function citationPageTargets(citation) {
  if (!citation) return { pages: [], kind: 'unavailable' }
  if (citation.verification?.location_valid) {
    const pages = pageList(citation.quote_location_pdf_page_indexes?.length
      ? citation.quote_location_pdf_page_indexes
      : citation.spans?.map(span => span.pdf_page_index))
    if (pages.length) return { pages, kind: 'quote' }
  }
  const spanPages = pageList(citation.spans?.map(span => span.pdf_page_index))
  if (spanPages.length) return { pages: spanPages, kind: 'source' }
  const chunkPages = pageList(citation.chunk_pdf_page_indexes)
  if (chunkPages.length) return { pages: chunkPages, kind: 'chunk' }
  return { pages: [], kind: 'unavailable' }
}

export function citationButtonLabel(citation) {
  const target = citationPageTargets(citation)
  if (!target.pages.length) return citation ? '来源暂无法定位' : '来源记录不可用'
  const pageNames = target.pages.map(page => page + 1).join('、')
  if (target.kind === 'quote') return `查看原文 · PDF 第 ${pageNames} 页`
  if (target.kind === 'source') return `查看来源页 · PDF 第 ${pageNames} 页（块级/页级）`
  return `查看分块覆盖页 · PDF 第 ${pageNames} 页（非精确位置）`
}

export function citationCanOpen(citation) {
  return citationPageTargets(citation).pages.length > 0
}

export function displaySupportReason(reason) {
  return String(reason || '')
    .replace(/核验由模型辅助完成，不代表人工确认或事实正确；引句匹配原文也不等同于证据支持结论。/g, '')
    .replace(/[（(]模型辅助核验[）)]/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}
