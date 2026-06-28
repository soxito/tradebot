const ZA_LOCALE = 'en-ZA'
export const SOUTH_AFRICA_TIMEZONE = 'Africa/Johannesburg'

type DateLike = string | number | Date | null | undefined

function toDate(value: DateLike): Date | null {
  if (value == null) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatDateTimeZA(value: DateLike, fallback = '—'): string {
  const date = toDate(value)
  if (!date) return fallback
  return date.toLocaleString(ZA_LOCALE, {
    timeZone: SOUTH_AFRICA_TIMEZONE,
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function formatDateZA(value: DateLike, fallback = '—'): string {
  const date = toDate(value)
  if (!date) return fallback
  return date.toLocaleDateString(ZA_LOCALE, {
    timeZone: SOUTH_AFRICA_TIMEZONE,
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  })
}

export function formatTimeZA(value: DateLike, fallback = '—'): string {
  const date = toDate(value)
  if (!date) return fallback
  return date.toLocaleTimeString(ZA_LOCALE, {
    timeZone: SOUTH_AFRICA_TIMEZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function formatDateTimeCompactZA(value: DateLike, fallback = '—'): string {
  const date = toDate(value)
  if (!date) return fallback
  return date.toLocaleString(ZA_LOCALE, {
    timeZone: SOUTH_AFRICA_TIMEZONE,
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
