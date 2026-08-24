/**
 * useLivePrices — polled quotes for the pairs the room is watching.
 *
 * Feeds the big board on the trading-floor wall. Deliberately modest: one
 * request per symbol on a slow interval, skipped entirely while the tab is
 * hidden, because nobody is reading the board when the room is not on screen.
 */
import { useEffect, useRef, useState } from 'react'
import { apiClient } from '@/services/api'

export interface LiveQuote {
  symbol: string
  price: number | null
  /** Previous price, so the board can colour the move. */
  prev: number | null
  source: string | null
  at: number
}

const POLL_MS = 12_000

export function useLivePrices(symbols: string[], enabled = true): Record<string, LiveQuote> {
  const [quotes, setQuotes] = useState<Record<string, LiveQuote>>({})
  // Keyed lookup of the last price so a re-render never loses the direction.
  const prevRef = useRef<Record<string, number | null>>({})
  // Compared as a string so a new array of the same symbols doesn't re-subscribe.
  const key = symbols.join(',')

  useEffect(() => {
    const list = key ? key.split(',') : []
    if (!enabled || !list.length) return

    let cancelled = false
    const controller = new AbortController()

    const tick = async () => {
      if (typeof document !== 'undefined' && document.hidden) return
      for (const symbol of list) {
        try {
          const { data } = await apiClient.getMarketPrice(symbol, controller.signal)
          if (cancelled) return
          const price = typeof data?.price === 'number' ? data.price : null
          if (price == null) continue

          const last = prevRef.current[symbol] ?? null
          prevRef.current[symbol] = price
          setQuotes((prev) => ({
            ...prev,
            [symbol]: {
              symbol,
              // An unchanged quote keeps the previous direction on the board
              // rather than flattening it to "no move".
              prev: price === last ? prev[symbol]?.prev ?? null : last,
              price,
              source: data?.source ?? null,
              at: Date.now(),
            },
          }))
        } catch {
          /* a missing quote just leaves the previous one on the board */
        }
      }
    }

    void tick()
    const id = window.setInterval(() => void tick(), POLL_MS)
    return () => {
      cancelled = true
      controller.abort()
      window.clearInterval(id)
    }
  }, [key, enabled])

  return quotes
}
