import test from 'node:test'
import assert from 'node:assert/strict'
import { classifyUploadFailure, latestUploadTaskForVersion, uploadIdempotencyKey, uploadPrimaryAction, uploadSelectionDisposition, uploadStageIndex, uploadStageStatus, uploadTaskGuidance, uploadVectorProgress } from '../apps/web/src/uploadSubmission.mjs'

test('upload idempotency key is stable for the same SHA-256 content', () => {
  const digest = 'BDFAA68D8984F0DC02BEACA527B76F207D99B666D31D1DA728EE0728182DF697'
  assert.equal(uploadIdempotencyKey(digest), `upload-${digest.toLowerCase()}`)
  assert.equal(uploadIdempotencyKey(digest), uploadIdempotencyKey(digest))
  assert.throws(() => uploadIdempotencyKey('not-a-digest'), TypeError)
})

test('received client errors are explicit rejections', () => {
  assert.deepEqual(classifyUploadFailure({ status: 422 }), { kind: 'http_rejected', status: 422 })
  assert.deepEqual(classifyUploadFailure({ status: 413 }), { kind: 'http_rejected', status: 413 })
})

test('server errors, timeouts, and transport errors preserve uncertain outcome', () => {
  assert.deepEqual(classifyUploadFailure({ status: 502 }), { kind: 'http_outcome_unknown', status: 502 })
  assert.deepEqual(classifyUploadFailure({ name: 'AbortError' }), { kind: 'timeout_outcome_unknown' })
  assert.deepEqual(classifyUploadFailure(new TypeError('Failed to fetch')), { kind: 'network_outcome_unknown' })
})

test('refresh can restore the newest retry for the same uploaded document version', () => {
  const original = { task_id: 'task_old', kind: 'document_ingestion', status: 'outcome_unknown',
    created_at: '2026-10-13T09:08:46Z', request_summary: { document_version_id: 'doc_user_fixed' } }
  const latest = { task_id: 'task_retry', kind: 'document_ingestion', status: 'succeeded',
    created_at: '2026-10-13T09:15:28Z', request_summary: { document_version_id: 'doc_user_fixed' } }
  const unrelated = { ...latest, task_id: 'task_other', request_summary: { document_version_id: 'doc_other' } }
  assert.equal(latestUploadTaskForVersion(original, [unrelated, latest]).task_id, 'task_retry')
  assert.equal(latestUploadTaskForVersion(original, [unrelated]).task_id, 'task_old')
})

test('upload terminal states keep deterministic OCR failures apart from uncertain embedding', () => {
  const noText = {status:'failed', error:{code:'no_extractable_text',message:'none',retryable:false,
    details:{ocr_status:'disabled'}}}
  assert.equal(uploadTaskGuidance(noText, {ocr_enabled:false,ocr_runtime_present:true}).canRetry, false)
  const enabled = uploadTaskGuidance(noText, {ocr_enabled:true,ocr_runtime_present:true})
  assert.equal(enabled.canRetry, true)
  assert.match(enabled.message, /OCR/)
  assert.equal(uploadTaskGuidance({...noText,error:{...noText.error,code:'pdf_parse_failed'}},
    {ocr_enabled:true,ocr_runtime_present:true}).canRetry, false)
  assert.equal(uploadTaskGuidance({status:'failed',error:{code:'embedding_failed',message:'',retryable:false,
    details:{reason:'authentication_error'}}}).canRetry, false)
  const uncertain = uploadTaskGuidance({status:'outcome_unknown',error:{code:'embedding_failed',message:'',
    retryable:true,details:{reason:'read_error'}}})
  assert.equal(uncertain.canRetry, true)
  assert.match(uncertain.message, /无法确认/)
  assert.equal(uncertain.retryLabel, '确认后重试未知批次')
})

test('upload progress maps persisted phases without inventing a percentage', () => {
  assert.equal(uploadStageIndex({ phase: 'parsing', status: 'running' }), 1)
  assert.equal(uploadStageIndex({ phase: 'chunking', status: 'running' }), 2)
  assert.equal(uploadStageIndex({ phase: 'embedding', status: 'outcome_unknown' }), 3)
  assert.equal(uploadStageIndex({ phase: 'completed', status: 'succeeded' }), 4)
  assert.equal(uploadVectorProgress({ phase: 'embedding' }), null)
  assert.deepEqual(uploadVectorProgress({ progress: {
    completed_vectors: 20, total_vectors: 33, missing_vectors: 13, ready: false,
  } }), { completed: 20, total: 33, missing: 13, ready: false })
})

test('ready upload marks every stage complete and offers to open the saved paper', () => {
  const ready = { kind: 'document_ingestion', status: 'succeeded', phase: 'completed',
    request_summary: { paper_id: 'user_paper', sha256: 'same-sha' },
    progress: { completed_vectors: 48, total_vectors: 48, missing_vectors: 0, ready: true } }
  assert.deepEqual([0, 1, 2, 3, 4].map(index => uploadStageStatus(ready, index)),
    ['done', 'done', 'done', 'done', 'done'])
  assert.equal(uploadPrimaryAction(ready, 'same-sha'), 'open_paper')
  assert.equal(uploadPrimaryAction(ready, null), 'open_paper')
  assert.equal(uploadPrimaryAction(ready, null, true), 'upload')
  assert.equal(uploadPrimaryAction(ready, 'different-sha'), 'upload')
  assert.equal(uploadPrimaryAction({ ...ready, status: 'outcome_unknown' }, 'same-sha'), 'use_task_actions')
})

test('unknown embedding outcomes require explicit confirmation and show a distinct action', () => {
  const guidance = uploadTaskGuidance({ status: 'outcome_unknown', error: { code: 'embedding_failed' } })
  assert.equal(guidance.canRetry, true)
  assert.match(guidance.retryLabel, /确认后/)
  assert.match(guidance.message, /无法确认/)
  assert.equal(uploadTaskGuidance({ status: 'failed', error: {
    code: 'embedding_failed', retryable: true, details: { reason: 'connection_error' },
  } }).retryLabel, '继续处理未完成索引')
})

test('selected upload matching a persisted task cannot be submitted as a new upload', () => {
  const task = { kind: 'document_ingestion', status: 'outcome_unknown', request_summary: { sha256: 'abc123' } }
  assert.deepEqual(uploadSelectionDisposition('abc123', task), {
    matchesTask: true, label: '该文件结果未知，请使用下方任务操作',
  })
  assert.deepEqual(uploadSelectionDisposition('different-sha', task), { matchesTask: false, label: '' })
  assert.deepEqual(uploadSelectionDisposition('abc123', { ...task, kind: 'qa' }), { matchesTask: false, label: '' })
})
