/**
 * Shared "price data source" preference for realtime tickers on /mt5-live.
 *
 * The user picks which exchange supplies live ticker prices (for the chart's
 * live line + realtime floating P&L / margin). Stored in localStorage so both
 * the chart component and the page-level poll read the same value without prop
 * drilling. Falls back to the broker's natural exchange when unset.
 */

const KEY = 'mt5_price_source'

/** Exchanges that can serve public ticker data. */
export const PRICE_SOURCE_OPTIONS = ['bitget', 'binance', 'bybit', 'okx', 'kucoin'] as const
export type PriceSource = (typeof PRICE_SOURCE_OPTIONS)[number]

/** Read the saved price source, falling back to `fallback` (broker default). */
export function getPriceSource(fallback?: string): string {
  if (typeof window === 'undefined') return fallback || 'binance'
  const saved = window.localStorage.getItem(KEY)
  return saved || fallback || 'binance'
}

/** Persist the chosen price source and notify listeners in this tab. */
export function setPriceSource(source: string): void {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(KEY, source)
  // Notify same-tab listeners (storage event only fires across tabs).
  window.dispatchEvent(new CustomEvent('mt5-price-source-change', { detail: source }))
}
