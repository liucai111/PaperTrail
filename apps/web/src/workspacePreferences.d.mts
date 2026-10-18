export const WORKSPACE_PREFERENCES_KEY: string
export function clampReaderShare(value: unknown): number
export function readWorkspacePreferences(storage: Pick<Storage, 'getItem'>): {
  libraryCollapsed: boolean
  readerSharePercent: number
}
export function writeWorkspacePreferences(storage: Pick<Storage, 'setItem'>, value: {
  libraryCollapsed: boolean
  readerSharePercent: unknown
}): void
