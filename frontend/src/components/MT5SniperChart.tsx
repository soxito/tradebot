/**
 * MT5SniperChart — Smart Money Concepts "sniper" workspace for /mt5-live.
 *
 * Renders an MT5 candlestick chart (lightweight-charts) and overlays the output
 * of the plugin's SMC strategy engine:
 *   • order-block / fair-value-gap zones (faint horizontal bounds),
 *   • premium/discount equilibrium + dealing range,
 *   • the selected sniper setup's BUY/SELL entry, SL and TP price lines,
 *   • liquidity pools.
 *
 * Side panel:
 *   • market read (bias / momentum / volume) + optional AI review,
 *   • ranked limit setups with one-click "Place Limit + TP",
 *   • an embedded walk-forward backtester with stats + trade list.
 *
 * All heavy computation lives in the MT5 plugin backend; this component only
 * fetches, draws and dispatches orders — so the feature is fully removable.
 */
import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import {
  createChart, IChartApi, ISeriesApi, SeriesMarker, Time, LineStyle,
  CrosshairMode, ColorType,
} from 'lightweight-charts'
import { apiClient } from '@/services/api'
import { ZoneBoxPrimitive, ZoneBox } from './MT5ZonePrimitive'
import {
  Crosshair, RefreshCw, Target, TrendingUp, TrendingDown, Activity,
  Zap, FlaskConical, ChevronRight, AlertTriangle, Brain, CheckCircle, X,
  Calculator, Maximize2, Minimize2, Settings, Wifi,
} from 'lucide-react'
import { formatTimeZA } from '@/utils/datetime'
import { getPriceSource, setPriceSource as savePriceSource, PRICE_SOURCE_OPTIONS } from '@/utils/priceSource'

// ── Types ────────────────────────────────────────────────────────────────────

interface Candle { time: number; open: number; high: number; low: number; close: number; volume?: number | null }

interface SmcSignal {
  side: 'buy' | 'sell'
  order_type: string
  entry: number
  stop_loss: number
  take_profit: number
  rr: number
  confidence: number
  reason: string
  zone_kind: string
  formed_index: number
  formed_time: number
  confluence: string[]
  tp1?: number
  tp2?: number
  tp3?: number
  in_us_session?: boolean
  lot?: number
  risk_amount?: number
  risk_pct?: number
  reward_amount?: number
  risk_exceeds_cap?: boolean
  point_size?: number
  pip_size?: number
  contract_size?: number
  sl_points?: number
  tp_points?: number
  sl_pips?: number
  tp_pips?: number
  pip_value?: number
}

interface SmcZone { kind: string; top: number; bottom: number; time: number; index: number }

interface SmcStructureEvent {
  index: number
  time: number
  type: 'CHoCH' | 'BOS'
  direction: 'bullish' | 'bearish'
  level: number
  protected_low?: number
  protected_high?: number
}

interface AiReview {
  available: boolean
  reason?: string
  bias_comment?: string
  market_read?: string
  risk_warning?: string
  top_pick_entry?: number | null
  provider?: string
  model?: string
  rated_signals?: { entry: number; verdict: string; confidence: number; note: string }[]
}

interface Analysis {
  symbol: string
  timeframe: string
  bias?: string
  last_price?: number
  atr?: number
  atr_pct?: number
  rsi?: number
  volume_z?: number
  momentum?: string
  equilibrium?: number | null
  range?: { low: number; high: number } | null
  liquidity?: { buyside: number[]; sellside: number[] }
  zones?: SmcZone[]
  signals?: SmcSignal[]
  structure_events?: SmcStructureEvent[]
  us_session?: { enabled: boolean; open_time: number | null; open_price: number | null; live_in_session: boolean }
  ai?: AiReview | null
  error?: string
}

interface BacktestStats {
  total: number; wins: number; losses: number; win_rate: number
  breakevens?: number; decided?: number
  total_r: number; expectancy_r: number; profit_factor: number
  max_drawdown_r: number; avg_win_r: number; avg_loss_r: number
  // Balance-aware equity simulation (present when a starting balance was given)
  starting_balance?: number; ending_balance?: number; net_profit?: number
  net_profit_pct?: number; peak_balance?: number; min_balance?: number
  max_drawdown_currency?: number; recovered?: boolean; risk_per_trade_pct?: number
  // Risk-control + daily-target reporting
  max_loss_cap?: number; max_total_loss_seen?: number; loss_cap_respected?: boolean
  skipped_trades?: number; daily_target?: number; daily_target_pct?: number
  trading_days?: number; days_hit_target?: number; best_day?: number
  worst_day?: number; avg_daily_profit?: number; avg_daily_pct?: number
  daily_breakdown?: { day: string; profit: number; pct: number; trades: number; balance: number; hit_target: boolean }[]
}
interface BacktestTrade {
  side: string; entry: number; sl: number; tp: number
  fill_time: number; exit_time: number; exit_price: number
  outcome: string; r_multiple: number; confidence: number; zone_kind: string
  pnl?: number; balance_after?: number; lot?: number; risk_amount?: number
  trailed?: boolean
}

interface Props {
  accountId: number
  defaultSymbol?: string
  /** Account balance for risk-based position sizing + balance backtest. */
  accountBalance?: number
  /** Account currency symbol for display (e.g. 'USD'). */
  accountCurrency?: string
  /** Exchange to fall back to when MT5 history is unavailable (e.g. 'bitget'). */
  fallbackExchange?: string
  /** Fired after a limit order is placed so the page can refresh positions/orders. */
  onPlaced?: () => void | Promise<void>
  /** Current pending orders (from the page) so they can be shown + cancelled on the chart. */
  orders?: PendingChartOrder[]
  /** Cancel a pending order by ticket. */
  onCancelOrder?: (ticket: number) => void | Promise<void>
  /** Open positions (from the page) so the active trade + live P&L is shown on the chart. */
  positions?: ActiveChartPosition[]
}

interface ActiveChartPosition {
  mt5_ticket: number
  symbol: string
  side: string
  volume: number
  price_open: number
  price_current: number | null
  sl: number | null
  tp: number | null
  profit: number
}

interface PendingChartOrder {
  id: number
  mt5_ticket: number
  symbol: string
  order_type: string
  volume: number
  price: number
  sl?: number | null
  tp?: number | null
}

const TIMEFRAMES = ['M5', 'M15', 'M30', 'H1', 'H4', 'D1'] as const
const TF_MINUTES: Record<string, number> = { M5: 5, M15: 15, M30: 30, H1: 60, H4: 240, D1: 1440 }
const MT5_TF_TO_EXCHANGE: Record<string, string> = { M5: '5m', M15: '15m', M30: '30m', H1: '1h', H4: '4h', D1: '1d' }
const CRYPTO_EXCHANGES = new Set(['bitget', 'binance', 'bybit', 'okx', 'kucoin', 'coinbase', 'huobi', 'gate'])
const FOREX_TO_CRYPTO: Record<string, string> = {
  XAUUSD: 'XAUUSDT', XAGUSD: 'XAGUSDT', BTCUSD: 'BTCUSDT',
  ETHUSD: 'ETHUSDT', BNBUSD: 'BNBUSDT', SOLUSD: 'SOLUSDT',
}

/** Map an MT5 symbol to the closest tradeable pair on a crypto exchange. */
function mapForExchange(sym: string, exchange?: string): string {
  const upper = sym.toUpperCase()
  const isCrypto = exchange ? CRYPTO_EXCHANGES.has(exchange.toLowerCase()) : false
  if (isCrypto && FOREX_TO_CRYPTO[upper]) return FOREX_TO_CRYPTO[upper].replace(/(\w+)(USDT|USDC)$/, '$1/$2')
  if (sym.includes('/')) return sym
  for (const q of ['USDT', 'USDC', 'USD', 'BTC', 'ETH']) {
    if (upper.endsWith(q) && sym.length > q.length) {
      const base = sym.slice(0, sym.length - q.length)
      return `${base}/${isCrypto && q === 'USD' ? 'USDT' : q}`
    }
  }
  return sym
}

const THEME = {
  bg: '#0b0e16', grid: '#161b2b', text: '#a9b1c4', border: '#222838',
  up: '#26a69a', down: '#ef5350',
  buy: '#3b82f6', sell: '#f97316', sl: '#ef4444', tp: '#22c55e',
  zoneBull: 'rgba(38,166,154,0.45)', zoneBear: 'rgba(239,83,80,0.45)',
  eq: '#eab308', liq: '#8b5cf6',
}

/** Point size (smallest increment), pip size (10 points) and contract size per lot. */
function instrumentSpec(symbol: string): { pointSize: number; pipSize: number; contractSize: number } {
  const s = (symbol || '').toUpperCase().replace('/', '')
  let pointSize = 0.01, contractSize = 100
  // Gold: 1 point = $0.01, 1 pip = $0.10; standard lot = 100 oz
  if (s.startsWith('XAU')) { pointSize = 0.01; contractSize = 100 }
  else if (s.startsWith('XAG')) { pointSize = 0.001; contractSize = 5000 }
  else if (/^(BTC|ETH|BNB|SOL|XRP|ADA|DOGE|LTC|AVAX|LINK)/.test(s)) { pointSize = 0.01; contractSize = 1 }
  else if (s.endsWith('JPY')) { pointSize = 0.001; contractSize = 100000 }
  else if (/^[A-Z]{6}$/.test(s)) { pointSize = 0.00001; contractSize = 100000 }
  return { pointSize, pipSize: pointSize * 10, contractSize }
}

function precisionFor(prices: number[]): number {
  const valid = prices.filter(p => p > 0)
  if (valid.length === 0) return 2
  const minP = Math.min(...valid)
  return minP < 0.001 ? 6 : minP < 0.01 ? 5 : minP < 1 ? 4 : minP < 10 ? 3 : 2
}

// ── Component ────────────────────────────────────────────────────────────────

export default function MT5SniperChart({ accountId, defaultSymbol = 'XAUUSD', accountBalance = 0, accountCurrency = 'USD', fallbackExchange, onPlaced, orders = [], onCancelOrder, positions = [] }: Props) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candleSeries = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const chartMounted = useRef(false)  // set false in cleanup so async callbacks don't use disposed chart
  const overlayLines = useRef<any[]>([])
  const zonePrimitive = useRef<ZoneBoxPrimitive | null>(null)
  const livePriceLine = useRef<any>(null)
  const liveBar = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null)
  const candlesRef = useRef<Candle[]>([])
  const lastHistTime = useRef<number>(0)  // newest historical bar time — guards out-of-order cs.update()
  const lastLiveWrite = useRef<number>(0) // throttle livePrice state writes (ms epoch)

  const [symbol, setSymbol] = useState(defaultSymbol)
  const [symbolInput, setSymbolInput] = useState(defaultSymbol)
  const [timeframe, setTimeframe] = useState('H1')
  const [minRR, setMinRR] = useState(2)
  const [maxLoss, setMaxLoss] = useState(15)        // hard max $ loss the user will accept
  const [useCap, setUseCap] = useState(false)       // false = risk-% only (no cap, fixed-fractional)
  const [riskPct, setRiskPct] = useState(5)         // risk per trade as % of balance
  const [dailyTargetPct, setDailyTargetPct] = useState(200)  // daily profit goal (% of balance)
  const [usSession, setUsSession] = useState(false) // anchor entries to the US (NY) session
  const [btFrom, setBtFrom] = useState('')          // backtest date range (YYYY-MM-DD)
  const [btTo, setBtTo] = useState('')
  const [isFullscreen, setIsFullscreen] = useState(false)  // maximize chart to monitor
  const [useAI, setUseAI] = useState(false)  // off by default so first auto-run is fast
  const [lot, setLot] = useState('0.01')

  const [loading, setLoading] = useState(true)
  const [analyzing, setAnalyzing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sourceLabel, setSourceLabel] = useState<string>('mt5')
  const [livePrice, setLivePrice] = useState<number | null>(null)  // realtime price (MT5 or exchange ticker)
  // Price data source (exchange) for live tickers — persisted in localStorage.
  const [priceSource, setPriceSource] = useState<string>(() => getPriceSource(fallbackExchange))
  const [showSourceSettings, setShowSourceSettings] = useState(false)
  const [testingConn, setTestingConn] = useState(false)
  const [connResult, setConnResult] = useState<{ ok: boolean; text: string } | null>(null)
  // Draggable active-trade overlay position (px offset within the chart). null = default top-center.
  const [overlayPos, setOverlayPos] = useState<{ x: number; y: number } | null>(null)
  const overlayDrag = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [selectedIdx, setSelectedIdx] = useState(0)
  const [placeMsg, setPlaceMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [placingIdx, setPlacingIdx] = useState<number | null>(null)
  const [cancelingTicket, setCancelingTicket] = useState<number | null>(null)
  // Position review apply (SL/TP modify)
  const [applying, setApplying] = useState<'sl' | 'tp' | 'both' | null>(null)
  const [applyMsg, setApplyMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // Pending-order price lines drawn on the chart (separate from analysis overlays)
  const orderPriceLines = useRef<any[]>([])

  const [backtesting, setBacktesting] = useState(false)
  const [btStats, setBtStats] = useState<BacktestStats | null>(null)
  const [btTrades, setBtTrades] = useState<BacktestTrade[]>([])
  const [btError, setBtError] = useState<string | null>(null)
  const [btAiAnalysis, setBtAiAnalysis] = useState<{ available: boolean; verdict?: string; summary?: string; strengths?: string[]; weaknesses?: string[]; recommendations?: string[]; reason?: string } | null>(null)

  // ── Active trade (open position) for the current symbol + live calculation ──
  const activeTrade = useMemo(() => {
    const pos = (positions || []).find(p => (p.symbol || '').toUpperCase() === symbol.toUpperCase())
    if (!pos) return null
    const { pointSize, pipSize, contractSize } = instrumentSpec(symbol)
    const isBuy = (pos.side || '').toLowerCase() === 'buy'
    // Prefer the realtime price (live ticker / MT5 quote) over the broker's
    // 5s-stale price_current so the floating P&L ticks between syncs.
    const cur = livePrice ?? pos.price_current ?? pos.price_open
    const dir = isBuy ? 1 : -1
    const moved = (cur - pos.price_open) * dir          // favourable price move
    const points = pointSize > 0 ? moved / pointSize : 0
    const pips = pipSize > 0 ? moved / pipSize : 0
    const risk = pos.sl != null ? Math.abs(pos.price_open - pos.sl) : 0
    const rMultiple = risk > 0 ? moved / risk : 0
    const toTpPips = pos.tp != null && pipSize > 0 ? Math.abs(pos.tp - cur) / pipSize : null
    const toSlPips = pos.sl != null && pipSize > 0 ? Math.abs(cur - pos.sl) / pipSize : null
    // Floating P&L = the broker's real profit (source of truth — includes spread,
    // swap, commission and the broker's exact contract/currency math). We do NOT
    // extrapolate from price because the broker's price_current is unreliable
    // (often stuck at the entry price) and a naive (price-entry)×contract figure
    // diverges from the broker's effective per-point value.
    const liveFloat = pos.profit
    return {
      pos, isBuy, cur, points: Math.round(points * 10) / 10, pips: Math.round(pips * 10) / 10,
      rMultiple: Math.round(rMultiple * 100) / 100,
      toTpPips: toTpPips != null ? Math.round(toTpPips * 10) / 10 : null,
      toSlPips: toSlPips != null ? Math.round(toSlPips * 10) / 10 : null,
      pointSize, pipSize, contractSize,
      floatPL: Math.round(liveFloat * 100) / 100,
      isLive: livePrice != null,
    }
  }, [positions, symbol, livePrice])

  // ── Position review: SMC risk assessment + suggested SL/TP for the open trade ─
  // Built from the same structure the Analyze pass produces (ATR, dealing range,
  // liquidity pools, CHoCH protected levels). Lets the trader tighten/extend an
  // existing position to align with structure, and apply it with one click.
  const positionReview = useMemo(() => {
    if (!activeTrade || !analysis || analysis.error) return null
    const { pos, isBuy, cur, pipSize, contractSize } = activeTrade
    const atr = analysis.atr ?? 0
    if (atr <= 0) return null
    const entry = pos.price_open
    const curSL = pos.sl && pos.sl > 0 ? pos.sl : null
    const curTP = pos.tp && pos.tp > 0 ? pos.tp : null
    const rng = analysis.range
    const liq = analysis.liquidity
    const buf = atr * 0.5
    const round6 = (n: number) => Math.round(n * 1e6) / 1e6

    // Most recent CHoCH protective levels from structure events.
    let protLow: number | null = null, protHigh: number | null = null
    for (const e of analysis.structure_events ?? []) {
      if (e.protected_low != null) protLow = e.protected_low
      if (e.protected_high != null) protHigh = e.protected_high
    }

    let suggSL: number, suggTP: number, slBasis: string, tpBasis: string
    if (isBuy) {
      const sellsideBelow = (liq?.sellside ?? []).filter(p => p < cur)
      const candidates = [protLow, rng?.low ?? null, sellsideBelow.length ? Math.max(...sellsideBelow) : null]
        .filter((v): v is number => v != null && v < cur)
      const structureLow = candidates.length ? Math.max(...candidates) : entry - 2 * atr
      suggSL = round6(structureLow - buf)
      slBasis = structureLow === protLow ? 'below protected low'
        : structureLow === rng?.low ? 'below range low' : 'below structure'
      const buysideAbove = (liq?.buyside ?? []).filter(p => p > cur)
      suggTP = round6(buysideAbove.length ? Math.min(...buysideAbove) : (rng?.high ?? cur + 2 * atr))
      tpBasis = buysideAbove.length ? 'buyside liquidity' : 'range high'
    } else {
      const buysideAbove = (liq?.buyside ?? []).filter(p => p > cur)
      const candidates = [protHigh, rng?.high ?? null, buysideAbove.length ? Math.min(...buysideAbove) : null]
        .filter((v): v is number => v != null && v > cur)
      const structureHigh = candidates.length ? Math.min(...candidates) : entry + 2 * atr
      suggSL = round6(structureHigh + buf)
      slBasis = structureHigh === protHigh ? 'above protected high'
        : structureHigh === rng?.high ? 'above range high' : 'above structure'
      const sellsideBelow = (liq?.sellside ?? []).filter(p => p < cur)
      suggTP = round6(sellsideBelow.length ? Math.max(...sellsideBelow) : (rng?.low ?? cur - 2 * atr))
      tpBasis = sellsideBelow.length ? 'sellside liquidity' : 'range low'
    }

    const dir = isBuy ? 1 : -1
    const suggRisk = Math.abs(entry - suggSL)
    const suggRR = suggRisk > 0 ? Math.abs(suggTP - entry) / suggRisk : 0
    const curRisk = curSL != null ? Math.abs(entry - curSL) : null
    const curRR = (curRisk && curTP != null) ? Math.abs(curTP - entry) / curRisk : null
    const moveR = curRisk ? ((cur - entry) * dir) / curRisk : null    // current profit in R
    const curRiskUSD = curRisk != null ? curRisk * contractSize * (pos.volume || 0) : null
    const suggRiskUSD = suggRisk * contractSize * (pos.volume || 0)

    // Assessment lines + overall risk level.
    const flags: { level: 'good' | 'warn' | 'bad'; text: string }[] = []
    let level: 'low' | 'medium' | 'high' = 'low'
    if (curSL == null) {
      flags.push({ level: 'bad', text: 'No stop-loss — position is unprotected. Set the suggested SL.' })
      level = 'high'
    } else {
      // Is the current SL beyond the protective structure (safe) or inside it (vulnerable)?
      const slBeyond = isBuy ? curSL <= suggSL + buf : curSL >= suggSL - buf
      if (!slBeyond) {
        flags.push({ level: 'warn', text: `SL sits inside structure — vulnerable to a stop-hunt. Suggest ${slBasis}.` })
        level = 'medium'
      } else {
        flags.push({ level: 'good', text: `SL is beyond structure (${slBasis}) — well protected.` })
      }
    }
    if (curTP == null) {
      flags.push({ level: 'warn', text: `No take-profit set. Suggest targeting ${tpBasis}.` })
      if (level === 'low') level = 'medium'
    } else if (curRR != null && curRR < 1) {
      flags.push({ level: 'warn', text: `Current R:R is ${curRR.toFixed(2)} (< 1). Suggested setup is ${suggRR.toFixed(2)}.` })
      if (level === 'low') level = 'medium'
    }
    if (moveR != null && moveR >= 1) {
      flags.push({ level: 'good', text: `In profit ${moveR.toFixed(1)}R — consider trailing SL to lock gains.` })
    }

    return {
      ticket: pos.mt5_ticket, isBuy, entry, curSL, curTP, suggSL, suggTP,
      slBasis, tpBasis, suggRR: Math.round(suggRR * 100) / 100,
      curRR: curRR != null ? Math.round(curRR * 100) / 100 : null,
      moveR: moveR != null ? Math.round(moveR * 100) / 100 : null,
      curRiskUSD: curRiskUSD != null ? Math.round(curRiskUSD * 100) / 100 : null,
      suggRiskUSD: Math.round(suggRiskUSD * 100) / 100,
      flags, level,
    }
  }, [activeTrade, analysis])

  // ── Draggable overlay handlers ──────────────────────────────────────────────
  const onOverlayPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const host = chartRef.current
    if (!host) return
    const rect = host.getBoundingClientRect()
    // Current overlay top-left in container px (default ≈ top-center).
    const cur = overlayPos ?? { x: rect.width / 2 - 95, y: 8 }
    overlayDrag.current = { startX: e.clientX, startY: e.clientY, origX: cur.x, origY: cur.y }
    ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
    e.preventDefault()
    e.stopPropagation()
  }, [overlayPos])

  const onOverlayPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const d = overlayDrag.current
    const host = chartRef.current
    if (!d || !host) return
    const rect = host.getBoundingClientRect()
    const w = 190, h = 90
    const nx = Math.max(0, Math.min(rect.width - w, d.origX + (e.clientX - d.startX)))
    const ny = Math.max(0, Math.min(rect.height - h, d.origY + (e.clientY - d.startY)))
    setOverlayPos({ x: nx, y: ny })
  }, [])

  const onOverlayPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    overlayDrag.current = null
    ;(e.target as HTMLElement).releasePointerCapture?.(e.pointerId)
  }, [])

  // ── Chart construction ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!chartRef.current) return
    const container = chartRef.current
    const c = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight || 460,
      layout: { background: { type: ColorType.Solid, color: THEME.bg }, textColor: THEME.text, fontSize: 11 },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.border, scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
    })
    const cs = c.addCandlestickSeries({
      upColor: THEME.up, downColor: THEME.down,
      borderUpColor: THEME.up, borderDownColor: THEME.down,
      wickUpColor: THEME.up, wickDownColor: THEME.down,
    })
    chart.current = c
    candleSeries.current = cs
    chartMounted.current = true

    // Attach the SMC zone-box primitive (shaded supply/demand rectangles).
    const zp = new ZoneBoxPrimitive()
    cs.attachPrimitive(zp)
    zonePrimitive.current = zp

    // Manually handle resize (avoids autoSize ResizeObserver firing after disposal)
    const handleResize = () => {
      if (chartMounted.current && chart.current && container) {
        try { chart.current.applyOptions({ width: container.clientWidth }) } catch {}
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chartMounted.current = false
      // Clear stale price-line handles BEFORE removing the chart so that any
      // useEffect that runs after unmount doesn't try to call removePriceLine on
      // the disposed series (which throws "Object is disposed").
      overlayLines.current = []
      orderPriceLines.current = []
      zonePrimitive.current = null
      try { c.remove() } catch {}
      chart.current = null
      candleSeries.current = null
    }
  }, [])

  // ── Candle source (MT5 primary, exchange fallback when history is empty) ─────
  const loadSourceCandles = useCallback(async (tf: string, count: number): Promise<{ candles: Candle[]; source: string }> => {
    // 1) MT5 primary
    try {
      const res = await apiClient.mt5.getCandles(accountId, symbol, tf, count)
      const raw: Candle[] = res.data?.candles ?? []
      if (raw.length > 0) return { candles: raw, source: 'mt5' }
    } catch { /* fall through */ }
    // 2) Exchange fallback — always try when MT5 returns nothing.
    //    mapForExchange handles XAUUSD→XAU/USDT etc. safely.
    //    If the symbol cannot be mapped or the exchange lacks it, the call
    //    returns empty which is caught by the outer length check.
    if (fallbackExchange) {
      try {
        const tfx = MT5_TF_TO_EXCHANGE[tf] ?? '1h'
        const sym = mapForExchange(symbol, fallbackExchange)
        const res = await apiClient.getOHLCV(fallbackExchange, sym, tfx, count)
        const raw: Candle[] = (res.data?.data ?? []).map((c: any) => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume ?? null,
        }))
        if (raw.length > 0) return { candles: raw, source: fallbackExchange }
      } catch { /* fall through */ }
    }
    return { candles: [], source: 'none' }
  }, [accountId, symbol, fallbackExchange])

  const fetchCandles = useCallback(async () => {
    if (!accountId) return
    setError(null)
    try {
      const { candles: loaded, source } = await loadSourceCandles(timeframe, 400)
      // Sanitise: sort ascending by time + drop duplicate/zero timestamps so
      // lightweight-charts never throws "data must be asc ordered by time".
      const seen = new Set<number>()
      const raw = [...loaded]
        .filter(c => c && typeof c.time === 'number' && c.time > 0)
        .sort((a, b) => a.time - b.time)
        .filter(c => (seen.has(c.time) ? false : (seen.add(c.time), true)))
      candlesRef.current = raw
      lastHistTime.current = raw.length ? raw[raw.length - 1].time : 0
      liveBar.current = null  // reset forming bar for the new dataset
      setSourceLabel(source)
      const cs = candleSeries.current
      if (!chartMounted.current || !cs) {
        if (raw.length === 0) setError('No candle data for this symbol/timeframe (MT5 history + fallback empty).')
        return
      }
      if (raw.length > 0) {
        try {
          const prec = precisionFor(raw.flatMap(c => [c.open, c.high, c.low, c.close]))
          cs.applyOptions({ priceFormat: { type: 'price', precision: prec, minMove: Math.pow(10, -prec) } })
          cs.setData(raw.map(c => ({ time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close })))
          chart.current?.timeScale().fitContent()
        } catch { /* chart disposed between ref read and use */ }
      } else {
        setError('No candle data for this symbol/timeframe (MT5 history + fallback empty).')
      }
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? e?.message ?? 'Failed to load candles')
    } finally {
      setLoading(false)
    }
  }, [accountId, symbol, timeframe, loadSourceCandles])

  // ── SMC analysis (runs on whatever candles are currently displayed) ─────────
  const runAnalysis = useCallback(async () => {
    if (!accountId) return
    const raw = candlesRef.current
    if (!raw || raw.length < 40) { setError('Not enough candle data to analyze (need >= 40 bars).'); return }
    setAnalyzing(true)
    setPlaceMsg(null)
    try {
      const res = await apiClient.mt5.smcAnalyzeData({
        symbol, timeframe,
        // Reward-first defaults: min_rr is the floor (reward must beat risk) and
        // max_rr lets TP run to real liquidity above the floor, so reward > risk.
        min_rr: minRR, max_rr: Math.max(minRR + 1, 3), sl_buffer_atr: 1.0, min_confidence: 0.6,
        // Balance-aware position sizing (lot/risk shown per setup).
        account_balance: accountBalance, risk_per_trade_pct: riskPct,
        max_total_loss: useCap ? maxLoss : 0, daily_profit_target_pct: dailyTargetPct,
        us_session_only: usSession,
        use_ai: useAI,
        candles: raw.map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume ?? 0 })),
      })
      const a: Analysis = res.data
      setAnalysis(a)
      setSelectedIdx(0)
      if (a.error) setError(a.error)
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? e?.message ?? 'Analysis failed')
    } finally {
      setAnalyzing(false)
    }
  }, [accountId, symbol, timeframe, minRR, useAI, accountBalance, maxLoss, dailyTargetPct, usSession, riskPct, useCap])

  useEffect(() => {
    setLoading(true)
    liveBar.current = null
    lastHistTime.current = 0
    setLivePrice(null)  // clear stale price so P&L doesn't bleed across symbols
    setAnalysis(null)
    setBtStats(null)
    setBtTrades([])
    setBtAiAnalysis(null)
    fetchCandles().then(() => {
      // Auto-run analysis immediately after candles load — no manual click needed.
      runAnalysis()
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchCandles])

  // ── Live price polling (forming bar + live line + realtime P&L) ─────────────
  // Source-aware: MT5-sourced symbols use the broker quote; exchange-sourced
  // symbols (e.g. gold via Binance fallback) use the exchange ticker so the live
  // line, forming bar and floating P&L keep ticking even when the broker is
  // unreachable.
  useEffect(() => {
    if (!accountId || loading || sourceLabel === 'none') return
    const isMt5 = sourceLabel === 'mt5'
    let cancelled = false
    let failStreak = 0
    let timeoutId: ReturnType<typeof setTimeout>
    const tfSec = (TF_MINUTES[timeframe] ?? 60) * 60
    const candleStart = (ts: number) => Math.floor(ts / tfSec) * tfSec

    const fetchPrice = async (): Promise<number | null> => {
      if (isMt5) {
        const res = await apiClient.mt5.getPrice(accountId, symbol)
        const { ask } = res.data as { bid: number; ask: number }
        return typeof ask === 'number' ? ask : null
      }
      // Exchange ticker — use the user-chosen price source (XAUUSD→XAU/USDT).
      const exSym = mapForExchange(symbol, priceSource)
      const res = await apiClient.getTicker(priceSource, exSym)
      const t = res.data?.ticker as { last?: number; bid?: number; ask?: number } | null
      if (!t) return null
      return (t.last ?? t.ask ?? t.bid) ?? null
    }

    const poll = async () => {
      if (cancelled) return
      try {
        const price = await fetchPrice()
        if (cancelled || price == null || !isFinite(price)) { failStreak++; }
        else {
          failStreak = 0
          // Throttle React state writes to ~1/sec to avoid excessive re-renders.
          const now = Date.now()
          if (now - lastLiveWrite.current >= 1000) {
            lastLiveWrite.current = now
            setLivePrice(price)
          }
          const cs = candleSeries.current
          if (chartMounted.current && cs) {
            try {
              if (livePriceLine.current) cs.removePriceLine(livePriceLine.current)
              livePriceLine.current = cs.createPriceLine({
                price, color: '#facc15', lineWidth: 1, lineStyle: LineStyle.Dotted,
                axisLabelVisible: true, title: 'Live',
              })
            } catch { /* non-fatal */ }
            const thisCandle = candleStart(Math.floor(Date.now() / 1000))
            // Guard: never write a forming bar older than the newest historical
            // bar — lightweight-charts throws "data must be asc ordered by time".
            if (thisCandle >= lastHistTime.current) {
              const hist = candlesRef.current
              const lastHist = hist.length ? hist[hist.length - 1] : null
              const prev = liveBar.current
              if (!prev || prev.time !== thisCandle) {
                const seed = lastHist ? lastHist.close : price
                liveBar.current = { time: thisCandle, open: seed, high: Math.max(seed, price), low: Math.min(seed, price), close: price }
              } else {
                liveBar.current = { ...prev, high: Math.max(prev.high, price), low: Math.min(prev.low, price), close: price }
              }
              try {
                const b = liveBar.current
                cs.update({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })
              } catch { /* non-fatal */ }
            }
          }
        }
      } catch {
        failStreak++
      }
      if (!cancelled) timeoutId = setTimeout(poll, failStreak >= 3 ? 30_000 : 2_500)
    }
    poll()
    return () => {
      cancelled = true
      clearTimeout(timeoutId)
      if (candleSeries.current && livePriceLine.current) {
        try { candleSeries.current.removePriceLine(livePriceLine.current) } catch {}
        livePriceLine.current = null
      }
    }
  }, [accountId, symbol, timeframe, loading, sourceLabel, priceSource])

  // Persist the chosen price source + run a one-off connection test.
  const testPriceSource = useCallback(async () => {
    setTestingConn(true); setConnResult(null)
    const exSym = mapForExchange(symbol, priceSource)
    const t0 = performance.now()
    try {
      const res = await apiClient.getTicker(priceSource, exSym)
      const tk = res.data?.ticker as { last?: number; bid?: number; ask?: number } | null
      const price = tk ? (tk.last ?? tk.ask ?? tk.bid) : null
      const ms = Math.round(performance.now() - t0)
      if (price != null && isFinite(price)) {
        setConnResult({ ok: true, text: `${priceSource} · ${exSym} = ${price} (${ms}ms)` })
      } else {
        const err = res.data?.error ? String(res.data.error).slice(0, 80) : `${priceSource} has no ${exSym}`
        setConnResult({ ok: false, text: err })
      }
    } catch (e: any) {
      setConnResult({ ok: false, text: e?.response?.data?.detail ?? e?.message ?? 'Connection failed' })
    } finally {
      setTestingConn(false)
    }
  }, [priceSource, symbol])

  const changePriceSource = useCallback((src: string) => {
    setPriceSource(src)
    savePriceSource(src)        // persist + notify the page-level poll
    setConnResult(null)
    setLivePrice(null)          // drop stale price from the old source
  }, [])

  // Apply the suggested SL and/or TP to ALL open positions on the same symbol
  // and same direction — not just the first one .find() returns.
  const applyPositionSLTP = useCallback(async (which: 'sl' | 'tp' | 'both') => {
    if (!positionReview) return
    setApplying(which); setApplyMsg(null)
    const sl = which === 'tp' ? undefined : positionReview.suggSL
    const tp = which === 'sl' ? undefined : positionReview.suggTP
    // Collect every position on the same symbol AND same side so we update all of them.
    const sameSymSide = (positions || []).filter(p =>
      (p.symbol || '').toUpperCase() === symbol.toUpperCase() &&
      (p.side || '').toLowerCase() === (positionReview.isBuy ? 'buy' : 'sell')
    )
    const tickets = sameSymSide.length > 0 ? sameSymSide.map(p => p.mt5_ticket) : [positionReview.ticket]
    try {
      await Promise.all(tickets.map(ticket => apiClient.mt5.modifyTrade(accountId, ticket, sl, tp)))
      const label = which === 'both' ? 'SL + TP' : which.toUpperCase()
      const ticketList = tickets.map(t => `#${t}`).join(', ')
      setApplyMsg({ ok: true, text: `${label} updated on ${ticketList}` })
      onPlaced?.()  // refresh positions so the new SL/TP shows
    } catch (e: any) {
      setApplyMsg({ ok: false, text: e?.response?.data?.detail ?? e?.message ?? 'Modify failed' })
    } finally {
      setApplying(null)
    }
  }, [positionReview, accountId, positions, symbol, onPlaced])

  const signals = analysis?.signals ?? []
  const selected = signals[selectedIdx] ?? null

  // ── Draw overlays (zones, equilibrium, liquidity, selected setup) ───────────
  useEffect(() => {
    const cs = candleSeries.current
    if (!cs || !chartMounted.current) return
    overlayLines.current.forEach(l => { try { cs.removePriceLine(l) } catch {} })
    overlayLines.current = []
    if (!analysis) { try { cs.setMarkers([]) } catch {}; return }

    const add = (opts: any) => { try { overlayLines.current.push(cs.createPriceLine(opts)) } catch {} }

    // ── Order blocks and Fair Value Gaps (shaded boxes via primitive) ────────
    // Each zone is a translucent rectangle that extends from its formation bar
    // to the right edge of the chart — TradingView supply/demand-box style.
    //   OB  — blue (bullish demand) / orange (bearish supply)
    //   FVG — teal (bullish)       / red (bearish)
    const zoneBoxes: ZoneBox[] = (analysis.zones ?? []).slice(-12).map(z => {
      const isBull = z.kind.startsWith('bullish')
      const isOB   = z.kind.includes('ob')
      const rgb = isOB
        ? (isBull ? '59,130,246' : '249,115,22')
        : (isBull ? '38,166,154' : '239,83,80')
      return {
        time:   z.time as Time,
        top:    z.top,
        bottom: z.bottom,
        fill:   `rgba(${rgb},0.12)`,
        border: `rgba(${rgb},0.55)`,
        label:  isOB ? 'OB' : 'FVG',
        labelColor: `rgba(${rgb},0.95)`,
      }
    })
    try { zonePrimitive.current?.setBoxes(zoneBoxes) } catch {}

    // Equilibrium (premium/discount divider)
    if (analysis.equilibrium) {
      add({ price: analysis.equilibrium, color: THEME.eq, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'EQ 50%' })
    }
    // Dealing range extremes — labelled Strong High / Weak Low (reference style)
    if (analysis.range) {
      add({ price: analysis.range.high, color: 'rgba(239,83,80,0.55)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Strong High' })
      add({ price: analysis.range.low, color: 'rgba(96,165,250,0.55)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Weak Low' })
    }
    // Liquidity pools — equal highs (EQH) / equal lows (EQL)
    ;(analysis.liquidity?.buyside ?? []).slice(-2).forEach(p =>
      add({ price: p, color: 'rgba(139,92,246,0.45)', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'EQH' }))
    ;(analysis.liquidity?.sellside ?? []).slice(-2).forEach(p =>
      add({ price: p, color: 'rgba(139,92,246,0.45)', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'EQL' }))

    // Selected setup: entry / SL / TP1 / TP2 / TP3 (scale-out ladder)
    if (selected) {
      const isBuy = selected.side === 'buy'
      add({ price: selected.entry, color: isBuy ? THEME.buy : THEME.sell, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: `${isBuy ? 'BUY' : 'SELL'} LIMIT` })
      add({ price: selected.stop_loss, color: THEME.sl, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'SL' })
      const tps: [number | undefined, string][] = [
        [selected.tp1, 'TP 1'], [selected.tp2 ?? selected.take_profit, 'TP 2'], [selected.tp3, 'TP 3'],
      ]
      tps.forEach(([p, label]) => {
        if (p && p > 0) add({ price: p, color: THEME.tp, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: label })
      })
    }

    // US (New York) session open — reference candle the day's entries build from.
    if (analysis.us_session?.enabled && analysis.us_session.open_price) {
      add({ price: analysis.us_session.open_price, color: 'rgba(250,204,21,0.6)', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'US open' })
    }

    // Active trade (open position) — entry / SL / TP lines, drawn boldly.
    if (activeTrade) {
      const ap = activeTrade.pos
      add({ price: ap.price_open, color: activeTrade.isBuy ? THEME.buy : THEME.sell, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: `OPEN ${ap.volume}L` })
      if (ap.sl != null) add({ price: ap.sl, color: THEME.sl, lineWidth: 2, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Pos SL' })
      if (ap.tp != null) add({ price: ap.tp, color: THEME.tp, lineWidth: 2, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Pos TP' })
    }

    // ── Markers: CHoCH / BOS labels + signal setup arrows ───────────────────
    // CHoCH = square marker, larger — signals a shift in market character.
    // BOS   = circle marker, smaller — confirms continuation of existing bias.
    const structureMarkers: SeriesMarker<Time>[] = (analysis.structure_events ?? []).map(e => {
      const isBull  = e.direction === 'bullish'
      const isChoCH = e.type === 'CHoCH'
      return {
        time:     e.time as Time,
        position: isBull ? 'belowBar' : 'aboveBar',
        color:    isChoCH
          ? (isBull ? '#10b981' : '#f43f5e')
          : (isBull ? '#60a5fa' : '#fb923c'),
        shape:    isChoCH ? 'square' : 'circle',
        text:     e.type,
        size:     isChoCH ? 2 : 1,
      }
    })

    const signalMarkers: SeriesMarker<Time>[] = signals.map(s => ({
      time:     s.formed_time as Time,
      position: s.side === 'buy' ? 'belowBar' : 'aboveBar',
      color:    s.side === 'buy' ? THEME.buy : THEME.sell,
      shape:    s.side === 'buy' ? 'arrowUp' : 'arrowDown',
      text:     `${s.side === 'buy' ? 'BUY' : 'SELL'} ${(s.confidence * 100).toFixed(0)}%`,
      size:     1,
    }))

    const allMarkers = [...structureMarkers, ...signalMarkers]
      .sort((a, b) => (a.time as number) - (b.time as number))
    try { cs.setMarkers(allMarkers) } catch {}
  }, [analysis, selected, signals, activeTrade])

  // Resize the chart when toggling fullscreen so it fills the new container.
  useEffect(() => {
    const id = setTimeout(() => {
      const container = chartRef.current
      if (chartMounted.current && chart.current && container) {
        try {
          chart.current.applyOptions({ width: container.clientWidth, height: container.clientHeight || 460 })
          chart.current.timeScale().fitContent()
        } catch { /* disposed */ }
      }
    }, 60)
    return () => clearTimeout(id)
  }, [isFullscreen])

  // ── Draw pending orders as price lines on chart ─────────────────────────────
  useEffect(() => {
    const cs = candleSeries.current
    if (!cs || !chartMounted.current) return
    // Remove previous order lines
    orderPriceLines.current.forEach(l => { try { cs.removePriceLine(l) } catch {} })
    orderPriceLines.current = []

    const symbolOrders = (orders || []).filter(
      o => (o.symbol || '').toUpperCase() === symbol.toUpperCase()
    )

    symbolOrders.forEach(o => {
      const isBuy = /buy/i.test(o.order_type)
      const entryColor = isBuy ? THEME.buy : THEME.sell
      const label = `${o.order_type.replace(/_/g, ' ').toUpperCase()} ${o.volume}L`
      try {
        orderPriceLines.current.push(cs.createPriceLine({
          price: o.price, color: entryColor, lineWidth: 2,
          lineStyle: LineStyle.Solid, axisLabelVisible: true, title: label,
        }))
        if (o.sl) {
          orderPriceLines.current.push(cs.createPriceLine({
            price: o.sl, color: THEME.sl, lineWidth: 1,
            lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: 'SL',
          }))
        }
        if (o.tp) {
          orderPriceLines.current.push(cs.createPriceLine({
            price: o.tp, color: THEME.tp, lineWidth: 1,
            lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: 'TP',
          }))
        }
      } catch { /* chart disposed */ }
    })
  }, [orders, symbol])

  // ── Place limit order ───────────────────────────────────────────────────────
  const placeSignal = async (sig: SmcSignal, idx: number) => {
    if (!accountId) return
    setPlacingIdx(idx)
    setPlaceMsg(null)
    try {
      await apiClient.mt5.smcPlace({
        account_id: accountId, symbol, side: sig.side,
        entry: sig.entry, stop_loss: sig.stop_loss, take_profit: sig.take_profit,
        volume: parseFloat(lot) || 0.01,
        comment: `SMC ${sig.zone_kind}`,
      })
      setPlaceMsg({ ok: true, text: `${sig.side.toUpperCase()} LIMIT @ ${sig.entry} placed (SL ${sig.stop_loss} / TP ${sig.take_profit})` })
      await onPlaced?.()
    } catch (e: any) {
      setPlaceMsg({ ok: false, text: e?.response?.data?.detail ?? e?.message ?? 'Order failed' })
    } finally {
      setPlacingIdx(null)
    }
  }

  // ── Backtest ────────────────────────────────────────────────────────────────
  const runBacktest = async () => {
    if (!accountId) return
    setBacktesting(true)
    setBtError(null)
    try {
      // Pull a generous window, then filter to the chosen From–To date range.
      const isGold = symbol.toUpperCase().startsWith('XAU')
      const tfMinutes = TF_MINUTES[timeframe] ?? 60

      // Calculate how many candles to fetch based on the selected date range
      // so every range button (5D–60D) gets full coverage on any timeframe.
      const rangeMs = btFrom && btTo
        ? new Date(btTo + 'T23:59:59Z').getTime() - new Date(btFrom + 'T00:00:00Z').getTime()
        : 30 * 24 * 60 * 60 * 1000  // default 30 days
      const rangeDays = Math.max(rangeMs / (24 * 60 * 60 * 1000), 1)
      // bars per day × range days × 1.4 buffer (weekends / gaps on XAU)
      // Cap at 1500 — the backend OHLCV endpoint limit.
      const barsPerDay = 1440 / tfMinutes
      const fetchCount = Math.min(Math.max(Math.ceil(rangeDays * barsPerDay * 1.4), isGold ? 200 : 200), 1500)

      const { candles: all } = await loadSourceCandles(timeframe, fetchCount)
      const fromTs = btFrom ? Math.floor(new Date(btFrom + 'T00:00:00Z').getTime() / 1000) : null
      const toTs = btTo ? Math.floor(new Date(btTo + 'T23:59:59Z').getTime() / 1000) : null
      const raw = all.filter(c => (fromTs == null || c.time >= fromTs) && (toTs == null || c.time <= toTs))

      // Minimum candle threshold is timeframe-aware: require at least 5 full bars
      // per day so the engine has enough structure to work with.
      const minRequired = Math.max(10, Math.ceil(5 * barsPerDay))
      if (raw.length < minRequired) {
        setBtError(`Not enough candles in range (${raw.length}, need ≥ ${minRequired} for ${timeframe}). Widen the date range or switch timeframe.`)
        return
      }
      // Instrument-adaptive parameters: gold uses tighter SL buffer + lower confidence threshold
      const slBuf = isGold ? 0.5 : 1.0
      const minConf = isGold ? 0.5 : 0.6
      const maxRR = isGold ? 4.0 : Math.max(minRR + 1, 3)
      // Equity simulation — always provide a non-zero starting balance so the
      // daily profit accumulation table can be computed. Fall back to $10,000
      // (a sensible demo balance) when the live account balance hasn't loaded.
      const simBalance = accountBalance > 0 ? accountBalance : 10000
      const res = await apiClient.mt5.smcBacktestData({
        symbol, timeframe,
        min_rr: minRR, max_rr: maxRR, sl_buffer_atr: slBuf, min_confidence: minConf,
        expiry_bars: 24,
        starting_balance: simBalance, risk_per_trade_pct: riskPct,
        // No cap → fixed-fractional compounding (recovery off, the % is the protection).
        recovery_enabled: useCap, max_risk_multiplier: 3.0,
        max_total_loss: useCap ? maxLoss : 0, daily_profit_target_pct: dailyTargetPct,
        use_ai: true,  // AI analysis always on
        candles: raw.map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume ?? 0 })),
      })
      setBtStats(res.data?.stats ?? null)
      setBtTrades(res.data?.trades ?? [])
      setBtAiAnalysis(res.data?.ai ?? null)
      if (res.data?.error) setBtError(res.data.error)
    } catch (e: any) {
      setBtError(e?.response?.data?.detail ?? e?.message ?? 'Backtest failed')
    } finally {
      setBacktesting(false)
    }
  }

  const applySymbol = (s: string) => {
    const clean = s.trim().toUpperCase()
    if (!clean) return
    setSymbol(clean)
    setSymbolInput(clean)
  }

  const biasColor = analysis?.bias === 'bullish' ? 'text-green-400' : analysis?.bias === 'bearish' ? 'text-red-400' : 'text-gray-400'
  const ai = analysis?.ai

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div className={`bg-[#0b0e16] border border-gray-700/50 flex flex-col ${isFullscreen ? 'fixed inset-0 z-50 rounded-none overflow-auto' : 'rounded-xl overflow-hidden'}`}>
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-700/50 flex-wrap bg-[#0d1119]">
        <div className="flex items-center gap-1.5 text-tradebot-accent font-semibold text-sm">
          <Crosshair className="w-4 h-4" /> SMC Sniper
        </div>
        <div className="flex items-center bg-gray-800 border border-gray-600 rounded-lg overflow-hidden">
          <input
            value={symbolInput}
            onChange={e => setSymbolInput(e.target.value.toUpperCase())}
            onKeyDown={e => e.key === 'Enter' && applySymbol(symbolInput)}
            className="bg-transparent text-white text-sm px-2 py-1 w-24 focus:outline-none font-semibold"
            placeholder="Symbol"
          />
          <button onClick={() => applySymbol(symbolInput)} className="px-2 py-1 text-xs text-gray-400 hover:text-white">
            <ChevronRight className="w-3 h-3" />
          </button>
        </div>
        <div className="flex gap-0.5 bg-gray-800/70 rounded-lg p-0.5">
          {TIMEFRAMES.map(tf => (
            <button key={tf} onClick={() => setTimeframe(tf)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${timeframe === tf ? 'bg-tradebot-accent/30 text-tradebot-accent' : 'text-gray-400 hover:text-gray-200'}`}>
              {tf}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-400">
          <span>Min RR</span>
          <select value={minRR} onChange={e => setMinRR(Number(e.target.value))}
            className="bg-gray-800 border border-gray-600 rounded px-1.5 py-1 text-white">
            {[1.5, 2, 2.5, 3].map(v => <option key={v} value={v}>{v}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-400">
          <span>Risk %</span>
          <select value={riskPct} onChange={e => setRiskPct(Number(e.target.value))}
            className="bg-gray-800 border border-gray-600 rounded px-1.5 py-1 text-white"
            title="Risk per trade as % of balance — higher = more profit and deeper drawdown (compounded)">
            {[0.5, 1, 2, 3, 5, 8, 10, 15].map(v => <option key={v} value={v}>{v}%</option>)}
          </select>
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-400">
          <label className="flex items-center gap-1 cursor-pointer" title="Hard $ loss cap. Off = risk-% only (fixed-fractional compounding, can't be driven negative).">
            <input type="checkbox" checked={useCap} onChange={e => setUseCap(e.target.checked)} className="accent-tradebot-accent" />
            Max loss
          </label>
          {useCap && (
            <div className="flex items-center bg-gray-800 border border-gray-600 rounded px-1 py-0.5">
              <span className="text-gray-500">{accountCurrency}</span>
              <input type="number" min={1} step={1} value={maxLoss}
                onChange={e => setMaxLoss(Math.max(1, Number(e.target.value) || 0))}
                className="w-12 bg-transparent text-white outline-none text-center" />
            </div>
          )}
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-400">
          <span>Daily target</span>
          <select value={dailyTargetPct} onChange={e => setDailyTargetPct(Number(e.target.value))}
            className="bg-gray-800 border border-gray-600 rounded px-1.5 py-1 text-white">
            {[50, 100, 150, 200, 300, 400, 500, 750, 1000].map(v => <option key={v} value={v}>{v}%</option>)}
          </select>
        </div>
        <label className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer" title="Anchor entries to the US (New York) session 13:30–20:00 UTC">
          <input type="checkbox" checked={usSession} onChange={e => setUsSession(e.target.checked)} className="accent-tradebot-accent" />
          US session
        </label>
        <label className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer">
          <input type="checkbox" checked={useAI} onChange={e => setUseAI(e.target.checked)} className="accent-tradebot-accent" />
          <Brain className="w-3.5 h-3.5" /> AI
        </label>
        <button
          onClick={runAnalysis}
          disabled={analyzing}
          className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-tradebot-accent/20 text-tradebot-accent border border-tradebot-accent/30 text-xs font-medium hover:bg-tradebot-accent/30 disabled:opacity-50"
        >
          {analyzing ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Target className="w-3.5 h-3.5" />}
          Analyze
        </button>
        {/* Price data source settings */}
        <div className="relative">
          <button
            onClick={() => { setShowSourceSettings(s => !s); setConnResult(null) }}
            title="Price data source (live ticker)"
            className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium ${showSourceSettings ? 'bg-tradebot-accent/20 text-tradebot-accent border-tradebot-accent/40' : 'bg-gray-800 text-gray-300 border-gray-600 hover:bg-gray-700'}`}
          >
            <Settings className="w-3.5 h-3.5" /> {priceSource}
          </button>
          {showSourceSettings && (
            <div className="absolute right-0 top-full mt-1 z-30 w-64 bg-gray-900 border border-gray-700 rounded-lg shadow-xl p-3 text-xs">
              <div className="flex items-center justify-between mb-2">
                <span className="font-semibold text-gray-200">Price Data Source</span>
                <button onClick={() => setShowSourceSettings(false)} className="text-gray-500 hover:text-gray-300"><X className="w-3.5 h-3.5" /></button>
              </div>
              <p className="text-[10px] text-gray-500 mb-2 leading-relaxed">
                Exchange used for live ticker prices (chart line, floating P&amp;L, margin).
                Crypto pairs work on any exchange; gold (XAU) is only on Binance.
              </p>
              <label className="block text-gray-400 mb-1">Exchange</label>
              <select
                value={priceSource}
                onChange={e => changePriceSource(e.target.value)}
                className="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-white mb-2"
              >
                {PRICE_SOURCE_OPTIONS.map(ex => <option key={ex} value={ex}>{ex}</option>)}
              </select>
              <button
                onClick={testPriceSource}
                disabled={testingConn}
                className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded bg-cyan-600/20 text-cyan-300 border border-cyan-500/30 font-medium hover:bg-cyan-600/30 disabled:opacity-50"
              >
                {testingConn ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Wifi className="w-3.5 h-3.5" />}
                Test Connection
              </button>
              {connResult && (
                <div className={`mt-2 flex items-start gap-1.5 p-2 rounded text-[11px] leading-relaxed ${connResult.ok ? 'bg-green-500/10 text-green-300 border border-green-500/30' : 'bg-red-500/10 text-red-300 border border-red-500/30'}`}>
                  {connResult.ok ? <CheckCircle className="w-3.5 h-3.5 shrink-0 mt-px" /> : <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />}
                  <span className="break-words">{connResult.text}</span>
                </div>
              )}
            </div>
          )}
        </div>
        <button
          onClick={() => setIsFullscreen(f => !f)}
          title={isFullscreen ? 'Exit fullscreen' : 'Maximize chart to monitor'}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-gray-800 text-gray-300 border border-gray-600 text-xs font-medium hover:bg-gray-700"
        >
          {isFullscreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          {isFullscreen ? 'Exit' : 'Full screen'}
        </button>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-0">
        {/* Chart */}
        <div className="xl:col-span-2 relative">
          <div ref={chartRef} style={{ height: isFullscreen ? 'calc(100vh - 230px)' : 460 }} className="w-full" />
          {/* Active trade — live calculation overlay (draggable; default top-center) */}
          {activeTrade && (
            <div
              className="absolute z-10 bg-gray-900/92 border border-gray-700/60 rounded-lg p-2 text-[11px] w-[190px] shadow-lg select-none"
              style={overlayPos
                ? { left: overlayPos.x, top: overlayPos.y }
                : { left: '50%', top: 8, transform: 'translateX(-50%)' }}
            >
              <div
                onPointerDown={onOverlayPointerDown}
                onPointerMove={onOverlayPointerMove}
                onPointerUp={onOverlayPointerUp}
                className="flex items-center justify-between mb-1 gap-2 cursor-move"
                title="Drag to reposition"
              >
                <span className={`font-bold flex items-center gap-1 ${activeTrade.isBuy ? 'text-blue-400' : 'text-orange-400'}`}>
                  {activeTrade.isLive && <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" title="Live" />}
                  {activeTrade.isBuy ? 'LONG' : 'SHORT'} {activeTrade.pos.volume}L
                </span>
                <span className={`font-mono font-semibold tabular-nums ${activeTrade.floatPL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {activeTrade.floatPL >= 0 ? '+' : ''}{accountCurrency} {activeTrade.floatPL.toFixed(2)}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-gray-300 tabular-nums">
                <div><span className="text-gray-500">Entry</span> {activeTrade.pos.price_open}</div>
                <div><span className="text-gray-500">Now</span> {typeof activeTrade.cur === 'number' ? activeTrade.cur.toFixed(2) : activeTrade.cur}</div>
                <div><span className="text-gray-500">P/L pips</span> <span className={activeTrade.pips >= 0 ? 'text-green-300' : 'text-red-300'}>{activeTrade.pips}</span></div>
                <div><span className="text-gray-500">R</span> <span className={activeTrade.rMultiple >= 0 ? 'text-green-300' : 'text-red-300'}>{activeTrade.rMultiple}R</span></div>
                {activeTrade.toTpPips != null && <div><span className="text-gray-500">→ TP</span> {activeTrade.toTpPips}p</div>}
                {activeTrade.toSlPips != null && <div><span className="text-gray-500">→ SL</span> {activeTrade.toSlPips}p</div>}
              </div>
            </div>
          )}
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-[#0b0e16]/70 text-gray-400 text-sm">
              <RefreshCw className="w-4 h-4 animate-spin mr-2" /> Loading chart…
            </div>
          )}
          {error && !loading && (
            <div className="absolute top-2 left-2 right-2 flex items-center gap-2 bg-red-900/30 border border-red-700/40 rounded px-3 py-1.5 text-xs text-red-300">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> {error}
            </div>
          )}
          {(() => {
            const symbolOrders = (orders || []).filter(
              o => (o.symbol || '').toUpperCase() === symbol.toUpperCase(),
            )
            if (symbolOrders.length === 0) return null
            return (
              <div className="absolute bottom-2 left-2 z-10 bg-gray-900/90 border border-gray-700/60 rounded-lg p-2 text-[11px] space-y-1 max-w-[280px]">
                <div className="text-gray-400 font-medium mb-0.5">Pending Orders ({symbolOrders.length})</div>
                {symbolOrders.map(o => (
                  <div key={o.id} className="flex items-center gap-2">
                    <span className={`font-mono uppercase ${/sell/i.test(o.order_type) ? 'text-orange-400' : 'text-blue-400'}`}>
                      {o.order_type.replace('_', ' ')}
                    </span>
                    <span className="text-gray-300">{o.volume}L @ {o.price}</span>
                    {onCancelOrder && (
                      <button
                        onClick={async () => {
                          setCancelingTicket(o.mt5_ticket)
                          try { await onCancelOrder(o.mt5_ticket) } finally { setCancelingTicket(null) }
                        }}
                        disabled={cancelingTicket === o.mt5_ticket}
                        title="Cancel order"
                        className="ml-auto flex items-center gap-1 px-1.5 py-0.5 rounded bg-red-600/80 hover:bg-red-600 text-white disabled:opacity-50"
                      >
                        <X className="w-3 h-3" />{cancelingTicket === o.mt5_ticket ? '…' : 'Cancel'}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )
          })()}
        </div>

        {/* Side panel */}
        <div className="border-l border-gray-700/50 p-3 space-y-3 max-h-[460px] overflow-y-auto">
          {/* Market read */}
          {analysis && !analysis.error && (
            <div className="bg-gray-800/50 rounded-lg p-2.5 text-xs space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-gray-400">Bias</span>
                <span className={`font-semibold flex items-center gap-1 ${biasColor}`}>
                  {analysis.bias === 'bullish' ? <TrendingUp className="w-3.5 h-3.5" /> : analysis.bias === 'bearish' ? <TrendingDown className="w-3.5 h-3.5" /> : <Activity className="w-3.5 h-3.5" />}
                  {(analysis.bias ?? 'neutral').toUpperCase()}
                </span>
              </div>
              <div className="flex items-center justify-between"><span className="text-gray-400">Momentum</span><span className="text-gray-200">{analysis.momentum} · vol z {analysis.volume_z?.toFixed(2)}</span></div>
              <div className="flex items-center justify-between"><span className="text-gray-400">ATR / RSI</span><span className="text-gray-200">{analysis.atr_pct?.toFixed(2)}% · {analysis.rsi?.toFixed(0)}</span></div>
              <div className="flex items-center justify-between"><span className="text-gray-400">Last</span><span className="text-gray-200 font-mono">{analysis.last_price}</span></div>
              <div className="flex items-center justify-between"><span className="text-gray-400">Source</span><span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${sourceLabel === 'mt5' ? 'bg-green-900/40 text-green-400' : sourceLabel === 'none' ? 'bg-red-900/40 text-red-400' : 'bg-yellow-900/40 text-yellow-400'}`}>{sourceLabel === 'mt5' ? 'MT5 LIVE' : sourceLabel.toUpperCase()}</span></div>
            </div>
          )}

          {/* AI review */}
          {ai && (
            <div className={`rounded-lg p-2.5 text-xs border ${ai.available ? 'bg-violet-900/15 border-violet-700/30' : 'bg-gray-800/40 border-gray-700/40'}`}>
              <div className="flex items-center gap-1.5 font-semibold text-violet-300 mb-1">
                <Brain className="w-3.5 h-3.5" /> AI Review
                {ai.available && ai.provider && <span className="text-[10px] text-gray-500 font-normal">({ai.provider})</span>}
              </div>
              {ai.available ? (
                <div className="space-y-1 text-gray-300">
                  {ai.market_read && <p>{ai.market_read}</p>}
                  {ai.risk_warning && <p className="text-amber-300/90 flex items-start gap-1"><AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />{ai.risk_warning}</p>}
                </div>
              ) : (
                <p className="text-gray-500">{ai.reason ?? 'AI unavailable'}</p>
              )}
            </div>
          )}

          {/* Position review — risk + suggested SL/TP for the open trade */}
          {positionReview && (() => {
            // All positions on the same symbol AND same direction that will be updated.
            const allMatchingPositions = (positions || []).filter(p =>
              (p.symbol || '').toUpperCase() === symbol.toUpperCase() &&
              (p.side || '').toLowerCase() === (positionReview.isBuy ? 'buy' : 'sell')
            )
            const posCount = allMatchingPositions.length
            return (
            <div className={`rounded-lg p-2.5 text-xs border ${
              positionReview.level === 'high' ? 'bg-red-900/15 border-red-700/40'
              : positionReview.level === 'medium' ? 'bg-amber-900/15 border-amber-700/40'
              : 'bg-green-900/12 border-green-700/30'
            }`}>
              <div className="flex items-center justify-between mb-1.5">
                <span className="flex items-center gap-1.5 font-semibold text-gray-200">
                  <Activity className="w-3.5 h-3.5" /> Position Review
                  {posCount > 1 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-tradebot-accent/20 text-tradebot-accent font-bold" title={`Will apply to all ${posCount} ${positionReview.isBuy ? 'BUY' : 'SELL'} ${symbol} positions`}>
                      ×{posCount}
                    </span>
                  )}
                </span>
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase ${
                  positionReview.level === 'high' ? 'bg-red-500/20 text-red-300'
                  : positionReview.level === 'medium' ? 'bg-amber-500/20 text-amber-300'
                  : 'bg-green-500/20 text-green-300'
                }`}>{positionReview.level} risk</span>
              </div>
              {posCount > 1 && (
                <div className="mb-2 px-2 py-1 rounded bg-tradebot-accent/10 border border-tradebot-accent/20 text-tradebot-accent text-[10px]">
                  Apply buttons will update all {posCount} {positionReview.isBuy ? 'BUY' : 'SELL'} {symbol} positions
                </div>
              )}
              {/* Assessment lines */}
              <ul className="space-y-1 mb-2">
                {positionReview.flags.map((f, i) => (
                  <li key={i} className={`flex items-start gap-1.5 leading-relaxed ${
                    f.level === 'bad' ? 'text-red-300' : f.level === 'warn' ? 'text-amber-300' : 'text-green-300'
                  }`}>
                    {f.level === 'good' ? <CheckCircle className="w-3 h-3 mt-0.5 shrink-0" /> : <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />}
                    <span>{f.text}</span>
                  </li>
                ))}
              </ul>
              {/* Current vs suggested SL/TP */}
              <div className="grid grid-cols-3 gap-1 text-[11px] font-mono mb-2 bg-gray-900/40 rounded p-1.5">
                <div className="text-gray-500"></div>
                <div className="text-gray-500 text-center">Current</div>
                <div className="text-gray-400 text-center font-semibold">Suggested</div>
                <div className="text-gray-500 self-center">SL</div>
                <div className="text-center text-gray-300">{positionReview.curSL ?? '—'}</div>
                <div className="text-center text-cyan-300 font-semibold">{positionReview.suggSL}</div>
                <div className="text-gray-500 self-center">TP</div>
                <div className="text-center text-gray-300">{positionReview.curTP ?? '—'}</div>
                <div className="text-center text-cyan-300 font-semibold">{positionReview.suggTP}</div>
                <div className="text-gray-500 self-center">R:R</div>
                <div className="text-center text-gray-300">{positionReview.curRR ?? '—'}</div>
                <div className="text-center text-cyan-300 font-semibold">{positionReview.suggRR}</div>
              </div>
              <div className="flex items-center justify-between text-[10px] text-gray-500 mb-2">
                <span>SL {positionReview.slBasis} · TP {positionReview.tpBasis}</span>
                {positionReview.suggRiskUSD != null && <span>risk ≈ {accountCurrency} {positionReview.suggRiskUSD}</span>}
              </div>
              {/* Apply buttons */}
              <div className="grid grid-cols-3 gap-1.5">
                <button
                  onClick={() => applyPositionSLTP('sl')}
                  disabled={applying != null}
                  className="flex items-center justify-center gap-1 px-2 py-1.5 rounded bg-red-600/20 text-red-300 border border-red-500/30 text-[11px] font-medium hover:bg-red-600/30 disabled:opacity-50"
                >
                  {applying === 'sl' ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Target className="w-3 h-3" />} Apply SL
                </button>
                <button
                  onClick={() => applyPositionSLTP('tp')}
                  disabled={applying != null}
                  className="flex items-center justify-center gap-1 px-2 py-1.5 rounded bg-green-600/20 text-green-300 border border-green-500/30 text-[11px] font-medium hover:bg-green-600/30 disabled:opacity-50"
                >
                  {applying === 'tp' ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Target className="w-3 h-3" />} Apply TP
                </button>
                <button
                  onClick={() => applyPositionSLTP('both')}
                  disabled={applying != null}
                  className="flex items-center justify-center gap-1 px-2 py-1.5 rounded bg-cyan-600/20 text-cyan-300 border border-cyan-500/30 text-[11px] font-semibold hover:bg-cyan-600/30 disabled:opacity-50"
                >
                  {applying === 'both' ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />} Apply Both
                </button>
              </div>
              {applyMsg && (
                <div className={`mt-2 flex items-center gap-1.5 text-[11px] ${applyMsg.ok ? 'text-green-400' : 'text-red-400'}`}>
                  {applyMsg.ok ? <CheckCircle className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
                  {applyMsg.text}
                </div>
              )}
            </div>
            )
          })()}

          {/* Signals */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs font-semibold text-gray-300">Sniper Setups ({signals.length})</span>
              <div className="flex items-center gap-1 text-[11px] text-gray-400">
                Lot
                <input value={lot} onChange={e => setLot(e.target.value)} className="w-14 bg-gray-800 border border-gray-600 rounded px-1.5 py-0.5 text-white text-xs" />
              </div>
            </div>
            {placeMsg && (
              <div className={`mb-2 px-2 py-1.5 rounded text-[11px] flex items-center gap-1.5 ${placeMsg.ok ? 'bg-green-900/30 text-green-300 border border-green-700/40' : 'bg-red-900/30 text-red-300 border border-red-700/40'}`}>
                {placeMsg.ok ? <CheckCircle className="w-3 h-3" /> : <AlertTriangle className="w-3 h-3" />}{placeMsg.text}
              </div>
            )}
            {signals.length === 0 && !analyzing && (
              <p className="text-xs text-gray-500 py-3 text-center">No sniper-grade setups right now. Wait for price to build structure or lower Min RR.</p>
            )}
            <div className="space-y-2">
              {signals.map((s, i) => {
                const isBuy = s.side === 'buy'
                const aiRate = ai?.rated_signals?.find(r => Math.abs(r.entry - s.entry) <= Math.max(Math.abs(s.entry) * 1e-4, 1e-6))
                return (
                  <div
                    key={`${s.side}-${s.entry}-${i}`}
                    onClick={() => setSelectedIdx(i)}
                    className={`rounded-lg p-2.5 cursor-pointer border transition ${selectedIdx === i ? 'border-tradebot-accent/60 bg-gray-800/70' : 'border-gray-700/50 bg-gray-800/40 hover:bg-gray-800/60'}`}
                  >
                    <div className="flex items-center justify-between">
                      <span className={`font-bold text-sm flex items-center gap-1 ${isBuy ? 'text-blue-400' : 'text-orange-400'}`}>
                        {isBuy ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                        {isBuy ? 'BUY LIMIT' : 'SELL LIMIT'}
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-tradebot-accent/20 text-tradebot-accent font-semibold">
                        {(s.confidence * 100).toFixed(0)}% · RR {s.rr}
                      </span>
                    </div>
                    <div className="grid grid-cols-3 gap-1 mt-1.5 text-[11px] font-mono">
                      <div><span className="text-gray-500">Entry</span><div className="text-gray-200">{s.entry}</div></div>
                      <div><span className="text-gray-500">SL</span><div className="text-red-400">{s.stop_loss}</div></div>
                      <div><span className="text-gray-500">TP</span><div className="text-green-400">{s.take_profit}</div></div>
                    </div>
                    {(s.tp1 ?? 0) > 0 && (
                      <div className="grid grid-cols-3 gap-1 mt-1 text-[10px] font-mono">
                        <div><span className="text-gray-500">TP1</span><div className="text-green-300/80">{s.tp1}</div></div>
                        <div><span className="text-gray-500">TP2</span><div className="text-green-300/80">{s.tp2}</div></div>
                        <div><span className="text-gray-500">TP3</span><div className="text-green-300/80">{s.tp3}</div></div>
                      </div>
                    )}
                    {(s.sl_points ?? 0) > 0 && (
                      <div className="grid grid-cols-3 gap-1 mt-1 text-[10px] font-mono">
                        <div><span className="text-gray-500">Distance</span><div className="text-gray-500">pips · points</div></div>
                        <div><div className="text-red-300/80">{s.sl_pips} · {s.sl_points}</div></div>
                        <div><div className="text-green-300/80">{s.tp_pips} · {s.tp_points}</div></div>
                      </div>
                    )}
                    {accountBalance > 0 && (s.risk_amount ?? 0) > 0 && (
                      <div className="mt-1.5 rounded bg-gray-900/40 px-1.5 py-1">
                        <div className="grid grid-cols-4 gap-1 text-[11px] font-mono">
                          <div><span className="text-gray-500">Lot</span><div className="text-tradebot-accent">{s.lot}</div></div>
                          <div><span className="text-gray-500">Risk</span><div className="text-red-300">-{accountCurrency} {s.risk_amount}</div></div>
                          <div><span className="text-gray-500">R / R %</span><div className="text-gray-300">{s.risk_pct}% / {s.reward_amount && s.risk_amount ? Math.round((s.reward_amount / accountBalance) * 100 * 100) / 100 : 0}%</div></div>
                          <div><span className="text-gray-500">Reward</span><div className="text-green-300">+{accountCurrency} {s.reward_amount}</div></div>
                        </div>
                        {s.risk_exceeds_cap && (
                          <div className="text-[10px] text-amber-300/90 mt-1">⚠ 0.01 lot risks {accountCurrency}{s.risk_amount} &gt; your {accountCurrency}{maxLoss} cap — smaller size not possible at this broker minimum.</div>
                        )}
                      </div>
                    )}
                    {selectedIdx === i && (s.sl_points ?? 0) > 0 && (
                      <div className="mt-2 rounded bg-gray-900/60 border border-gray-700/40 px-2 py-1.5 text-[10px] font-mono leading-relaxed text-gray-400">
                        <div className="text-gray-300 font-semibold mb-1 flex items-center gap-1"><Calculator className="w-3 h-3" /> Calculation</div>
                        <div>1 point = {s.point_size} · 1 pip = {s.pip_size} (10 pts) · contract = {s.contract_size}/lot</div>
                        <div>SL = |{s.entry} − {s.stop_loss}| = {(Math.abs(s.entry - s.stop_loss)).toFixed(s.point_size && s.point_size < 0.001 ? 5 : 2)} → {s.sl_points} pts ({s.sl_pips} pips)</div>
                        <div>TP = |{s.take_profit} − {s.entry}| = {(Math.abs(s.take_profit - s.entry)).toFixed(s.point_size && s.point_size < 0.001 ? 5 : 2)} → {s.tp_points} pts ({s.tp_pips} pips)</div>
                        {accountBalance > 0 && (s.lot ?? 0) > 0 ? (
                          <>
                            <div>pip value = {s.pip_size} × {s.contract_size} × {s.lot} lot = {accountCurrency} {s.pip_value}/pip</div>
                            <div className="text-red-300/90">Risk = {s.sl_pips} pip × {accountCurrency} {s.pip_value} = -{accountCurrency} {s.risk_amount}</div>
                            <div className="text-green-300/90">Reward = {s.tp_pips} pip × {accountCurrency} {s.pip_value} = +{accountCurrency} {s.reward_amount} (RR {s.rr})</div>
                          </>
                        ) : (
                          <div className="text-gray-500">Set an account balance to size the lot and see {accountCurrency} risk/reward per pip.</div>
                        )}
                      </div>
                    )}
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {s.confluence.slice(0, 4).map(c => (
                        <span key={c} className="text-[9px] px-1.5 py-0.5 rounded bg-gray-700/60 text-gray-300">{c.replace(/_/g, ' ')}</span>
                      ))}
                    </div>
                    {aiRate && (
                      <div className={`mt-1.5 text-[10px] ${aiRate.verdict === 'take' ? 'text-green-400' : aiRate.verdict === 'skip' ? 'text-red-400' : 'text-amber-400'}`}>
                        <span className="flex items-center gap-1 font-semibold">
                          <Brain className="w-3 h-3 shrink-0" /> AI: {aiRate.verdict.toUpperCase()}
                        </span>
                        {aiRate.note && (
                          <p className="text-gray-300 font-normal mt-0.5 leading-relaxed break-words">
                            {aiRate.note}
                          </p>
                        )}
                      </div>
                    )}
                    <button
                      onClick={(e) => { e.stopPropagation(); placeSignal(s, i) }}
                      disabled={placingIdx === i}
                      className={`mt-2 w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded text-xs font-medium transition disabled:opacity-50 ${isBuy ? 'bg-blue-600/80 hover:bg-blue-600 text-white' : 'bg-orange-600/80 hover:bg-orange-600 text-white'}`}
                    >
                      {placingIdx === i ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
                      Place Limit + TP
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      </div>

      {/* Backtest */}
      <div className="border-t border-gray-700/50 p-3">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <span className="text-xs font-semibold text-gray-300 flex items-center gap-1.5">
            <FlaskConical className="w-3.5 h-3.5 text-tradebot-accent" /> Backtest · {timeframe}
          </span>
          <div className="flex items-center gap-1.5 flex-wrap">
            {([['5D', 5], ['7D', 7], ['14D', 14], ['30D', 30], ['60D', 60]] as [string, number][]).map(([label, days]) => {
              // Highlight the active preset — compare selected From date to today - days
              const expectedFrom = (() => { const d = new Date(); d.setUTCDate(d.getUTCDate() - days); return d.toISOString().slice(0, 10) })()
              const isActive = btFrom === expectedFrom
              return (
                <button key={label}
                  onClick={() => {
                    const to = new Date()
                    const from = new Date(); from.setUTCDate(from.getUTCDate() - days)
                    setBtFrom(from.toISOString().slice(0, 10))
                    setBtTo(to.toISOString().slice(0, 10))
                  }}
                  className={`px-2 py-1 rounded text-[11px] font-semibold border transition-colors ${
                    isActive
                      ? 'bg-cyan-500/20 border-cyan-400/60 text-cyan-300'
                      : 'bg-gray-800 border-gray-600 text-gray-400 hover:text-white hover:border-gray-400'
                  }`}>
                  {label}
                </button>
              )
            })}
            <label className="flex items-center gap-1 text-[11px] text-gray-400">
              From
              <input type="date" value={btFrom} onChange={e => setBtFrom(e.target.value)}
                className="bg-gray-800 border border-gray-600 rounded px-1.5 py-1 text-white text-[11px]" />
            </label>
            <label className="flex items-center gap-1 text-[11px] text-gray-400">
              To
              <input type="date" value={btTo} onChange={e => setBtTo(e.target.value)}
                className="bg-gray-800 border border-gray-600 rounded px-1.5 py-1 text-white text-[11px]" />
            </label>
            <button
              onClick={runBacktest}
              disabled={backtesting}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyan-600/20 text-cyan-300 border border-cyan-500/30 text-xs font-medium hover:bg-cyan-600/30 disabled:opacity-50"
            >
              {backtesting ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <FlaskConical className="w-3.5 h-3.5" />}
              Run Backtest
            </button>
          </div>
        </div>
        {btError && <p className="text-xs text-red-400 mb-2">{btError}</p>}
        {btStats && (
          <>
            {/* Balance equity metrics (Peak, Low, DD, Recovery) */}
            {btStats.starting_balance != null && btStats.ending_balance != null && (
              <div className="mb-3 rounded-lg border border-gray-700/50 bg-gray-800/40 p-3">
                <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="text-gray-400">Balance</span>
                    <span className="font-mono text-gray-300">{accountCurrency} {btStats.starting_balance.toFixed(2)}</span>
                    <ChevronRight className="w-3.5 h-3.5 text-gray-500" />
                    <span className={`font-mono font-semibold ${(btStats.ending_balance ?? 0) >= (btStats.starting_balance ?? 0) ? 'text-green-400' : 'text-red-400'}`}>
                      {accountCurrency} {btStats.ending_balance.toFixed(2)}
                    </span>
                  </div>
                  <div className={`flex items-center gap-1.5 text-sm font-semibold ${(btStats.net_profit ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {(btStats.net_profit ?? 0) >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
                    {(btStats.net_profit ?? 0) >= 0 ? '+' : ''}{accountCurrency} {(btStats.net_profit ?? 0).toFixed(2)}
                    <span className="text-gray-500 font-normal">({(btStats.net_profit_pct ?? 0) >= 0 ? '+' : ''}{btStats.net_profit_pct}%)</span>
                  </div>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
                  <div><span className="text-gray-500">Peak</span><div className="text-gray-200 font-mono">{accountCurrency} {(btStats.peak_balance ?? 0).toFixed(2)}</div></div>
                  <div><span className="text-gray-500">Low</span><div className="text-gray-200 font-mono">{accountCurrency} {(btStats.min_balance ?? 0).toFixed(2)}</div></div>
                  <div><span className="text-gray-500">Max DD</span><div className="text-red-300 font-mono">{accountCurrency} {(btStats.max_drawdown_currency ?? 0).toFixed(2)}</div></div>
                  <div><span className="text-gray-500">Recovered</span><div className={btStats.recovered ? 'text-green-400 font-semibold' : 'text-amber-400 font-semibold'}>{btStats.recovered ? 'YES ✓' : 'NO'}</div></div>
                </div>
              </div>
            )}
            {/* Risk-control: max-loss cap + daily profit target */}
            {(btStats.max_loss_cap ?? 0) > 0 && (
              <div className="mb-3 rounded-lg border border-gray-700/50 bg-gray-800/40 p-3 text-[11px]">
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  <div>
                    <span className="text-gray-500">Max loss cap</span>
                    <div className="text-gray-200 font-mono">{accountCurrency} {btStats.max_loss_cap?.toFixed(2)}</div>
                  </div>
                  <div>
                    <span className="text-gray-500">Worst loss hit</span>
                    <div className={`font-mono font-semibold ${(btStats.loss_cap_respected ?? true) ? 'text-green-400' : 'text-red-400'}`}>
                      {accountCurrency} {btStats.max_total_loss_seen?.toFixed(2)} {(btStats.loss_cap_respected ?? true) ? '✓' : '✕'}
                    </div>
                  </div>
                  <div>
                    <span className="text-gray-500">Avg / day</span>
                    <div className={`font-mono ${(btStats.avg_daily_profit ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {(btStats.avg_daily_profit ?? 0) >= 0 ? '+' : ''}{accountCurrency} {btStats.avg_daily_profit?.toFixed(2)} ({btStats.avg_daily_pct}%)
                    </div>
                  </div>
                  <div>
                    <span className="text-gray-500">Days hit {btStats.daily_target_pct}% target</span>
                    <div className="text-gray-200 font-mono">{btStats.days_hit_target}/{btStats.trading_days}</div>
                  </div>
                </div>
                {(btStats.skipped_trades ?? 0) > 0 && (
                  <p className="text-[10px] text-amber-300/80 mt-2 leading-relaxed">
                    {btStats.skipped_trades} setup{(btStats.skipped_trades ?? 0) === 1 ? '' : 's'} skipped — the minimum 0.01 lot would have risked more than the
                    remaining {accountCurrency}{btStats.max_loss_cap} budget, so they were not taken (this is how the loss cap is honoured).
                    On a {accountCurrency}{(btStats.starting_balance ?? 0).toFixed(0)} balance a {btStats.daily_target_pct}%/day target is not reachable inside a {accountCurrency}{btStats.max_loss_cap} loss limit.
                  </p>
                )}
              </div>
            )}
            {/* Per-day profit accumulation — always show when we have daily data */}
            {(btStats.daily_breakdown?.length ?? 0) > 0 && (
              <div className="mb-3 rounded-lg border border-blue-700/30 bg-blue-900/10 p-2">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[11px] font-semibold text-blue-300 uppercase tracking-wide">Daily Profit Accumulation</span>
                  <span className="text-[10px] text-gray-500">{btStats.daily_breakdown!.length} trading days</span>
                </div>
                <div className="space-y-1">
                  {btStats.daily_breakdown!.map((d, idx) => {
                    const isProfit = d.profit >= 0
                    const maxAbs = Math.max(...btStats.daily_breakdown!.map(x => Math.abs(x.profit)), 0.01)
                    const barW = Math.round((Math.abs(d.profit) / maxAbs) * 60)
                    return (
                      <div key={d.day} className="flex items-center gap-2 text-[11px] font-mono">
                        {/* Date */}
                        <span className="text-gray-400 w-12 shrink-0">{d.day.slice(5)}</span>
                        {/* Trades */}
                        <span className="text-gray-600 w-7 shrink-0 text-right">{d.trades}t</span>
                        {/* Visual bar */}
                        <div className="flex-1 flex items-center gap-1">
                          <div
                            className={`h-2.5 rounded-sm shrink-0 ${isProfit ? 'bg-green-500/70' : 'bg-red-500/70'}`}
                            style={{ width: `${barW}%`, minWidth: 2 }}
                          />
                        </div>
                        {/* % change */}
                        <span className={`w-14 text-right shrink-0 font-semibold ${isProfit ? 'text-green-400' : 'text-red-400'}`}>
                          {isProfit ? '+' : ''}{d.pct}%
                        </span>
                        {/* $ P&L */}
                        <span className={`w-16 text-right shrink-0 ${isProfit ? 'text-green-300/80' : 'text-red-300/80'}`}>
                          {isProfit ? '+' : ''}{accountCurrency}{d.profit.toFixed(2)}
                        </span>
                        {/* Running balance */}
                        <span className="w-20 text-right shrink-0 text-gray-300 font-semibold">
                          {accountCurrency}{d.balance.toFixed(2)}
                        </span>
                      </div>
                    )
                  })}
                </div>
                {/* Summary row */}
                {btStats.starting_balance != null && btStats.ending_balance != null && (
                  <div className="mt-2 pt-2 border-t border-blue-700/20 flex items-center justify-between text-[11px]">
                    <span className="text-gray-500">Start → End</span>
                    <span className="font-mono">
                      <span className="text-gray-400">{accountCurrency}{btStats.starting_balance.toFixed(2)}</span>
                      <span className="text-gray-600 mx-1">→</span>
                      <span className={`font-bold ${(btStats.ending_balance ?? 0) >= (btStats.starting_balance ?? 0) ? 'text-green-400' : 'text-red-400'}`}>
                        {accountCurrency}{btStats.ending_balance.toFixed(2)}
                      </span>
                      <span className="text-gray-500 ml-1">({(btStats.net_profit_pct ?? 0) >= 0 ? '+' : ''}{btStats.net_profit_pct}%)</span>
                    </span>
                  </div>
                )}
              </div>
            )}
            <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mb-3">
              <Stat label="Trades" value={String(btStats.total)} />
              <Stat label="Win rate" value={`${btStats.win_rate}%`} good={btStats.win_rate >= 80} />
              <Stat label="Total R" value={btStats.total_r.toFixed(1)} good={btStats.total_r >= 0} />
              <Stat label="Expectancy" value={`${btStats.expectancy_r}R`} good={btStats.expectancy_r >= 0} />
              <Stat label="Profit factor" value={btStats.profit_factor.toFixed(2)} good={btStats.profit_factor >= 1} />
              <Stat label="Max DD" value={`${btStats.max_drawdown_r}R`} good={btStats.max_drawdown_r > -5} />
            </div>
            <p className="text-[10px] text-gray-500 mb-2 leading-relaxed">
              Win rate is over decided trades (wins vs losses). Management: book a partial at 0.6R and move
              the stop to breakeven; once price is 50% of the way to TP3 a trailing stop activates and locks
              ~75% of the run, so after TP2 profit is protected and the runner extends until the trail is
              hit{(btStats.breakevens ?? 0) > 0 ? ` · ${btStats.breakevens} breakeven scratch${(btStats.breakevens ?? 0) === 1 ? '' : 'es'}` : ''}.
            </p>
            {btTrades.length > 0 && (
              <div className="overflow-x-auto max-h-48 overflow-y-auto">
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 bg-[#0b0e16]">
                    <tr className="text-gray-500 border-b border-gray-700/40">
                      <th className="text-left py-1 px-2">Filled</th>
                      <th className="text-left py-1 px-2">Side</th>
                      <th className="text-right py-1 px-2">Entry</th>
                      <th className="text-right py-1 px-2">Exit</th>
                      <th className="text-center py-1 px-2">Result</th>
                      <th className="text-right py-1 px-2">R</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800/60">
                    {btTrades.slice().reverse().map((t, i) => (
                      <tr key={i} className="hover:bg-gray-800/30">
                        <td className="py-1 px-2 text-gray-400 whitespace-nowrap">{formatTimeZA(t.fill_time * 1000)}</td>
                        <td className={`py-1 px-2 font-semibold ${t.side === 'buy' ? 'text-blue-400' : 'text-orange-400'}`}>{t.side.toUpperCase()}</td>
                        <td className="py-1 px-2 text-right font-mono text-gray-300">{t.entry}</td>
                        <td className="py-1 px-2 text-right font-mono text-gray-300">{t.exit_price}</td>
                        <td className="py-1 px-2 text-center">
                          <span className={`px-1.5 py-0.5 rounded text-[9px] font-semibold ${t.outcome === 'win' ? 'bg-green-500/20 text-green-400' : t.outcome === 'breakeven' ? 'bg-amber-500/20 text-amber-300' : 'bg-red-500/20 text-red-400'}`}>{t.outcome}{t.trailed ? ' ⤴' : ''}</span>
                        </td>
                        <td className={`py-1 px-2 text-right font-mono font-semibold ${t.r_multiple > 0 ? 'text-green-400' : t.r_multiple < 0 ? 'text-red-400' : 'text-gray-400'}`}>{t.r_multiple >= 0 ? '+' : ''}{t.r_multiple}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {/* AI Backtest Analysis */}
            {btAiAnalysis && btAiAnalysis.available && (
              <div className="mt-3 rounded-lg border border-purple-700/40 bg-purple-900/20 p-3">
                <div className="flex items-center gap-2 mb-2">
                  <Brain className="w-4 h-4 text-purple-400" />
                  <span className="text-xs font-semibold text-purple-300 uppercase">AI Analysis</span>
                  <span className={`ml-auto px-2 py-0.5 rounded text-xs font-semibold ${
                    btAiAnalysis.verdict === 'strong' ? 'bg-green-500/20 text-green-400' :
                    btAiAnalysis.verdict === 'workable' ? 'bg-amber-500/20 text-amber-300' :
                    'bg-red-500/20 text-red-400'
                  }`}>{btAiAnalysis.verdict || 'unknown'}</span>
                </div>
                {btAiAnalysis.summary && (
                  <p className="text-[11px] text-purple-200 mb-2 leading-relaxed">{btAiAnalysis.summary}</p>
                )}
                {(btAiAnalysis.strengths?.length ?? 0) > 0 && (
                  <div className="mb-2">
                    <div className="text-[10px] font-semibold text-green-300 mb-1">Strengths</div>
                    <ul className="text-[11px] text-green-200/80 space-y-0.5 ml-2">
                      {btAiAnalysis.strengths?.map((s, i) => (
                        <li key={i} className="list-disc">{s}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(btAiAnalysis.weaknesses?.length ?? 0) > 0 && (
                  <div className="mb-2">
                    <div className="text-[10px] font-semibold text-red-300 mb-1">Weaknesses</div>
                    <ul className="text-[11px] text-red-200/80 space-y-0.5 ml-2">
                      {btAiAnalysis.weaknesses?.map((w, i) => (
                        <li key={i} className="list-disc">{w}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(btAiAnalysis.recommendations?.length ?? 0) > 0 && (
                  <div>
                    <div className="text-[10px] font-semibold text-blue-300 mb-1">Recommendations</div>
                    <ul className="text-[11px] text-blue-200/80 space-y-0.5 ml-2">
                      {btAiAnalysis.recommendations?.map((r, i) => (
                        <li key={i} className="list-disc">{r}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {!btStats && !backtesting && !btError && (
          <p className="text-xs text-gray-500">Run a backtest to see how these sniper rules would have performed on recent history.</p>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-2 text-center">
      <div className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-sm font-bold ${good === undefined ? 'text-gray-200' : good ? 'text-green-400' : 'text-red-400'}`}>{value}</div>
    </div>
  )
}
