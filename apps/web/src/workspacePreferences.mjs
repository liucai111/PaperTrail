export const WORKSPACE_PREFERENCES_KEY = 'papertrail.workspace-preferences'

export function clampReaderShare(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return 40
  return Math.min(70, Math.max(30, Math.round(numeric)))
}

export function readWorkspacePreferences(storage) {
  try {
    const saved = JSON.parse(storage.getItem(WORKSPACE_PREFERENCES_KEY) || 'null')
    return {
      libraryCollapsed: saved?.libraryCollapsed === true,
      readerSharePercent: clampReaderShare(saved?.readerSharePercent),
    }
  } catch {
    return { libraryCollapsed: false, readerSharePercent: 40 }
  }
}

export function writeWorkspacePreferences(storage, value) {
  storage.setItem(WORKSPACE_PREFERENCES_KEY, JSON.stringify({
    libraryCollapsed: value.libraryCollapsed === true,
    readerSharePercent: clampReaderShare(value.readerSharePercent),
  }))
}
