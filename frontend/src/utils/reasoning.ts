/**
 * Agents may return `reasoning` as a plain string or as a structured object
 * (e.g. {technical_analysis, market_sentiment, catalyst_risk}). Rendering an
 * object directly crashes React, so coerce anything into readable text.
 */
export function toReasoningText(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value.map((v) => toReasoningText(v)).filter(Boolean).join(' · ')
  }
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${toReasoningText(v)}`)
      .filter(Boolean)
      .join(' · ')
  }
  return String(value)
}
