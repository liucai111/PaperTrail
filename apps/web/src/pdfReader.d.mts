export function computeFitScale(availableWidth: number, pageWidth: number, zoomMultiplier?: number): number
export function pdfPageNumber(pageIndex: number): number | null
export function renderPixelSize(cssPixels: number, devicePixelRatio?: number): number
export function isRenderCurrent(input: {token: number; currentToken: number; document: unknown; currentDocument: unknown; canvas: unknown; currentCanvas: unknown}): boolean
export function isPdfRenderCancellation(reason: unknown): boolean
