export const SUPPORT_EXPLANATION: string
export function stripKnownEvidenceMarkers(answer: unknown, citations?: Array<{ evidence_id?: string }>): string
export function supportLabel(status?: string): string
export function claimSupportLabel(claim?: { support_status?: string; support_verification?: string } | null): string
export function claimSourceItems(claim?: { evidence_ids?: string[] } | null, citations?: any[]): Array<{ evidenceId: string; citation: any | null }>
export function citationPageTargets(citation?: any): { pages: number[]; kind: 'quote' | 'source' | 'chunk' | 'unavailable' }
export function citationButtonLabel(citation?: any): string
export function citationCanOpen(citation?: any): boolean
export function displaySupportReason(reason?: string): string
