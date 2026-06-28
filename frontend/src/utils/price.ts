/**
 * Smart price formatting for crypto tokens.
 * Handles everything from BTC ($67,000) to PEPE ($0.00001234).
 */

/**
 * Returns the appropriate number of decimal places for a given price.
 * - >= 1000       → 2   (BTC, ETH)
 * - >= 1          → 4   (SOL, LINK)
 * - >= 0.01       → 6   (small altcoins)
 * - >= 0.0001     → 8   (PEPE, SHIB)
 * - < 0.0001      → 10  (ultra-micro tokens)
 */
export function pricePrecision(price: number): number {
  const abs = Math.abs(price);
  if (abs === 0) return 2;
  if (abs >= 1000) return 2;
  if (abs >= 1) return 4;
  if (abs >= 0.01) return 6;
  if (abs >= 0.0001) return 8;
  return 10;
}

/**
 * Format a price with smart decimal places.
 * Includes dollar sign by default.
 */
export function formatPrice(price: number | null | undefined, withDollar = true): string {
  if (price == null || isNaN(price)) return withDollar ? '$0.00' : '0.00';
  const decimals = pricePrecision(price);
  const formatted = price.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: decimals,
  });
  return withDollar ? `$${formatted}` : formatted;
}

/**
 * Calculate the minMove value for lightweight-charts priceFormat
 * based on the minimum price in a dataset.
 */
export function calcMinMove(prices: number[]): number {
  const minPrice = Math.min(...prices.filter(p => p > 0));
  if (!isFinite(minPrice) || minPrice === 0) return 0.01;
  const precision = pricePrecision(minPrice);
  return parseFloat(`1e-${precision}`);
}

/**
 * Calculate precision (number of decimal places) for lightweight-charts.
 */
export function calcChartPrecision(prices: number[]): number {
  const minPrice = Math.min(...prices.filter(p => p > 0));
  if (!isFinite(minPrice) || minPrice === 0) return 2;
  return pricePrecision(minPrice);
}
