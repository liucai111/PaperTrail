export function computeFitScale(availableWidth, pageWidth, zoomMultiplier = 1) {
  if (!Number.isFinite(availableWidth) || !Number.isFinite(pageWidth) || pageWidth <= 0 || availableWidth <= 0) return 0
  const multiplier = Number.isFinite(zoomMultiplier) && zoomMultiplier > 0 ? zoomMultiplier : 1
  return Math.max(0.2, availableWidth / pageWidth * multiplier)
}

export function pdfPageNumber(pageIndex) {
  return Number.isInteger(pageIndex) && pageIndex >= 0 ? pageIndex + 1 : null
}

export function renderPixelSize(cssPixels, devicePixelRatio = 1) {
  if (!Number.isFinite(cssPixels) || cssPixels < 0) return 0
  const ratio = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0 ? devicePixelRatio : 1
  return Math.ceil(cssPixels * ratio)
}

export function isRenderCurrent({ token, currentToken, document, currentDocument, canvas, currentCanvas }) {
  return token === currentToken && document === currentDocument && canvas === currentCanvas
}

export function isPdfRenderCancellation(reason) {
  return Boolean(reason && typeof reason === 'object' && 'name' in reason &&
    ['RenderingCancelledException', 'AbortException'].includes(reason.name))
}
