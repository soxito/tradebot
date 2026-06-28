import { useEffect, useState, useCallback } from 'react'

const ZAR_CACHE_KEY = 'usdt_zar_rate'
const ZAR_CACHE_TS_KEY = 'usdt_zar_rate_ts'
const ONE_HOUR_MS = 60 * 60 * 1000

interface ZarRateState {
  rate: number | null
  loading: boolean
  lastUpdated: Date | null
}

/**
 * Hook that fetches the USDT→ZAR exchange rate.
 * Caches in localStorage and refreshes every 1 hour.
 */
export function useZarRate() {
  const [state, setState] = useState<ZarRateState>({
    rate: null,
    loading: true,
    lastUpdated: null,
  })

  const fetchRate = useCallback(async () => {
    try {
      // CoinGecko free endpoint: USDT price in ZAR
      const res = await fetch(
        'https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=zar'
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const rate = data?.tether?.zar
      if (typeof rate === 'number' && rate > 0) {
        const now = Date.now()
        localStorage.setItem(ZAR_CACHE_KEY, String(rate))
        localStorage.setItem(ZAR_CACHE_TS_KEY, String(now))
        setState({ rate, loading: false, lastUpdated: new Date(now) })
      }
    } catch {
      // If fetch fails, keep existing cached rate
      setState((prev) => ({ ...prev, loading: false }))
    }
  }, [])

  useEffect(() => {
    // Load from cache first
    const cached = localStorage.getItem(ZAR_CACHE_KEY)
    const cachedTs = localStorage.getItem(ZAR_CACHE_TS_KEY)
    if (cached && cachedTs) {
      const ts = Number(cachedTs)
      setState({ rate: Number(cached), loading: false, lastUpdated: new Date(ts) })
      // Only fetch if cache is older than 1 hour
      if (Date.now() - ts > ONE_HOUR_MS) {
        fetchRate()
      }
    } else {
      fetchRate()
    }

    // Refresh every hour
    const interval = setInterval(fetchRate, ONE_HOUR_MS)
    return () => clearInterval(interval)
  }, [fetchRate])

  const toZar = useCallback(
    (usdt: number): string | null => {
      if (state.rate === null) return null
      const zar = usdt * state.rate
      return `R${zar.toLocaleString('en-ZA', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    },
    [state.rate]
  )

  return { ...state, toZar }
}
