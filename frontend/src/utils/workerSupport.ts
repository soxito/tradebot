/**
 * workerSupport — feature detection helpers for the HTML Web Workers API.
 *
 * Every worker in the app pairs with a main-thread fallback, so these guards
 * decide whether the off-thread path can be used. Detection is fail-safe: any
 * uncertainty returns `false` and the caller keeps running on the main thread.
 */

/** True when Dedicated Web Workers are available (browser, not SSR). */
export function supportsWorker(): boolean {
  return typeof window !== 'undefined' && typeof Worker !== 'undefined'
}

/**
 * True when a canvas can be driven from a worker via OffscreenCanvas.
 * Pass the target canvas to also verify `transferControlToOffscreen` exists
 * (older Safari exposes `OffscreenCanvas` for images but not canvas transfer).
 */
export function supportsOffscreenCanvas(canvas?: HTMLCanvasElement | null): boolean {
  if (!supportsWorker()) return false
  if (typeof OffscreenCanvas === 'undefined') return false
  if (canvas && typeof canvas.transferControlToOffscreen !== 'function') return false
  return true
}
