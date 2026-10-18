export type SavedViewState = {
  view: 'reader' | 'history' | 'notes' | 'reports' | 'evaluation'
  paperId: string | null
  versionId: string | null
  pageIndex: number
  activeBlockId: string | null
  readerScrollTop: number
  readerScrollLeft: number
  selectedPaperIds: string[]
  question: string
  historyRunId: string | null
  historyOffset: number
  readerReturnMode: 'history' | 'notes' | 'reports' | null
  reportId: string | null
  reportRevisionId: string | null
  noteId: string | null
  notesOffset: number
  reportsOffset: number
}
export function normalizeViewState(value: unknown): SavedViewState | null
export function readViewState(storage: Pick<Storage, 'getItem'>): SavedViewState | null
export function explicitQaRunId(hash: string): string | null
export function readerLocationFromHash(hash: string): { versionId: string; pageIndex: number; activeBlockId: string | null; scrollTop: number; scrollLeft: number } | null
export function qaRouteWithReaderLocation(runId: string, location: { versionId: string | null; pageIndex: number; activeBlockId?: string | null; scrollTop?: number; scrollLeft?: number } | null): string
export function qaRunToRestore(saved: SavedViewState | null, hash: string): string | null
export function shouldRestoreSavedView(navigationType: string, hasExplicitRoute?: boolean): boolean
