const messages = {
  connection_error: '查询向量服务连接失败，请检查服务连接后重试。',
  timeout: '查询向量服务响应超时，请稍后重试。',
  authentication_error: '查询向量服务认证失败，请检查 Embedding 凭据。',
  parameter_error: '查询向量服务拒绝了模型或请求参数，请检查 Embedding 配置。',
  provider_http_error: '查询向量服务暂时不可用，请稍后重试。',
  configuration_error: '查询向量服务配置不完整，请检查 Embedding 配置。',
  invalid_response: '查询向量服务返回了无法识别的响应。',
  internal_embedding_error: '查询向量处理失败，请检查服务日志。'
}

export function describeQaFailure(error) {
  const payload = error?.payload
  if (!payload) return { message: '问答请求失败，请检查网络连接后重试。', code: 'network_error', reason: 'network_error', retryable: false }
  const reason = payload.details?.reason || 'unknown'
  let message
  if (payload.code === 'query_embedding_failed') message = messages[reason] || '查询向量失败，请检查服务状态。'
  else if (payload.code === 'llm_request_failed') {
    message = ({ read_error: '生成模型连接中断，请检查服务连接后重试。',
      connection_error: '生成模型连接失败，请检查服务连接后重试。',
      timeout: '生成模型响应超时，请稍后重试。',
      output_truncated: '生成模型输出达到长度上限，结果不完整，未保存答案。请缩短问题或减少证据后重新提交。',
      invalid_json_response: '生成模型返回的结构化内容无法解析，任务失败且未保存答案；请展开诊断详情。',
      invalid_provider_json: '生成模型服务返回的响应格式无效，任务失败且未保存答案；请展开诊断详情。',
      invalid_provider_response: '生成模型服务返回了不完整或不兼容的响应，任务失败且未保存答案。',
      invalid_model_response: '生成模型返回内容缺少必需字段，任务失败且未保存答案。',
      authentication_error: '生成模型认证失败，请检查 Chat 凭据。',
      parameter_error: '生成模型拒绝了模型或请求参数，请检查 Chat 配置。',
      provider_http_error: '生成模型服务暂时不可用，请稍后重试。',
      protocol_error: '生成模型连接协议异常，请检查服务连接。' })[reason] || '问答生成失败，请检查模型服务状态。'
  } else message = '问答请求失败，请检查请求状态。'
  const status = Number.isInteger(payload.details?.status_code) ? payload.details.status_code : undefined
  const phase = typeof payload.details?.phase === 'string' ? payload.details.phase : undefined
  const endpointHost = typeof payload.details?.endpoint_host === 'string' ? payload.details.endpoint_host : undefined
  const elapsedMs = Number.isFinite(payload.details?.elapsed_ms) ? payload.details.elapsed_ms : undefined
  const exceptionChain = Array.isArray(payload.details?.exception_chain) ? payload.details.exception_chain.filter(item => typeof item === 'string').join(' → ') : undefined
  const diagnostic = payload.details ? Object.fromEntries(Object.entries(payload.details).filter(([key, value]) =>
    ['request_id','provider_request_id','finish_reason','prompt_tokens','completion_tokens','total_tokens',
      'response_elapsed_ms','response_content_chars','json_error','json_error_line','json_error_column',
      'json_error_position','configured_max_tokens','content_starts_object','content_ends_object',
      'content_wrapped_in_fence','response_shape','response_json_error','effective_timeout_seconds'].includes(key) && value !== undefined)) : {}
  return { message, code: payload.code || 'request_failed', reason, status, phase, endpointHost, elapsedMs, exceptionChain, diagnostic,
    retryable: payload.retryable === true }
}

export function describeOperationTaskError(task) {
  const error = task?.error
  if (!error) return ''
  const details = error.details || {}
  const fallback = error.message || '后台任务失败。'
  const exceptionChain = Array.isArray(details.exception_chain) ? details.exception_chain : []
  const parseFailure = details.reason === 'invalid_json_response' || exceptionChain.includes('JSONDecodeError')
  const message = error.code === 'llm_request_failed' || error.code === 'comparison_generation_failed'
    ? (parseFailure ? '模型响应的结构化内容无法解析，任务失败且未保存结果。' : ({ output_truncated: '模型输出达到长度上限，任务失败且未保存结果。',
        invalid_json_response: '模型返回的结构化内容无法解析，任务失败且未保存结果。',
        invalid_provider_json: '模型服务响应格式无效，任务失败且未保存结果。',
        invalid_provider_response: '模型服务响应缺少必需字段，任务失败且未保存结果。',
        timeout: '模型响应超时，任务失败且未保存结果。',
        read_error: '模型连接在读取响应时中断，任务失败且未保存结果。',
        connection_error: '无法连接模型服务，任务失败且未保存结果。' })[details.reason] || fallback)
    : fallback
  return message + (task.status === 'outcome_unknown' ? ' 请求可能已被服务端处理；先检查历史，不要直接重复提交。' : '')
}

export function isHistoricalFailureTask(task) {
  return ['failed', 'outcome_unknown', 'interrupted', 'cancelled'].includes(task?.status)
}

export function canDismissOperationTask(task) {
  return Boolean(task && !['waiting', 'running', 'outcome_unknown'].includes(task.status))
}

export async function runQaSubmission(setPending, request, onSuccess, onFailure) {
  setPending(true)
  try {
    onSuccess(await request())
  } catch (error) {
    onFailure(error)
  } finally {
    setPending(false)
  }
}
