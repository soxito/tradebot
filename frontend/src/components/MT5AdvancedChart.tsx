/**
 * MT5AdvancedChart — TradingView-style advanced chart for the MT5 Live page.
 *
 * Built on lightweight-charts with a composed two-pane workspace:
 *   • Main price pane  : candlesticks (or line), EMA overlays, volume overlay,
 *                        auto support/resistance levels, manual horizontal levels,
 *                        position entry / SL / TP lines, deal markers, live price.
 *   • Lower study pane : RSI(14) oscillator, time-synced with the price pane.
 *
 * Data: MT5 terminal (mtapi-io) is the primary source. When it is empty/unreachable
 * the component auto-selects the best available source (configured exchange fallback)
 * and surfaces the decision + reason in the header badge.
 *
 * This component is additive — the classic MT5Chart remains available behind a toggle.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import {
  createChart, IChartApi, ISeriesApi, SeriesMarker, Time, LineStyle,
  CrosshairMode, ColorType,
} from 'lightweight-charts'
import { apiClient } from '@/services/api'
import {
  Search, RefreshCw, ChevronDown, TrendingUp, CandlestickChart, LineChart,
  Maximize2, Minimize2, Ruler, Eraser, Activity,
} from 'lucide-react'
import { formatTimeZA } from '@/utils/datetime'
import { pollMultiplier } from '@/utils/devicePerformance'

// ── Types ────────────────────────────────────────────────────────────────────

interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume?: number | null
}

export interface MT5PositionForChart {
  id: number
  symbol: string
  side: string
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
  deal_type: string
  price: number | null
  mt5_time: string | null
}

/** Why the chart is showing data from a particular source. */
type SourceReason = 'mt5-live' | 'mt5-stale' | 'fallback-empty' | 'fallback-error' | 'forex-only' | 'no-source'

interface MT5AdvancedChartProps {
  accountId: number
  defaultSymbol?: string
  positions?: MT5PositionForChart[]
  deals?: MT5DealForChart[]
  onSymbolChange?: (symbol: string) => void
  fallbackExchange?: string
  preferForexSymbols?: boolean
  /** Optional quick-trade hook fired by the header BUY/SELL buttons. */
  onQuickTrade?: (operation: 'buy' | 'sell', symbol: string, volume: number) => void | Promise<void>
}

// ── Constants ────────────────────────────────────────────────────────────────

const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'] as const

const MT5_TF_TO_EXCHANGE: Record<string, string> = {
  M1: '1m', M5: '5m', M15: '15m', M30: '30m', H1: '1h', H4: '4h', D1: '1d', W1: '1w',
}
const TF_MINUTES: Record<string, number> = {
  M1: 1, M5: 5, M15: 15, M30: 30, H1: 60, H4: 240, D1: 1440, W1: 10080,
}

/** Default framing applied whenever a new symbol/timeframe is loaded. */
const VISIBLE_BARS = 60   // recent candles visible by default
const RIGHT_PAD    = 5    // empty bars after the newest candle

const CRYPTO_EXCHANGES = new Set(['bitget', 'binance', 'bybit', 'okx', 'kucoin', 'coinbase', 'huobi', 'gate'])

const FOREX_TO_CRYPTO: Record<string, string> = {
  XAUUSD: 'XAUUSDT',
  XAGUSD: 'XAGUSDT',
  BTCUSD: 'BTCUSDT',
  ETHUSD: 'ETHUSDT',
  BNBUSD: 'BNBUSDT',
  SOLUSD: 'SOLUSDT',
}
const FOREX_PAIRS = new Set([
  'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD',
  'EURGBP', 'EURJPY', 'GBPJPY', 'XAUEUR', 'NAS100', 'US30', 'GER40', 'SPX500', 'UK100',
])

const CRYPTO_QUICK = ['BTCUSDT', 'ETHUSDT', 'XAUUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'AVAXUSDT']
const MT5_QUICK = ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD', 'USDCHF', 'XAGUSD', 'NAS100']

// Theme — tuned for parity with TradingView dark.
const THEME = {
  bg: '#0b0e16',
  grid: '#161b2b',
  text: '#a9b1c4',
  border: '#222838',
  up: '#26a69a',
  down: '#ef5350',
  ema9: '#f5c542',
  ema21: '#42a5f5',
  ema50: '#ab47bc',
  volUp: 'rgba(38,166,154,0.4)',
  volDown: 'rgba(239,83,80,0.4)',
  rsi: '#e2e8f0',
  level: '#64748b',
  resistance: '#ef5350',
  support: '#26a69a',
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function normaliseSymbolForExchange(sym: string, exchange?: string): string {
  const upper = sym.toUpperCase()
  const isCrypto = exchange ? CRYPTO_EXCHANGES.has(exchange.toLowerCase()) : false
  if (isCrypto && FOREX_TO_CRYPTO[upper]) {
    return FOREX_TO_CRYPTO[upper].replace(/(\w+)(USDT|USDC|BTC|ETH)$/, '$1/$2')
  }
  if (sym.includes('/')) return sym
  const quotes = isCrypto
    ? ['USDT', 'USDC', 'BTC', 'ETH', 'BNB', 'USD']
    : ['USDT', 'USDC', 'BUSD', 'USD', 'BTC', 'ETH', 'BNB', 'TRX']
  for (const q of quotes) {
    if (upper.endsWith(q) && sym.length > q.length) {
      const base = sym.slice(0, sym.length - q.length)
      const finalQuote = (isCrypto && q === 'USD') ? 'USDT' : q
      return `${base}/${finalQuote}`
    }
  }
  return sym
}

const isForexOnly = (sym: string) => FOREX_PAIRS.has(sym.toUpperCase())

function precisionFor(prices: number[]): number {
  const valid = prices.filter(p => p > 0)
  if (valid.length === 0) return 2
  const minP = Math.min(...valid)
  return minP < 0.001 ? 6 : minP < 0.01 ? 5 : minP < 1 ? 4 : minP < 10 ? 3 : 2
}

/** Exponential moving average over candle closes. Returns time/value points. */
function ema(candles: Candle[], period: number): { time: Time; value: number }[] {
  if (candles.length < period) return []
  const k = 2 / (period + 1)
  const out: { time: Time; value: number }[] = []
  let prev = candles.slice(0, period).reduce((s, c) => s + c.close, 0) / period
  out.push({ time: candles[period - 1].time as Time, value: prev })
  for (let i = period; i < candles.length; i++) {
    prev = candles[i].close * k + prev * (1 - k)
    out.push({ time: candles[i].time as Time, value: prev })
  }
  return out
}

/** Wilder's RSI over candle closes. */
function rsi(candles: Candle[], period = 14): { time: Time; value: number }[] {
  if (candles.length <= period) return []
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i++) {
    const diff = candles[i].close - candles[i - 1].close
    if (diff >= 0) gain += diff
    else loss -= diff
  }
  let avgGain = gain / period
  let avgLoss = loss / period
  const out: { time: Time; value: number }[] = []
  const push = (i: number) => {
    const rs = avgLoss === 0 ? 100 : avgGain / avgLoss
    out.push({ time: candles[i].time as Time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + rs) })
  }
  push(period)
  for (let i = period + 1; i < candles.length; i++) {
    const diff = candles[i].close - candles[i - 1].close
    const g = diff > 0 ? diff : 0
    const l = diff < 0 ? -diff : 0
    avgGain = (avgGain * (period - 1) + g) / period
    avgLoss = (avgLoss * (period - 1) + l) / period
    push(i)
  }
  return out
}

/**
 * Detect support/resistance levels from recent swing pivots and cluster nearby
 * pivots into levels. Returns up to `max` levels with a type hint.
 */
function detectLevels(candles: Candle[], max = 4): { price: number; type: 'support' | 'resistance' }[] {
  if (candles.length < 30) return []
  const lookback = 3
  const recent = candles.slice(-160)
  const pivots: { price: number; type: 'support' | 'resistance' }[] = []
  for (let i = lookback; i < recent.length - lookback; i++) {
    const h = recent[i].high
    const l = recent[i].low
    let isHigh = true
    let isLow = true
    for (let j = 1; j <= lookback; j++) {
      if (recent[i - j].high >= h || recent[i + j].high >= h) isHigh = false
      if (recent[i - j].low <= l || recent[i + j].low <= l) isLow = false
    }
    if (isHigh) pivots.push({ price: h, type: 'resistance' })
    if (isLow) pivots.push({ price: l, type: 'support' })
  }
  if (pivots.length === 0) return []
  const last = recent[recent.length - 1].close
  const span = Math.max(...recent.map(c => c.high)) - Math.min(...recent.map(c => c.low))
  const tol = span * 0.004
  const clusters: { price: number; type: 'support' | 'resistance'; count: number }[] = []
  for (const p of pivots) {
    const hit = clusters.find(c => Math.abs(c.price - p.price) <= tol)
    if (hit) {
      hit.price = (hit.price * hit.count + p.price) / (hit.count + 1)
      hit.count += 1
      if (p.type === 'resistance' && p.price >= last) hit.type = 'resistance'
      if (p.type === 'support' && p.price <= last) hit.type = 'support'
    } else {
      clusters.push({ price: p.price, type: p.price >= last ? 'resistance' : 'support', count: 1 })
    }
  }
  return clusters
    .sort((a, b) => b.count - a.count)
    .slice(0, max)
    .map(c => ({ price: c.price, type: c.type }))
}

// ── Component ────────────────────────────────────────────────────────────────

export default function MT5AdvancedChart({
  accountId,
  defaultSymbol = 'XAUUSD',
  positions = [],
  deals = [],
  onSymbolChange,
  fallbackExchange,
  preferForexSymbols = false,
  onQuickTrade,
}: MT5AdvancedChartProps) {
  const priceRef = useRef<HTMLDivElement>(null)
  const studyRef = useRef<HTMLDivElement>(null)

  const priceChart = useRef<IChartApi | null>(null)
  const studyChart = useRef<IChartApi | null>(null)
  const candleSeries = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const lineSeries = useRef<ISeriesApi<'Line'> | null>(null)
  const volSeries = useRef<ISeriesApi<'Histogram'> | null>(null)
  const ema9Series = useRef<ISeriesApi<'Line'> | null>(null)
  const ema21Series = useRef<ISeriesApi<'Line'> | null>(null)
  const ema50Series = useRef<ISeriesApi<'Line'> | null>(null)
  const rsiSeries = useRef<ISeriesApi<'Line'> | null>(null)

  const positionLines = useRef<any[]>([])
  const levelLines = useRef<any[]>([])
  const manualLines = useRef<any[]>([])
  const livePriceLine = useRef<any>(null)

  const liveBar = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null)
  const candlesRef = useRef<Candle[]>([])
  const needsFit = useRef(true)
  const syncing = useRef(false)

  const isCryptoExchange = !preferForexSymbols && fallbackExchange != null &&
    CRYPTO_EXCHANGES.has(fallbackExchange.toLowerCase())
  const quickSymbols = isCryptoExchange ? CRYPTO_QUICK : MT5_QUICK

  const [symbol, setSymbol] = useState(defaultSymbol)
  const [symbolInput, setSymbolInput] = useState(defaultSymbol)
  const [timeframe, setTimeframe] = useState('H1')
  const [chartType, setChartType] = useState<'candles' | 'line'>('candles')
  const [showEMA, setShowEMA] = useState(true)
  const [showLevels, setShowLevels] = useState(true)
  const [showVolume, setShowVolume] = useState(true)
  const [showRSI, setShowRSI] = useState(true)
  const [drawMode, setDrawMode] = useState(false)
  const [maximized, setMaximized] = useState(false)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [livePrice, setLivePrice] = useState<{ bid: number; ask: number } | null>(null)
  const [candles, setCandles] = useState<Candle[]>([])
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [showSymbolList, setShowSymbolList] = useState(false)
  const [dataSource, setDataSource] = useState<string>('mt5')
  const [sourceReason, setSourceReason] = useState<SourceReason>('mt5-live')
  const [tradeBusy, setTradeBusy] = useState<'buy' | 'sell' | null>(null)
  const [lot, setLot] = useState('0.01')

  const priceHeight = maximized ? () => window.innerHeight - 230 : () => 460

  // ── Chart construction ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!priceRef.current || !studyRef.current) return

    const baseLayout = {
      background: { type: ColorType.Solid, color: THEME.bg },
      textColor: THEME.text,
      fontSize: 11,
    }
    const grid = { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } }

    const pc = createChart(priceRef.current, {
      width: priceRef.current.clientWidth,
      height: priceRef.current.clientHeight || priceHeight(),
      layout: baseLayout,
      grid,
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.border, scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
    })
    const sc = createChart(studyRef.current, {
      width: studyRef.current.clientWidth,
      height: studyRef.current.clientHeight || 140,
      layout: baseLayout,
      grid,
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.border, scaleMargins: { top: 0.15, bottom: 0.15 } },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
    })

    const cs = pc.addCandlestickSeries({
      upColor: THEME.up, downColor: THEME.down,
      borderUpColor: THEME.up, borderDownColor: THEME.down,
      wickUpColor: THEME.up, wickDownColor: THEME.down,
    })
    const ls = pc.addLineSeries({ color: '#60a5fa', lineWidth: 2, visible: false })
    const vol = pc.addHistogramSeries({
      priceScaleId: 'vol', priceFormat: { type: 'volume' }, color: THEME.volUp,
    })
    pc.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })

    const e9 = pc.addLineSeries({ color: THEME.ema9, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
    const e21 = pc.addLineSeries({ color: THEME.ema21, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
    const e50 = pc.addLineSeries({ color: THEME.ema50, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })

    const rsiS = sc.addLineSeries({ color: THEME.rsi, lineWidth: 1, priceLineVisible: false, lastValueVisible: true })
    rsiS.createPriceLine({ price: 70, color: 'rgba(239,83,80,0.5)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '70' })
    rsiS.createPriceLine({ price: 30, color: 'rgba(38,166,154,0.5)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '30' })

    priceChart.current = pc
    studyChart.current = sc
    candleSeries.current = cs
    lineSeries.current = ls
    volSeries.current = vol
    ema9Series.current = e9
    ema21Series.current = e21
    ema50Series.current = e50
    rsiSeries.current = rsiS

    // Time-scale sync between the price and study panes (guarded against loops).
    const linkRange = (from: IChartApi, to: IChartApi) =>
      from.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (syncing.current || !range) return
        syncing.current = true
        to.timeScale().setVisibleLogicalRange(range)
        syncing.current = false
      })
    linkRange(pc, sc)
    linkRange(sc, pc)

    // Manual resize handler (avoids autoSize ResizeObserver firing after disposal)
    const handleResize = () => {
      if (priceRef.current && priceChart.current) {
        try { priceChart.current.applyOptions({ width: priceRef.current.clientWidth }) } catch {}
      }
      if (studyRef.current && studyChart.current) {
        try { studyChart.current.applyOptions({ width: studyRef.current.clientWidth }) } catch {}
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      try { pc.remove() } catch {}
      try { sc.remove() } catch {}
      priceChart.current = null
      studyChart.current = null
    }
  }, [])

  // Toggle study pane visibility by resizing — handled in render via height.

  /**
   * Wipe every series when the new symbol has no data. Without this the
   * previous pair's candles, EMAs and volume stay on screen — and keep their
   * price scale — so a switch to a pair the source can't serve looks like the
   * chart is still stuck on the old symbol.
   */
  const clearSeries = useCallback(() => {
    candlesRef.current = []
    liveBar.current = null
    try {
      candleSeries.current?.setData([])
      lineSeries.current?.setData([])
      volSeries.current?.setData([])
      ema9Series.current?.setData([])
      ema21Series.current?.setData([])
      ema50Series.current?.setData([])
      rsiSeries.current?.setData([])
    } catch { /* chart disposed */ }
  }, [])

  // ── Apply candle data + indicators ──────────────────────────────────────────
  const applyCandles = useCallback((raw: Candle[]) => {
    candlesRef.current = raw
    const cs = candleSeries.current
    const ls = lineSeries.current
    if (!cs || raw.length === 0) return

    cs.setData(raw.map(c => ({ time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close })))
    ls?.setData(raw.map(c => ({ time: c.time as Time, value: c.close })))

    const prec = precisionFor(raw.flatMap(c => [c.open, c.high, c.low, c.close]))
    const fmt = { type: 'price' as const, precision: prec, minMove: Math.pow(10, -prec) }
    cs.applyOptions({ priceFormat: fmt })
    ls?.applyOptions({ priceFormat: fmt })

    // Volume
    if (volSeries.current) {
      volSeries.current.setData(raw.map(c => ({
        time: c.time as Time,
        value: c.volume ?? 0,
        color: c.close >= c.open ? THEME.volUp : THEME.volDown,
      })))
    }
    // EMAs
    ema9Series.current?.setData(ema(raw, 9))
    ema21Series.current?.setData(ema(raw, 21))
    ema50Series.current?.setData(ema(raw, 50))
    // RSI
    rsiSeries.current?.setData(rsi(raw, 14))

    if (needsFit.current) {
      // Frame the newest bars of the new symbol rather than all ~400 of them,
      // and re-enable vertical auto-scaling: lightweight-charts switches
      // autoScale off for good once the user drags the price axis, which would
      // otherwise keep the previous pair's price range (e.g. XAUUSD's ~3300)
      // and squash the incoming series into a flat line.
      const frame = () => {
        try {
          candleSeries.current?.priceScale().applyOptions({ autoScale: true })
          const range = {
            from: Math.max(0, raw.length - VISIBLE_BARS),
            to: raw.length - 1 + RIGHT_PAD,
          }
          // The panes are range-linked, so setting the price chart syncs the study one.
          priceChart.current?.timeScale().setVisibleLogicalRange(range)
        } catch { /* chart disposed */ }
      }
      frame()
      // setData restores the old scroll position a frame later — re-pin it.
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(frame)
      needsFit.current = false
    }
    setLastUpdate(new Date())
  }, [])

  // ── Source-arbitrated candle fetch ──────────────────────────────────────────
  const fetchCandles = useCallback(async () => {
    if (!accountId) return
    setError(null)
    candleSeries.current?.setData([])
    lineSeries.current?.setData([])

    const tfSec = (TF_MINUTES[timeframe] ?? 60) * 60

    // 1) MT5 primary
    try {
      const res = await apiClient.mt5.getCandles(accountId, symbol, timeframe, 400)
      const raw: Candle[] = res.data?.candles ?? []
      if (raw.length > 0) {
        const lastTs = raw[raw.length - 1].time
        const ageSec = Date.now() / 1000 - lastTs
        const stale = ageSec > tfSec * 6
        setCandles(raw)
        applyCandles(raw)
        setDataSource('mt5')
        setSourceReason(stale ? 'mt5-stale' : 'mt5-live')
        setLoading(false)
        return
      }
    } catch (e: any) {
      if (e?.response?.status === 404) {
        setError('Account not found.')
        setLoading(false)
        return
      }
      // fall through to fallback
    }

    // 2) Exchange fallback (best available source)
    if (fallbackExchange) {
      if (isForexOnly(symbol) && CRYPTO_EXCHANGES.has(fallbackExchange.toLowerCase())) {
        clearSeries()
        setSourceReason('forex-only')
        setError(`${symbol} is a forex/index pair — not available on ${fallbackExchange}. Try BTCUSDT or ETHUSDT.`)
        setLoading(false)
        return
      }
      try {
        const tf = MT5_TF_TO_EXCHANGE[timeframe] ?? '1h'
        const sym = normaliseSymbolForExchange(symbol, fallbackExchange)
        const res = await apiClient.getOHLCV(fallbackExchange, sym, tf, 400)
        const raw: Candle[] = (res.data?.data ?? []).map((c: any) => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume ?? null,
        }))
        if (raw.length > 0) {
          setCandles(raw)
          applyCandles(raw)
          setDataSource(fallbackExchange)
          setSourceReason('fallback-empty')
          setError(null)
          setLoading(false)
          return
        }
        clearSeries()
        setSourceReason('fallback-empty')
        setError(`No data from ${fallbackExchange} for ${sym}.`)
      } catch (e: any) {
        clearSeries()
        setSourceReason('fallback-error')
        setError(`MT5 unreachable — ${fallbackExchange} fallback failed: ${e?.response?.data?.detail ?? e?.message ?? 'unknown error'}`)
      }
    } else {
      clearSeries()
      setSourceReason('no-source')
      setError('MT5 API unreachable. Configure mtapi-io or attach an exchange account for chart data.')
    }
    setLoading(false)
  }, [accountId, symbol, timeframe, fallbackExchange, applyCandles, clearSeries])

  useEffect(() => {
    setLoading(true)
    liveBar.current = null
    needsFit.current = true
    fetchCandles()
  }, [fetchCandles])

  // ── Live price polling + forming bar ────────────────────────────────────────
  useEffect(() => {
    if (!accountId || loading) return
    let cancelled = false
    let failStreak = 0
    let timeoutId: ReturnType<typeof setTimeout>

    const tfSec = (TF_MINUTES[timeframe] ?? 60) * 60
    const candleStart = (ts: number) => Math.floor(ts / tfSec) * tfSec

    const poll = async () => {
      if (cancelled) return
      try {
        const res = await apiClient.mt5.getPrice(accountId, symbol)
        if (cancelled) return
        failStreak = 0
        const { bid, ask } = res.data as { bid: number; ask: number; time: number }
        setLivePrice({ bid, ask })
        const cs = candleSeries.current
        const ls = lineSeries.current
        const thisCandle = candleStart(Math.floor(Date.now() / 1000))

        if (cs) {
          try {
            if (livePriceLine.current) cs.removePriceLine(livePriceLine.current)
            livePriceLine.current = cs.createPriceLine({
              price: ask, color: '#facc15', lineWidth: 1, lineStyle: LineStyle.Dotted,
              axisLabelVisible: true, title: 'Live',
            })
          } catch { /* non-fatal */ }

          const prev = liveBar.current
          const hist = candlesRef.current
          const lastHist = hist.length ? hist[hist.length - 1] : null
          if (!prev || prev.time !== thisCandle) {
            if (lastHist && lastHist.time === thisCandle) {
              liveBar.current = { time: thisCandle, open: lastHist.open, high: Math.max(lastHist.high, ask), low: Math.min(lastHist.low, ask), close: ask }
            } else {
              const seed = lastHist ? lastHist.close : ask
              liveBar.current = { time: thisCandle, open: seed, high: Math.max(seed, ask), low: Math.min(seed, ask), close: ask }
            }
          } else {
            liveBar.current = { ...prev, high: Math.max(prev.high, ask), low: Math.min(prev.low, ask), close: ask }
          }
          const b = liveBar.current
          cs.update({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })
          ls?.update({ time: b.time as Time, value: b.close })
        }
      } catch {
        failStreak++
      }
      if (!cancelled) timeoutId = setTimeout(poll, failStreak >= 3 ? 30_000 * pollMultiplier() : document.hidden ? 15_000 * pollMultiplier() : 2_000 * pollMultiplier())
    }

    const reload = setInterval(() => {
      if (!cancelled) { liveBar.current = null; fetchCandles() }
    }, 60_000 * pollMultiplier())

    poll()
    return () => {
      cancelled = true
      clearTimeout(timeoutId)
      clearInterval(reload)
      if (candleSeries.current && livePriceLine.current) {
        try { candleSeries.current.removePriceLine(livePriceLine.current) } catch {}
        livePriceLine.current = null
      }
    }
  }, [accountId, symbol, timeframe, loading, fetchCandles])

  // ── Indicator visibility toggles ────────────────────────────────────────────
  useEffect(() => {
    candleSeries.current?.applyOptions({ visible: chartType === 'candles' })
    lineSeries.current?.applyOptions({ visible: chartType === 'line' })
  }, [chartType])

  useEffect(() => {
    ema9Series.current?.applyOptions({ visible: showEMA })
    ema21Series.current?.applyOptions({ visible: showEMA })
    ema50Series.current?.applyOptions({ visible: showEMA })
  }, [showEMA])

  useEffect(() => { volSeries.current?.applyOptions({ visible: showVolume }) }, [showVolume])

  // ── Support / resistance levels ─────────────────────────────────────────────
  useEffect(() => {
    const cs = candleSeries.current
    if (!cs) return
    levelLines.current.forEach(l => { try { cs.removePriceLine(l) } catch {} })
    levelLines.current = []
    if (!showLevels || candles.length === 0) return
    detectLevels(candles).forEach((lvl, i) => {
      const isR = lvl.type === 'resistance'
      const line = cs.createPriceLine({
        price: lvl.price,
        color: isR ? THEME.resistance : THEME.support,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `${isR ? 'R' : 'S'}${i + 1}`,
      })
      levelLines.current.push(line)
    })
  }, [candles, showLevels])

  // ── Position / SL / TP overlays ─────────────────────────────────────────────
  useEffect(() => {
    const cs = candleSeries.current
    if (!cs) return
    positionLines.current.forEach(l => { try { cs.removePriceLine(l) } catch {} })
    positionLines.current = []
    positions.filter(p => p.symbol === symbol).forEach(pos => {
      const isBuy = pos.side === 'buy'
      const pnl = pos.profit >= 0 ? `+$${pos.profit.toFixed(2)}` : `-$${Math.abs(pos.profit).toFixed(2)}`
      positionLines.current.push(cs.createPriceLine({
        price: pos.price_open, color: isBuy ? '#3b82f6' : '#f97316', lineWidth: 2,
        lineStyle: LineStyle.Solid, axisLabelVisible: true,
        title: `${isBuy ? '▲ BUY' : '▼ SELL'} #${pos.mt5_ticket} ${pos.volume}l ${pnl}`,
      }))
      if (pos.sl) positionLines.current.push(cs.createPriceLine({ price: pos.sl, color: THEME.down, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: `SL #${pos.mt5_ticket}` }))
      if (pos.tp) positionLines.current.push(cs.createPriceLine({ price: pos.tp, color: THEME.up, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: `TP #${pos.mt5_ticket}` }))
    })
  }, [positions, symbol])

  // ── Deal markers ────────────────────────────────────────────────────────────
  useEffect(() => {
    const cs = candleSeries.current
    if (!cs || candles.length === 0) return
    const relevant = deals.filter(d => d.symbol === symbol && d.price != null && d.mt5_time != null).slice(-200)
    const markers: SeriesMarker<Time>[] = relevant.map(d => {
      const ts = Math.floor(new Date(d.mt5_time!).getTime() / 1000)
      const isBuy = d.deal_type === 'buy'
      return {
        time: ts as Time, position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#3b82f6' : '#f97316', shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${isBuy ? 'B' : 'S'} ${d.price?.toFixed(5) ?? ''}`, size: 1,
      } as SeriesMarker<Time>
    })
    try { cs.setMarkers(markers.sort((a, b) => (a.time as number) - (b.time as number))) } catch {}
  }, [deals, symbol, candles])

  // ── Manual horizontal level drawing ─────────────────────────────────────────
  useEffect(() => {
    const pc = priceChart.current
    const cs = candleSeries.current
    if (!pc || !cs) return
    const handler = (param: any) => {
      if (!drawMode || !param.point) return
      const price = cs.coordinateToPrice(param.point.y)
      if (price == null) return
      const line = cs.createPriceLine({
        price, color: '#22d3ee', lineWidth: 1, lineStyle: LineStyle.Solid,
        axisLabelVisible: true, title: 'Level',
      })
      manualLines.current.push(line)
      setDrawMode(false)
    }
    pc.subscribeClick(handler)
    return () => { try { pc.unsubscribeClick(handler) } catch {} }
  }, [drawMode])

  const clearManualLevels = () => {
    const cs = candleSeries.current
    manualLines.current.forEach(l => { try { cs?.removePriceLine(l) } catch {} })
    manualLines.current = []
  }

  // ── Symbol apply ────────────────────────────────────────────────────────────
  const applySymbol = (s: string) => {
    let clean = s.trim().toUpperCase()
    if (!clean) return
    if (isCryptoExchange) {
      if (FOREX_TO_CRYPTO[clean]) clean = FOREX_TO_CRYPTO[clean]
      else if (!clean.includes('/') && clean.endsWith('USD') && !clean.endsWith('USDT') && !clean.endsWith('USDC')) {
        clean = clean.slice(0, -3) + 'USDT'
      }
    }
    setSymbol(clean)
    setSymbolInput(clean)
    setShowSymbolList(false)
    onSymbolChange?.(clean)
  }

  const quickTrade = async (op: 'buy' | 'sell') => {
    if (!onQuickTrade) return
    setTradeBusy(op)
    try { await onQuickTrade(op, symbol, parseFloat(lot) || 0.01) } finally { setTradeBusy(null) }
  }

  const sourceBadge = () => {
    const map: Record<SourceReason, { text: string; cls: string }> = {
      'mt5-live': { text: 'MT5 LIVE', cls: 'bg-green-900/40 text-green-400 border-green-700/40' },
      'mt5-stale': { text: 'MT5 (delayed)', cls: 'bg-amber-900/40 text-amber-300 border-amber-700/40' },
      'fallback-empty': { text: `${dataSource.toUpperCase()} fallback`, cls: 'bg-yellow-900/40 text-yellow-400 border-yellow-700/40' },
      'fallback-error': { text: 'No source', cls: 'bg-red-900/40 text-red-400 border-red-700/40' },
      'forex-only': { text: 'FX only', cls: 'bg-red-900/40 text-red-400 border-red-700/40' },
      'no-source': { text: 'No source', cls: 'bg-red-900/40 text-red-400 border-red-700/40' },
    }
    const b = map[sourceReason]
    return <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${b.cls}`}>{b.text}</span>
  }

  const pricePrec = candles.length ? precisionFor(candles.flatMap(c => [c.open, c.high, c.low, c.close])) : 5

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div className={`bg-[#0b0e16] border border-gray-700/50 rounded-xl overflow-hidden flex flex-col ${maximized ? 'fixed inset-3 z-50 shadow-2xl' : ''}`}>
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-700/50 flex-wrap bg-[#0d1119]">
        {/* Symbol */}
        <div className="relative">
          <div className="flex items-center bg-gray-800 border border-gray-600 rounded-lg overflow-hidden">
            <Search className="w-3.5 h-3.5 text-gray-400 ml-2 flex-shrink-0" />
            <input
              value={symbolInput}
              onChange={e => setSymbolInput(e.target.value.toUpperCase())}
              onKeyDown={e => e.key === 'Enter' && applySymbol(symbolInput)}
              onFocus={() => setShowSymbolList(true)}
              onBlur={() => setTimeout(() => setShowSymbolList(false), 200)}
              className="bg-transparent text-white text-sm px-2 py-1 w-24 focus:outline-none font-semibold"
              placeholder="Symbol"
            />
            <button onClick={() => applySymbol(symbolInput)} className="px-2 py-1 text-xs text-gray-400 hover:text-white">
              <ChevronDown className="w-3 h-3" />
            </button>
          </div>
          {showSymbolList && (
            <div className="absolute top-full left-0 mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl z-50 min-w-36">
              <div className="px-3 py-1 text-xs text-gray-500 border-b border-gray-700/50">
                {isCryptoExchange ? 'Crypto pairs' : 'FX / Metals'}
              </div>
              {quickSymbols.map(s => (
                <button key={s} onMouseDown={() => applySymbol(s)} className="w-full text-left px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 hover:text-white">
                  {s}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Timeframe */}
        <div className="flex gap-0.5 bg-gray-800/70 rounded-lg p-0.5">
          {TIMEFRAMES.map(tf => (
            <button key={tf} onClick={() => setTimeframe(tf)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${timeframe === tf ? 'bg-tradebot-accent/30 text-tradebot-accent' : 'text-gray-400 hover:text-gray-200'}`}>
              {tf}
            </button>
          ))}
        </div>

        {/* Chart type + indicators */}
        <div className="flex items-center gap-0.5 bg-gray-800/70 rounded-lg p-0.5">
          <IconToggle active={chartType === 'candles'} onClick={() => setChartType('candles')} title="Candles"><CandlestickChart className="w-3.5 h-3.5" /></IconToggle>
          <IconToggle active={chartType === 'line'} onClick={() => setChartType('line')} title="Line"><LineChart className="w-3.5 h-3.5" /></IconToggle>
          <span className="w-px h-4 bg-gray-700 mx-0.5" />
          <IconToggle active={showEMA} onClick={() => setShowEMA(v => !v)} title="EMA 9/21/50"><TrendingUp className="w-3.5 h-3.5" /></IconToggle>
          <IconToggle active={showLevels} onClick={() => setShowLevels(v => !v)} title="S/R levels"><Activity className="w-3.5 h-3.5" /></IconToggle>
          <IconToggle active={drawMode} onClick={() => setDrawMode(v => !v)} title="Draw level (click chart)"><Ruler className="w-3.5 h-3.5" /></IconToggle>
          <IconToggle active={false} onClick={clearManualLevels} title="Clear drawn levels"><Eraser className="w-3.5 h-3.5" /></IconToggle>
        </div>

        {/* Live price */}
        {livePrice && (
          <div className="flex items-center gap-2 text-xs">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500" />
            </span>
            <span className="text-gray-400">Bid</span>
            <span className="font-mono text-white">{livePrice.bid.toFixed(pricePrec)}</span>
            <span className="text-gray-500">│</span>
            <span className="text-gray-400">Ask</span>
            <span className="font-mono text-yellow-300">{livePrice.ask.toFixed(pricePrec)}</span>
          </div>
        )}

        {/* Right cluster: source + quick trade + refresh + maximize */}
        <div className="ml-auto flex items-center gap-2">
          {sourceBadge()}

          {onQuickTrade && (
            <div className="flex items-center gap-1">
              <input value={lot} onChange={e => setLot(e.target.value)}
                className="w-12 bg-gray-800 border border-gray-600 rounded px-1.5 py-1 text-xs text-white font-mono focus:outline-none"
                title="Lot size" />
              <button onClick={() => quickTrade('buy')} disabled={tradeBusy !== null}
                className="px-2.5 py-1 rounded text-xs font-bold bg-green-600/80 hover:bg-green-600 text-white disabled:opacity-50">
                {tradeBusy === 'buy' ? '…' : 'BUY'}
              </button>
              <button onClick={() => quickTrade('sell')} disabled={tradeBusy !== null}
                className="px-2.5 py-1 rounded text-xs font-bold bg-red-600/80 hover:bg-red-600 text-white disabled:opacity-50">
                {tradeBusy === 'sell' ? '…' : 'SELL'}
              </button>
            </div>
          )}

          <button onClick={() => { setLoading(true); fetchCandles() }} disabled={loading}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400 hover:text-white disabled:opacity-50" title="Reload">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={() => setMaximized(v => !v)} className="p-1.5 rounded hover:bg-gray-700 text-gray-400 hover:text-white" title="Maximize">
            {maximized ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </button>
        </div>
      </div>

      {/* Chart body */}
      <div className="relative flex-1">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-gray-900/70 z-10">
            <div className="flex items-center gap-2 text-gray-400 text-sm">
              <RefreshCw className="w-4 h-4 animate-spin" /> Loading {symbol} {timeframe}…
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
                  {CRYPTO_QUICK.slice(0, 4).map(s => (
                    <button key={s} onClick={() => applySymbol(s)}
                      className="px-2.5 py-1 rounded-lg bg-gray-700 text-gray-200 text-xs hover:bg-tradebot-accent/30 hover:text-tradebot-accent">
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
        <div ref={priceRef} style={{ height: priceHeight() }} />
        <div ref={studyRef} style={{ height: showRSI ? 120 : 0, display: showRSI ? 'block' : 'none' }} className="border-t border-gray-800" />
      </div>

      {/* Footer */}
      <div className="px-3 py-1.5 border-t border-gray-700/30 flex items-center gap-3 text-xs text-gray-500 bg-[#0d1119]">
        <span className="font-medium text-gray-400">{symbol} · {timeframe}</span>
        {showEMA && <span className="flex items-center gap-2">
          <span className="text-[#f5c542]">EMA9</span><span className="text-[#42a5f5]">EMA21</span><span className="text-[#ab47bc]">EMA50</span>
        </span>}
        <button onClick={() => setShowRSI(v => !v)} className={`hover:text-white ${showRSI ? 'text-gray-300' : ''}`}>
          RSI {showRSI ? 'on' : 'off'}
        </button>
        <button onClick={() => setShowVolume(v => !v)} className={`hover:text-white ${showVolume ? 'text-gray-300' : ''}`}>
          Vol {showVolume ? 'on' : 'off'}
        </button>
        {lastUpdate && <span>Updated {formatTimeZA(lastUpdate)}</span>}
        {positions.filter(p => p.symbol === symbol).length > 0 && (
          <span className="text-blue-400 ml-auto">{positions.filter(p => p.symbol === symbol).length} position(s)</span>
        )}
      </div>
    </div>
  )
}

// ── Small header toggle button ────────────────────────────────────────────────
function IconToggle({ active, onClick, title, children }: { active: boolean; onClick: () => void; title: string; children: React.ReactNode }) {
  return (
    <button onClick={onClick} title={title}
      className={`p-1 rounded transition-colors ${active ? 'bg-tradebot-accent/30 text-tradebot-accent' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-700/50'}`}>
      {children}
    </button>
  )
}
