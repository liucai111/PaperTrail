import assert from 'node:assert/strict'
import test from 'node:test'
import { computeFitScale, isPdfRenderCancellation, isRenderCurrent, pdfPageNumber, renderPixelSize } from '../src/pdfReader.mjs'

test('fit width accounts for available content width and zoom while preserving page ratio', () => {
  assert.equal(computeFitScale(800, 1000), 0.8)
  assert.equal(computeFitScale(800, 1000, 1.35), 1.08)
  assert.equal(computeFitScale(0, 1000), 0)
  assert.equal(computeFitScale(800, 0), 0)
})

test('UI page index zero maps to PDF.js page one', () => {
  assert.equal(pdfPageNumber(0), 1)
  assert.equal(pdfPageNumber(23), 24)
  assert.equal(pdfPageNumber(-1), null)
})

test('canvas backing store scales for high DPI without changing CSS geometry', () => {
  assert.equal(renderPixelSize(800, 1), 800)
  assert.equal(renderPixelSize(800.2, 2), 1601)
  assert.equal(renderPixelSize(800, 0), 800)
})

test('stale document, canvas, or generation cannot commit a completed render', () => {
  const document = {}
  const canvas = {}
  assert.equal(isRenderCurrent({ token: 3, currentToken: 3, document, currentDocument: document, canvas, currentCanvas: canvas }), true)
  assert.equal(isRenderCurrent({ token: 3, currentToken: 4, document, currentDocument: document, canvas, currentCanvas: canvas }), false)
  assert.equal(isRenderCurrent({ token: 3, currentToken: 3, document, currentDocument: {}, canvas, currentCanvas: canvas }), false)
  assert.equal(isRenderCurrent({ token: 3, currentToken: 3, document, currentDocument: document, canvas, currentCanvas: {} }), false)
})

test('PDF render cancellation is distinguished from actual render failures', () => {
  assert.equal(isPdfRenderCancellation({ name: 'RenderingCancelledException' }), true)
  assert.equal(isPdfRenderCancellation({ name: 'AbortException' }), true)
  assert.equal(isPdfRenderCancellation(new Error('canvas failed')), false)
  assert.equal(isPdfRenderCancellation(null), false)
})
