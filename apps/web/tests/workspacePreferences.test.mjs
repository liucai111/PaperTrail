import test from 'node:test'
import assert from 'node:assert/strict'
import { clampReaderShare, readWorkspacePreferences, writeWorkspacePreferences } from '../src/workspacePreferences.mjs'

function storageWith(value = null) {
  return { value, getItem() { return this.value }, setItem(_key, next) { this.value = next } }
}

test('workspace layout preferences persist independently of navigation state', () => {
  const storage = storageWith()
  writeWorkspacePreferences(storage, { libraryCollapsed: true, readerSharePercent: 44 })
  assert.deepEqual(readWorkspacePreferences(storage), { libraryCollapsed: true, readerSharePercent: 44 })
})

test('malformed and out-of-range layout preferences fall back or clamp safely', () => {
  assert.deepEqual(readWorkspacePreferences(storageWith('{broken')), { libraryCollapsed: false, readerSharePercent: 40 })
  assert.equal(clampReaderShare(99), 70)
  assert.equal(clampReaderShare(1), 30)
  assert.equal(clampReaderShare('NaN'), 40)
})
