import test from 'node:test'
import assert from 'node:assert/strict'
import { describeQaFailure, describeOperationTaskError, isHistoricalFailureTask, canDismissOperationTask, runQaSubmission } from '../apps/web/src/qaSubmission.mjs'
import { explicitQaRunId, normalizeViewState, qaRouteWithReaderLocation, qaRunToRestore, readViewState,
  readerLocationFromHash, shouldRestoreSavedView } from '../apps/web/src/viewState.mjs'

test('connection failure clears loading, preserves retryability, and does not auto-repeat', async () => {
  const state = { asking: false }
  let calls = 0
  let failure
  await runQaSubmission(pending => { state.asking = pending }, async () => {
    calls += 1
    throw { status: 503, payload: { code: 'query_embedding_failed', retryable: true, details: { reason: 'connection_error' } } }
  }, () => assert.fail('request must not succeed'), error => { failure = describeQaFailure(error) })
  assert.equal(calls, 1)
  assert.equal(state.asking, false)
  assert.equal(failure.message, '查询向量服务连接失败，请检查服务连接后重试。')
  assert.equal(failure.retryable, true)
})

test('timeout failure clears loading and exposes a retry only when the server permits it', async () => {
  const state = { asking: false }
  let failure
  await runQaSubmission(pending => { state.asking = pending }, async () => {
    throw { status: 503, payload: { code: 'query_embedding_failed', retryable: false, details: { reason: 'timeout' } } }
  }, () => assert.fail('request must not succeed'), error => { failure = describeQaFailure(error) })
  assert.equal(state.asking, false)
  assert.equal(failure.message, '查询向量服务响应超时，请稍后重试。')
  assert.equal(failure.retryable, false)
})

test('successful request clears loading and returns its result to the view', async () => {
  const state = { asking: false }
  let received
  await runQaSubmission(pending => { state.asking = pending }, async () => ({ answer: 'mock answer' }), result => { received = result }, () => assert.fail('request must succeed'))
  assert.deepEqual(received, { answer: 'mock answer' })
  assert.equal(state.asking, false)
})

test('generation ReadError is shown safely with manual retry only when retryable', async () => {
  const state = { asking: false }
  let failure
  await runQaSubmission(pending => { state.asking = pending }, async () => {
    throw { status: 502, payload: { code: 'llm_request_failed', retryable: true,
      details: { reason: 'read_error', phase: 'generation', endpoint_host: 'api.deepseek.com', elapsed_ms: 9325.4,
        exception_chain: ['ChatProviderError', 'ReadError', 'ConnectionResetError'] } } }
  }, () => assert.fail('request must fail'), error => { failure = describeQaFailure(error) })
  assert.equal(state.asking, false)
  assert.equal(failure.message, '生成模型连接中断，请检查服务连接后重试。')
  assert.equal(failure.retryable, true)
  assert.equal(failure.endpointHost, 'api.deepseek.com')
  assert.equal(failure.phase, 'generation')
  assert.match(failure.exceptionChain, /ReadError/)
})

test('malformed model response exposes only safe diagnostics and a useful task error', () => {
  const failure = describeQaFailure({ status: 502, payload: { code: 'llm_request_failed', retryable: false,
    details: { reason: 'invalid_json_response', request_id: 'task_123', provider_request_id: 'upstream_42',
      json_error: 'Unterminated string', prompt_tokens: 54, completion_tokens: 16,
      response_content_chars: 80, ['secret']: 'must-not-be-forwarded' } } })
  assert.match(failure.message, /结构化内容无法解析/)
  assert.equal(failure.diagnostic.request_id, 'task_123')
  assert.equal(failure.diagnostic.prompt_tokens, 54)
  assert.equal('secret' in failure.diagnostic, false)
  const taskMessage = describeOperationTaskError({ status: 'failed', error: { code: 'comparison_generation_failed',
    message: 'generic', details: { reason: 'invalid_json_response' } } })
  assert.match(taskMessage, /结构化内容无法解析/)
})

test('terminal failed operations stay in task history and can be cleared from the banner', () => {
  const failed = { status: 'failed' }
  assert.equal(isHistoricalFailureTask(failed), true)
  assert.equal(canDismissOperationTask(failed), true)
  assert.equal(isHistoricalFailureTask({ status: 'succeeded' }), false)
})

test('historical structured-response parse failure gets an accurate safe Chinese message', () => {
  const message = describeOperationTaskError({ status: 'failed', error: { code: 'llm_request_failed', message: 'Chat provider request failed', details: {
    reason: 'RuntimeError', phase: 'generation', exception_chain: ['RuntimeError', 'JSONDecodeError']
  } } })
  assert.equal(message, '模型响应的结构化内容无法解析，任务失败且未保存结果。')
})

test('active and outcome-unknown operation banners cannot be dismissed', () => {
  for (const status of ['waiting', 'running', 'outcome_unknown']) {
    assert.equal(canDismissOperationTask({ status }), false)
  }
  assert.equal(isHistoricalFailureTask({ status: 'outcome_unknown' }), true)
})

test('saved view restoration keeps bounded identifiers and rejects malformed routes', () => {
  const saved = normalizeViewState({ view: 'reports', paperId: 'selfrag', versionId: 'doc_fixed', pageIndex: 8,
    activeBlockId: 'blk_source_1', readerScrollTop: 345.5, readerScrollLeft: 12,
    selectedPaperIds: ['selfrag', 'selfrag', 'lightrag', 4, 'crag', 'extra'], question: 'Q?',
    historyRunId: 'qrun_saved', reportId: 'rpt_saved', reportRevisionId: 'rrev_2', noteId: 'note_saved',
    readerReturnMode: 'reports', historyOffset: 40, reportsOffset: 20, notesOffset: 10 })
  assert.equal(saved.view, 'reports')
  assert.deepEqual(saved.selectedPaperIds, ['selfrag', 'lightrag', 'crag'])
  assert.equal(saved.historyRunId, 'qrun_saved')
  assert.equal(saved.reportRevisionId, 'rrev_2')
  assert.equal(saved.pageIndex, 8)
  assert.equal(saved.activeBlockId, 'blk_source_1')
  assert.equal(saved.readerScrollTop, 345.5)
  assert.equal(saved.readerScrollLeft, 12)
  assert.equal(normalizeViewState({ view: 'reader', activeBlockId: '../bad', readerScrollTop: -1, readerScrollLeft: 'x' }).activeBlockId, null)
  assert.equal(normalizeViewState({ view: 'reader', activeBlockId: '../bad', readerScrollTop: -1, readerScrollLeft: 'x' }).readerScrollTop, 0)
  assert.equal(normalizeViewState({ view: '../admin', historyRunId: '../bad', pageIndex: -1 }).view, 'reader')
  assert.equal(normalizeViewState({ view: 'reader', historyRunId: '../bad', pageIndex: -1 }).historyRunId, null)
  assert.equal(normalizeViewState({ view: 'evaluation' }).view, 'evaluation')
  assert.equal(normalizeViewState(null), null)
})

test('saved view restoration tolerates unavailable or malformed local storage', () => {
  assert.equal(readViewState({ getItem: () => '{broken' }), null)
  assert.equal(readViewState({ getItem: () => null }), null)
})

test('plain root starts a fresh QA while explicit history and note-source routes restore their QA', () => {
  const oldReader = { view: 'reader', historyRunId: 'qrun_previous', readerReturnMode: null }
  assert.equal(qaRunToRestore(oldReader, ''), null)
  assert.equal(qaRunToRestore(oldReader, '#qa/qrun_selected'), 'qrun_selected')
  assert.equal(explicitQaRunId('#qa/qrun_selected'), 'qrun_selected')
  assert.equal(explicitQaRunId('#qa/not-a-run'), null)
  assert.equal(qaRunToRestore({ ...oldReader, readerReturnMode: 'notes' }, ''), 'qrun_previous')
  assert.equal(shouldRestoreSavedView('navigate'), false)
  assert.equal(shouldRestoreSavedView('reload'), true)
  assert.equal(shouldRestoreSavedView('navigate', true), true)
})

test('history QA route carries validated PDF version, page, block, and scroll position across reload', () => {
  const hash = qaRouteWithReaderLocation('qrun_fixed', { versionId: 'doc_fixed', pageIndex: 23,
    activeBlockId: 'blk_source', scrollTop: 482.5, scrollLeft: 12 })
  assert.equal(explicitQaRunId(hash), 'qrun_fixed')
  assert.deepEqual(readerLocationFromHash(hash), { versionId: 'doc_fixed', pageIndex: 23,
    activeBlockId: 'blk_source', scrollTop: 483, scrollLeft: 12 })
  assert.equal(readerLocationFromHash('#qa/qrun_fixed?version=../bad&page=-1'), null)
  assert.equal(qaRouteWithReaderLocation('qrun_fixed', null), '#qa/qrun_fixed')
})
