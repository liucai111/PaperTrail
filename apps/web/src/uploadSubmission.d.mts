export type UploadFailure =
  | { kind: 'http_rejected' | 'http_outcome_unknown'; status: number }
  | { kind: 'timeout_outcome_unknown' | 'network_outcome_unknown' }

export function uploadIdempotencyKey(sha256: string): string
export function classifyUploadFailure(error: unknown): UploadFailure
export function latestUploadTaskForVersion<T extends { kind: string; created_at: string; request_summary?: Record<string, unknown> }>(
  currentTask: T,
  tasks: T[],
): T
export function uploadTaskGuidance(task: {status:string;error?:{code:string;message:string;retryable:boolean;details?:Record<string,unknown>}|null}|null,
  config?: {ocr_enabled?:boolean;ocr_runtime_present?:boolean}): {message:string;canRetry:boolean;retryLabel:string}
export function uploadStageIndex(task: {status?:string;phase?:string}|null): number
export function uploadStageStatus(task: {status?:string;phase?:string}|null, index:number): 'done'|'current'|'unknown'|'failed'|'pending'
export function uploadPrimaryAction(task: {kind?:string;status?:string;request_summary?:{paper_id?:string;sha256?:string}}|null,
  selectedSha256?:string|null, hasSelection?:boolean): 'open_paper'|'use_task_actions'|'upload'
export function uploadVectorProgress(task: {progress?:{completed_vectors?:number;total_vectors?:number;missing_vectors?:number;ready?:boolean}|null}|null):
  {completed:number;total:number;missing:number;ready:boolean}|null
export function uploadSelectionDisposition(sha256: string|null, task: {kind?:string;status?:string;request_summary?:{sha256?:string}}|null):
  {matchesTask:boolean;label:string}
