/**
 * MT5Chart — real-time candlestick chart sourced from MT5 terminal via mtapi-io.
 *
 * Data flow: MT5 Terminal → mtapi-io REST → /plugins/mt5/candles → this component
 *
 * Features:
 *  - Symbol picker + timeframe tabs
 *  - Candlestick OHLCV chart (lightweight-charts)
 *  - Position overlays: entry line (blue), SL line (red dashed), TP line (green dashed)
 *  - Deal markers: entry/exit triangles at the correct bar
 *  - Live bid/ask polling (3s) → updates last candle's close in real time
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, IChartApi, ISeriesApi, SeriesMarker, Time, LineStyle } from 'lightweight-charts'
import { apiClient } from '@/services/api'
import { Search, RefreshCw, ChevronDown } from 'lucide-react'
import { formatTimeZA } from '@/utils/datetime'

// ── Types ──────────────────────────────────────────────────────────────────────

interface MT5Candle {
  time: number   // Unix seconds
  open: number
  high: number
  low: number
  close: number
  volume?: number | null
}

export interface MT5PositionForChart {
  id: number
  symbol: string
  side: string          // 'buy' | 'sell'
  volume: number
  price_open: number
  price_current: number | null
  sl: number | null
  tp: number | null
  profit: number
  mt5_ticket: number
}

export interface MT5DealForChart {
  id: number
  symbol: string
  deal_type: string     // 'buy' | 'sell'
  price: number | null
  mt5_time: string | null  // ISO
}

interface MT5ChartProps {
  accountId: number
  defaultSymbol?: string
  positions?: MT5PositionForChart[]
  deals?: MT5DealForChart[]
  onSymbolChange?: (symbol: string) => void
  /** When set, used as fallback exchange if mtapi-io is unreachable (e.g. 'bitget') */
  fallbackExchange?: string
  /**
   * When true, uses MT5 forex/metals quick symbols (XAUUSD, EURUSD).
   * Set for real MT5 forex brokers like BTGT, ICMarkets, Pepperstone.
   * Overrides isCryptoExchange even when fallbackExchange is 'bitget'.
   */
  preferForexSymbols?: boolean
}

// ── Timeframe options ─────────────────────────────────────────────────────────

const TIMEFRAMES = [
  { label: 'M1',  value: 'M1'  },
  { label: 'M5',  value: 'M5'  },
  { label: 'M15', value: 'M15' },
  { label: 'M30', value: 'M30' },
  { label: 'H1',  value: 'H1'  },
  { label: 'H4',  value: 'H4'  },
  { label: 'D1',  value: 'D1'  },
  { label: 'W1',  value: 'W1'  },
]

// MT5 timeframe label → exchange API format
const MT5_TF_TO_EXCHANGE: Record<string, string> = {
  M1: '1m', M5: '5m', M15: '15m', M30: '30m',
  H1: '1h', H4: '4h', D1: '1d', W1: '1w',
}

// MT5 timeframe label → minutes (used to compute candle-start timestamps for live bar tracking)
const TF_MAP: Record<string, number> = {
  M1: 1, M5: 5, M15: 15, M30: 30,
  H1: 60, H4: 240, D1: 1440, W1: 10080,
}

// Crypto exchanges that trade USDT pairs, not USD
const CRYPTO_EXCHANGES = new Set(['bitget','binance','bybit','okx','kucoin','coinbase','huobi','gate'])

// Known forex/commodity MT5 symbols that have a USDT equivalent on crypto exchanges
const FOREX_TO_CRYPTO: Record<string, string> = {
  XAUUSD: 'XAUUSDT',   // Gold perp
  XAGUSD: 'XAGUDT',    // Silver perp
  BTCUSD: 'BTCUSDT',   // BTC/USD → BTC/USDT
  ETHUSD: 'ETHUSDT',
  BNBUSD: 'BNBUSDT',
  SOLUSD: 'SOLUSDT',
}

// Known pure forex pairs (not available on crypto exchanges)
const FOREX_PAIRS = new Set(['EURUSD','GBPUSD','USDJPY','USDCHF','AUDUSD','USDCAD','NZDUSD',
  'EURGBP','EURJPY','GBPJPY','XAUEUR','NAS100','US30','GER40','SPX500','UK100'])

/**
 * Normalise an MT5 symbol for exchange API lookup.
 * For crypto exchanges: converts USD quote to USDT (XAUUSD → XAUUSDT),
 * maps known forex/commodity pairs to their crypto equivalents, and adds slashes.
 */
function normaliseSymbolForExchange(sym: string, exchange?: string): string {
  const upper = sym.toUpperCase()
  const isCrypto = exchange ? CRYPTO_EXCHANGES.has(exchange.toLowerCase()) : false

  // Explicit forex→crypto map (highest priority)
  if (isCrypto && FOREX_TO_CRYPTO[upper]) return FOREX_TO_CRYPTO[upper].replace(/(\w+)(USDT|USDC|BTC|ETH)$/, '$1/$2')

  if (sym.includes('/')) return sym   // already has slash

  // For crypto: prefer USDT over USD as quote
  const quotes = isCrypto
    ? ['USDT', 'USDC', 'BTC', 'ETH', 'BNB', 'USD']
    : ['USDT', 'USDC', 'BUSD', 'USD', 'BTC', 'ETH', 'BNB', 'TRX']

  for (const q of quotes) {
    if (upper.endsWith(q) && sym.length > q.length) {
      const base = sym.slice(0, sym.length - q.length)
      // For crypto exchanges, upgrade USD → USDT when the pair likely trades as USDT
      const finalQuote = (isCrypto && q === 'USD') ? 'USDT' : q
      return `${base}/${finalQuote}`
    }
  }
  return sym
}

/**
 * Returns true if the symbol is a pure forex/index pair not available on crypto exchanges.
 */
function isForexOnlySymbol(sym: string): boolean {
  return FOREX_PAIRS.has(sym.toUpperCase())
}

// Symbol sets per context
const CRYPTO_QUICK_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'XAUUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'AVAXUSDT']
// MT5 forex quick symbols — XAUUSD first as gold is the most traded on BTGT/MT5 brokers
const MT5_QUICK_SYMBOLS    = ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD', 'USDCHF', 'XAGUSD', 'NAS100']

// ── Component ─────────────────────────────────────────────────────────────────

export default function MT5Chart({
  accountId,
  defaultSymbol = 'XAUUSD',
  positions = [],
  deals = [],
  onSymbolChange,
  fallbackExchange,
  preferForexSymbols = false,
}: MT5ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef     = useRef<IChartApi | null>(null)
  const seriesRef    = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const priceLineRefs = useRef<any[]>([])

  // isCryptoExchange: true only when the server is a pure crypto exchange (Bitget-Live etc.)
  // For MT5 forex brokers (BTGT, ICMarkets) that have Bitget as fallback, this is false.
  const isCryptoExchange = !preferForexSymbols &&
    fallbackExchange != null &&
    CRYPTO_EXCHANGES.has(fallbackExchange.toLowerCase())
  const quickSymbols     = isCryptoExchange ? CRYPTO_QUICK_SYMBOLS : MT5_QUICK_SYMBOLS

  const [symbol, setSymbol]           = useState(defaultSymbol)
  const [symbolInput, setSymbolInput] = useState(defaultSymbol)
  const [timeframe, setTimeframe]     = useState('H1')
  const [loading, setLoading]         = useState(true)
  const [error, setError]             = useState<string | null>(null)
  const [livePrice, setLivePrice]     = useState<{ bid: number; ask: number } | null>(null)
  const [candles, setCandles]         = useState<MT5Candle[]>([])
  const [lastUpdate, setLastUpdate]   = useState<Date | null>(null)
  const [showSymbolList, setShowSymbolList] = useState(false)
  const [dataSource, setDataSource]   = useState<string>('mt5')

  // Live bar tracking — the forming candle that updates with each tick
  const liveBarRef = useRef<{
    time: number; open: number; high: number; low: number; close: number
  } | null>(null)
  // Latest applied candle history (so the live poller can seed from the real last bar)
  const candlesRef = useRef<MT5Candle[]>([])
  // True until the chart has been fitted once for the current symbol/timeframe
  const needsFitRef = useRef(true)
  // Live price line drawn on the chart at current ask
  const livePriceLineRef = useRef<any>(null)

  // ── Chart init ──────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      width:  containerRef.current.clientWidth || 600,
      height: 420,
      layout: {
        background:  { color: '#0a0e27' },
        textColor:   '#d1d4dc',
      },
      grid: {
        vertLines: { color: '#1a1f3a' },
        horzLines: { color: '#1a1f3a' },
      },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: '#2a2f4a' },
      timeScale: {
        borderColor: '#2a2f4a',
        timeVisible: true,
        secondsVisible: false,
      },
    })

    const cs = chart.addCandlestickSeries({
      upColor:   '#22c55e',
      downColor: '#ef4444',
      borderUpColor:   '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor:   '#22c55e',
      wickDownColor: '#ef4444',
    })

    chartRef.current    = chart
    seriesRef.current   = cs

    // Keep the chart sized to its container. ResizeObserver catches layout
    // changes that a window 'resize' event misses (tab switch, sidebar toggle,
    // responsive reflow, or the container gaining width after mount). Without
    // this the chart can paint at 0 width and look blank/squished.
    const handleResize = () => {
      if (containerRef.current) {
        const width = containerRef.current.clientWidth
        if (width > 0) chart.applyOptions({ width })
      }
    }
    window.addEventListener('resize', handleResize)

    const resizeObserver = new ResizeObserver(handleResize)
    resizeObserver.observe(containerRef.current)

    return () => {
      window.removeEventListener('resize', handleResize)
      resizeObserver.disconnect()
      chart.remove()
    }
  }, [])

  // ── Fetch candles (MT5 first, exchange fallback on failure) ─────────────────

  /** Apply a raw candle array to the chart and update state. */
  const applyCandles = (raw: MT5Candle[]) => {
    if (!seriesRef.current || raw.length === 0) return
    candlesRef.current = raw
    const chartData = raw.map(c => ({ time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close }))
    seriesRef.current.setData(chartData)
    const allPrices = raw.flatMap(c => [c.open, c.high, c.low, c.close]).filter(p => p > 0)
    if (allPrices.length > 0) {
      const minP      = Math.min(...allPrices)
      const precision = minP < 0.001 ? 6 : minP < 0.01 ? 5 : minP < 1 ? 4 : minP < 10 ? 3 : 2
      seriesRef.current.applyOptions({ priceFormat: { type: 'price', precision, minMove: Math.pow(10, -precision) } })
    }
    // Only auto-fit the very first load for this symbol/timeframe — preserve the
    // user's zoom/pan on subsequent background reloads.
    if (needsFitRef.current) {
      chartRef.current?.timeScale().fitContent()
      needsFitRef.current = false
    }
    setLastUpdate(new Date())
  }

  const fetchCandles = useCallback(async () => {
    if (!accountId) return
    setError(null)

    // Clear stale candle data immediately so old symbol's bars don't linger
    if (seriesRef.current) seriesRef.current.setData([])

    // ── 1. Try MT5 via mtapi-io ───────────────────────────────────────────────
    try {
      const res = await apiClient.mt5.getCandles(accountId, symbol, timeframe, 300)
      const raw: MT5Candle[] = res.data?.candles ?? []
      if (raw.length > 0) {
        setCandles(raw)
        applyCandles(raw)
        setDataSource('mt5')
        setLoading(false)
        return
      }
    } catch (mt5Err: any) {
      const status: number = mt5Err?.response?.status ?? 0
      if (status === 404) {
        setError('Account not found.')
        setLoading(false)
        return
      }
      // 502 / network error → fall through to exchange fallback
    }

    // ── 2. Exchange fallback (Bitget / ccxt) ──────────────────────────────────
    if (fallbackExchange) {
      // Detect forex-only pairs and show a helpful message before even trying
      if (isForexOnlySymbol(symbol) && CRYPTO_EXCHANGES.has(fallbackExchange.toLowerCase())) {
        setError(
          `${symbol} is a forex/index pair — not available on ${fallbackExchange}. ` +
          `Try a crypto pair like BTCUSDT or ETHUSDT.`
        )
        setLoading(false)
        return
      }

      try {
        const exchangeTF = MT5_TF_TO_EXCHANGE[timeframe] ?? '1h'
        const normSym    = normaliseSymbolForExchange(symbol, fallbackExchange)
        const res        = await apiClient.getOHLCV(fallbackExchange, normSym, exchangeTF, 300)
        const raw: MT5Candle[] = (res.data?.data ?? []).map((c: any) => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume ?? null,
        }))
        if (raw.length > 0) {
          setCandles(raw)
          applyCandles(raw)
          setDataSource(fallbackExchange)
          setError(null)
          setLoading(false)
          return
        }
        setError(`No data from ${fallbackExchange} for ${normSym}. Try a crypto symbol like BTCUSDT.`)
      } catch (exchErr: any) {
        setError(
          `MT5 unreachable — ${fallbackExchange} fallback also failed: ` +
          (exchErr?.response?.data?.detail ?? exchErr?.message ?? 'unknown error')
        )
      }
    } else {
      setError('MT5 API unreachable. Set up mtapi-io or connect an exchange account for chart data.')
    }

    setLoading(false)
  }, [accountId, symbol, timeframe, fallbackExchange])

  useEffect(() => {
    setLoading(true)
    liveBarRef.current = null   // reset live bar when symbol/TF changes
    needsFitRef.current = true  // re-fit the chart for the new symbol/timeframe
    fetchCandles()
  }, [fetchCandles])

  // ── Real-time price polling ──────────────────────────────────────────────────
  // Polls GetQuote every 2s (backs off to 30s after 3 consecutive failures).
  // Tracks the forming candle (open/high/low/close) and detects new candle starts.
  // Also auto-reloads full candle history every 60s to fill any gaps.

  useEffect(() => {
    if (!accountId || loading) return
    let cancelled  = false
    let failStreak = 0
    let timeoutId: ReturnType<typeof setTimeout>
    let reloadTimer: ReturnType<typeof setInterval> | null = null

    // Candle period in seconds for the current timeframe
    const tfMin = TF_MAP[timeframe] ?? 60
    const tfSec = tfMin * 60

    /** Round a unix-second timestamp down to its candle-start time. */
    const candleStart = (ts: number) => Math.floor(ts / tfSec) * tfSec

    const poll = async () => {
      if (cancelled) return
      try {
        const res = await apiClient.mt5.getPrice(accountId, symbol)
        if (cancelled) return
        failStreak = 0

        const price = res.data as { bid: number; ask: number; time: number }
        const ask   = price.ask
        const bid   = price.bid
        const nowSec = Math.floor(Date.now() / 1000)
        const thisCandleTime = candleStart(nowSec)

        setLivePrice({ bid, ask })

        if (seriesRef.current) {
          // ── Live price line (dotted line at current ask) ──────────────────
          try {
            if (livePriceLineRef.current) {
              seriesRef.current.removePriceLine(livePriceLineRef.current)
            }
            livePriceLineRef.current = seriesRef.current.createPriceLine({
              price:     ask,
              color:     '#facc15',
              lineWidth: 1,
              lineStyle: LineStyle.Dotted,
              axisLabelVisible: true,
              title: `Live ${ask.toFixed(5)}`,
            })
          } catch { /* price line errors are non-fatal */ }

          // ── Forming candle update ─────────────────────────────────────────
          const prev = liveBarRef.current
          const hist = candlesRef.current
          const lastHist = hist.length > 0 ? hist[hist.length - 1] : null

          if (!prev || prev.time !== thisCandleTime) {
            // New candle period. Decide how to seed the forming bar:
            if (lastHist && lastHist.time === thisCandleTime) {
              // MT5 already returned this period as the (still-forming) last candle.
              // Continue it: keep its real open, extend high/low, set close to live price.
              liveBarRef.current = {
                time:  thisCandleTime,
                open:  lastHist.open,
                high:  Math.max(lastHist.high, ask),
                low:   Math.min(lastHist.low,  ask),
                close: ask,
              }
            } else {
              // Brand-new period not yet in history. Seed open from the previous
              // candle's close so candles connect cleanly (no flat/gapped open).
              const seedOpen = lastHist ? lastHist.close : ask
              liveBarRef.current = {
                time:  thisCandleTime,
                open:  seedOpen,
                high:  Math.max(seedOpen, ask),
                low:   Math.min(seedOpen, ask),
                close: ask,
              }
            }
          } else {
            // Tick within the same candle period — update OHLC
            liveBarRef.current = {
              ...prev,
              high:  Math.max(prev.high, ask),
              low:   Math.min(prev.low,  ask),
              close: ask,
            }
          }

          // Push the live bar to lightweight-charts
          seriesRef.current.update({
            time:  liveBarRef.current.time as Time,
            open:  liveBarRef.current.open,
            high:  liveBarRef.current.high,
            low:   liveBarRef.current.low,
            close: liveBarRef.current.close,
          })
        }
      } catch {
        failStreak++
      }

      if (!cancelled) {
        const delay = failStreak >= 3 ? 30_000 : document.hidden ? 15_000 : 2_000
        timeoutId = setTimeout(poll, delay)
      }
    }

    // Auto-reload full candle history every 60s to pick up closed bars
    reloadTimer = setInterval(() => {
      if (!cancelled) {
        liveBarRef.current = null   // reset so next tick initialises fresh live bar
        fetchCandles()
      }
    }, 60_000)

    poll()
    return () => {
      cancelled = true
      clearTimeout(timeoutId)
      if (reloadTimer) clearInterval(reloadTimer)
      // Remove live price line when unmounting
      if (seriesRef.current && livePriceLineRef.current) {
        try { seriesRef.current.removePriceLine(livePriceLineRef.current) } catch {}
        livePriceLineRef.current = null
      }
    }
  }, [accountId, symbol, timeframe, loading, fetchCandles])

  // ── Position / SL / TP price lines ─────────────────────────────────────────

  useEffect(() => {
    if (!seriesRef.current) return

    // Remove old price lines
    priceLineRefs.current.forEach(pl => {
      try { seriesRef.current?.removePriceLine(pl) } catch { /* already removed */ }
    })
    priceLineRefs.current = []

    // Draw lines only for positions matching the current symbol
    const active = positions.filter(p => p.symbol === symbol)
    active.forEach(pos => {
      const isBuy  = pos.side === 'buy'
      const entry  = pos.price_open
      const lotStr = `${pos.volume} lot${pos.volume !== 1 ? 's' : ''}`
      const pnlStr = pos.profit >= 0 ? `+$${pos.profit.toFixed(2)}` : `-$${Math.abs(pos.profit).toFixed(2)}`

      // Entry line
      const entryLine = seriesRef.current!.createPriceLine({
        price:      entry,
        color:      isBuy ? '#3b82f6' : '#f97316',
        lineWidth:  2,
        lineStyle:  LineStyle.Solid,
        axisLabelVisible: true,
        title:      `${isBuy ? '▲ BUY' : '▼ SELL'} #${pos.mt5_ticket} ${lotStr} ${pnlStr}`,
      })
      priceLineRefs.current.push(entryLine)

      // Stop Loss line
      if (pos.sl) {
        const slLine = seriesRef.current!.createPriceLine({
          price:     pos.sl,
          color:     '#ef4444',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title:     `SL #${pos.mt5_ticket}`,
        })
        priceLineRefs.current.push(slLine)
      }

      // Take Profit line
      if (pos.tp) {
        const tpLine = seriesRef.current!.createPriceLine({
          price:     pos.tp,
          color:     '#22c55e',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title:     `TP #${pos.mt5_ticket}`,
        })
        priceLineRefs.current.push(tpLine)
      }
    })
  }, [positions, symbol])

  // ── Deal markers ─────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!seriesRef.current || candles.length === 0) return

    const relevantDeals = deals
      .filter(d => d.symbol === symbol && d.price != null && d.mt5_time != null)
      .slice(-200) // cap at 200 markers

    if (relevantDeals.length === 0) return

    const markers: SeriesMarker<Time>[] = relevantDeals.map(d => {
      const ts = Math.floor(new Date(d.mt5_time!).getTime() / 1000)
      const isBuy = d.deal_type === 'buy'
      return {
        time:     ts as Time,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color:    isBuy ? '#3b82f6' : '#f97316',
        shape:    isBuy ? 'arrowUp' : 'arrowDown',
        text:     isBuy ? `B ${d.price?.toFixed(5) ?? ''}` : `S ${d.price?.toFixed(5) ?? ''}`,
        size:     1,
      } as SeriesMarker<Time>
    })

    try {
      seriesRef.current.setMarkers(markers.sort((a, b) => (a.time as number) - (b.time as number)))
    } catch {
      // marker errors are non-fatal (e.g. time outside chart range)
    }
  }, [deals, symbol, candles])

  // ── Symbol submit ─────────────────────────────────────────────────────────────

  const applySymbol = (s: string) => {
    let clean = s.trim().toUpperCase()
    if (!clean) return

    // On crypto exchanges, auto-correct common MT5 symbol formats:
    // XAUUSD → XAUUSDT, ETHUSD → ETHUSDT, etc.
    if (isCryptoExchange) {
      const upper = clean
      if (FOREX_TO_CRYPTO[upper]) {
        clean = FOREX_TO_CRYPTO[upper]   // already correct (XAUUSD → XAUUSDT)
      } else if (!upper.includes('/') && upper.endsWith('USD') && !upper.endsWith('USDT') && !upper.endsWith('USDC')) {
        clean = upper.slice(0, -3) + 'USDT'
      }
    }

    setSymbol(clean)
    setSymbolInput(clean)
    setShowSymbolList(false)
    onSymbolChange?.(clean)
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="bg-gray-900/50 border border-gray-700/50 rounded-xl overflow-hidden flex flex-col">
      {/* Chart toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-700/50 flex-wrap">
        {/* Symbol input */}
        <div className="relative">
          <div className="flex items-center bg-gray-800 border border-gray-600 rounded-lg overflow-hidden">
            <Search className="w-3.5 h-3.5 text-gray-400 ml-2 flex-shrink-0" />
            <input
              value={symbolInput}
              onChange={e => setSymbolInput(e.target.value.toUpperCase())}
              onKeyDown={e => e.key === 'Enter' && applySymbol(symbolInput)}
              onFocus={() => setShowSymbolList(true)}
              onBlur={() => setTimeout(() => setShowSymbolList(false), 200)}
              className="bg-transparent text-white text-sm px-2 py-1 w-24 focus:outline-none"
              placeholder="Symbol"
            />
            <button
              onClick={() => applySymbol(symbolInput)}
              className="px-2 py-1 text-xs text-gray-400 hover:text-white"
            >
              <ChevronDown className="w-3 h-3" />
            </button>
          </div>
          {showSymbolList && (
            <div className="absolute top-full left-0 mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl z-50 min-w-36">
              <div className="px-3 py-1 text-xs text-gray-500 border-b border-gray-700/50">
                {isCryptoExchange ? 'Crypto pairs' : 'FX / Metals'}
              </div>
              {quickSymbols.map(s => (
                <button
                  key={s}
                  className="w-full text-left px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 hover:text-white"
                  onClick={() => applySymbol(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Timeframe tabs */}
        <div className="flex gap-0.5 bg-gray-800/70 rounded-lg p-0.5">
          {TIMEFRAMES.map(tf => (
            <button
              key={tf.value}
              onClick={() => setTimeframe(tf.value)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                timeframe === tf.value
                  ? 'bg-tradebot-accent/30 text-tradebot-accent'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              {tf.label}
            </button>
          ))}
        </div>

        {/* Live price + pulse dot */}
        {livePrice && (
          <div className="flex items-center gap-2 text-xs">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500"></span>
            </span>
            <span className="text-gray-400">Bid</span>
            <span className="font-mono text-white font-medium">{livePrice.bid.toFixed(5)}</span>
            <span className="text-gray-500">│</span>
            <span className="text-gray-400">Ask</span>
            <span className="font-mono text-yellow-300 font-medium">{livePrice.ask.toFixed(5)}</span>
          </div>
        )}

        {/* Data source badge */}
        <div className="ml-auto flex items-center gap-2">
          {dataSource !== 'mt5' ? (
            <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-yellow-900/40 text-yellow-400 border border-yellow-700/40">
              {dataSource.toUpperCase()} data
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-green-900/40 text-green-400 border border-green-700/40">
              MT5 live
            </span>
          )}
        </div>

        {/* Refresh */}
        <button
          onClick={() => { setLoading(true); fetchCandles() }}
          disabled={loading}
          className="p-1.5 rounded hover:bg-gray-700 transition-colors text-gray-400 hover:text-white disabled:opacity-50"
          title="Reload candles"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Chart area */}
      <div className="relative">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-gray-900/70 z-10">
            <div className="flex items-center gap-2 text-gray-400 text-sm">
              <RefreshCw className="w-4 h-4 animate-spin" />
              Loading {symbol} {timeframe}…
            </div>
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-gray-900/70 z-10">
            <div className="text-center text-sm text-gray-400 max-w-sm px-5">
              <div className="text-yellow-400 mb-2 font-medium">No chart data</div>
              <div className="mb-3">{error}</div>
              {isCryptoExchange && (
                <div className="flex flex-wrap gap-1.5 justify-center">
                  {CRYPTO_QUICK_SYMBOLS.slice(0, 4).map(s => (
                    <button
                      key={s}
                      onClick={() => applySymbol(s)}
                      className="px-2.5 py-1 rounded-lg bg-gray-700 text-gray-200 text-xs hover:bg-tradebot-accent/30 hover:text-tradebot-accent transition-colors"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
        <div ref={containerRef} style={{ height: 420 }} />
      </div>

      {/* Footer: last update + source + position count */}
      <div className="px-3 py-1.5 border-t border-gray-700/30 flex items-center gap-3 text-xs text-gray-500">
        <span>{symbol} · {timeframe}</span>
        {lastUpdate && (
          <span>Updated {formatTimeZA(lastUpdate)}</span>
        )}
        {dataSource !== 'mt5' && (
          <span className="text-yellow-400/80">via {dataSource} (mtapi-io not running)</span>
        )}
        {positions.filter(p => p.symbol === symbol).length > 0 && (
          <span className="text-blue-400 ml-auto">
            {positions.filter(p => p.symbol === symbol).length} position(s) overlaid
          </span>
        )}
      </div>
    </div>
  )
}
