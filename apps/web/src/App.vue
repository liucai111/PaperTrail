<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import type * as pdfjsTypes from 'pdfjs-dist'
import { describeQaFailure, describeOperationTaskError, isHistoricalFailureTask, canDismissOperationTask, runQaSubmission } from './qaSubmission.mjs'
import { explicitQaRunId, normalizeViewState, qaRouteWithReaderLocation, qaRunToRestore, readViewState, readerLocationFromHash, shouldRestoreSavedView } from './viewState.mjs'
import { classifyUploadFailure, latestUploadTaskForVersion, uploadIdempotencyKey, uploadPrimaryAction, uploadSelectionDisposition, uploadTaskGuidance, uploadStageStatus, uploadVectorProgress } from './uploadSubmission.mjs'
import { renderAnswerMarkdown } from './answerMarkdown.mjs'
import { clampReaderShare, readWorkspacePreferences, writeWorkspacePreferences } from './workspacePreferences.mjs'
import { SUPPORT_EXPLANATION, citationButtonLabel, citationCanOpen, citationPageTargets, claimSourceItems, claimSupportLabel, displaySupportReason, stripKnownEvidenceMarkers, supportLabel } from './answerPresentation.mjs'
import { computeFitScale, isPdfRenderCancellation, isRenderCurrent, pdfPageNumber, renderPixelSize } from './pdfReader.mjs'

const EvaluationResults = defineAsyncComponent(() => import('./components/EvaluationResults.vue'))

let pdfjsRuntimePromise: Promise<typeof import('pdfjs-dist')> | null = null
async function loadPdfjsRuntime() {
  if (!pdfjsRuntimePromise) {
    pdfjsRuntimePromise = Promise.all([
      import('pdfjs-dist'),
      import('pdfjs-dist/build/pdf.worker.mjs?url'),
    ]).then(([runtime, worker]) => {
      runtime.GlobalWorkerOptions.workerSrc = worker.default
      return runtime
    }).catch(error => {
      pdfjsRuntimePromise = null
      throw error
    })
  }
  return pdfjsRuntimePromise
}

type Version = { id: string; status: string; source_version: string; sha256: string; page_count: number; block_count: number; file_size_bytes?: number | null; quality: { empty_text_pages?: number[]; possible_table_pages?: number[]; limits?: string[] }; error: string | null }
type Paper = { id: string; title: string; authors: string[]; arxiv_id: string; source: 'manifest' | 'user_upload'; original_filename?: string | null; status: string; versions: Version[] }
type Block = { block_id: string; pdf_page_index: number; ordinal: number; text: string; bbox: number[]; page_width: number; page_height: number; section_path: string[] }
type Candidate = { paper_id: string; title: string; source_version: string; source?: 'manifest' | 'user_upload' }
type Span = { block_id: string; pdf_page_index: number; bbox?: number[]; page_width?: number; page_height?: number; source_text?: string }
type Citation = { evidence_id: string; chunk_id: string; paper_id: string; title: string; document_version_id: string; parse_snapshot_id?: string | null; parser_identity?: string | null; source_version: string; sha256: string; exact_quote?: string; chunk_text?: string; text?: string; section_path: string[]; spans: Span[]; location_valid?: boolean; chunk_pdf_page_indexes?: number[]; quote_location_pdf_page_indexes?: number[]; page_number_basis?: string; verification?: { location_valid: boolean; quote_valid: boolean; support_status: string }; retrieval?: { rrf_score: number; sources: Record<string, number>; ranks: Record<string, number> } }
type Claim = { claim_id?: string; text: string; evidence_ids: string[]; quote: string; quote_valid: boolean; location_valid: boolean; support_status: string; support_verification?: string; support_reason?: string }
type QaResult = { run_id: string; status: string; query?: string; retrieval_query?: string; paper_ids?: string[]; answer: string; claims: Claim[]; citations: Citation[]; chat_model: string; embedding_model: string; document_version_ids: string[]; parse_snapshot_ids?: Array<string | null> | null; baseline_answer?: string | null; support_trace?: Record<string, any> | null; created_at?: string; fixture_label?: string; controlled_test?: boolean }
type SnapshotOption = { parse_snapshot_id: string; parser_identity: string; index_id: string; embedding_model: string; dimensions: number; chunk_count: number }
type HistoryItem = { run_id: string; query: string; paper_ids: string[]; status: string; created_at: string; chat_model: string }
type NoteItem = { note_id: string; title: string; source_qa_run_id: string | null; paper_ids: string[]; created_at: string; updated_at: string }
type NoteDetail = NoteItem & { body: string; source_query_snapshot: string; source_qa_available: boolean; claims: Array<Claim & { claim_id: string; source_index: number; support_verification: string }>; citations: Array<Citation & { source_available: boolean; evidence_available: boolean }> }
type ComparisonReportItem = { report_id: string; title: string; paper_ids: string[]; created_at: string }
type ReportClaim = { claim_id: string; text: string; support_status: string; support_reason: string; citations: Citation[]; revision_edit?: {status:string;reason:string;original_text:string;original_support_status:string} }
type ReportChange = {claim_id:string;original_text:string;revised_text:string;reason:string;original_support_status:string;revised_support_status:string}
type ComparisonReportData = { report_id: string; title: string; paper_ids: string[]; document_version_ids: string[]; chat_model: string; embedding_model: string; created_at: string; revision_id?:string; revision_number?:number; revision_created_at?:string; source_report_id?:string; changes?:ReportChange[]; report_data: { paper_snapshots: Array<{paper_id:string;title:string;document_version_id:string;source_version:string;sha256:string}>; dimensions: Array<{name:string;cells:Array<{paper_id:string;coverage_status:string;claims:ReportClaim[]}>;comparisons:ReportClaim[]}>; supplemental_notes: Array<{note_id?:string;title:string;body:string;label:string}>; support_trace: Record<string,any> } }
type ReportRevisionItem = {revision_id:string;revision_number:number;created_at:string;changes:ReportChange[]}
type OperationTask = {task_id:string;kind:'qa'|'comparison_report'|'document_ingestion';status:string;phase:string;created_at:string;started_at?:string|null;finished_at?:string|null;elapsed_ms:number;cancel_requested:boolean;request_summary?:Record<string,any>;result_id?:string|null;progress?:{completed_vectors?:number;total_vectors?:number;missing_vectors?:number;batch_size?:number;current_batch?:number;index_status?:string;ready?:boolean}|null;error?:{code:string;message:string;retryable:boolean;details?:Record<string,unknown>}|null}
type ConfigStatus = { embedding_provider: string; embedding_model: string; embedding_ready: boolean; chat_model: string; chat_ready: boolean; ocr_enabled?: boolean; ocr_runtime_present?: boolean }
type ApiFailurePayload = { code: string; retryable: boolean; details?: Record<string, unknown> | null }
class ApiFailure extends Error {
  constructor(readonly status: number, readonly payload: ApiFailurePayload) { super(payload.code || `HTTP ${status}`) }
}

const papers = ref<Paper[]>([])
const candidates = ref<Candidate[]>([])
const paperRows = computed<Candidate[]>(() => [
  ...candidates.value.map(candidate => ({ ...candidate, source: 'manifest' as const })),
  ...papers.value.filter(paper => paper.source === 'user_upload').map(paper => ({
    paper_id: paper.id, title: paper.title,
    source_version: paper.versions.find(v => ['ready', 'quality_warning'].includes(v.status))?.source_version || paper.versions[0]?.source_version || '等待处理',
    source: 'user_upload' as const,
  })),
])
const selectedPaperIds = ref<string[]>([])
const initialWorkspacePreferences = readWorkspacePreferences(window.localStorage)
const libraryCollapsed = ref(initialWorkspacePreferences.libraryCollapsed)
const readerSharePercent = ref(initialWorkspacePreferences.readerSharePercent)
const workspaceMain = ref<HTMLElement | null>(null)
const renderedScale = ref(1)
const displayAnswer = computed(() => stripKnownEvidenceMarkers(qaResult.value?.answer || '', qaResult.value?.citations || []))
const displayBaselineAnswer = computed(() => stripKnownEvidenceMarkers(qaResult.value?.baseline_answer || '', qaResult.value?.citations || []))
const historyDisplayAnswer = computed(() => stripKnownEvidenceMarkers(historyRecord.value?.answer || '', historyRecord.value?.citations || []))
const answerHtml = computed(() => renderAnswerMarkdown(displayAnswer.value))
const selected = ref<Paper | null>(null)
const activeVersion = ref<Version | null>(null)
const snapshotOptions = ref<SnapshotOption[]>([])
const parseSnapshotChoice = ref('')
const snapshotOptionsLoading = ref(false)
let snapshotOptionsToken = 0
const version = computed(() => activeVersion.value || selected.value?.versions[0] || null)
const blocks = ref<Block[]>([])
const pageIndex = ref(0)
const activeBlock = ref<Block | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const overlay = ref<HTMLDivElement | null>(null)
const pageScroll = ref<HTMLDivElement | null>(null)
const busy = ref(false)
const uploadFileSelection = ref<File | null>(null)
const uploadSelectionDigest = ref<string | null>(null)
const uploadDigestPending = ref(false)
const uploadTask = ref<OperationTask | null>(null)
const uploadBusy = ref(false)
const uploadDialogOpen = ref(false)
const uploadElapsedSeconds = ref(0)
const uploadError = ref('')
const uploadDeduplicated = ref(false)
const uploadOutcomeUnknown = ref(false)
const uploadNeedsReconcile = ref(false)
const uploadRequestTimeoutMs = 90_000
const uploadPhaseLabels: Record<string,string> = {waiting:'等待处理',validating:'验证文件',parsing:'解析 PDF',ocr:'OCR 解析',chunking:'整理文本块',indexing:'准备索引',embedding:'生成索引向量',persisting:'保存索引',completed:'处理完成',failed:'处理失败',interrupted:'已中断',cancelled:'已取消'}
const uploadStageLabels = ['文件已接收', '解析 PDF', '整理文本块', '生成向量', '索引可用']
const uploadVectors = computed(() => uploadVectorProgress(uploadTask.value))
const uploadSelectionTask = computed(() => uploadSelectionDisposition(uploadSelectionDigest.value, uploadTask.value))
const uploadMainAction = computed(() => uploadPrimaryAction(uploadTask.value, uploadSelectionDigest.value, Boolean(uploadFileSelection.value)))
let uploadPollToken = 0
let uploadElapsedTimer: number | undefined
const error = ref('')
const scale = ref(1)
const pdfLoading = ref(false)
const pdfPageReady = ref(false)
const pdfError = ref('')
const questionEditing = ref(true)
const sideTab = ref<'qa' | 'blocks'>('qa')
const viewMode = ref<'reader' | 'history' | 'notes' | 'reports' | 'evaluation'>('reader')
const historyItems = ref<HistoryItem[]>([])
const historyOffset = ref(0)
const historyTotal = ref(0)
const historyLimit = 20
const historyLoading = ref(false)
const historyError = ref('')
const taskHistoryItems = ref<OperationTask[]>([])
const taskHistoryLoading = ref(false)
const taskHistoryError = ref('')
const historyRecord = ref<QaResult | null>(null)
const showBaseline = ref(false)
const historyList = ref<HTMLElement | null>(null)
const historyScrollTop = ref(0)
const readerReturnMode = ref<'history' | 'notes' | 'reports' | null>(null)
const comparisonReports = ref<ComparisonReportItem[]>([])
const comparisonReportsOffset = ref(0)
const comparisonReportsTotal = ref(0)
const comparisonReport = ref<ComparisonReportData | null>(null)
const comparisonReportRevisions = ref<ReportRevisionItem[]>([])
const reportEditingClaim = ref<ReportClaim | null>(null)
const reportEditText = ref('')
const reportEditReason = ref('')
const reportSavingEdit = ref(false)
const comparisonReportsLoading = ref(false)
const comparisonReportsError = ref('')
const comparisonTitle = ref('论文方法比较报告')
const comparisonPaperIds = ref<string[]>([])
const comparisonVersionIds = ref<string[]>([])
const comparisonDimensionsText = ref('研究问题\n检索机制\n纠错/自评机制\n实验设置与局限')
const comparisonCreating = ref(false)
const comparisonNoteIds = ref<string[]>([])
const notes = ref<NoteItem[]>([])
const notesOffset = ref(0)
const notesTotal = ref(0)
const notesLoading = ref(false)
const notesError = ref('')
const noteDetail = ref<NoteDetail | null>(null)
const noteCompose = ref(false)
const noteEditing = ref(false)
const noteTitle = ref('')
const noteBody = ref('')
const noteSaving = ref(false)
const noteDeleting = ref(false)
const noteSource = ref<QaResult | null>(null)
const notePaperIds = ref<string[]>([])
const noteSelectedClaims = ref<string[]>([])
const noteSelectedEvidence = ref<string[]>([])
const noteEditorDirty = computed(() => {
  if (noteCompose.value) return !!(noteTitle.value.trim() || noteBody.value || notePaperIds.value.length || noteSelectedClaims.value.length || noteSelectedEvidence.value.length)
  if (noteEditing.value && noteDetail.value) return noteTitle.value !== noteDetail.value.title || noteBody.value !== noteDetail.value.body ||
    JSON.stringify([...notePaperIds.value].sort()) !== JSON.stringify([...(noteDetail.value.paper_ids || [])].sort())
  return false
})
const noteLimit = 20
const config = ref<ConfigStatus | null>(null)
const question = ref('')
const asking = ref(false)
const qaError = ref('')
const qaFailure = ref<{ message: string; code: string; reason: string; status?: number; phase?: string; endpointHost?: string; elapsedMs?: number; exceptionChain?: string; retryable: boolean; diagnostic?: Record<string, unknown> } | null>(null)
const qaResult = ref<QaResult | null>(null)
const searchEvidence = ref<Citation[]>([])
const operationTask = ref<OperationTask | null>(null)
const operationTaskError = ref('')
const operationSubmitting = ref(false)
let restoringViewState = false
let initializingApp = true
const startupViewState = (() => { try { return readViewState(window.localStorage) } catch { return null } })()
const startupNavigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
const startupNavigationType = startupNavigation?.type || 'navigate'
let operationPollToken = 0
let pdf: pdfjsTypes.PDFDocumentProxy | null = null
let pdfLoadingTask: ReturnType<typeof pdfjsTypes.getDocument> | null = null
let pdfRenderTask: { cancel: () => void; promise: Promise<unknown> } | null = null
let readerLoadToken = 0
let renderToken = 0
const autoRecoveredPages = new Set<string>()
let pageResizeObserver: ResizeObserver | undefined
let lastReaderWidth = 0
let lastRenderedWidth = 0
let resizeRenderPending = false

function isCurrentReaderLoad(token: number) { return token === readerLoadToken }
async function cancelCurrentRender() {
  const task = pdfRenderTask
  pdfRenderTask = null
  if (!task) return
  try { task.cancel() } catch { /* A completed task may no longer be cancellable. */ }
  try { await task.promise } catch { /* Cancellation is expected when switching pages/documents. */ }
}
async function destroyPdfDocument(document: pdfjsTypes.PDFDocumentProxy | null) {
  if (!document) return
  try { await document.destroy() } catch { /* Old worker teardown must not block the new document. */ }
}
async function loadPdfDocument(versionId: string, loadToken = ++readerLoadToken) {
  const render = ++renderToken
  pdfLoading.value = true
  pdfPageReady.value = false
  pdfError.value = ''
  const previousTask = pdfLoadingTask
  pdfLoadingTask = null
  const previousDocument = pdf
  pdf = null
  await cancelCurrentRender()
  if (render !== renderToken || !isCurrentReaderLoad(loadToken)) return false
  if (canvas.value) { canvas.value.width = 0; canvas.value.height = 0; canvas.value.style.width = ''; canvas.value.style.height = '' }
  if (overlay.value) { overlay.value.replaceChildren(); overlay.value.style.width = ''; overlay.value.style.height = '' }
  if (previousTask) { try { await previousTask.destroy() } catch { /* Superseded loading task. */ } }
  if (previousDocument) await destroyPdfDocument(previousDocument)
  if (!isCurrentReaderLoad(loadToken)) return false
  let task: ReturnType<typeof pdfjsTypes.getDocument> | null = null
  try {
    const runtime = await loadPdfjsRuntime()
    if (!isCurrentReaderLoad(loadToken)) return false
    task = runtime.getDocument(`/api/v1/documents/${versionId}/pdf`)
    pdfLoadingTask = task
    const document = await task.promise
    if (!isCurrentReaderLoad(loadToken) || pdfLoadingTask !== task) {
      await destroyPdfDocument(document)
      return false
    }
    pdf = document
    scale.value = 1
    pageIndex.value = 0
    lastRenderedWidth = 0
    return true
  } catch (cause) {
    if (isCurrentReaderLoad(loadToken)) pdfError.value = `PDF 加载失败：${cause instanceof Error ? cause.message : String(cause)}`
    return false
  } finally {
    if (isCurrentReaderLoad(loadToken)) {
      pdfLoading.value = false
      if (task && pdfLoadingTask === task) pdfLoadingTask = null
    }
  }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api/v1' + path, options)
  if (!response.ok) {
    let payload: ApiFailurePayload = { code: 'request_failed', retryable: false }
    try {
      const body = await response.json() as Partial<ApiFailurePayload>
      if (typeof body.code === 'string') payload = { code: body.code, retryable: body.retryable === true,
        details: body.details && typeof body.details === 'object' ? body.details as ApiFailurePayload['details'] : null }
    } catch { /* Never surface an unstructured response body; it may contain provider diagnostics. */ }
    throw new ApiFailure(response.status, payload)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
async function refresh() {
  papers.value = await api<Paper[]>('/papers')
  if (selected.value) selected.value = papers.value.find(p => p.id === selected.value?.id) || null
}
async function loadSnapshotOptions(versionId: string) {
  const token = ++snapshotOptionsToken
  snapshotOptionsLoading.value = true
  snapshotOptions.value = []
  try {
    const snapshots = await api<Array<{parse_snapshot_id:string;parser_identity:string;status:string}>>(
      `/documents/${versionId}/parse-snapshots`)
    const ready = snapshots.filter(item => item.status === 'ready')
    const groups = await Promise.all(ready.map(async snapshot => ({ snapshot,
      indexes: await api<Array<{index_id:string;embedding_model:string;dimensions:number;chunk_count:number;status:string}>>(
        `/documents/${versionId}/parse-snapshots/${snapshot.parse_snapshot_id}/indexes`),
    })))
    const options = groups.flatMap(({snapshot,indexes}) => indexes
      .filter(index => index.status === 'ready' && index.embedding_model === config.value?.embedding_model)
      .map(index => ({parse_snapshot_id:snapshot.parse_snapshot_id,parser_identity:snapshot.parser_identity,
        index_id:index.index_id,embedding_model:index.embedding_model,dimensions:index.dimensions,chunk_count:index.chunk_count})))
    if (token === snapshotOptionsToken) snapshotOptions.value = options
  } catch {
    // Snapshot discovery is optional; legacy reading and QA remain available.
    if (token === snapshotOptionsToken) snapshotOptions.value = []
  } finally { if (token === snapshotOptionsToken) snapshotOptionsLoading.value = false }
}
async function loadHistory(offset = historyOffset.value) {
  historyLoading.value = true; historyError.value = ''
  try {
    const page = await api<{ items: HistoryItem[]; total: number; limit: number; offset: number }>(`/qa?limit=${historyLimit}&offset=${offset}`)
    historyItems.value = page.items; historyTotal.value = page.total; historyOffset.value = page.offset
  } catch (e) { historyError.value = String(e) } finally { historyLoading.value = false }
}
async function loadTaskHistory() {
  taskHistoryLoading.value = true; taskHistoryError.value = ''
  try {
    const result = await api<{ items: OperationTask[] }>('/tasks?limit=20')
    taskHistoryItems.value = result.items.filter(isHistoricalFailureTask)
  } catch (e) { taskHistoryError.value = `读取任务记录失败：${String(e)}` }
  finally { taskHistoryLoading.value = false }
}
async function selectHistory(item: HistoryItem) {
  const loadToken = ++readerLoadToken
  historyError.value = ''
  if (historyList.value) historyScrollTop.value = historyList.value.scrollTop
  readerReturnMode.value = 'history'
  setQaRoute(item.run_id)
  try {
    const record = await api<QaResult>(`/qa/${item.run_id}`)
    if (!isCurrentReaderLoad(loadToken)) return
    historyRecord.value = record; qaResult.value = record; qaError.value = ''; qaFailure.value = null; searchEvidence.value = []; showBaseline.value = false; question.value = record.query || item.query; questionEditing.value = false
    selectedPaperIds.value = record.paper_ids || item.paper_ids
    viewMode.value = 'reader'; sideTab.value = 'qa'
    const citation = record.citations[0]
    const paperId = citation?.paper_id || selectedPaperIds.value[0]
    if (paperId) {
      if (!papers.value.length) await refresh()
      selected.value = papers.value.find(p => p.id === paperId) || null
      activeVersion.value = selected.value?.versions.find(v => v.id === citation?.document_version_id)
        || selected.value?.versions.find(v => record.document_version_ids.includes(v.id)) || selected.value?.versions[0] || null
      if (activeVersion.value) {
        parseSnapshotChoice.value = citation?.parse_snapshot_id || ''
        const versionId = activeVersion.value.id
        await loadSnapshotOptions(versionId)
        if (!isCurrentReaderLoad(loadToken) || !await loadPdfDocument(versionId, loadToken)) return
        const renderPromise = setPage(0)
        const snapshotQuery = citation?.parse_snapshot_id ? `&parse_snapshot_id=${encodeURIComponent(citation.parse_snapshot_id)}` : ''
        const loadedBlocks = await api<Block[]>(`/documents/${versionId}/blocks?limit=1000${snapshotQuery}`)
        if (!isCurrentReaderLoad(loadToken)) return
        await renderPromise
        blocks.value = loadedBlocks
        highlight()
      }
    }
  } catch (e) { if (isCurrentReaderLoad(loadToken)) historyError.value = String(e) }
}
async function restoreViewState(savedOverride: ReturnType<typeof readViewState> = startupViewState, forceRestore = false) {
  const saved = savedOverride
  const routeRunId = explicitQaRunId(window.location.hash)
  const routeReaderLocation = readerLocationFromHash(window.location.hash)
  if (!saved && !routeRunId) return
  restoringViewState = true
  try {
    let paper: Paper | undefined
    if (saved) {
      selectedPaperIds.value = saved.selectedPaperIds.filter(id => papers.value.some(p => p.id === id && p.versions.some(v => ['ready','quality_warning'].includes(v.status))))
      question.value = ''
      historyOffset.value = saved.historyOffset
      notesOffset.value = saved.notesOffset
      comparisonReportsOffset.value = saved.reportsOffset
      paper = papers.value.find(item => item.id === saved.paperId)
      selected.value = paper || null
      activeVersion.value = paper?.versions.find(item => item.id === saved.versionId) || paper?.versions[0] || null
    }

    if (routeRunId) {
      const record = await api<QaResult>(`/qa/${routeRunId}`)
      historyRecord.value = record
      const readerLocation = routeReaderLocation || (saved ? {
        versionId: saved.versionId, pageIndex: saved.pageIndex, activeBlockId: saved.activeBlockId,
        scrollTop: saved.readerScrollTop, scrollLeft: saved.readerScrollLeft,
      } : undefined)
      await openRecordInReader(record, record.query || '', 'history', readerLocation)
      if (readerLocation && activeVersion.value?.id === readerLocation.versionId && pageIndex.value !== readerLocation.pageIndex) {
        const targetPage = Math.min(readerLocation.pageIndex, Math.max(0, activeVersion.value.page_count - 1))
        activeBlock.value = blocks.value.find(block => block.block_id === readerLocation.activeBlockId && block.pdf_page_index === targetPage) || null
        await setPage(targetPage)
        if (pageScroll.value) {
          pageScroll.value.scrollTop = readerLocation.scrollTop
          pageScroll.value.scrollLeft = readerLocation.scrollLeft
        }
      }
      return
    }

    if (!forceRestore && !shouldRestoreSavedView(startupNavigationType)) {
      historyRecord.value = null; qaResult.value = null; question.value = ''
      viewMode.value = 'reader'
      if (paper && activeVersion.value) await restoreReaderDocument(activeVersion.value.id, saved?.pageIndex || 0,
        saved?.activeBlockId || null, saved?.readerScrollTop || 0, saved?.readerScrollLeft || 0)
      return
    }

    if (!saved) return
    if (saved.view === 'history') {
      viewMode.value = 'history'
      await Promise.all([loadHistory(saved.historyOffset), loadTaskHistory()])
      historyRecord.value = null; qaResult.value = null
    } else if (saved.view === 'notes') {
      viewMode.value = 'notes'
      await loadNotes(saved.notesOffset)
      const item = notes.value.find(note => note.note_id === saved.noteId)
      if (item) await openNote(item)
    } else if (saved.view === 'reports') {
      viewMode.value = 'reports'
      await loadComparisonReports(saved.reportsOffset)
      const item = comparisonReports.value.find(report => report.report_id === saved.reportId)
      if (item) {
        await openComparisonReport(item)
        if (saved.reportRevisionId && comparisonReportRevisions.value.some(revision => revision.revision_id === saved.reportRevisionId))
          await openReportRevision(saved.reportRevisionId)
      }
    } else if (saved.view === 'evaluation') {
      viewMode.value = 'evaluation'
    } else if (saved.readerReturnMode === 'reports' && saved.reportId) {
      const report = await api<ComparisonReportData>(`/comparison-reports/${saved.reportId}`)
      comparisonReport.value = report
      await loadReportRevisions(saved.reportId)
      if (saved.reportRevisionId && comparisonReportRevisions.value.some(revision => revision.revision_id === saved.reportRevisionId))
        await openReportRevision(saved.reportRevisionId)
      if (paper && activeVersion.value) await restoreReaderDocument(activeVersion.value.id, saved.pageIndex,
        saved.activeBlockId, saved.readerScrollTop, saved.readerScrollLeft)
      readerReturnMode.value = 'reports'
      viewMode.value = 'reader'
    } else if (qaRunToRestore(saved, window.location.hash)) {
      const restoreRunId = qaRunToRestore(saved, window.location.hash)!
      const record = await api<QaResult>(`/qa/${restoreRunId}`)
      historyRecord.value = record
      readerReturnMode.value = saved.readerReturnMode
      await openRecordInReader(record, record.query || saved.question, saved.readerReturnMode === 'notes' ? 'notes' : 'history', {
        versionId: saved.versionId, pageIndex: saved.pageIndex, activeBlockId: saved.activeBlockId,
        scrollTop: saved.readerScrollTop, scrollLeft: saved.readerScrollLeft,
      })
      readerReturnMode.value = saved.readerReturnMode
    } else if (paper && activeVersion.value) {
      await restoreReaderDocument(activeVersion.value.id, saved.pageIndex, saved.activeBlockId, saved.readerScrollTop, saved.readerScrollLeft)
      viewMode.value = 'reader'
    } else {
      viewMode.value = 'reader'
    }
  } catch (e) {
    error.value = `恢复上次页面失败；已有记录未修改：${String(e)}`
  } finally {
    restoringViewState = false
    persistViewState()
  }
}
async function restoreReaderDocument(versionId: string, page = 0, blockId: string | null = null, scrollTop = 0, scrollLeft = 0) {
  const loadToken = ++readerLoadToken
  const owner = papers.value.find(item => item.versions.some(v => v.id === versionId))
  const fixed = owner?.versions.find(v => v.id === versionId)
  if (!owner || !fixed) return
  selected.value = owner; activeVersion.value = fixed
  viewMode.value = 'reader'
  await nextTick()
  await loadSnapshotOptions(fixed.id)
  if (!isCurrentReaderLoad(loadToken) || !await loadPdfDocument(fixed.id, loadToken)) return
  const restoredPage = Math.min(page, Math.max(0, fixed.page_count - 1))
  const renderPromise = setPage(restoredPage)
  const loadedBlocks = await api<Block[]>(`/documents/${fixed.id}/blocks?limit=1000`)
  if (!isCurrentReaderLoad(loadToken)) return
  await renderPromise
  blocks.value = loadedBlocks
  activeBlock.value = blocks.value.find(block => block.block_id === blockId && block.pdf_page_index === restoredPage) || null
  highlight()
  if (pageScroll.value) { pageScroll.value.scrollTop = scrollTop; pageScroll.value.scrollLeft = scrollLeft }
}
function persistViewState() {
  if (restoringViewState || initializingApp) return
  try {
    const snapshot = currentViewSnapshot()
    localStorage.setItem('papertrail.view-state', JSON.stringify(snapshot))
    const routeRunId = explicitQaRunId(window.location.hash)
    const hash = routeRunId ? qaRouteWithReaderLocation(routeRunId, {
      versionId: snapshot.versionId, pageIndex: snapshot.pageIndex, activeBlockId: snapshot.activeBlockId,
      scrollTop: snapshot.readerScrollTop, scrollLeft: snapshot.readerScrollLeft,
    }) : window.location.hash
    window.history.replaceState({ ...(window.history.state || {}), papertrailViewState: snapshot }, '',
      `${window.location.pathname}${window.location.search}${hash}`)
  } catch { /* Browser storage can be disabled; the current session remains usable. */ }
}
function currentViewSnapshot() {
  return {
    view: viewMode.value, paperId: selected.value?.id || null, versionId: version.value?.id || null,
    pageIndex: pageIndex.value, activeBlockId: activeBlock.value?.block_id || null,
    readerScrollTop: pageScroll.value?.scrollTop || 0, readerScrollLeft: pageScroll.value?.scrollLeft || 0,
    selectedPaperIds: [...selectedPaperIds.value], question: question.value,
    historyRunId: historyRecord.value?.run_id || null, historyOffset: historyOffset.value,
    readerReturnMode: readerReturnMode.value, reportId: comparisonReport.value?.report_id || null,
    reportRevisionId: comparisonReport.value?.revision_id || null, reportsOffset: comparisonReportsOffset.value,
    noteId: noteDetail.value?.note_id || null, notesOffset: notesOffset.value,
  }
}
watch(viewMode, (nextView, previousView) => {
  if (initializingApp || restoringViewState || nextView === previousView) return
  try {
    const snapshot = currentViewSnapshot()
    window.history.pushState({ ...(window.history.state || {}), papertrailViewState: snapshot }, '',
      `${window.location.pathname}${window.location.search}${window.location.hash}`)
  } catch { /* View changes remain usable when browser history state is unavailable. */ }
})
function restoreBrowserHistoryEntry(event: PopStateEvent) {
  const saved = normalizeViewState(event.state?.papertrailViewState)
    || (event.state == null ? readViewState(window.localStorage) : null)
  if (!saved) return
  void restoreViewState(saved, true)
}
function supportTone(status?: string) {
  if (status === 'supported') return 'tone-supported'
  if (status === 'partially_supported' || status === 'partial') return 'tone-partial'
  if (status === 'unsupported' || status === 'conflicting' || status === 'failed' || status === 'error' || status === 'verification_failed') return 'tone-unsupported'
  return 'tone-unverified'
}
function claimSupportTone(claim: Claim) {
  return claimSupportLabel(claim) === '核验失败' ? 'tone-unsupported' : supportTone(claim.support_status)
}
function paperLabel(id: string) { return papers.value.find(paper => paper.id === id)?.title || id }
function citationTooltip(evidenceId: string) {
  const citation = qaResult.value?.citations.find(item => item.evidence_id === evidenceId)
  if (!citation) return '此来源记录不可用。'
  const pages = quotePageIndexes(citation).map(index => index + 1)
  const target = citationPageTargets(citation)
  const location = pages.length ? `引句原文页 ${[...new Set(pages)].join('、')}`
    : target.kind === 'source' ? `来源块/页 ${target.pages.map(page => page + 1).join('、')}（非精确引句位置）`
      : target.kind === 'chunk' ? `分块覆盖页 ${target.pages.map(page => page + 1).join('、')}（非精确位置）` : '暂无法定位'
  return `${citation.title} · ${citation.source_version} · ${citation.section_path.join(' / ') || '章节未识别'} · ${location}`
}
function openCitationSource(citation: Citation | undefined) {
  if (!citation) return
  const spanIndex = citation.spans?.findIndex(span => span.bbox && Number.isInteger(span.pdf_page_index) && citation.verification?.location_valid) ?? -1
  if (spanIndex >= 0) { void locateCitation(citation, spanIndex); return }
  const target = citationPageTargets(citation)
  if (target.pages.length) void jumpCitationPage(citation, target.pages[0], target.kind === 'quote')
}
function completenessLabel(result: QaResult) {
  const status = result.support_trace?.answer_completeness?.status
  return status === 'complete' ? '已检查：完整' : status === 'incomplete' ? '已检查：存在缺项' : '未检查'
}
function evidenceSupportLabel(result: QaResult) {
  if (!result.support_trace?.enabled) return '未核验'
  if (!result.claims.length) return '无 claim 可核验'
  const counts = new Map<string, number>()
  result.claims.forEach(claim => {
    const label = claimSupportLabel(claim)
    counts.set(label, (counts.get(label) || 0) + 1)
  })
  return [...counts].map(([label, count]) => `${label} × ${count}`).join('；')
}
function formatTrace(value: unknown) { return JSON.stringify(value, null, 2) }
function isMockHistory(item: HistoryItem) { return /(^|[-_])mock($|[-_])/i.test(item.chat_model) }
function quotePageIndexes(citation: Citation) {
  return citation.verification?.location_valid ? citation.quote_location_pdf_page_indexes || citation.spans.map(s => s.pdf_page_index) : []
}
function chunkPageIndexes(citation: Citation) { return citation.chunk_pdf_page_indexes || [] }
function togglePaper(id: string) {
  const paper = papers.value.find(item => item.id === id)
  if (!paper?.versions.some(item => ['ready','quality_warning'].includes(item.status))) return
  selectedPaperIds.value = selectedPaperIds.value.includes(id)
    ? selectedPaperIds.value.filter(item => item !== id)
    : [...selectedPaperIds.value, id]
}
async function ask() {
  if (!question.value.trim() || selectedPaperIds.value.length === 0) return
  if (parseSnapshotChoice.value && (selectedPaperIds.value.length !== 1 || selectedPaperIds.value[0] !== selected.value?.id || !version.value)) {
    qaError.value = '实验解析模式仅支持将当前阅读论文作为唯一提问范围；清除实验模式或调整论文范围后继续。'
    return
  }
  qaError.value = ''; qaFailure.value = null; qaResult.value = null; searchEvidence.value = []
  historyRecord.value = null; setQaRoute(null)
  if (!config.value?.chat_ready) {
    await runQaSubmission(pending => { asking.value = pending }, () => api<{ results: Citation[] }>('/search', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: question.value, paper_ids: selectedPaperIds.value, top_k: 10,
        ...(parseSnapshotChoice.value ? {document_version_ids:[version.value!.id],parse_snapshot_ids:[parseSnapshotChoice.value]} : {}) }) }), search => {
      searchEvidence.value = search.results
      qaError.value = search.results.length
        ? '证据已检索并可定位；配置 PAPERTRAIL_LLM_API_KEY 和 PAPERTRAIL_LLM_MODEL 后可生成回答。'
        : '所选论文中未检索到匹配的原文块。'
    }, e => { qaFailure.value = describeQaFailure(e) })
    return
  }
  await runOperation('qa', '/qa', { query: question.value, paper_ids: selectedPaperIds.value, top_k: 10,
    ...(parseSnapshotChoice.value ? {document_version_ids:[version.value!.id],parse_snapshot_ids:[parseSnapshotChoice.value]} : {}) }, pending => { asking.value = pending })
}

function setQaRoute(runId: string | null) {
  const hash = runId ? `#qa/${runId}` : ''
  if ((runId && explicitQaRunId(window.location.hash) === runId) || (!runId && !explicitQaRunId(window.location.hash))) return
  if (window.location.hash !== hash) {
    const currentViewState = {
      view: viewMode.value, paperId: selected.value?.id || null, versionId: version.value?.id || null,
      pageIndex: pageIndex.value, activeBlockId: activeBlock.value?.block_id || null,
      readerScrollTop: pageScroll.value?.scrollTop || 0, readerScrollLeft: pageScroll.value?.scrollLeft || 0,
      selectedPaperIds: [...selectedPaperIds.value], question: question.value,
      historyRunId: historyRecord.value?.run_id || null, historyOffset: historyOffset.value,
      readerReturnMode: readerReturnMode.value, reportId: comparisonReport.value?.report_id || null,
      reportRevisionId: comparisonReport.value?.revision_id || null, reportsOffset: comparisonReportsOffset.value,
      noteId: noteDetail.value?.note_id || null, notesOffset: notesOffset.value,
    }
    window.history.pushState({ papertrailViewState: currentViewState }, '', `${window.location.pathname}${window.location.search}${hash}`)
  }
}
function startNewQa() {
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  historyRecord.value = null; qaResult.value = null; question.value = ''
  questionEditing.value = true
  qaError.value = ''; qaFailure.value = null; searchEvidence.value = []; showBaseline.value = false
  readerReturnMode.value = null; viewMode.value = 'reader'; sideTab.value = 'qa'
  operationTaskError.value = ''
  if (canDismissOperationTask(operationTask.value)) dismissOperationTask()
}

const activeTaskStatuses = new Set(['waiting', 'running'])
const operationActive = computed(() => Boolean(operationTask.value && activeTaskStatuses.has(operationTask.value.status)))
const operationPhaseLabels: Record<string,string> = { waiting:'等待执行', validating:'检查请求', retrieving:'检索证据', generating:'生成回答/报告', verifying:'核验引用', repairing:'受限补检索与修复', revising:'针对性修订', persisting:'保存结果', completed:'已完成', failed:'失败', interrupted:'已中断', cancelled:'已取消' }
function savedTaskKey() { try { return JSON.parse(localStorage.getItem('papertrail.active-operation') || 'null') as {kind:string;key:string;task_id?:string;payload_fingerprint?:string}|null } catch { return null } }
function saveTaskKey(value: {kind:string;key:string;task_id?:string;payload_fingerprint?:string}|null) { if (!value) localStorage.removeItem('papertrail.active-operation'); else localStorage.setItem('papertrail.active-operation', JSON.stringify(value)) }
async function updateOperationResult(task: OperationTask) {
  operationTask.value = task
  const summary = task.request_summary || {}
  if (task.kind === 'qa' && typeof summary.query === 'string') {
    question.value = summary.query
    if (Array.isArray(summary.paper_ids)) selectedPaperIds.value = summary.paper_ids
    if (Array.isArray(summary.parse_snapshot_ids)) parseSnapshotChoice.value = summary.parse_snapshot_ids.find((id: unknown) => typeof id === 'string') || ''
  } else if (task.kind === 'comparison_report') {
    if (typeof summary.title === 'string') comparisonTitle.value = summary.title
    if (Array.isArray(summary.paper_ids)) comparisonPaperIds.value = summary.paper_ids
    if (Array.isArray(summary.document_version_ids)) comparisonVersionIds.value = summary.document_version_ids
    if (Array.isArray(summary.dimensions)) comparisonDimensionsText.value = summary.dimensions.join('\n')
  }
  if (activeTaskStatuses.has(task.status)) return
  if (task.status === 'succeeded' && task.result_id) {
    try {
      if (task.kind === 'qa') {
        const result = await api<QaResult>(`/qa/${task.result_id}`)
        qaResult.value = result; historyRecord.value = result; questionEditing.value = false
      } else {
        comparisonReport.value = await api<ComparisonReportData>(`/comparison-reports/${task.result_id}`)
        await loadReportRevisions(task.result_id); await loadComparisonReports(0)
      }
      operationTaskError.value = ''
    } catch (e) { operationTaskError.value = `任务已完成，但读取保存结果失败：${String(e)}` }
  } else if (task.error) {
    operationTaskError.value = describeOperationTaskError(task)
  }
}
async function pollOperation(taskId:string) {
  const token = ++operationPollToken
  operationTaskError.value = ''
  while (token === operationPollToken) {
    try {
      const task = await api<OperationTask>(`/tasks/${taskId}`)
      await updateOperationResult(task)
      if (!activeTaskStatuses.has(task.status)) { saveTaskKey(null); return }
    } catch (e) { operationTaskError.value = `任务状态暂时无法读取；任务不会自动重跑。刷新后可继续查询。${String(e)}`; return }
    await new Promise(resolve => window.setTimeout(resolve, 900))
  }
}
async function runOperation(kind:'qa'|'comparison_report', path:string, payload:Record<string,unknown>, setPending:(pending:boolean)=>void) {
  if (operationSubmitting.value || operationTask.value && activeTaskStatuses.has(operationTask.value.status)) return
  operationSubmitting.value = true; setPending(true); operationTaskError.value = ''
  qaFailure.value = null
  const existing = savedTaskKey()
  if (operationTask.value?.status === 'outcome_unknown' && operationTask.value.kind === kind
      && !window.confirm('上次任务的外部请求结果不确定，可能已经产生模型费用。请先检查历史记录。仍要创建新任务吗？')) {
    operationSubmitting.value = false; setPending(false); return
  }
  const fingerprint = JSON.stringify(payload)
  const hasUnresolvedKey = existing?.kind === kind && (!operationTask.value || activeTaskStatuses.has(operationTask.value.status))
  if (hasUnresolvedKey && existing?.payload_fingerprint && existing.payload_fingerprint !== fingerprint) {
    operationTaskError.value = '存在尚未确认结果的旧提交。请先重新读取其状态；不会将新问题复用旧任务键。'
    operationSubmitting.value = false; setPending(false); return
  }
  const reuseExisting = hasUnresolvedKey
  const requestKey = reuseExisting ? existing! : {kind, key:crypto.randomUUID(), payload_fingerprint:fingerprint}
  saveTaskKey(requestKey)
  try {
    let task: OperationTask
    if (reuseExisting && existing?.task_id) {
      task = await api<OperationTask>(`/tasks/${existing.task_id}`)
    } else if (reuseExisting && existing) {
      try { task = await api<OperationTask>(`/tasks/by-idempotency/${kind}/${encodeURIComponent(requestKey.key)}`) }
      catch (lookupError) {
        if (!(lookupError instanceof ApiFailure) || lookupError.status !== 404) throw lookupError
        task = await api<OperationTask>(path, {method:'POST',headers:{'Content-Type':'application/json','Prefer':'respond-async','Idempotency-Key':requestKey.key},body:JSON.stringify(payload)})
      }
    } else {
      task = await api<OperationTask>(path, {method:'POST',headers:{'Content-Type':'application/json','Prefer':'respond-async','Idempotency-Key':requestKey.key},body:JSON.stringify(payload)})
    }
    saveTaskKey({kind,key:requestKey.key,task_id:task.task_id,payload_fingerprint:fingerprint})
    await updateOperationResult(task)
    if (activeTaskStatuses.has(task.status)) await pollOperation(task.task_id)
    else saveTaskKey(null)
  } catch (e) {
    operationTaskError.value = `提交或查询状态失败。为避免重复计费，没有自动重试；可刷新后按原任务键查询。${String(e)}`
  } finally { operationSubmitting.value = false; setPending(false) }
}
async function cancelActiveOperation() {
  const task = operationTask.value
  if (!task || !activeTaskStatuses.has(task.status)) return
  try { await api<OperationTask>(`/tasks/${task.task_id}/cancel`, {method:'POST'}); await pollOperation(task.task_id) }
  catch (e) { operationTaskError.value = `取消状态提交失败；后台任务状态待确认：${String(e)}` }
}
function dismissOperationTask() {
  const task = operationTask.value
  if (!canDismissOperationTask(task)) return
  if (savedTaskKey()?.task_id === task?.task_id) saveTaskKey(null)
  operationTask.value = null
  operationTaskError.value = ''
}
async function restoreOperation() {
  const saved = savedTaskKey()
  if (!saved) return
  try {
    const task = saved.task_id
      ? await api<OperationTask>(`/tasks/${saved.task_id}`)
      : await api<OperationTask>(`/tasks/by-idempotency/${saved.kind}/${encodeURIComponent(saved.key)}`)
    saveTaskKey({kind:saved.kind,key:saved.key,task_id:task.task_id,payload_fingerprint:saved.payload_fingerprint})
    if (activeTaskStatuses.has(task.status)) {
      await updateOperationResult(task)
      void pollOperation(task.task_id)
    } else {
      // Terminal results remain in history; a plain root visit starts with a fresh QA.
      saveTaskKey(null)
    }
  } catch (e) {
    if (e instanceof ApiFailure && e.status === 404) return
    operationTaskError.value = `恢复任务状态失败；没有重新执行任务：${String(e)}`
  }
}
function operationElapsed(task:OperationTask) { return `${(task.elapsed_ms/1000).toFixed(1)} 秒` }
async function importPaper(id: string) {
  busy.value = true; error.value = ''
  try {
    await api(`/papers/import/${id}`, { method: 'POST' })
    await refresh()
    await openPaper(id)
  } catch (e) { error.value = String(e) } finally { busy.value = false }
}
function selectUploadFile(file: File | null) {
  uploadFileSelection.value = file
  uploadSelectionDigest.value = null
  uploadDigestPending.value = Boolean(file)
  uploadError.value = ''
  uploadDeduplicated.value = false
  if (!file) return
  const selectedFile = file
  void file.arrayBuffer().then(bytes => crypto.subtle.digest('SHA-256', bytes)).then(hash => {
    if (uploadFileSelection.value !== selectedFile) return
    uploadSelectionDigest.value = [...new Uint8Array(hash)]
      .map(value => value.toString(16).padStart(2, '0')).join('')
    uploadDigestPending.value = false
  }).catch(() => {
    if (uploadFileSelection.value === selectedFile) uploadDigestPending.value = false
  })
}
function handleUploadFileChange(event: Event) {
  selectUploadFile((event.target as HTMLInputElement).files?.[0] || null)
}
function handleUploadDrop(event: DragEvent) {
  event.preventDefault()
  if (uploadBusy.value) return
  const file = event.dataTransfer?.files?.[0]
  if (file) selectUploadFile(file)
}
function savedUploadTask() {
  try { return JSON.parse(localStorage.getItem('papertrail.user-upload-task') || 'null') as {key:string;task_id?:string}|null }
  catch { return null }
}
function persistUploadTask(value: {key:string;task_id?:string}|null) {
  if (!value) localStorage.removeItem('papertrail.user-upload-task')
  else localStorage.setItem('papertrail.user-upload-task', JSON.stringify(value))
}
async function pollUploadTask(taskId: string) {
  const token = ++uploadPollToken
  while (token === uploadPollToken) {
    try {
      const task = await api<OperationTask>(`/tasks/${taskId}`)
      uploadTask.value = task
      uploadOutcomeUnknown.value = task.status === 'outcome_unknown'
      uploadNeedsReconcile.value = false
      const sha256 = task.request_summary?.sha256
      if (typeof sha256 === 'string') persistUploadTask({key:uploadIdempotencyKey(sha256), task_id:task.task_id})
      if (!activeTaskStatuses.has(task.status)) {
        if (task.status === 'succeeded') await refresh()
        return
      }
    } catch (e) { uploadError.value = `无法读取上传处理状态；任务没有自动重跑。刷新后会继续查询。${String(e)}`; return }
    await new Promise(resolve => window.setTimeout(resolve, 900))
  }
}
async function restoreUploadTask() {
  const saved = savedUploadTask()
  if (!saved) return
  try {
    let task = saved.task_id
      ? await api<OperationTask>(`/tasks/${saved.task_id}`)
      : await api<OperationTask>(`/tasks/by-idempotency/document_ingestion/${encodeURIComponent(saved.key)}`)
    if (task.kind === 'document_ingestion' && ['failed','interrupted','outcome_unknown'].includes(task.status)) {
      try {
        const recent = await api<{items:OperationTask[]}>('/tasks?limit=100')
        task = latestUploadTaskForVersion(task, recent.items)
      } catch { /* Keep the known persisted task if the bounded history lookup is unavailable. */ }
    }
    persistUploadTask({key:saved.key, task_id:task.task_id})
    uploadTask.value = task
    uploadOutcomeUnknown.value = task.status === 'outcome_unknown'
    uploadNeedsReconcile.value = false
    if (activeTaskStatuses.has(task.status)) void pollUploadTask(task.task_id)
    else if (task.status === 'succeeded') await refresh()
  } catch (e) {
    if (e instanceof ApiFailure && e.status === 404) {
      uploadOutcomeUnknown.value = true
      uploadNeedsReconcile.value = true
      uploadError.value = '本地保存了上传幂等键，但服务端尚无对应任务记录。没有重新上传；请重新选择同一 PDF 后显式安全重试。'
    } else {
      uploadOutcomeUnknown.value = true
      uploadNeedsReconcile.value = true
      uploadError.value = `上传任务状态无法确认；没有重新上传。请检查服务连接后再读取状态。${String(e)}`
    }
  }
}
async function openUploadDialog() {
  uploadDialogOpen.value = true
  // Reconcile on every reopen. The remembered task can be outcome_unknown even
  // after a later explicit recovery task made this same version ready.
  if (savedUploadTask()) await restoreUploadTask()
}
function closeUploadDialog() { uploadDialogOpen.value = false }
async function openUploadedPaper() {
  const paperId = uploadTask.value?.request_summary?.paper_id
  if (uploadTask.value?.kind !== 'document_ingestion' || uploadTask.value.status !== 'succeeded' || !paperId) return
  await refresh()
  const paper = papers.value.find(item => item.id === paperId)
  if (!paper?.versions.some(item => ['ready', 'quality_warning'].includes(item.status))) {
    uploadError.value = '任务已完成，但论文当前没有可阅读的就绪版本；请检查论文库状态。'
    return
  }
  closeUploadDialog()
  await openPaper(paperId)
}
async function retryUserUploadTask() {
  const task = uploadTask.value
  const summary = task?.request_summary || {}
  if (!task || task.kind !== 'document_ingestion' || !summary.paper_id || !summary.document_version_id) return
  const confirmUnknown = task.status === 'outcome_unknown'
  if (confirmUnknown && !window.confirm('上次索引 Embedding 请求结果不确定，可能已产生费用。请先检查任务详情；仍要明确重试吗？')) return
  uploadBusy.value = true; uploadError.value = ''; uploadElapsedSeconds.value = 0
  uploadElapsedTimer = window.setInterval(() => { uploadElapsedSeconds.value += 1 }, 1000)
  try {
    const key = `retry-${crypto.randomUUID()}`
    const retried = await api<OperationTask>(`/user-papers/${summary.paper_id}/versions/${summary.document_version_id}/retry`, {
      method:'POST', headers:{'Idempotency-Key':key, ...(confirmUnknown ? {'X-Confirm-Outcome-Unknown-Retry':'true'} : {})},
    })
    uploadTask.value = retried
    uploadOutcomeUnknown.value = false
    persistUploadTask({key, task_id:retried.task_id})
    if (activeTaskStatuses.has(retried.status)) await pollUploadTask(retried.task_id)
    else if (retried.status === 'succeeded') await refresh()
  } catch (e) { uploadError.value = `处理重试未完成：${String(e)}；请重新读取任务状态，不会自动重复提交。` }
  finally {
    uploadBusy.value = false
    if (uploadElapsedTimer !== undefined) window.clearInterval(uploadElapsedTimer)
    uploadElapsedTimer = undefined
  }
}
function uploadTaskStatusLabel(status: string) {
  return ({waiting:'等待处理',running:'处理中',succeeded:'已完成',failed:'失败',interrupted:'已中断',cancelled:'已取消',outcome_unknown:'结果未知'} as Record<string,string>)[status] || status
}
function uploadTaskFilename(task: OperationTask | null) {
  const paperId = task?.request_summary?.paper_id
  return papers.value.find(paper => paper.id === paperId)?.original_filename ||
    (uploadSelectionDigest.value === task?.request_summary?.sha256 ? uploadFileSelection.value?.name : null) ||
    '原文件名未保存在任务摘要中'
}
function uploadTaskFileSize(task: OperationTask | null) {
  const versionId = task?.request_summary?.document_version_id
  const paperId = task?.request_summary?.paper_id
  const storedSize = papers.value.find(paper => paper.id === paperId)?.versions.find(item => item.id === versionId)?.file_size_bytes
  if (typeof storedSize === 'number') return storedSize
  return uploadSelectionDigest.value === task?.request_summary?.sha256 ? uploadFileSelection.value?.size ?? null : null
}
function uploadTaskFileSizeLabel(task: OperationTask | null) {
  const bytes = uploadTaskFileSize(task)
  return bytes === null ? '未知' : `${(bytes / 1024 / 1024).toFixed(2)} MiB`
}
function uploadTaskDiagnostic(task: OperationTask) {
  const detail = task.error?.details || {}
  const reason = typeof detail.reason === 'string' ? detail.reason : task.error?.code || '未知错误'
  const batch = Number.isInteger(detail.batch_index)
    ? ` · 第 ${detail.batch_index} 批${Number.isInteger(detail.batch_size) ? `（${detail.batch_size} 条）` : ''}` : ''
  return `${task.phase === 'embedding' ? 'Embedding' : uploadPhaseLabels[task.phase] || task.phase} · ${reason}${batch}`
}
function uploadTaskErrorMessage(task: OperationTask) {
  return uploadTaskGuidance(task, config.value || {}).message
}
function uploadTaskRetry(task: OperationTask) { return uploadTaskGuidance(task, config.value || {}) }
async function refreshUploadTaskStatus() {
  if (!uploadTask.value) return
  uploadBusy.value = true
  uploadError.value = ''
  try {
    let task = await api<OperationTask>(`/tasks/${uploadTask.value.task_id}`)
    if (task.kind === 'document_ingestion' && ['failed','interrupted','outcome_unknown'].includes(task.status)) {
      try {
        const recent = await api<{items:OperationTask[]}>('/tasks?limit=100')
        task = latestUploadTaskForVersion(task, recent.items)
      } catch { /* Preserve the task that was directly queried. */ }
    }
    uploadTask.value = task
    uploadOutcomeUnknown.value = task.status === 'outcome_unknown'
    uploadNeedsReconcile.value = false
    const sha256 = task.request_summary?.sha256
    if (typeof sha256 === 'string') persistUploadTask({key:uploadIdempotencyKey(sha256), task_id:task.task_id})
    if (activeTaskStatuses.has(task.status)) await pollUploadTask(task.task_id)
    else if (task.status === 'succeeded') await refresh()
  } catch (e) { uploadError.value = `读取任务状态失败；未重新发送文件或向量请求。${String(e)}` }
  finally { uploadBusy.value = false }
}
async function submitUserPdf() {
  const file = uploadFileSelection.value
  if (!file || uploadBusy.value) return
  uploadBusy.value = true; uploadError.value = ''; uploadElapsedSeconds.value = 0
  uploadElapsedTimer = window.setInterval(() => { uploadElapsedSeconds.value += 1 }, 1000)
  let timeoutId: number | undefined
  let controller: AbortController | undefined
  try {
    const digest = uploadSelectionDigest.value || [...new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))]
      .map(value => value.toString(16).padStart(2, '0')).join('')
    const key = uploadIdempotencyKey(digest)
    uploadSelectionDigest.value = digest
    uploadDigestPending.value = false
    const previous = savedUploadTask()
    if (uploadTask.value?.kind === 'document_ingestion' && uploadTask.value.request_summary?.sha256 === digest) {
      try {
        const known = await api<OperationTask>(`/tasks/${uploadTask.value.task_id}`)
        uploadTask.value = known
        if (activeTaskStatuses.has(known.status)) await pollUploadTask(known.task_id)
        else if (known.status === 'succeeded') await refresh()
        return
      } catch (lookupError) {
        uploadOutcomeUnknown.value = true
        uploadError.value = `无法读取该 PDF 的现有任务；没有重新上传或重发向量。请先检查任务状态。${String(lookupError)}`
        return
      }
    }
    if (previous?.key === key) {
      try {
        const known = previous.task_id
          ? await api<OperationTask>(`/tasks/${previous.task_id}`)
          : await api<OperationTask>(`/tasks/by-idempotency/document_ingestion/${encodeURIComponent(key)}`)
        uploadTask.value = known
        persistUploadTask({key, task_id:known.task_id})
        uploadNeedsReconcile.value = false
        uploadOutcomeUnknown.value = false
        if (activeTaskStatuses.has(known.status)) await pollUploadTask(known.task_id)
        else if (known.status === 'succeeded') await refresh()
        return
      } catch (lookupError) {
        if (!(lookupError instanceof ApiFailure) || lookupError.status !== 404) {
          uploadOutcomeUnknown.value = true
          uploadError.value = `无法确认此前上传任务状态；为避免重复处理，本次没有重新发送文件。请检查服务后再试。${String(lookupError)}`
          return
        }
      }
    }
    persistUploadTask({key})
    const form = new FormData(); form.append('file', file)
    controller = new AbortController()
    timeoutId = window.setTimeout(() => controller?.abort(), uploadRequestTimeoutMs)
    const result = await api<{deduplicated:boolean;paper:Paper;version:Version;task:OperationTask}>(
      '/user-papers/upload', {method:'POST', headers:{'Idempotency-Key':key}, body:form, signal:controller.signal})
    uploadDeduplicated.value = result.deduplicated
    uploadTask.value = result.task
    uploadOutcomeUnknown.value = false
    uploadNeedsReconcile.value = false
    persistUploadTask({key, task_id:result.task.task_id})
    await refresh()
    if (activeTaskStatuses.has(result.task.status)) await pollUploadTask(result.task.task_id)
    else if (result.task.status === 'succeeded') await refresh()
  } catch (e) {
    const failure = classifyUploadFailure(e)
    if (failure.kind === 'http_rejected') {
      uploadOutcomeUnknown.value = false
      uploadNeedsReconcile.value = false
      const apiFailure = e as ApiFailure
      uploadError.value = `服务器明确拒绝上传（HTTP ${failure.status}，${apiFailure.payload.code}）。请检查文件类型、大小和 PDF 有效性。`
    } else {
      uploadOutcomeUnknown.value = true
      uploadNeedsReconcile.value = true
      const key = savedUploadTask()?.key
      if (key) {
        try {
          const known = await api<OperationTask>(`/tasks/by-idempotency/document_ingestion/${encodeURIComponent(key)}`)
          uploadTask.value = known
          persistUploadTask({key, task_id:known.task_id})
          uploadOutcomeUnknown.value = false
          uploadNeedsReconcile.value = false
          if (activeTaskStatuses.has(known.status)) await pollUploadTask(known.task_id)
          else if (known.status === 'succeeded') await refresh()
          return
        } catch (lookupError) {
          if (!(lookupError instanceof ApiFailure) || lookupError.status !== 404) {
            uploadError.value = `上传响应未确认，任务状态查询也失败；没有自动重试。请先恢复服务后重新读取任务。${String(lookupError)}`
            return
          }
        }
      }
      const reason = failure.kind === 'timeout_outcome_unknown'
        ? `上传请求超过 ${Math.round(uploadRequestTimeoutMs / 1000)} 秒未收到确认`
        : failure.kind === 'http_outcome_unknown'
          ? `服务器返回 HTTP ${failure.status}，处理结果仍需查询`
          : '上传连接中断且没有收到 HTTP 响应'
      uploadError.value = `${reason}；服务器当前没有对应任务记录。系统未自动重传。保留同一文件后再次点击会先查询任务，并复用相同 SHA-256 幂等键。`
    }
  } finally {
    if (timeoutId !== undefined) window.clearTimeout(timeoutId)
    uploadBusy.value = false
    if (uploadElapsedTimer !== undefined) window.clearInterval(uploadElapsedTimer)
    uploadElapsedTimer = undefined
  }
}
async function uploadFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  busy.value = true; error.value = ''
  try {
    const form = new FormData(); form.append('file', file)
    const result = await api<{ version: Version }>('/papers/upload', { method: 'POST', body: form })
    await refresh(); await openPaper(result.version.id.split('_')[1])
  } catch (e) { error.value = String(e) } finally { busy.value = false; input.value = '' }
}
async function openPaper(id: string) {
  const loadToken = ++readerLoadToken
  const target = papers.value.find(p => p.id === id) || null
  const readyVersion = target?.versions.find(v => ['ready', 'quality_warning'].includes(v.status)) || null
  if (!target || !readyVersion) { error.value = '这篇论文尚未完成解析和索引，暂时不能阅读或提问。'; return }
  error.value = ''
  selected.value = target
  activeVersion.value = readyVersion
  parseSnapshotChoice.value = ''
  viewMode.value = 'reader'; sideTab.value = 'qa'; questionEditing.value = true
  const versionId = readyVersion.id
  const pdfPromise = loadPdfDocument(versionId, loadToken)
  await loadSnapshotOptions(readyVersion.id)
  if (!isCurrentReaderLoad(loadToken) || !await pdfPromise) return
  const renderPromise = setPage(0)
  activeBlock.value = null
  const loadedBlocks = await api<Block[]>(`/documents/${versionId}/blocks?limit=1000`)
  if (!isCurrentReaderLoad(loadToken)) return
  await renderPromise
  blocks.value = loadedBlocks
  highlight()
}
async function waitForReaderWidth(token: number) {
  await nextTick()
  for (let attempt = 0; attempt < 30; attempt++) {
    if (token !== renderToken) return 0
    const element = pageScroll.value
    if (element) {
      const styles = getComputedStyle(element)
      const padding = (Number.parseFloat(styles.paddingLeft) || 0) + (Number.parseFloat(styles.paddingRight) || 0)
      const width = element.clientWidth - padding
      if (Number.isFinite(width) && width > 80) return width
    }
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  }
  return 0
}
async function setPage(index: number, allowOneRecovery = true) {
  const document = pdf
  if (!document || index < 0 || index >= document.numPages) return
  const token = ++renderToken
  pageIndex.value = index
  pdfPageReady.value = false
  pdfError.value = ''
  await cancelCurrentRender()
  if (token !== renderToken || document !== pdf) return
  try {
    const width = await waitForReaderWidth(token)
    if (!width) {
      if (token === renderToken) pdfError.value = 'PDF 阅读区域尚未完成布局；布局可用后会自动继续。'
      return
    }
    const pageNumber = pdfPageNumber(index)
    if (pageNumber === null) return
    const page = await document.getPage(pageNumber)
    if (token !== renderToken || document !== pdf) return
    const baseWidth = page.getViewport({ scale: 1 }).width
    const pageScale = computeFitScale(width, baseWidth, scale.value)
    if (!pageScale) throw new Error('PDF 页面宽度无效，无法计算适合宽度缩放')
    const viewport = page.getViewport({ scale: pageScale })
    const targetCanvas = canvas.value
    if (!targetCanvas) {
      if (token === renderToken) pdfError.value = 'PDF 画布尚未挂载；请重新加载 PDF。'
      return
    }
    renderedScale.value = pageScale
    const dpr = window.devicePixelRatio || 1
    targetCanvas.width = renderPixelSize(viewport.width, dpr)
    targetCanvas.height = renderPixelSize(viewport.height, dpr)
    targetCanvas.style.width = `${viewport.width}px`
    targetCanvas.style.height = `${viewport.height}px`
    if (overlay.value) { overlay.value.style.width = `${viewport.width}px`; overlay.value.style.height = `${viewport.height}px` }
    const context = targetCanvas.getContext('2d')
    if (!context) throw new Error('浏览器无法创建 PDF 画布上下文')
    const task = page.render({ canvas: targetCanvas, canvasContext: context, viewport,
      ...(dpr !== 1 ? { transform: [dpr, 0, 0, dpr, 0, 0] as [number, number, number, number, number, number] } : {}) })
    pdfRenderTask = task
    await task.promise
    if (!isRenderCurrent({ token, currentToken: renderToken, document, currentDocument: pdf, canvas: targetCanvas, currentCanvas: canvas.value })) return
    pdfRenderTask = null
    pdfError.value = ''
    pdfPageReady.value = true
    lastRenderedWidth = width
    highlight()
    persistViewState()
  } catch (cause) {
    if (token !== renderToken || document !== pdf || isPdfRenderCancellation(cause)) return
    pdfRenderTask = null
    const recoveryKey = `${readerLoadToken}:${index}`
    if (allowOneRecovery && !autoRecoveredPages.has(recoveryKey)) {
      autoRecoveredPages.add(recoveryKey)
      await setPage(index, false)
      return
    }
    pdfError.value = `PDF 页面渲染失败：${cause instanceof Error ? cause.message : String(cause)}`
  }
}
async function reloadPdf() {
  const currentVersion = version.value
  if (!currentVersion) return
  const loadToken = ++readerLoadToken
  const targetPage = pageIndex.value
  if (!await loadPdfDocument(currentVersion.id, loadToken)) return
  await setPage(Math.min(targetPage, Math.max(0, (pdf?.numPages || 1) - 1)), false)
}
function highlight() {
  if (!overlay.value) return
  overlay.value.replaceChildren()
  const b = activeBlock.value
  if (!b || b.pdf_page_index !== pageIndex.value) return
  const [x0, y0, x1, y1] = b.bbox
  const mark = document.createElement('div')
  mark.className = 'highlight'
  mark.style.left = `${x0 * renderedScale.value}px`
  mark.style.top = `${y0 * renderedScale.value}px`
  mark.style.width = `${(x1 - x0) * renderedScale.value}px`
  mark.style.height = `${(y1 - y0) * renderedScale.value}px`
  overlay.value.append(mark)
}
async function locate(b: Block) {
  activeBlock.value = b
  if (pageIndex.value !== b.pdf_page_index) await setPage(b.pdf_page_index)
  else highlight()
  if (pageScroll.value) {
    pageScroll.value.scrollTop = Math.max(0, b.bbox[1] * renderedScale.value - pageScroll.value.clientHeight / 3)
    pageScroll.value.scrollLeft = Math.max(0, b.bbox[0] * renderedScale.value - pageScroll.value.clientWidth / 3)
  }
  persistViewState()
}
function toggleLibrary() {
  libraryCollapsed.value = !libraryCollapsed.value
  writeWorkspacePreferences(window.localStorage, { libraryCollapsed: libraryCollapsed.value, readerSharePercent: readerSharePercent.value })
}
function setReaderShare(value: number) {
  readerSharePercent.value = clampReaderShare(value)
  writeWorkspacePreferences(window.localStorage, { libraryCollapsed: libraryCollapsed.value, readerSharePercent: readerSharePercent.value })
}
function resetReaderShare() { setReaderShare(40) }
function handleSplitterKeydown(event: KeyboardEvent) {
  const step = event.shiftKey ? 5 : 2
  if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { event.preventDefault(); setReaderShare(readerSharePercent.value - step) }
  else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { event.preventDefault(); setReaderShare(readerSharePercent.value + step) }
  else if (event.key === 'Home') { event.preventDefault(); resetReaderShare() }
}
function startSplitterResize(event: PointerEvent) {
  if (!workspaceMain.value || event.button !== 0) return
  event.preventDefault()
  const mainRect = workspaceMain.value.getBoundingClientRect()
  const libraryWidth = libraryCollapsed.value ? 52 : 260
  const usableWidth = Math.max(1, mainRect.width - libraryWidth - 8)
  const next = ((event.clientX - mainRect.left - libraryWidth) / usableWidth) * 100
  setReaderShare(next)
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function moveSplitterResize(event: PointerEvent) {
  if (event.buttons !== 1 || !workspaceMain.value) return
  const mainRect = workspaceMain.value.getBoundingClientRect()
  const libraryWidth = libraryCollapsed.value ? 52 : 260
  const usableWidth = Math.max(1, mainRect.width - libraryWidth - 8)
  setReaderShare(((event.clientX - mainRect.left - libraryWidth) / usableWidth) * 100)
}
async function rerenderPageForResize() {
  if (resizeRenderPending || !pdf || pdfError.value || !pageScroll.value || viewMode.value !== 'reader') return
  resizeRenderPending = true
  const scroller = pageScroll.value
  const topRatio = scroller.scrollHeight > scroller.clientHeight ? scroller.scrollTop / (scroller.scrollHeight - scroller.clientHeight) : 0
  const leftRatio = scroller.scrollWidth > scroller.clientWidth ? scroller.scrollLeft / (scroller.scrollWidth - scroller.clientWidth) : 0
  try {
    await setPage(pageIndex.value)
    await nextTick()
    if (pageScroll.value === scroller) {
      scroller.scrollTop = topRatio * Math.max(0, scroller.scrollHeight - scroller.clientHeight)
      scroller.scrollLeft = leftRatio * Math.max(0, scroller.scrollWidth - scroller.clientWidth)
      persistViewState()
    }
  } finally {
    resizeRenderPending = false
    const styles = getComputedStyle(scroller)
    const currentWidth = scroller.clientWidth - (Number.parseFloat(styles.paddingLeft) || 0) - (Number.parseFloat(styles.paddingRight) || 0)
    if (pdfPageReady.value && currentWidth > 80 && Math.abs(currentWidth - lastRenderedWidth) > 2) void rerenderPageForResize()
  }
}
async function locateCitation(citation: Citation, spanIndex = 0) {
  const span = citation.spans?.[spanIndex]
  if (!span || !span.bbox || !Number.isInteger(span.pdf_page_index) || !(citation.verification?.location_valid ?? citation.location_valid ?? true)) return
  if (!pdf || version.value?.id !== citation.document_version_id) {
    const loadToken = ++readerLoadToken
    selected.value = papers.value.find(p => p.id === citation.paper_id) || null
    activeVersion.value = selected.value?.versions.find(v => v.id === citation.document_version_id) || null
    if (!activeVersion.value) return
    const versionId = activeVersion.value.id
    viewMode.value = 'reader'
    if (!await loadPdfDocument(versionId, loadToken)) return
    const snapshotQuery = citation.parse_snapshot_id ? `&parse_snapshot_id=${encodeURIComponent(citation.parse_snapshot_id)}` : ''
    const loadedBlocks = await api<Block[]>(`/documents/${versionId}/blocks?limit=1000${snapshotQuery}`)
    if (!isCurrentReaderLoad(loadToken)) return
    blocks.value = loadedBlocks
  }
  await locate({ block_id: span.block_id, pdf_page_index: span.pdf_page_index, ordinal: 0,
    text: citation.exact_quote || citation.chunk_text || citation.text || '', bbox: span.bbox,
    page_width: span.page_width || 0, page_height: span.page_height || 0, section_path: citation.section_path || [] })
  viewMode.value = 'reader'; sideTab.value = 'qa'
}
async function showHistory(reset = true) {
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  viewMode.value = 'history'
  if (reset) { historyScrollTop.value = 0; await Promise.all([loadHistory(0), loadTaskHistory()]) }
  else await Promise.all([loadHistory(historyOffset.value), loadTaskHistory()])
  await nextTick()
  if (historyList.value) historyList.value.scrollTop = historyScrollTop.value
}
async function returnToCollection() {
  if (readerReturnMode.value === 'history') setQaRoute(null)
  if (readerReturnMode.value === 'notes') { viewMode.value = 'notes'; readerReturnMode.value = null; return }
  if (readerReturnMode.value === 'history') { await showHistory(false); readerReturnMode.value = null; return }
  if (readerReturnMode.value === 'reports') { viewMode.value = 'reports'; readerReturnMode.value = null; return }
  viewMode.value = 'reader'
}
async function loadComparisonReports(offset = comparisonReportsOffset.value) {
  comparisonReportsLoading.value = true; comparisonReportsError.value = ''
  try {
    const page = await api<{items:ComparisonReportItem[];total:number;offset:number}>(`/comparison-reports?limit=20&offset=${offset}`)
    comparisonReports.value = page.items; comparisonReportsTotal.value = page.total; comparisonReportsOffset.value = page.offset
  } catch (e) { comparisonReportsError.value = `读取报告失败：${String(e)}` }
  finally { comparisonReportsLoading.value = false }
}
async function showComparisonReports() {
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  viewMode.value = 'reports'; comparisonReport.value = null
  if (!papers.value.length) await refresh()
  comparisonPaperIds.value = []; comparisonVersionIds.value = []
  comparisonNoteIds.value = []; await Promise.all([loadComparisonReports(0), loadNotes(0)])
}
function showEvaluationResults() {
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  viewMode.value = 'evaluation'
}
function showReader() {
  startNewQa()
}
function toggleComparisonPaper(paperId: string) {
  if (comparisonPaperIds.value.includes(paperId)) comparisonPaperIds.value = comparisonPaperIds.value.filter(id => id !== paperId)
  else if (comparisonPaperIds.value.length < 3) comparisonPaperIds.value.push(paperId)
  comparisonVersionIds.value = comparisonPaperIds.value.map(id => papers.value.find(p => p.id === id)?.versions.find(v => ['ready','quality_warning'].includes(v.status))?.id || '')
}
async function createComparisonReport() {
  if (comparisonCreating.value || comparisonPaperIds.value.length < 2) return
  comparisonReportsError.value = ''
  await runOperation('comparison_report','/comparison-reports',{title:comparisonTitle.value,paper_ids:comparisonPaperIds.value,document_version_ids:comparisonVersionIds.value,
    dimensions:comparisonDimensionsText.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean),note_ids:comparisonNoteIds.value},pending=>{comparisonCreating.value=pending})
}
async function openComparisonReport(item: ComparisonReportItem) {
  comparisonReportsError.value = ''
  try { comparisonReport.value = await api<ComparisonReportData>(`/comparison-reports/${item.report_id}`); await loadReportRevisions(item.report_id) }
  catch (e) { comparisonReportsError.value = `打开报告失败：${String(e)}` }
}
async function loadReportRevisions(reportId = comparisonReport.value?.report_id) {
  if (!reportId) return
  try { comparisonReportRevisions.value = (await api<{items:ReportRevisionItem[]}>(`/comparison-reports/${reportId}/revisions`)).items }
  catch (e) { comparisonReportsError.value = `读取修订历史失败：${String(e)}` }
}
async function openReportRevision(revisionId: string) {
  if (!comparisonReport.value) return
  try { comparisonReport.value = await api<ComparisonReportData>(`/comparison-reports/${comparisonReport.value.report_id}/revisions/${revisionId}`); reportEditingClaim.value = null }
  catch (e) { comparisonReportsError.value = `打开修订失败：${String(e)}` }
}
async function openOriginalReport() {
  if (!comparisonReport.value) return
  try { comparisonReport.value = await api<ComparisonReportData>(`/comparison-reports/${comparisonReport.value.report_id}`); reportEditingClaim.value = null }
  catch (e) { comparisonReportsError.value = `打开原版失败：${String(e)}` }
}
function beginReportEdit(claim: ReportClaim) { reportEditingClaim.value = claim; reportEditText.value = claim.text; reportEditReason.value = '' }
async function saveReportEdit() {
  if (!comparisonReport.value || !reportEditingClaim.value || !reportEditText.value.trim() || !reportEditReason.value.trim() || reportSavingEdit.value) return
  reportSavingEdit.value = true
  try {
    const revised = await api<ComparisonReportData>(`/comparison-reports/${comparisonReport.value.report_id}/revisions`, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({changes:[{claim_id:reportEditingClaim.value.claim_id,text:reportEditText.value,reason:reportEditReason.value}]})})
    comparisonReport.value = revised; reportEditingClaim.value = null; await loadReportRevisions(revised.report_id)
  } catch (e) { comparisonReportsError.value = `保存修订失败：${String(e)}` }
  finally { reportSavingEdit.value = false }
}
async function exportComparisonReport() {
  if (!comparisonReport.value) return
  try {
    const endpoint = comparisonReport.value.revision_id
      ? `/api/v1/comparison-reports/${comparisonReport.value.report_id}/revisions/${comparisonReport.value.revision_id}/markdown`
      : `/api/v1/comparison-reports/${comparisonReport.value.report_id}/markdown`
    const response = await fetch(endpoint)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a')
    link.href=url; link.download=`${comparisonReport.value.title.replace(/[\\/:*?"<>|\x00-\x1f]/g,'_')}${comparisonReport.value.revision_number?`-修订${comparisonReport.value.revision_number}`:''}.md`
    document.body.append(link); link.click(); link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { comparisonReportsError.value = `导出失败：${String(e)}` }
}
async function openReportCitation(citation: Citation) {
  const paper = papers.value.find(item => item.id === citation.paper_id)
  const fixedVersion = paper?.versions.find(item => item.id === citation.document_version_id)
  if (!paper || !fixedVersion) { comparisonReportsError.value = '引用所属文档版本当前不可用。'; return }
  setQaRoute(null)
  const loadToken = ++readerLoadToken
  selected.value = paper; activeVersion.value = fixedVersion
  readerReturnMode.value = 'reports'; viewMode.value = 'reader'
  if (!await loadPdfDocument(fixedVersion.id, loadToken)) return
  const loadedBlocks = await api<Block[]>(`/documents/${fixedVersion.id}/blocks?limit=1000`)
  if (!isCurrentReaderLoad(loadToken)) return
  blocks.value = loadedBlocks
  const span = citation.spans?.find(item => item.bbox && citation.verification?.location_valid)
  if (span) await locateCitation(citation, citation.spans.indexOf(span))
  else {
    const quotePages = citation.quote_location_pdf_page_indexes || []
    const pages = quotePages.length ? quotePages : citation.chunk_pdf_page_indexes || []
    if (pages.length) await setPage(Math.min(...pages))
  }
}
async function loadNotes(offset = notesOffset.value) {
  notesLoading.value = true; notesError.value = ''
  try {
    const page = await api<{items: NoteItem[]; total: number; offset: number}>('/notes?limit=' + noteLimit + '&offset=' + offset)
    notes.value = page.items; notesTotal.value = page.total; notesOffset.value = page.offset
  } catch (e) { notesError.value = `读取笔记失败：${String(e)}` }
  finally { notesLoading.value = false }
}
function confirmNoteDiscard() {
  return !noteEditorDirty.value || window.confirm('有尚未保存的笔记修改；离开会丢弃这些内容。继续吗？')
}
async function showNotes() {
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  viewMode.value = 'notes'; noteCompose.value = false; noteEditing.value = false; noteSource.value = null
  noteDetail.value = null; notesError.value = ''; await loadNotes(0)
}
function startNoteDraft() {
  if (!qaResult.value || qaResult.value.fixture_label || qaResult.value.controlled_test || !qaResult.value.run_id.startsWith('qrun_')) return
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  noteDetail.value = null; noteCompose.value = true; noteEditing.value = false
  noteSource.value = qaResult.value; notePaperIds.value = [...(qaResult.value.paper_ids || [])]
  noteTitle.value = ''; noteBody.value = ''; noteSelectedClaims.value = []; noteSelectedEvidence.value = []; notesError.value = ''
  viewMode.value = 'notes'; void loadNotes(notesOffset.value)
}
function startStandaloneNoteDraft() {
  if (!confirmNoteDiscard()) return
  setQaRoute(null)
  noteDetail.value = null; noteCompose.value = true; noteEditing.value = false; noteSource.value = null
  viewMode.value = 'notes'
  noteTitle.value = ''; noteBody.value = ''; notePaperIds.value = []
  noteSelectedClaims.value = []; noteSelectedEvidence.value = []; notesError.value = ''
}
function cancelNoteEdit() {
  if (!confirmNoteDiscard()) return
  noteCompose.value = false; noteEditing.value = false; noteSource.value = null
  if (noteDetail.value) {
    noteTitle.value = noteDetail.value.title; noteBody.value = noteDetail.value.body
    notePaperIds.value = [...(noteDetail.value.paper_ids || [])]
  }
}
async function openNote(item: NoteItem) {
  if (!confirmNoteDiscard()) return
  notesError.value = ''
  try {
    noteDetail.value = await api<NoteDetail>(`/notes/${item.note_id}`); noteCompose.value = false; noteEditing.value = false; noteSource.value = null
    noteTitle.value = noteDetail.value.title; noteBody.value = noteDetail.value.body; notePaperIds.value = [...(noteDetail.value.paper_ids || [])]
  }
  catch (e) { notesError.value = `打开笔记失败：${String(e)}` }
}
async function saveNote() {
  if (!noteTitle.value.trim() || noteSaving.value) return
  noteSaving.value = true; notesError.value = ''
  try {
    if (noteCompose.value) {
      noteDetail.value = await api<NoteDetail>('/notes', { method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ title: noteTitle.value, body: noteBody.value, paper_ids: notePaperIds.value,
          source_qa_run_id: noteSource.value?.run_id || null,
          claim_ids: noteSource.value ? noteSelectedClaims.value : [],
          evidence_ids: noteSource.value ? noteSelectedEvidence.value : [] }) })
      noteCompose.value = false; noteSource.value = null
    } else if (noteDetail.value) {
      noteDetail.value = await api<NoteDetail>(`/notes/${noteDetail.value.note_id}`, { method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ title: noteTitle.value, body: noteBody.value, paper_ids: notePaperIds.value }) })
      noteEditing.value = false
    }
    await loadNotes(notesOffset.value)
  } catch (e) { notesError.value = `保存笔记失败：${String(e)}` }
  finally { noteSaving.value = false }
}
function editNote() { if (!noteDetail.value) return; noteTitle.value = noteDetail.value.title; noteBody.value = noteDetail.value.body; notePaperIds.value = [...(noteDetail.value.paper_ids || [])]; noteEditing.value = true }
async function deleteNote() {
  if (!noteDetail.value || noteDeleting.value || !window.confirm(`删除笔记“${noteDetail.value.title}”？此操作不可撤销。`)) return
  noteDeleting.value = true; notesError.value = ''
  try {
    await api<void>(`/notes/${noteDetail.value.note_id}`, { method: 'DELETE' })
    noteDetail.value = null; noteEditing.value = false
    await loadNotes(notes.value.length === 1 && notesOffset.value > 0 ? Math.max(0, notesOffset.value - noteLimit) : notesOffset.value)
  } catch (e) { notesError.value = `删除笔记失败：${String(e)}` }
  finally { noteDeleting.value = false }
}
async function exportNote() {
  if (!noteDetail.value) return
  try {
    const response = await fetch(`/api/v1/notes/${noteDetail.value.note_id}/markdown`)
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`)
    const blob = await response.blob(); const url = URL.createObjectURL(blob)
    const disposition = response.headers.get('content-disposition') || ''
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
    const filename = encodedName ? decodeURIComponent(encodedName) : 'papertrail-note.md'
    const link = document.createElement('a'); link.href = url; link.download = filename
    document.body.append(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { notesError.value = `导出失败：${String(e)}` }
}
async function openNoteSource() {
  if (!noteDetail.value?.source_qa_available) return
  try {
    const record = await api<QaResult>(`/qa/${noteDetail.value.source_qa_run_id}`)
    await openRecordInReader(record, noteDetail.value.source_query_snapshot, 'notes')
  } catch (e) { notesError.value = `来源问答无法打开：${String(e)}` }
}
async function openRecordInReader(record: QaResult, fallbackQuery: string, returnMode: 'history' | 'notes', readerLocation?: {
  versionId: string | null; pageIndex: number; activeBlockId: string | null; scrollTop: number; scrollLeft: number
}) {
  const loadToken = ++readerLoadToken
  historyRecord.value = record; qaResult.value = record; question.value = record.query || fallbackQuery
  questionEditing.value = false
  selectedPaperIds.value = record.paper_ids || []; searchEvidence.value = []; qaError.value = ''; qaFailure.value = null; showBaseline.value = false
  readerReturnMode.value = returnMode
  if (returnMode === 'history') setQaRoute(record.run_id)
  else setQaRoute(null)
  // The history/notes page may still be mounted when this function is called.
  // Switch the reader into the tree before setPage() so PDF.js has a canvas to
  // render into; otherwise the saved page number changes while the canvas stays
  // at its empty 300×150 default until another resize or page navigation.
  viewMode.value = 'reader'; sideTab.value = 'qa'
  const preferredVersion = readerLocation?.versionId && record.document_version_ids.includes(readerLocation.versionId)
    ? papers.value.flatMap(paper => paper.versions.map(fixed => ({ paper, fixed }))).find(item => item.fixed.id === readerLocation.versionId)
    : undefined
  const citation = record.citations.find(item => item.document_version_id === preferredVersion?.fixed.id) || record.citations[0]
  const paperId = preferredVersion?.paper.id || citation?.paper_id || selectedPaperIds.value[0]
  if (paperId) {
    if (!papers.value.length) await refresh()
    if (!isCurrentReaderLoad(loadToken)) return
    selected.value = papers.value.find(p => p.id === paperId) || null
    activeVersion.value = preferredVersion?.fixed || selected.value?.versions.find(v => v.id === citation?.document_version_id)
      || selected.value?.versions.find(v => record.document_version_ids.includes(v.id)) || selected.value?.versions[0] || null
    if (activeVersion.value) {
      parseSnapshotChoice.value = citation?.parse_snapshot_id || ''
      const versionId = activeVersion.value.id
      await loadSnapshotOptions(versionId)
      if (!isCurrentReaderLoad(loadToken) || !await loadPdfDocument(versionId, loadToken)) return
      const desiredPage = preferredVersion && readerLocation
        ? Math.min(readerLocation.pageIndex, Math.max(0, activeVersion.value.page_count - 1)) : 0
      const renderPromise = setPage(desiredPage)
      const snapshotQuery = citation?.parse_snapshot_id ? `&parse_snapshot_id=${encodeURIComponent(citation.parse_snapshot_id)}` : ''
      const loadedBlocks = await api<Block[]>(`/documents/${versionId}/blocks?limit=1000${snapshotQuery}`)
      if (!isCurrentReaderLoad(loadToken)) return
      await renderPromise
      blocks.value = loadedBlocks
      activeBlock.value = preferredVersion && readerLocation?.activeBlockId
        ? blocks.value.find(block => block.block_id === readerLocation.activeBlockId && block.pdf_page_index === desiredPage) || null
        : null
      highlight()
      if (preferredVersion && readerLocation && pageScroll.value) {
        pageScroll.value.scrollTop = readerLocation.scrollTop; pageScroll.value.scrollLeft = readerLocation.scrollLeft
      }
    }
  }
}
async function openNoteCitation(citation: Citation & { source_available: boolean }) {
  if (!citation.source_available) { notesError.value = '来源文档版本已不可用；笔记中的证据快照仍可阅读。'; return }
  notesError.value = ''; readerReturnMode.value = 'notes'
  if (citation.spans?.some(span => span.bbox) && citation.verification?.location_valid) await locateCitation(citation)
  else if (quotePageIndexes(citation).length) await jumpCitationPage(citation, quotePageIndexes(citation)[0], true)
  else if (chunkPageIndexes(citation).length) await jumpCitationPage(citation, chunkPageIndexes(citation)[0])
  else notesError.value = '此引用没有可靠页码或 bbox，无法定位；已保存快照仍可阅读。'
}
async function jumpCitationPage(citation: Citation, pdfPageIndex: number, preciseQuotePage = false) {
  if (!Number.isInteger(pdfPageIndex) || pdfPageIndex < 0) return
  if (preciseQuotePage && !citation.verification?.location_valid) return
  if (!pdf || version.value?.id !== citation.document_version_id) {
    const loadToken = ++readerLoadToken
    selected.value = papers.value.find(p => p.id === citation.paper_id) || null
    activeVersion.value = selected.value?.versions.find(v => v.id === citation.document_version_id) || null
    if (!activeVersion.value) return
    viewMode.value = 'reader'
    const versionId = activeVersion.value.id
    if (!await loadPdfDocument(versionId, loadToken)) return
    const snapshotQuery = citation.parse_snapshot_id ? `&parse_snapshot_id=${encodeURIComponent(citation.parse_snapshot_id)}` : ''
    const loadedBlocks = await api<Block[]>(`/documents/${versionId}/blocks?limit=1000${snapshotQuery}`)
    if (!isCurrentReaderLoad(loadToken)) return
    blocks.value = loadedBlocks
  }
  activeBlock.value = null
  await setPage(pdfPageIndex)
  viewMode.value = 'reader'; sideTab.value = 'qa'
}
watch(scale, () => { if (pdf) void setPage(pageIndex.value) })
watch([libraryCollapsed, readerSharePercent], () => {
  writeWorkspacePreferences(window.localStorage, { libraryCollapsed: libraryCollapsed.value, readerSharePercent: readerSharePercent.value })
})
watch(pageScroll, element => {
  pageResizeObserver?.disconnect()
  lastReaderWidth = 0
  if (!element || typeof ResizeObserver === 'undefined') return
  pageResizeObserver = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width || 0
    if (width > 80 && pdf && !pdfLoading.value && !pdfPageReady.value && (!pdfError.value || pdfError.value.startsWith('PDF 阅读区域尚未'))) void setPage(pageIndex.value)
    else if (lastReaderWidth && Math.abs(width - lastReaderWidth) > 1) void rerenderPageForResize()
    lastReaderWidth = width
  })
  pageResizeObserver.observe(element)
}, { flush: 'post' })
watch([viewMode, selected, activeVersion, pageIndex, selectedPaperIds, question, historyRecord, historyOffset,
  activeBlock, readerReturnMode, comparisonReport, comparisonReportsOffset, noteDetail, notesOffset], persistViewState)
onMounted(async () => { window.addEventListener('popstate', restoreBrowserHistoryEntry); try { candidates.value = await api<Candidate[]>('/manifest'); await refresh(); config.value = await api<ConfigStatus>('/config/status'); await restoreOperation(); await restoreUploadTask(); await restoreViewState() } catch (e) { error.value = String(e) } finally { initializingApp = false; persistViewState() } })
onBeforeUnmount(() => {
  window.removeEventListener('popstate', restoreBrowserHistoryEntry)
  pageResizeObserver?.disconnect()
  ++readerLoadToken; ++renderToken
  void cancelCurrentRender()
  const oldPdf = pdf; pdf = null
  void destroyPdfDocument(oldPdf)
  if (pdfLoadingTask) { const task = pdfLoadingTask; pdfLoadingTask = null; void task.destroy() }
})
</script>

<template>
  <div class="app">
    <header><div><strong>PaperTrail</strong><span>论文原文阅读与证据问答工作台</span></div><nav class="top-actions"><button @click="showReader">阅读与问答</button><button @click="viewMode === 'reader' && readerReturnMode ? returnToCollection() : showHistory()">{{ viewMode === 'reader' && readerReturnMode === 'notes' ? '返回笔记' : viewMode === 'reader' && readerReturnMode === 'reports' ? '返回报告' : '历史问答' }}</button><button @click="showNotes">证据笔记</button><button @click="showComparisonReports">论文比较报告</button><button @click="showEvaluationResults" :aria-current="viewMode === 'evaluation' ? 'page' : undefined">实验结果</button><button class="upload-entry" @click="openUploadDialog"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m0 0L7 9m5-5 5 5M5 14v5h14v-5" /></svg>上传 PDF</button></nav></header>
    <section v-if="operationTask || operationTaskError" class="operation-task-banner" role="status" aria-live="polite">
      <div v-if="operationTask"><strong>{{ operationTask.kind === 'qa' ? '问答任务' : '报告任务' }}：{{ operationTask.status }}</strong>
        <span> · {{ operationPhaseLabels[operationTask.phase] || operationTask.phase }} · 已耗时 {{ operationElapsed(operationTask) }}</span>
      <span v-if="operationTask.status === 'succeeded' && operationTask.result_id"> · 保存记录 {{ operationTask.result_id }}</span>
        <p v-if="operationTask.error" class="muted">{{ describeOperationTaskError(operationTask) }}</p>
        <details v-if="operationTask.error?.details && Object.keys(operationTask.error.details).length"><summary>任务技术诊断</summary><pre>{{ JSON.stringify(operationTask.error.details, null, 2) }}</pre></details>
        <p v-if="operationTask.status === 'outcome_unknown'" class="caveat">请求可能已到达模型服务。系统不会自动重试；先检查历史记录后再决定是否新建任务。</p>
      </div>
      <div v-if="operationTaskError" class="error">{{ operationTaskError }} <button @click="restoreOperation">重新读取任务状态</button></div>
      <button v-if="operationTask && activeTaskStatuses.has(operationTask.status)" @click="cancelActiveOperation">请求停止</button>
      <button v-if="canDismissOperationTask(operationTask)" @click="dismissOperationTask">清除当前展示</button>
    </section>
    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="uploadDialogOpen" class="modal-backdrop" @click.self="closeUploadDialog">
      <section class="upload-dialog" role="dialog" aria-modal="true" aria-labelledby="upload-title" @dragover.prevent @drop="handleUploadDrop">
        <div class="upload-dialog-head"><div><h2 id="upload-title">上传 PDF</h2><p class="muted">用户 PDF 独立登记；完成解析和索引后才可阅读与提问。最大 40 MiB。</p></div><button aria-label="关闭上传窗口" @click="closeUploadDialog">×</button></div>
        <div class="upload-dropzone"><p>拖放 PDF 到此处，或</p><label class="upload">选择本地 PDF<input type="file" accept="application/pdf,.pdf" :disabled="uploadBusy" @change="handleUploadFileChange"></label></div>
        <p v-if="uploadFileSelection" class="upload-file-selection">已选择：<strong>{{ uploadFileSelection.name }}</strong> · {{ (uploadFileSelection.size / 1024 / 1024).toFixed(2) }} MiB · {{ uploadDigestPending ? '正在核对文件内容' : uploadSelectionTask.matchesTask ? uploadSelectionTask.label : uploadFileSelection.size <= 40*1024*1024 && /\.pdf$/i.test(uploadFileSelection.name) ? '文件名和大小通过初检，服务端仍会验证 PDF 内容' : '文件名或大小不符合要求' }}</p>
        <div v-if="uploadBusy" class="upload-live-status" role="status">{{ uploadTask ? `${uploadPhaseLabels[uploadTask.phase] || uploadTask.phase} · ${uploadTaskStatusLabel(uploadTask.status)}` : '正在提交上传请求' }} · 已耗时 {{ uploadTask ? operationElapsed(uploadTask) : `${uploadElapsedSeconds} 秒` }}</div>
        <button :disabled="Boolean(uploadBusy || (uploadMainAction !== 'open_paper' && (!uploadFileSelection || uploadDigestPending || uploadSelectionTask.matchesTask || uploadFileSelection.size > 40*1024*1024 || !/\.pdf$/i.test(uploadFileSelection.name))) )" @click="uploadMainAction === 'open_paper' ? openUploadedPaper() : submitUserPdf()">{{ uploadBusy ? '处理中…' : uploadMainAction === 'open_paper' ? '打开论文' : uploadDigestPending ? '正在校验文件…' : uploadSelectionTask.matchesTask ? '请使用下方任务操作' : '上传并处理' }}</button>
        <p v-if="uploadDeduplicated" class="muted">已识别相同 SHA-256 内容，复用已有论文版本与处理任务。</p>
        <p v-if="uploadOutcomeUnknown && !uploadTask" class="upload-unknown" role="alert">上传响应尚未确认。未重新发送文件；可先重新读取任务状态。</p>
        <article v-if="uploadTask" class="upload-task" :class="`status-${uploadTask.status}`" role="status" aria-live="polite">
          <p v-if="uploadFileSelection && !uploadDigestPending && !uploadSelectionTask.matchesTask" class="upload-other-task-note">下方显示的是另一份 PDF 的任务；新选择的文件会单独上传。</p>
          <header class="upload-task-heading"><div><strong>{{ uploadTask.status === 'succeeded' ? '索引完成' : uploadTask.status === 'failed' ? '处理失败' : uploadTaskStatusLabel(uploadTask.status) }}</strong><span>{{ uploadPhaseLabels[uploadTask.phase] || uploadTask.phase }} · 已耗时 {{ operationElapsed(uploadTask) }}</span></div><div class="upload-task-heading-actions"><button v-if="uploadTask.status === 'succeeded'" type="button" class="upload-secondary" @click="openUploadedPaper">打开论文</button><button v-else-if="['outcome_unknown','failed','interrupted'].includes(uploadTask.status)" type="button" class="upload-secondary" :disabled="uploadBusy" @click="refreshUploadTaskStatus">检查任务状态</button></div></header>
          <p class="muted upload-file-meta">文件：{{ uploadTaskFilename(uploadTask) }} · 文件大小：{{ uploadTaskFileSizeLabel(uploadTask) }}</p>
          <ol class="upload-stage-list" aria-label="PDF 处理阶段">
            <li v-for="(label,index) in uploadStageLabels" :key="label" :class="uploadStageStatus(uploadTask,index)" :aria-current="uploadStageStatus(uploadTask,index) === 'current' ? 'step' : undefined"><span class="upload-stage-dot" aria-hidden="true"></span>{{ label }}<span v-if="uploadStageStatus(uploadTask,index) === 'done'" class="upload-stage-complete">已完成</span></li>
          </ol>
          <p v-if="uploadVectors" class="upload-vector-count">已持久化 {{ uploadVectors.completed }} / {{ uploadVectors.total }} 个向量<span v-if="uploadVectors.missing"> · 尚缺 {{ uploadVectors.missing }} 个</span></p>
          <div v-if="uploadTask.status === 'outcome_unknown'" class="upload-unknown" role="alert"><strong>索引请求结果未知</strong><p>{{ uploadTaskErrorMessage(uploadTask) }}</p><small>{{ uploadTaskDiagnostic(uploadTask) }}。已有向量会复用；未知且仍缺失的向量只有在明确确认后才会重发。</small></div>
          <div v-else-if="uploadTask.error" class="upload-task-error" role="alert"><strong>{{ uploadTaskErrorMessage(uploadTask) }}</strong><small>{{ uploadTaskDiagnostic(uploadTask) }}</small></div>
          <details v-if="uploadTask.error?.details"><summary>查看安全诊断</summary><pre>{{ JSON.stringify(uploadTask.error.details, null, 2) }}</pre></details>
          <button v-if="uploadTaskRetry(uploadTask).canRetry" class="upload-retry" :disabled="uploadBusy" @click="retryUserUploadTask">{{ uploadTaskRetry(uploadTask).retryLabel }}</button>
          <p v-if="uploadTask.status === 'succeeded'" class="upload-ready-note">论文已完成向量和索引校验，可在左侧论文库中阅读与提问。</p>
        </article>
        <p v-if="uploadError" class="error" role="alert">{{ uploadError }}</p>
        <p v-if="uploadTask && ['succeeded','failed','interrupted','cancelled'].includes(uploadTask.status)" class="muted">关闭窗口不会删除任务记录；之后可重新打开查看状态。</p>
      </section>
    </div>
    <main ref="workspaceMain" :class="{ 'library-collapsed': libraryCollapsed, 'reader-layout': !!(selected && version && viewMode === 'reader') }" :style="{ '--reader-track': `minmax(0, ${readerSharePercent}fr)`, '--answer-track': `minmax(0, ${100 - readerSharePercent}fr)` }">
      <aside class="library" :class="{ collapsed: libraryCollapsed }" :aria-label="libraryCollapsed ? '论文库已收起' : '论文库'">
        <div class="library-head"><h2 v-show="!libraryCollapsed">论文库</h2><button class="library-toggle" type="button" :aria-label="libraryCollapsed ? '展开论文列表' : '收起论文列表'" :aria-expanded="!libraryCollapsed" :title="libraryCollapsed ? '展开论文列表' : '收起论文列表'" @click="toggleLibrary"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="libraryCollapsed ? 'M9 5l7 7-7 7' : 'M15 5l-7 7 7 7'" /></svg></button></div>
        <div v-show="!libraryCollapsed" class="library-content">
        <p class="muted">固定论文来自 manifest；用户上传的论文独立登记，不进入固定评测集。</p>
        <article v-for="c in paperRows" :key="c.paper_id" :class="{ chosen: selected?.id === c.paper_id }">
          <label class="scope"><input type="checkbox" :checked="selectedPaperIds.includes(c.paper_id)" :disabled="!papers.find(p => p.id === c.paper_id)?.versions.some(v => ['ready','quality_warning'].includes(v.status)) || selectedPaperIds.length >= 3 && !selectedPaperIds.includes(c.paper_id)" @change="togglePaper(c.paper_id)"> 纳入提问</label>
          <button class="paper-title" @click="openPaper(c.paper_id)">{{ c.title }}</button>
          <div class="meta">{{ c.source === 'user_upload' ? `用户上传 · ${papers.find(p=>p.id===c.paper_id)?.original_filename || 'PDF'}` : '固定论文 · manifest' }} · {{ c.source_version }}</div>
          <div class="state">{{ papers.find(p => p.id === c.paper_id)?.versions.find(v => ['ready','quality_warning'].includes(v.status))?.status || papers.find(p=>p.id===c.paper_id)?.status || '未导入' }}</div>
          <button v-if="c.source !== 'user_upload' && !papers.some(p => p.id === c.paper_id)" @click="importPaper(c.paper_id)" :disabled="busy">导入固定 PDF</button>
        </article>
        </div>
      </aside>
      <section class="reader" v-if="selected && version && viewMode === 'reader'">
        <div class="reader-head"><button v-if="readerReturnMode" @click="returnToCollection">返回{{ readerReturnMode === 'notes' ? '笔记' : readerReturnMode === 'reports' ? '报告' : '历史列表' }}</button><h2>{{ selected.title }}</h2><div class="meta">{{ selected.source === 'user_upload' ? `用户上传 · ${selected.original_filename || 'PDF'} · ${version.source_version}` : `固定论文 · arXiv ${selected.arxiv_id} · ${version.source_version}` }}</div><div class="meta">{{ version.status }} · {{ version.page_count }} 页 · {{ version.block_count }} 块</div></div>
        <div class="toolbar"><button @click="setPage(pageIndex - 1)" :disabled="pageIndex === 0">上一页</button><span>PDF 第 {{ pageIndex + 1 }} / {{ version.page_count }} 页</span><button @click="setPage(pageIndex + 1)" :disabled="pageIndex >= version.page_count - 1">下一页</button><label>缩放 <select v-model.number="scale"><option :value="1">适合宽度</option><option :value="1.35">适合宽度 · 1.35×</option><option :value="1.7">适合宽度 · 1.7×</option></select></label></div>
        <div ref="pageScroll" class="page-scroll" @scroll.passive="persistViewState" :aria-busy="pdfLoading || !pdfPageReady">
          <div class="page" :class="{ 'page-pending': pdfLoading || !pdfPageReady }"><canvas ref="canvas"></canvas><div ref="overlay" class="overlay"></div></div>
          <div v-if="pdfLoading" class="pdf-reader-state" role="status" aria-live="polite">正在加载 PDF…</div>
          <div v-else-if="pdfError" class="pdf-reader-state pdf-reader-error" role="alert"><strong>PDF 暂时无法显示</strong><span>{{ pdfError }}</span><button type="button" @click="reloadPdf">重新加载 PDF</button></div>
          <div v-else-if="!pdfPageReady" class="pdf-reader-state" role="status" aria-live="polite">正在渲染第 {{ pageIndex + 1 }} 页…</div>
        </div>
      </section>
      <div v-if="selected && version && viewMode === 'reader'" class="pane-splitter" role="separator" tabindex="0" aria-orientation="vertical" aria-valuemin="30" aria-valuemax="70" :aria-valuenow="readerSharePercent" :aria-valuetext="`PDF ${readerSharePercent}%，答案 ${100 - readerSharePercent}%`" aria-label="调整 PDF 与答案区域宽度，方向键调整，双击恢复 40 比 60" title="拖动调整阅读/答案宽度 · 双击恢复 40:60" @pointerdown="startSplitterResize" @pointermove="moveSplitterResize" @keydown="handleSplitterKeydown" @dblclick="resetReaderShare"><span aria-hidden="true"></span></div>
      <section ref="historyList" class="history-page" v-if="viewMode === 'history'">
        <div class="history-head"><div><h1>历史问答</h1><p class="muted">读取已保存记录；打开记录不会重新检索或调用模型。</p></div></div>
        <div v-if="historyError" class="error">{{ historyError }}</div><div v-if="historyLoading" class="muted">正在读取历史记录…</div>
        <button v-for="item in historyItems" :key="item.run_id" class="history-item" @click="selectHistory(item)"><strong>{{ item.query }}</strong><span>{{ item.created_at }} · {{ item.paper_ids.join('、') }} · {{ item.status }}<b v-if="isMockHistory(item)" class="mock-tag">自动化测试记录</b></span><small>{{ item.run_id }}</small></button>
        <p v-if="!historyLoading && !historyItems.length && !historyError" class="muted">暂无已保存问答记录。</p>
        <div class="history-pagination"><button :disabled="historyOffset <= 0 || historyLoading" @click="loadHistory(Math.max(0, historyOffset - historyLimit))">上一页</button><span>{{ historyTotal ? `${historyOffset + 1}–${Math.min(historyOffset + historyLimit, historyTotal)} / ${historyTotal}` : '0 条' }}</span><button :disabled="historyOffset + historyLimit >= historyTotal || historyLoading" @click="loadHistory(historyOffset + historyLimit)">下一页</button></div>
        <section class="task-history"><h2>失败与中断的任务</h2><p class="muted">任务记录与已保存问答分开保存。失败任务可能没有可打开的 QA 答案；查看这里不会重跑任务或调用模型。</p><div v-if="taskHistoryError" class="error">{{ taskHistoryError }}</div><p v-if="taskHistoryLoading" class="muted">正在读取任务记录…</p><article v-for="task in taskHistoryItems" :key="task.task_id" class="history-item task-history-item"><strong>{{ task.request_summary?.query || task.request_summary?.title || (task.kind === 'qa' ? '问答任务' : '报告任务') }}</strong><span>{{ task.kind === 'qa' ? '问答任务' : '报告任务' }} · {{ task.status }} · {{ operationPhaseLabels[task.phase] || task.phase }} · 已耗时 {{ operationElapsed(task) }}</span><small>{{ task.task_id }} · {{ task.status === 'succeeded' && task.result_id ? task.result_id : '无已保存结果' }}</small><p v-if="task.error">{{ describeOperationTaskError(task) }}</p><details v-if="task.error?.details && Object.keys(task.error.details).length"><summary>任务技术诊断</summary><pre>{{ JSON.stringify(task.error.details, null, 2) }}</pre></details></article><p v-if="!taskHistoryLoading && !taskHistoryError && !taskHistoryItems.length" class="muted">没有近期失败或中断任务。</p></section>
      </section>
      <section class="notes-page" v-else-if="viewMode === 'notes'">
        <div class="notes-head"><div><h1>证据笔记</h1><p class="muted">笔记正文由你撰写；论文、claim 与引用仅作为可选来源快照。</p></div><div class="note-actions"><button @click="startStandaloneNoteDraft">新建笔记</button><button v-if="qaResult && !qaResult.fixture_label && !qaResult.controlled_test && qaResult.run_id.startsWith('qrun_')" @click="startNoteDraft">从当前问答新建</button></div></div>
        <div v-if="notesError" class="error note-error">{{ notesError }}</div>
        <div v-if="notesLoading" class="muted">正在读取笔记…</div>
        <button v-for="item in notes" :key="item.note_id" class="note-item" :class="{active: noteDetail?.note_id === item.note_id}" @click="openNote(item)"><strong>{{ item.title }}</strong><span>关联论文：{{ item.paper_ids?.join('、') || '无' }} · QA：{{ item.source_qa_run_id || '未关联' }}</span><small>更新于 {{ item.updated_at }}</small></button>
        <p v-if="!notesLoading && !notes.length && !notesError" class="muted">暂无笔记。可以直接新建，也可以从已保存问答中选择来源。</p>
        <div class="history-pagination"><button :disabled="notesOffset <= 0 || notesLoading" @click="loadNotes(Math.max(0, notesOffset - noteLimit))">上一页</button><span>{{ notesTotal ? `${notesOffset + 1}–${Math.min(notesOffset + noteLimit, notesTotal)} / ${notesTotal}` : '0 条' }}</span><button :disabled="notesOffset + noteLimit >= notesTotal || notesLoading" @click="loadNotes(notesOffset + noteLimit)">下一页</button></div>
        <div v-if="noteCompose" class="note-editor"><h2>新建证据笔记</h2><label>标题<input v-model="noteTitle" maxlength="240" placeholder="填写笔记标题"></label><label>我的理解<textarea v-model="noteBody" rows="8" placeholder="写下自己的理解；不会修改原问答或论文引用"></textarea></label><p class="muted">{{ noteSource ? `来源问题快照：${noteSource.query} · ${noteSource.run_id}` : '未关联问答；可以只保存个人笔记。' }}</p><div class="note-paper-select"><strong>关联论文（可选）</strong><label v-for="paper in papers" :key="paper.id"><input type="checkbox" :value="paper.id" v-model="notePaperIds"> {{ paper.title }}</label></div><div class="note-actions"><button @click="saveNote" :disabled="noteSaving || !noteTitle.trim()">{{ noteSaving ? '保存中…' : '保存笔记' }}</button><button @click="cancelNoteEdit">取消</button><span v-if="noteSaving" class="muted">正在保存…</span></div><p v-if="noteEditorDirty" class="muted">有未保存修改</p></div>
      </section>
      <section class="reports-page" v-else-if="viewMode === 'reports'">
        <div class="notes-head"><div><h1>跨论文方法对比</h1><p class="muted">固定版本、逐篇证据检索；空白单元格表示证据不足，不代表论文未报告。</p></div><button v-if="comparisonReport" @click="comparisonReport=null">新建报告</button></div>
        <div v-if="comparisonReportsError" class="error note-error">{{ comparisonReportsError }}</div>
        <template v-if="!comparisonReport">
          <div class="report-form"><label>报告标题<input v-model="comparisonTitle" maxlength="200"></label><label>对比维度（每行一项）<textarea v-model="comparisonDimensionsText" rows="5"></textarea></label><h3>选择 2–3 篇论文及固定版本</h3>
            <article v-for="candidate in paperRows" :key="candidate.paper_id" class="report-paper-choice"><label><input type="checkbox" :checked="comparisonPaperIds.includes(candidate.paper_id)" :disabled="!papers.find(p=>p.id===candidate.paper_id)?.versions.some(v=>['ready','quality_warning'].includes(v.status))||(!comparisonPaperIds.includes(candidate.paper_id)&&comparisonPaperIds.length>=3)" @change="toggleComparisonPaper(candidate.paper_id)"> {{ candidate.title }} · {{ candidate.source === 'user_upload' ? '用户上传' : '固定论文' }}</label><select v-if="comparisonPaperIds.includes(candidate.paper_id)" v-model="comparisonVersionIds[comparisonPaperIds.indexOf(candidate.paper_id)]"><option value="">选择已就绪版本</option><option v-for="v in (papers.find(p=>p.id===candidate.paper_id)?.versions||[]).filter(v=>['ready','quality_warning'].includes(v.status))" :key="v.id" :value="v.id">{{v.source_version}}</option></select></article>
            <h3>可选的用户笔记补充材料</h3><p class="muted">不会作为论文原文或证据发送给生成器/核验器。</p><label v-for="note in notes" :key="note.note_id" class="report-note-choice"><input type="checkbox" :value="note.note_id" v-model="comparisonNoteIds"> {{note.title}} · {{note.source_qa_run_id}}</label>
            <button class="save-note-button" :disabled="comparisonCreating||operationSubmitting||operationActive||comparisonPaperIds.length<2||comparisonVersionIds.some(id=>!id)" @click="createComparisonReport">{{comparisonCreating||(operationTask?.kind === 'comparison_report' && operationActive)?'报告任务执行中…':operationActive?'其他后台任务执行中…':'生成并保存报告'}}</button>
          </div><h2>已保存报告</h2><p v-if="comparisonReportsLoading" class="muted">正在读取…</p><button v-for="item in comparisonReports" :key="item.report_id" class="history-item" @click="openComparisonReport(item)"><strong>{{item.title}}</strong><span>{{item.created_at}} · {{item.paper_ids.join('、')}}</span><small>{{item.report_id}}</small></button><p v-if="!comparisonReportsLoading&&!comparisonReports.length" class="muted">暂无已保存报告。</p><div class="history-pagination"><button :disabled="comparisonReportsOffset<=0||comparisonReportsLoading" @click="loadComparisonReports(Math.max(0,comparisonReportsOffset-20))">上一页</button><span>{{comparisonReportsTotal?`${comparisonReportsOffset+1}–${Math.min(comparisonReportsOffset+20,comparisonReportsTotal)} / ${comparisonReportsTotal}`:'0 条'}}</span><button :disabled="comparisonReportsOffset+20>=comparisonReportsTotal||comparisonReportsLoading" @click="loadComparisonReports(comparisonReportsOffset+20)">下一页</button></div>
        </template>
        <template v-else><div class="report-toolbar"><h2>{{comparisonReport.title}}</h2><button @click="exportComparisonReport">导出 Markdown</button></div><p class="muted">{{comparisonReport.report_id}} · {{comparisonReport.revision_id ? `修订 ${comparisonReport.revision_number} · ${comparisonReport.revision_created_at}` : comparisonReport.created_at}} · {{comparisonReport.chat_model}} · {{comparisonReport.embedding_model}}</p><p class="caveat">支持状态是模型辅助核验，不是人工确认。报告覆盖独立显示；证据不足不等于论文未报告。查看或编辑修订不会调用模型。</p>
          <div class="report-revision-toolbar"><button :disabled="!comparisonReport.revision_id" @click="openOriginalReport">原始版本</button><button v-for="revision in comparisonReportRevisions" :key="revision.revision_id" @click="openReportRevision(revision.revision_id)">修订 {{revision.revision_number}} · {{revision.created_at}}</button></div>
          <p v-if="comparisonReport.revision_id" class="caveat">当前为修订版本。修改内容显示“已编辑，未重新核验”；原版核验状态仅在原始版本查看。</p>
          <div v-if="comparisonReport.changes?.length" class="report-revision-changes"><h3>本次修改说明</h3><article v-for="change in comparisonReport.changes" :key="change.claim_id"><strong>{{change.claim_id}}</strong><p>{{change.reason}}</p><small>原版核验：{{supportLabel(change.original_support_status)}} · 修订：已编辑，未重新核验</small></article></div>
          <div v-for="dimension in comparisonReport.report_data.dimensions" :key="dimension.name" class="report-dimension"><h3>{{dimension.name}}</h3><table><thead><tr><th>论文</th><th>结论与证据支持</th><th>覆盖</th></tr></thead><tbody><tr v-for="cell in dimension.cells" :key="cell.paper_id"><th>{{comparisonReport.report_data.paper_snapshots.find(p=>p.paper_id===cell.paper_id)?.title}}</th><td><span v-if="!cell.claims.length">证据不足</span><p v-for="claim in cell.claims" :key="claim.claim_id" class="report-claim">{{claim.text}}<small v-if="claim.revision_edit">已编辑，未重新核验 · 修改原因：{{claim.revision_edit.reason}} · 原版状态：{{supportLabel(claim.revision_edit.original_support_status)}}</small><small v-else>核验：{{supportLabel(claim.support_status)}} · {{displaySupportReason(claim.support_reason)}}</small><button v-if="!comparisonReport.revision_id" class="citation-link" @click="beginReportEdit(claim)">编辑此结论</button><button v-for="cite in claim.citations" :key="cite.evidence_id" class="citation-link" @click="openReportCitation(cite)">{{citationButtonLabel(cite)}} · {{cite.title}} · {{quotePageIndexes(cite).length?'引句原文页 '+quotePageIndexes(cite).map(i=>i+1).join(', '):chunkPageIndexes(cite).length?'仅分块覆盖页 '+chunkPageIndexes(cite).map(i=>i+1).join(', ')+'（非精确引用位置）':'暂无法精确定位'}}</button><div v-if="reportEditingClaim?.claim_id===claim.claim_id" class="report-edit-form"><label>修订内容<textarea v-model="reportEditText" rows="4"></textarea></label><label>修改原因<textarea v-model="reportEditReason" rows="3" placeholder="说明依据；引文会沿用原报告的固定版本快照"></textarea></label><button :disabled="reportSavingEdit" @click="saveReportEdit">{{reportSavingEdit?'保存中…':'保存为新修订'}}</button><button :disabled="reportSavingEdit" @click="reportEditingClaim=null">取消</button></div></p></td><td>{{cell.coverage_status==='has_cited_content'?'有引用内容':'证据不足'}}</td></tr></tbody></table><div v-if="dimension.comparisons.length"><h4>跨论文结论</h4><p v-for="claim in dimension.comparisons" :key="claim.claim_id" class="report-claim">{{claim.text}}<small v-if="claim.revision_edit">已编辑，未重新核验 · 修改原因：{{claim.revision_edit.reason}} · 原版状态：{{supportLabel(claim.revision_edit.original_support_status)}}</small><small v-else>核验：{{supportLabel(claim.support_status)}} · {{displaySupportReason(claim.support_reason)}}</small><button v-if="!comparisonReport.revision_id" class="citation-link" @click="beginReportEdit(claim)">编辑此结论</button><button v-for="cite in claim.citations" :key="cite.evidence_id" class="citation-link" @click="openReportCitation(cite)">{{citationButtonLabel(cite)}} · {{cite.title}}</button><div v-if="reportEditingClaim?.claim_id===claim.claim_id" class="report-edit-form"><label>修订内容<textarea v-model="reportEditText" rows="4"></textarea></label><label>修改原因<textarea v-model="reportEditReason" rows="3"></textarea></label><button :disabled="reportSavingEdit" @click="saveReportEdit">{{reportSavingEdit?'保存中…':'保存为新修订'}}</button><button @click="reportEditingClaim=null">取消</button></div></p></div></div>
          <h3>补充材料</h3><p v-if="!comparisonReport.report_data.supplemental_notes.length" class="muted">未附用户笔记。</p><article v-for="note in comparisonReport.report_data.supplemental_notes" :key="note.note_id" class="note-snapshot"><strong>{{note.title}} · {{note.label}}</strong><p>{{note.body}}</p></article>
        </template>
      </section>
      <EvaluationResults v-else-if="viewMode === 'evaluation'" />
      <section class="empty" v-else-if="viewMode === 'reader' && !(selected && version)"><h1>开始阅读与问答</h1><p>选择左侧已就绪论文并点击标题打开原文；勾选“纳入提问”设置检索范围。新建问答始终从空白问题开始，历史结果保存在历史问答中。</p></section>
      <aside class="history-detail" v-if="viewMode === 'history' && historyRecord"><h2>历史记录详情</h2><p>{{ historyRecord.query }}</p><div class="muted">{{ historyRecord.run_id }} · {{ historyRecord.created_at || '创建时间缺失' }}</div><h3>保存的最终答案</h3><pre>{{ historyDisplayAnswer }}</pre><p class="muted" :title="SUPPORT_EXPLANATION">答案完整性：{{ completenessLabel(historyRecord) }}；证据支持：{{ historyRecord.support_trace?.enabled ? '逐条状态见记录' : '未核实' }}</p><p v-if="historyError" class="error">{{ historyError }}</p><div v-for="citation in historyRecord.citations" :key="citation.evidence_id" class="evidence-card"><strong>{{ citation.title }}</strong><blockquote>{{ citation.exact_quote }}</blockquote><p class="muted">引用原文页：{{ quotePageIndexes(citation).length ? [...new Set(quotePageIndexes(citation).map(i => i + 1))].sort((a,b) => a-b).join(', ') : '暂无法精确定位' }}；分块覆盖页：{{ chunkPageIndexes(citation).length ? [...new Set(chunkPageIndexes(citation).map(i => i + 1))].sort((a,b) => a-b).join(', ') : '未记录' }}</p><button class="citation-link source-open" :disabled="!citationCanOpen(citation)" @click="openCitationSource(citation)">{{ citationButtonLabel(citation) }}</button><template v-for="(span, index) in citation.spans" :key="span.block_id"><button v-if="span.bbox && citation.verification?.location_valid" @click="locateCitation(citation, index)">定位原文块 · PDF 第 {{ span.pdf_page_index + 1 }} 页（块级高亮）</button></template><p v-if="citationPageTargets(citation).kind === 'source'" class="muted location-limit">来源页可用，但无可靠引句级定位。</p><p v-else-if="citationPageTargets(citation).kind === 'chunk'" class="muted location-limit">仅能跳至分块覆盖页，非精确引用位置。</p></div></aside>
      <aside class="note-detail" v-if="viewMode === 'notes' && noteCompose && noteSource && qaResult">
        <h2>选择要保留的来源</h2><p class="muted">默认不选中任何内容。核验状态按来源记录保存，不能通过保存笔记变成通过。</p>
        <h3>Claims</h3><label v-for="claim in qaResult.claims" :key="claim.claim_id" class="note-check"><input type="checkbox" :value="claim.claim_id" v-model="noteSelectedClaims"><span><strong>{{ claim.text }}</strong><small>{{ claim.claim_id }} · {{ supportLabel(claim.support_status) }}</small></span></label>
        <h3>引用</h3><label v-for="citation in qaResult.citations" :key="citation.evidence_id" class="note-check"><input type="checkbox" :value="citation.evidence_id" v-model="noteSelectedEvidence"><span><strong>{{ citation.title }}</strong><small>{{ citation.source_version }} · {{ citation.verification?.support_status ? supportLabel(citation.verification.support_status) : '未核实' }}</small><small>“{{ citation.exact_quote }}”</small></span></label>
      </aside>
      <aside class="note-detail" v-if="viewMode === 'notes' && noteCompose && !noteSource"><h2>来源</h2><p class="muted">此笔记不关联 QA、claim 或论文引用。可在左侧选择要关联的论文。</p></aside>
      <aside class="note-detail" v-if="viewMode === 'notes' && noteDetail && !noteCompose">
        <div class="note-actions"><button @click="openNoteSource" :disabled="!noteDetail.source_qa_available">打开来源问答</button><button @click="exportNote">导出 Markdown</button><button v-if="!noteEditing" @click="editNote">编辑笔记</button></div>
        <p v-if="noteDetail.source_qa_run_id && !noteDetail.source_qa_available" class="note-warning">来源问答已不可用；以下快照仍可阅读。</p>
        <p v-else-if="!noteDetail.source_qa_run_id" class="muted">此笔记没有关联来源问答；用户正文独立保存。</p>
        <template v-if="noteEditing"><label>标题<input v-model="noteTitle" maxlength="240"></label><label>我的理解<textarea v-model="noteBody" rows="9"></textarea></label><div class="note-paper-select"><strong>关联论文（可选）</strong><label v-for="paper in papers" :key="paper.id"><input type="checkbox" :value="paper.id" v-model="notePaperIds"> {{ paper.title }}</label></div><p v-if="noteEditorDirty" class="muted">有未保存修改</p><div class="note-actions"><button @click="saveNote" :disabled="noteSaving || !noteTitle.trim()">{{ noteSaving ? '保存中…' : '保存修改' }}</button><button @click="cancelNoteEdit">取消</button><button class="danger-button" @click="deleteNote" :disabled="noteDeleting">{{ noteDeleting ? '删除中…' : '删除笔记' }}</button></div></template>
        <template v-else><h2>{{ noteDetail.title }}</h2><div class="note-user-body"><h3>我的笔记</h3><p>{{ noteDetail.body || '（空）' }}</p></div><div class="muted">关联论文：{{ noteDetail.paper_ids?.join('、') || '无' }}<br>来源问题：{{ noteDetail.source_query_snapshot || '未关联' }}<br>来源 QA run：{{ noteDetail.source_qa_run_id || '未关联' }}</div>
          <h3>保存的 claims（原始快照）</h3><article v-for="claim in noteDetail.claims" :key="claim.claim_id" class="note-snapshot"><strong>{{ claim.text }}</strong><p>Claim ID：{{ claim.claim_id }} · 保存时状态：{{ supportLabel(claim.support_status) }}（{{ claim.support_verification || '未核验' }}）</p><blockquote v-if="claim.quote">{{ claim.quote }}</blockquote></article>
          <h3>保存的论文原文与位置快照</h3><article v-for="citation in noteDetail.citations" :key="citation.evidence_id" class="note-snapshot"><strong>{{ citation.title }} · {{ citation.source_version }}</strong><p>版本 ID：{{ citation.document_version_id }} · SHA-256 {{ citation.sha256 }}</p><blockquote>{{ citation.exact_quote }}</blockquote><p>保存时证据状态：{{ supportLabel(citation.verification?.support_status) }}<br>引用原文页：{{ quotePageIndexes(citation).length ? [...new Set(quotePageIndexes(citation).map(i => i + 1))].sort((a,b) => a-b).join(', ') : '未记录 / 无可靠位置' }}<br>分块覆盖页：{{ chunkPageIndexes(citation).length ? [...new Set(chunkPageIndexes(citation).map(i => i + 1))].sort((a,b) => a-b).join(', ') : '未记录' }}<br>定位说明：{{ citation.verification?.location_valid && citation.spans.some(span => span.bbox) ? '原文块 bbox 范围，非引句精确矩形' : quotePageIndexes(citation).length ? '仅有引用原文页码，无 bbox' : '暂无法精确定位；分块覆盖页不代表引句位置' }}</p><p v-if="!citation.source_available" class="note-warning">来源文档版本已不可用；证据快照仍可阅读。</p><button @click="openNoteCitation(citation)" :disabled="!citation.source_available || (!citation.spans.length && !quotePageIndexes(citation).length && !chunkPageIndexes(citation).length)">打开该文档版本的引用位置</button></article>
        </template>
      </aside>
      <aside class="blocks" v-if="selected && version && viewMode === 'reader'">
        <nav class="tabs"><button :class="{ active: sideTab === 'qa' }" @click="sideTab = 'qa'">问答与证据</button><button :class="{ active: sideTab === 'blocks' }" @click="sideTab = 'blocks'">解析块 {{ blocks.length }}</button></nav>
        <section v-if="sideTab === 'qa'" class="qa-panel">
          <div v-if="!historyRecord" class="provider-status"><span>检索向量：{{ config?.embedding_model || '读取中' }}</span><span :class="config?.chat_ready ? 'ready' : 'missing'">{{ config?.chat_ready ? `回答模型：${config.chat_model}` : '未配置问答模型' }}</span></div>
          <p v-if="!historyRecord" class="muted qa-guidance">问题只搜索所选论文的固定版本；答案、引用位置和语义支持状态分别展示。</p>
          <div class="question-head"><span class="question-label">研究问题</span><button @click="startNewQa">新建问答</button></div>
          <article v-if="!questionEditing && historyRecord" class="question-readonly"><p>{{ historyRecord.query || question || '（问题内容未保存）' }}</p><button type="button" @click="questionEditing = true">编辑问题 / 重新提问</button></article>
          <template v-else>
          <label class="sr-only" for="question">研究问题</label><textarea id="question" v-model="question" rows="4" placeholder="输入你想向论文提出的问题……"></textarea>
          <div class="scope-note">检索范围：{{ selectedPaperIds.length ? selectedPaperIds.map(paperLabel).join('、') : '请在左侧勾选至少一篇已就绪论文' }}</div>
          <label v-if="version" class="snapshot-mode">解析方式
            <select v-model="parseSnapshotChoice" :disabled="snapshotOptionsLoading || !snapshotOptions.length" :title="parseSnapshotChoice ? `${snapshotOptions.find(option => option.parse_snapshot_id === parseSnapshotChoice)?.parser_identity || '实验解析'} · ${parseSnapshotChoice}` : '使用默认 Legacy 解析与索引'">
              <option value="">默认解析（legacy）</option>
              <option v-for="option in snapshotOptions" :key="option.parse_snapshot_id" :value="option.parse_snapshot_id">
                实验解析（Snapshot）
              </option>
            </select>
            <small v-if="snapshotOptionsLoading">正在检查当前 PDF 的实验索引…</small>
            <small v-else-if="!snapshotOptions.length">当前 PDF 没有与已配置 embedding 模型匹配的 ready 实验索引。</small>
            <small v-else>默认始终使用旧索引；只有在此处明确选择快照才进入实验检索。实验模式要求仅选择当前阅读论文。</small>
          </label>
          <p v-if="parseSnapshotChoice && (selectedPaperIds.length !== 1 || selectedPaperIds[0] !== selected?.id)" class="caveat">实验解析只接受当前阅读论文的单论文范围，请调整“纳入提问”范围或切回默认解析。</p>
          <button class="ask-button" :disabled="asking || operationSubmitting || operationActive || !selectedPaperIds.length || !question.trim()" @click="ask">{{ asking || (operationTask?.kind === 'qa' && operationActive) ? '问答任务执行中…' : operationActive ? '其他后台任务执行中…' : '检索证据并回答' }}</button>
          </template>
          <div v-if="qaError" class="error qa-error">{{ qaError }}</div>
          <div v-if="qaFailure" class="error qa-error" role="alert"><p>{{ qaFailure.message }}</p><button v-if="qaFailure.retryable" :disabled="asking" @click="ask">重试</button><details><summary>技术详情</summary><code>{{ qaFailure.code }} · {{ qaFailure.reason }}<template v-if="qaFailure.phase"> · {{ qaFailure.phase }}</template><template v-if="qaFailure.endpointHost"> · {{ qaFailure.endpointHost }}</template><template v-if="qaFailure.elapsedMs !== undefined"> · {{ qaFailure.elapsedMs }} ms</template><template v-if="qaFailure.exceptionChain"> · {{ qaFailure.exceptionChain }}</template><template v-if="qaFailure.status"> · HTTP {{ qaFailure.status }}</template></code><pre v-if="qaFailure.diagnostic">{{ JSON.stringify(qaFailure.diagnostic, null, 2) }}</pre></details></div>
          <div v-if="!qaResult && !searchEvidence.length && !qaError && !qaFailure && !operationActive" class="qa-empty">选择论文并输入研究问题，开始证据驱动问答。</div>
          <div v-if="searchEvidence.length && !qaResult" class="answer-card"><div class="answer-meta">检索证据 · local/vector + BM25 + RRF · 未生成模型回答</div>
            <div v-for="citation in searchEvidence" :key="citation.evidence_id" class="evidence-card"><div class="evidence-title">{{ citation.title }} · {{ citation.source_version }} · {{ citation.section_path.join(' / ') || '章节未识别' }}</div><blockquote>{{ citation.text }}</blockquote><div class="evidence-meta">{{ citation.sha256.slice(0, 12) }}… · RRF {{ citation.retrieval?.rrf_score?.toFixed(4) || '未记录' }}</div><button v-for="(span, index) in citation.spans" :key="span.block_id" @click="locateCitation(citation, index)">定位 PDF 第 {{ span.pdf_page_index + 1 }} 页区域 {{ index + 1 }}</button></div>
          </div>
          <div v-if="qaResult" class="answer-card">
            <div class="answer-meta">{{ qaResult.status }} · {{ qaResult.chat_model }} · {{ qaResult.embedding_model }} · {{ qaResult.parse_snapshot_ids?.some(Boolean) ? '显式实验解析快照' : '默认旧解析' }}<span v-if="qaResult.created_at"> · {{ qaResult.created_at }}</span></div>
            <div class="qa-status-grid"><div><small>答案完整性</small><strong>{{ completenessLabel(qaResult) }}</strong></div><div :title="SUPPORT_EXPLANATION"><small>证据支持 · ⓘ</small><strong>{{ evidenceSupportLabel(qaResult) }}</strong></div></div>
            <div class="answer markdown-answer" v-html="answerHtml"></div>
            <button v-if="qaResult.run_id.startsWith('qrun_') && !qaResult.fixture_label && !qaResult.controlled_test" class="save-note-button" @click="startNoteDraft">保存笔记</button>
            <div v-if="qaResult.baseline_answer && qaResult.baseline_answer !== qaResult.answer" class="revision-box"><button @click="showBaseline = !showBaseline">{{ showBaseline ? '隐藏初始答案' : '查看初始答案' }}</button><pre v-if="showBaseline">{{ displayBaselineAnswer }}</pre><div class="muted">当前展示最终答案</div></div>
            <article v-for="claim in qaResult.claims" :key="claim.text + claim.evidence_ids.join()" class="claim"><strong>{{ claim.text }}</strong><div v-if="claim.quote" class="claim-quote">“{{ claim.quote }}”</div><div :class="['claim-meta', claimSupportTone(claim)]" :title="SUPPORT_EXPLANATION">{{ claim.quote_valid ? '引句是原文子串' : '引句未验证' }} · {{ claimSupportLabel(claim) }}</div><div v-if="displaySupportReason(claim.support_reason)" class="claim-meta">核验理由：{{ displaySupportReason(claim.support_reason) }}</div>
              <button v-for="source in claimSourceItems(claim, qaResult!.citations)" :key="source.evidenceId" class="citation-link" :disabled="!source.citation || !citationCanOpen(source.citation)" :title="citationTooltip(source.evidenceId)" @click="openCitationSource(source.citation || undefined)">{{ citationButtonLabel(source.citation || undefined) }}</button>
              <span v-if="!claim.evidence_ids.length" class="muted">未关联可用来源</span>
            </article>
            <details v-if="qaResult.support_trace"><summary>补检索与核验过程</summary><div class="trace-panel"><div>受控测试：{{ qaResult.controlled_test ? '是' : '否 / 未标记' }}</div><div>补检索次数：{{ qaResult.support_trace.repair_retrievals ?? '未记录' }} · 修订调用：{{ qaResult.support_trace.revision_calls ?? '未记录' }} · 核验调用：{{ qaResult.support_trace.verification_calls ?? '未记录' }}</div><div>模型调用：{{ qaResult.support_trace.model_calls ?? '未记录' }} · 总耗时：{{ qaResult.support_trace.total_elapsed_ms ?? '未记录' }} ms · 核验耗时：{{ qaResult.support_trace.support_elapsed_ms ?? '未记录' }} ms</div><div v-for="(query, index) in qaResult.support_trace.repair_queries || []" :key="index">补检索查询：{{ query }}</div><div v-for="(reason, index) in qaResult.support_trace.repair_reasons || []" :key="index">补检索原因：{{ reason }}</div><div>新增块：{{ (qaResult.support_trace.added_chunk_ids || []).join('、') || '无记录' }}</div><div v-for="(event, index) in qaResult.support_trace.events || []" :key="index">事件 {{ (event as any).step || (event as any).type || '已记录步骤' }} · 耗时：{{ (event as any).elapsed_ms ?? '未记录' }} ms</div><pre v-if="qaResult.support_trace.verification_results">核验各轮结果：{{ formatTrace(qaResult.support_trace.verification_results) }}</pre></div></details>
            <div v-for="citation in qaResult.citations" :key="citation.evidence_id" class="evidence-card"><div class="evidence-title">{{ citation.title }} · {{ citation.source_version }} · {{ citation.section_path.join(' / ') || '章节未识别' }}</div><blockquote>{{ citation.exact_quote || '未保存引句' }}</blockquote><div class="evidence-meta" :title="SUPPORT_EXPLANATION">引句校验：{{ citation.verification?.quote_valid ? '原文子串' : '未验证' }} · 支持：{{ supportLabel(citation.verification?.support_status) }}</div><div class="page-locations">引用原文页：{{ quotePageIndexes(citation).length ? [...new Set(quotePageIndexes(citation).map(i => i + 1))].sort((a,b) => a-b).join(', ') : '暂无法精确定位' }}<br>分块覆盖页：{{ chunkPageIndexes(citation).length ? [...new Set(chunkPageIndexes(citation).map(i => i + 1))].sort((a,b) => a-b).join(', ') : '未记录' }}</div><button class="citation-link source-open" :disabled="!citationCanOpen(citation)" :title="citationTooltip(citation.evidence_id)" @click="openCitationSource(citation)">{{ citationButtonLabel(citation) }}</button><template v-for="(span, index) in citation.spans" :key="span.block_id"><button v-if="span.bbox && citation.verification?.location_valid" @click="locateCitation(citation, index)">定位原文块 · PDF 第 {{ span.pdf_page_index + 1 }} 页（块级高亮）</button></template><p v-if="citationPageTargets(citation).kind === 'source'" class="muted location-limit">此来源可跳至原文页/来源块页，但没有可靠的引句级位置。</p><p v-else-if="citationPageTargets(citation).kind === 'chunk'" class="muted location-limit">当前仅有分块覆盖页；跳转不代表引句精确位置。</p><p v-else-if="!citationCanOpen(citation)" class="muted location-limit">来源缺少可靠页码或定位信息。</p></div>
          </div>
        </section>
        <section v-else class="block-panel"><p class="muted">点击任意页的块跳转并定位。章节为空表示未可靠识别。</p><div class="block-scroll"><button v-for="b in blocks" :key="b.block_id" class="block" :class="{ active: activeBlock?.block_id === b.block_id }" @click="locate(b)"><span class="block-meta">PDF 第 {{ b.pdf_page_index + 1 }} 页 · {{ b.section_path.join(' / ') || '章节未识别' }} · {{ b.block_id }}</span><span>{{ b.text }}</span></button></div><div class="limits" v-if="version.quality?.limits"><strong>解析限制</strong><div v-for="item in version.quality.limits" :key="item">{{ item }}</div></div></section>
      </aside>
    </main>
  </div>
</template>
