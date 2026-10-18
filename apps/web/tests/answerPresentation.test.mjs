import test from 'node:test'
import assert from 'node:assert/strict'
import { renderAnswerMarkdown } from '../src/answerMarkdown.mjs'
import {
  citationButtonLabel, citationCanOpen, citationPageTargets, claimSourceItems, claimSupportLabel, displaySupportReason,
  stripKnownEvidenceMarkers, supportLabel,
} from '../src/answerPresentation.mjs'

const citation = (overrides = {}) => ({
  evidence_id: 'ev_52280b09e9dc6ea2376916f9',
  verification: { location_valid: true, support_status: 'supported' },
  quote_location_pdf_page_indexes: [23],
  spans: [{ block_id: 'blk_abc', pdf_page_index: 23, bbox: [1, 2, 3, 4] }],
  chunk_pdf_page_indexes: [22, 23],
  ...overrides,
})

test('removes only evidence markers present in saved citation metadata and keeps content/markdown', () => {
  const answer = 'Claim one [ev_52280b09e9dc6ea2376916f9] and [ev_unknown] stays;\n\n- Value: **4.4** [ev_52280b09e9dc6ea2376916f9].'
  assert.equal(stripKnownEvidenceMarkers(answer, [citation()]), 'Claim one and [ev_unknown] stays;\n\n- Value: **4.4**.')
  assert.equal(answer.includes('[ev_52280b09e9dc6ea2376916f9]'), true, 'display cleanup must not mutate the saved answer value')
  const finalHtml = renderAnswerMarkdown(stripKnownEvidenceMarkers('Final [ev_52280b09e9dc6ea2376916f9].', [citation()]))
  const initialHtml = renderAnswerMarkdown(stripKnownEvidenceMarkers('Initial [ev_52280b09e9dc6ea2376916f9].', [citation()]))
  assert.doesNotMatch(finalHtml, /\[ev_52280b09e9dc6ea2376916f9\]/)
  assert.doesNotMatch(initialHtml, /\[ev_52280b09e9dc6ea2376916f9\]/)
})

test('removes every known evidence marker when an answer cites multiple sources', () => {
  const second = citation({ evidence_id: 'ev_ee27c685004f0652273dcf67' })
  const answer = 'First fact [ev_52280b09e9dc6ea2376916f9] and second [ev_ee27c685004f0652273dcf67].'
  assert.equal(stripKnownEvidenceMarkers(answer, [citation(), second]), 'First fact and second.')
})

test('keeps every claim evidence mapping independently clickable and degrades missing sources', () => {
  const first = citation()
  const second = citation({ evidence_id: 'ev_ee27c685004f0652273dcf67', quote_location_pdf_page_indexes: [21] })
  const items = claimSourceItems({ evidence_ids: [first.evidence_id, second.evidence_id, 'ev_missing'] }, [first, second])
  assert.deepEqual(items.map(item => item.evidenceId), [first.evidence_id, second.evidence_id, 'ev_missing'])
  assert.equal(citationCanOpen(items[0].citation), true)
  assert.equal(citationButtonLabel(items[0].citation), '查看原文 · PDF 第 24 页')
  assert.equal(citationCanOpen(items[1].citation), true)
  assert.equal(citationButtonLabel(items[1].citation), '查看原文 · PDF 第 22 页')
  assert.equal(items[2].citation, null)
  assert.equal(citationCanOpen(items[2].citation), false)
  assert.equal(citationButtonLabel(items[2].citation), '来源记录不可用')
})

test('formats support states without changing their meaning, including failure and unknown', () => {
  assert.equal(supportLabel('supported'), '有证据支持')
  assert.equal(supportLabel('partially_supported'), '部分支持')
  assert.equal(supportLabel('unverified'), '未核实')
  assert.equal(supportLabel('verification_failed'), '核验失败')
  assert.equal(supportLabel('unsupported'), '不支持')
  assert.equal(claimSupportLabel({ support_status: 'supported', support_verification: 'failed' }), '核验失败')
  assert.equal(claimSupportLabel({ support_status: 'partially_supported', support_verification: 'automated' }), '部分支持')
})

test('citation labels expose source page without showing internal IDs and support fallback levels', () => {
  assert.equal(citationButtonLabel(citation()), '查看原文 · PDF 第 24 页')
  const pageOnly = citation({ verification: { location_valid: false }, quote_location_pdf_page_indexes: [], spans: [{ block_id: 'blk_abc', pdf_page_index: 23 }] })
  assert.equal(citationButtonLabel(pageOnly), '查看来源页 · PDF 第 24 页（块级/页级）')
  assert.equal(citationPageTargets(pageOnly).kind, 'source')
  const chunkOnly = citation({ verification: { location_valid: false }, quote_location_pdf_page_indexes: [], spans: [], chunk_pdf_page_indexes: [23] })
  assert.equal(citationButtonLabel(chunkOnly), '查看分块覆盖页 · PDF 第 24 页（非精确位置）')
  assert.equal(citationCanOpen(chunkOnly), true)
  assert.equal(citationCanOpen(citation({ spans: [], quote_location_pdf_page_indexes: [], chunk_pdf_page_indexes: [] })), false)
  assert.equal(citationButtonLabel(undefined), '来源记录不可用')
})

test('support reasons discard only the repeated generic disclaimer', () => {
  assert.equal(displaySupportReason('该数值与表 1 一致。核验由模型辅助完成，不代表人工确认或事实正确；引句匹配原文也不等同于证据支持结论。'), '该数值与表 1 一致。')
  assert.equal(displaySupportReason('核验由模型辅助完成，不代表人工确认或事实正确；引句匹配原文也不等同于证据支持结论。'), '')
  assert.equal(displaySupportReason('数值与表格一致。（模型辅助核验）'), '数值与表格一致。')
})
