const validViews = new Set(['reader', 'history', 'notes', 'reports', 'evaluation'])

export function normalizeViewState(value) {
  if (!value || typeof value !== 'object') return null
  const view = validViews.has(value.view) ? value.view : 'reader'
  const ids = (items) => Array.isArray(items) ? [...new Set(items.filter(item => typeof item === 'string'))].slice(0, 3) : []
  return {
    view,
    paperId: typeof value.paperId === 'string' ? value.paperId : null,
    versionId: typeof value.versionId === 'string' ? value.versionId : null,
    pageIndex: Number.isInteger(value.pageIndex) && value.pageIndex >= 0 ? value.pageIndex : 0,
    activeBlockId: typeof value.activeBlockId === 'string' && /^blk_[A-Za-z0-9_-]+$/.test(value.activeBlockId) ? value.activeBlockId : null,
    readerScrollTop: Number.isFinite(value.readerScrollTop) && value.readerScrollTop >= 0 ? value.readerScrollTop : 0,
    readerScrollLeft: Number.isFinite(value.readerScrollLeft) && value.readerScrollLeft >= 0 ? value.readerScrollLeft : 0,
    readerReturnMode: ['history', 'notes', 'reports'].includes(value.readerReturnMode) ? value.readerReturnMode : null,
    selectedPaperIds: ids(value.selectedPaperIds),
    question: typeof value.question === 'string' ? value.question.slice(0, 4000) : '',
    historyRunId: typeof value.historyRunId === 'string' && value.historyRunId.startsWith('qrun_') ? value.historyRunId : null,
    historyOffset: Number.isInteger(value.historyOffset) && value.historyOffset >= 0 ? value.historyOffset : 0,
    reportId: typeof value.reportId === 'string' && value.reportId.startsWith('rpt_') ? value.reportId : null,
    reportRevisionId: typeof value.reportRevisionId === 'string' && value.reportRevisionId.startsWith('rrev_') ? value.reportRevisionId : null,
    noteId: typeof value.noteId === 'string' && value.noteId.startsWith('note_') ? value.noteId : null,
    notesOffset: Number.isInteger(value.notesOffset) && value.notesOffset >= 0 ? value.notesOffset : 0,
    reportsOffset: Number.isInteger(value.reportsOffset) && value.reportsOffset >= 0 ? value.reportsOffset : 0,
  }
}

export function readViewState(storage) {
  try { return normalizeViewState(JSON.parse(storage.getItem('papertrail.view-state') || 'null')) }
  catch { return null }
}

export function explicitQaRunId(hash) {
  const match = typeof hash === 'string' ? hash.match(/^#qa\/(qrun_[A-Za-z0-9_-]+)(?:\?.*)?$/) : null
  return match?.[1] || null
}

export function readerLocationFromHash(hash) {
  if (!explicitQaRunId(hash)) return null
  const query = hash.slice(hash.indexOf('?') + 1)
  if (!hash.includes('?')) return null
  const params = new URLSearchParams(query)
  const pageIndex = Number(params.get('page'))
  const versionId = params.get('version')
  if (!Number.isInteger(pageIndex) || pageIndex < 0 || !/^doc_[A-Za-z0-9_-]+$/.test(versionId || '')) return null
  const number = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : 0
  const blockId = params.get('block')
  return { versionId, pageIndex,
    activeBlockId: /^blk_[A-Za-z0-9_-]+$/.test(blockId || '') ? blockId : null,
    scrollTop: number(params.get('top')), scrollLeft: number(params.get('left')) }
}

export function qaRouteWithReaderLocation(runId, location) {
  if (!/^qrun_[A-Za-z0-9_-]+$/.test(runId || '')) return ''
  if (!location || !/^doc_[A-Za-z0-9_-]+$/.test(location.versionId || '') || !Number.isInteger(location.pageIndex) || location.pageIndex < 0)
    return `#qa/${runId}`
  const params = new URLSearchParams({ version: location.versionId, page: String(location.pageIndex) })
  if (/^blk_[A-Za-z0-9_-]+$/.test(location.activeBlockId || '')) params.set('block', location.activeBlockId)
  if (Number.isFinite(location.scrollTop) && location.scrollTop > 0) params.set('top', String(Math.round(location.scrollTop)))
  if (Number.isFinite(location.scrollLeft) && location.scrollLeft > 0) params.set('left', String(Math.round(location.scrollLeft)))
  return `#qa/${runId}?${params}`
}

export function qaRunToRestore(saved, hash) {
  const routed = explicitQaRunId(hash)
  if (routed) return routed
  return saved?.view === 'reader' && saved.readerReturnMode === 'notes' && saved.historyRunId
    ? saved.historyRunId
    : null
}

export function shouldRestoreSavedView(navigationType, hasExplicitRoute = false) {
  return hasExplicitRoute || navigationType === 'reload'
}
