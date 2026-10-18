export function uploadIdempotencyKey(sha256) {
  if (typeof sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(sha256)) {
    throw new TypeError('Upload content digest must be a SHA-256 hex string')
  }
  return `upload-${sha256.toLowerCase()}`
}

export function classifyUploadFailure(error) {
  if (Number.isInteger(error?.status)) {
    return error.status >= 500
      ? { kind: 'http_outcome_unknown', status: error.status }
      : { kind: 'http_rejected', status: error.status }
  }
  if (error?.name === 'AbortError' || error?.name === 'UploadTimeoutError') {
    return { kind: 'timeout_outcome_unknown' }
  }
  return { kind: 'network_outcome_unknown' }
}

export function latestUploadTaskForVersion(currentTask, tasks) {
  const versionId = currentTask?.request_summary?.document_version_id
  if (currentTask?.kind !== 'document_ingestion' || !versionId || !Array.isArray(tasks)) return currentTask
  const newer = tasks.find(task => task?.kind === 'document_ingestion' &&
    task?.request_summary?.document_version_id === versionId &&
    Date.parse(task.created_at || '') > Date.parse(currentTask.created_at || ''))
  return newer || currentTask
}

export function uploadTaskGuidance(task, config = {}) {
  const code = task?.error?.code
  const reason = task?.error?.details?.reason
  if (task?.status === 'succeeded') return { message: '', canRetry: false, retryLabel: '' }
  if (task?.status === 'outcome_unknown') return {
    message: '服务端无法确认本批是否已处理。索引保持不可检索；确认后只会补发当前仍缺失的向量。',
    canRetry: true, retryLabel: '确认后重试未知批次',
  }
  if (code === 'no_extractable_text') {
    const ready = config.ocr_enabled === true && config.ocr_runtime_present === true
    return { message: ready
      ? '原任务没有可提取文本；当前已启用本地 OCR，可明确重新处理同一文档版本。'
      : 'PDF 没有足够的可提取文本。需先安装并启用本地 OCR，再重新处理；重复原解析不会改善结果。',
    canRetry: ready && task.status === 'failed', retryLabel: '启用 OCR 后重新处理' }
  }
  if (code === 'ocr_unavailable') return {
    message: '本地 OCR 运行时或模型不可用；安装并启用后才能重新处理。',
    canRetry: config.ocr_enabled === true && config.ocr_runtime_present === true && task.status === 'failed',
    retryLabel: '重新处理 PDF',
  }
  if (code === 'pdf_parse_failed' || code === 'no_indexable_text') return {
    message: 'PDF 解析或文本质量检查失败；相同配置下重试无法修复。', canRetry: false, retryLabel: '',
  }
  if (code === 'embedding_failed') return {
    message: `索引 Embedding 请求失败${typeof reason === 'string' ? `（${reason}）` : ''}；论文尚不可检索。`,
    canRetry: task.status === 'failed' && task.error?.retryable === true,
    retryLabel: '继续处理未完成索引',
  }
  return { message: task?.error?.message || '处理失败。',
    canRetry: task?.status === 'interrupted' || (task?.status === 'failed' && task?.error?.retryable === true),
    retryLabel: task?.status === 'interrupted' ? '继续未完成处理' : '安全重试处理' }
}

export function uploadStageIndex(task) {
  if (task?.status === 'succeeded' || task?.phase === 'completed') return 4
  const phase = task?.phase
  if (phase === 'waiting' || phase === 'validating') return 0
  if (phase === 'parsing' || phase === 'ocr') return 1
  if (phase === 'chunking') return 2
  if (phase === 'indexing' || phase === 'embedding') return 3
  if (phase === 'persisting') return 4
  return -1
}

export function uploadStageStatus(task, index) {
  if (!Number.isInteger(index) || index < 0 || index > 4) return 'pending'
  const current = uploadStageIndex(task)
  if (task?.status === 'succeeded') return 'done'
  if (index < current) return 'done'
  if (index !== current) return 'pending'
  if (task?.status === 'outcome_unknown') return 'unknown'
  if (task?.status === 'failed' || task?.status === 'interrupted' || task?.status === 'cancelled') return 'failed'
  return 'current'
}

export function uploadPrimaryAction(task, selectedSha256 = null, hasSelection = Boolean(selectedSha256)) {
  const taskSha = task?.request_summary?.sha256
  if (task?.kind === 'document_ingestion' && task?.status === 'succeeded' && task?.request_summary?.paper_id &&
      (!hasSelection || selectedSha256 === taskSha)) return 'open_paper'
  if (selectedSha256 && task?.kind === 'document_ingestion' && selectedSha256 === taskSha) return 'use_task_actions'
  return 'upload'
}

export function uploadVectorProgress(task) {
  const progress = task?.progress
  if (!progress || !Number.isInteger(progress.completed_vectors) || !Number.isInteger(progress.total_vectors)) return null
  return {
    completed: Math.max(0, progress.completed_vectors),
    total: Math.max(0, progress.total_vectors),
    missing: Number.isInteger(progress.missing_vectors)
      ? Math.max(0, progress.missing_vectors)
      : Math.max(0, progress.total_vectors - progress.completed_vectors),
    ready: progress.ready === true,
  }
}

export function uploadSelectionDisposition(sha256, task) {
  const matchesTask = Boolean(sha256 && task?.kind === 'document_ingestion'
    && task?.request_summary?.sha256 === sha256)
  if (!matchesTask) return { matchesTask: false, label: '' }
  const labels = {
    waiting: '该文件已有待处理任务',
    running: '该文件正在处理中',
    succeeded: '该文件已完成处理',
    outcome_unknown: '该文件结果未知，请使用下方任务操作',
    failed: '该文件已有失败任务，请使用下方恢复操作',
    interrupted: '该文件已有中断任务，请使用下方恢复操作',
    cancelled: '该文件已有已取消任务，请使用下方恢复操作',
  }
  return { matchesTask: true, label: labels[task.status] || '该文件已有处理任务' }
}
