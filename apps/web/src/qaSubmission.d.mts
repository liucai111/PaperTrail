export type QaFailureDisplay = { message: string; code: string; reason: string; status?: number; phase?: string; endpointHost?: string; elapsedMs?: number; exceptionChain?: string; diagnostic?: Record<string, unknown>; retryable: boolean }
export function describeQaFailure(error: unknown): QaFailureDisplay
export function describeOperationTaskError(task: { status?: string; error?: { code?: string; message?: string; details?: Record<string, unknown> } | null } | null | undefined): string
export function isHistoricalFailureTask(task: { status?: string } | null | undefined): boolean
export function canDismissOperationTask(task: { status?: string } | null | undefined): boolean
export function runQaSubmission<T>(setPending: (pending: boolean) => void, request: () => Promise<T>, onSuccess: (result: T) => void, onFailure: (error: unknown) => void): Promise<void>
