import Head from 'next/head'
import { useState, useEffect, useRef, useCallback } from 'react'
import {
  ComposedChart,
  Area,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts'
import { apiClient } from '@/services/api'
import { Activity, ChevronRight, RefreshCw } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface MarketOverview {
  total_market_cap_usd: number
  total3_estimate_usd: number
  total_volume_24h_usd: number
  volume_market_cap_ratio: number
  market_cap_change_24h_pct: number
  btc_dominance: number
  eth_dominance: number
  altcoin_dominance: number
  active_cryptocurrencies: number
  fear_greed_value: number
  fear_greed_label: string
  sentiment: string
  sentiment_reasons: string[]
  updated_at: string
}

interface HistoryPoint {
  date_iso: string
  market_cap_usd: number
  volume_usd: number
  divergence_zone: string
}

type ChartSymbol = 'CRYPTOCAP:TOTAL3' | 'CRYPTOCAP:TOTAL2' | 'CRYPTOCAP:TOTAL'

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt(n: number): string {
  if (!n) return '$0'
  if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`
  if (n >= 1e9)  return `$${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6)  return `$${(n / 1e6).toFixed(1)}M`
  return `$${n.toFixed(0)}`
}

function fmtAxis(n: number): string {
  if (!n) return '0'
  if (n >= 1e12) return `${(n / 1e12).toFixed(1)}T`
  if (n >= 1e9)  return `${(n / 1e9).toFixed(0)}B`
  return `${n.toFixed(0)}`
}

const ZONE_COLORS: Record<string, string> = {
  confirmed_bull: '#22c55e',
  distribution:   '#eab308',
  confirmed_bear: '#ef4444',
  accumulation:   '#3b82f6',
  neutral:        '#6b7280',
}

const SENTIMENT_CONFIG: Record<string, {
  emoji: string
  label: string
  color: string
  bg: string
  border: string
}> = {
  STRONG_BULL: { emoji: '🐂', label: 'STRONG BULL', color: 'text-emerald-400', bg: 'bg-emerald-500/15', border: 'border-emerald-500/40' },
  BULL:        { emoji: '↑',  label: 'BULL',         color: 'text-green-400',   bg: 'bg-green-500/15',   border: 'border-green-500/40' },
  WEAK_BULL:   { emoji: '~',  label: 'WEAK BULL',    color: 'text-yellow-400',  bg: 'bg-yellow-500/15',  border: 'border-yellow-500/40' },
  NEUTRAL:     { emoji: '⟺',  label: 'NEUTRAL',      color: 'text-gray-400',    bg: 'bg-gray-700/40',    border: 'border-gray-600' },
  WEAK_BEAR:   { emoji: '~',  label: 'WEAK BEAR',    color: 'text-orange-400',  bg: 'bg-orange-500/15',  border: 'border-orange-500/40' },
  BEAR:        { emoji: '↓',  label: 'BEAR',         color: 'text-red-400',     bg: 'bg-red-500/15',     border: 'border-red-500/40' },
  STRONG_BEAR: { emoji: '🐻', label: 'STRONG BEAR',  color: 'text-red-500',     bg: 'bg-red-600/15',     border: 'border-red-600/40' },
}

function fgColor(v: number): string {
  if (v <= 25) return 'text-red-400'
  if (v <= 45) return 'text-orange-400'
  if (v >= 80) return 'text-yellow-400'
  if (v >= 60) return 'text-green-400'
  return 'text-gray-300'
}

// ── TradingView chart (raw CRYPTOCAP symbol, no exchange prefix) ──────────────

function MarketCapTVChart({ symbol }: { symbol: ChartSymbol }) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current) return
    const container = containerRef.current
    container.innerHTML = ''

    const widgetDiv = document.createElement('div')
    widgetDiv.className = 'tradingview-widget-container'
    widgetDiv.style.cssText = 'height:100%;width:100%'

    const inner = document.createElement('div')
    inner.className = 'tradingview-widget-container__widget'
    inner.style.cssText = 'height:100%;width:100%'
    widgetDiv.appendChild(inner)
    container.appendChild(widgetDiv)

    const script = document.createElement('script')
    script.type = 'text/javascript'
    script.src =
      'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js'
    script.async = true
    script.textContent = JSON.stringify({
      symbol,
      interval: '60',
      timezone: 'Africa/Johannesburg',
      theme: 'dark',
      style: '1',
      locale: 'en',
      backgroundColor: '#0a0e27',
      gridColor: 'rgba(26, 30, 58, 0.8)',
      allow_symbol_change: true,
      calendar: false,
      hide_side_toolbar: false,
      hide_top_toolbar: false,
      hide_legend: false,
      hide_volume: false,
      save_image: true,
      withdateranges: true,
      support_host: 'https://www.tradingview.com',
      studies: ['CM_Williams_Vix_Fix@tv-basicstudies', 'Volume@tv-basicstudies'],
      autosize: true,
    })
    widgetDiv.appendChild(script)

    return () => {
      container.innerHTML = ''
    }
  }, [symbol])

  return (
    <div
      ref={containerRef}
      className="w-full rounded-xl overflow-hidden border border-gray-700/60"
      style={{ height: 520 }}
    />
  )
}

// ── Metric card ───────────────────────────────────────────────────────────────

function MetricCard({
  label,
  value,
  sub,
  color = 'text-white',
}: {
  label: string
  value: string
  sub?: string
  color?: string
}) {
  return (
    <div className="bg-gray-800/40 border border-gray-700/60 rounded-xl p-4">
      <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">{label}</div>
      <div className={`text-xl font-bold leading-tight ${color}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function MarketCapPage() {
  const [overview, setOverview]   = useState<MarketOverview | null>(null)
  const [history, setHistory]     = useState<HistoryPoint[]>([])
  const [loading, setLoading]     = useState(true)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [tvSymbol, setTvSymbol]   = useState<ChartSymbol>('CRYPTOCAP:TOTAL3')

  const fetchOverview = useCallback(async () => {
    try {
      const res = await apiClient.getMarketOverview()
      setOverview(res.data)
      setLastRefresh(new Date())
    } catch (e) {
      console.error('[MarketCap] overview fetch failed', e)
    }
  }, [])

  const fetchHistory = useCallback(async () => {
    try {
      const res = await apiClient.getMarketHistory(30)
      setHistory(res.data)
    } catch (e) {
      console.error('[MarketCap] history fetch failed', e)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    Promise.all([fetchOverview(), fetchHistory()]).finally(() => setLoading(false))

    // auto-refresh overview every 60 s
    const interval = setInterval(fetchOverview, 60_000)
    return () => clearInterval(interval)
  }, [fetchOverview, fetchHistory])

  const sc       = overview ? (SENTIMENT_CONFIG[overview.sentiment] ?? SENTIMENT_CONFIG.NEUTRAL) : null
  const chgColor = !overview
    ? 'text-gray-400'
    : overview.market_cap_change_24h_pct > 0
    ? 'text-green-400'
    : overview.market_cap_change_24h_pct < 0
    ? 'text-red-400'
    : 'text-gray-400'

  const chgPrefix = overview && overview.market_cap_change_24h_pct > 0 ? '+' : ''

  return (
    <>
      <Head><title>TradeBot — Market Cap Monitor</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">

        {/* ── Header ── */}
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white">Market Cap Monitor</h1>
            <p className="text-sm text-gray-400 mt-1">
              TOTAL3 (ex-BTC &amp; ETH) · real-time bull/bear via volume analysis
            </p>
          </div>
          <button
            onClick={fetchOverview}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-gray-800/60 border border-gray-700 text-sm text-gray-400 hover:text-white transition"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            {lastRefresh ? lastRefresh.toLocaleTimeString() : 'Refresh'}
          </button>
        </div>

        {/* ── Metrics Row ── */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <MetricCard
            label="TOTAL3"
            value={fmt(overview?.total3_estimate_usd ?? 0)}
            sub="ex-BTC &amp; ETH"
          />
          <MetricCard
            label="24h Volume"
            value={fmt(overview?.total_volume_24h_usd ?? 0)}
            sub="global market"
          />
          <MetricCard
            label="24h Change"
            value={overview
              ? `${chgPrefix}${overview.market_cap_change_24h_pct.toFixed(2)}%`
              : '—'}
            color={chgColor}
            sub="total market cap"
          />
          <MetricCard
            label="BTC Dominance"
            value={overview ? `${overview.btc_dominance.toFixed(1)}%` : '—'}
            sub="of total cap"
          />
          <MetricCard
            label="ETH Dominance"
            value={overview ? `${overview.eth_dominance.toFixed(1)}%` : '—'}
            sub="of total cap"
          />
          <MetricCard
            label="Fear &amp; Greed"
            value={overview ? `${overview.fear_greed_value}` : '—'}
            color={overview ? fgColor(overview.fear_greed_value) : 'text-gray-400'}
            sub={overview?.fear_greed_label ?? ''}
          />
        </div>

        {/* ── Signal Banner ── */}
        {overview && sc && (
          <div className={`rounded-xl border p-5 ${sc.bg} ${sc.border}`}>
            <div className="flex items-start gap-4">
              <div className="text-4xl pt-0.5" aria-hidden="true">
                {sc.emoji}
              </div>
              <div className="flex-1 min-w-0">
                <div className={`text-xl font-bold ${sc.color}`}>
                  {sc.label} MARKET
                </div>
                <ul className="mt-2 space-y-1">
                  {overview.sentiment_reasons.map((r, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-sm text-gray-300">
                      <ChevronRight className="w-3.5 h-3.5 mt-0.5 shrink-0 text-gray-500" />
                      {r}
                    </li>
                  ))}
                </ul>
              </div>
              <div className="text-right text-xs text-gray-500 shrink-0">
                <div>Vol / MCap</div>
                <div className="text-base font-semibold text-gray-300 tabular-nums">
                  {(overview.volume_market_cap_ratio * 100).toFixed(2)}%
                </div>
                <div className="mt-2">Active coins</div>
                <div className="text-base font-semibold text-gray-300 tabular-nums">
                  {overview.active_cryptocurrencies.toLocaleString()}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ── TradingView Chart ── */}
        <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h2 className="font-semibold text-white">Market Cap Chart</h2>
              <p className="text-xs text-gray-500">
                1h · TradingView · Williams Vix Fix + Volume · Live streaming data
              </p>
            </div>
            {/* Symbol toggle */}
            <div className="flex gap-1.5">
              {(['CRYPTOCAP:TOTAL3', 'CRYPTOCAP:TOTAL2', 'CRYPTOCAP:TOTAL'] as ChartSymbol[]).map(
                (s) => (
                  <button
                    key={s}
                    onClick={() => setTvSymbol(s)}
                    className={`px-2.5 py-1 rounded text-xs font-medium transition ${
                      tvSymbol === s
                        ? 'bg-[#00d4ff]/20 text-[#00d4ff] border border-[#00d4ff]/40'
                        : 'bg-gray-700/60 text-gray-400 hover:text-white border border-transparent'
                    }`}
                  >
                    {s.replace('CRYPTOCAP:', '')}
                  </button>
                )
              )}
            </div>
          </div>
          <MarketCapTVChart symbol={tvSymbol} />
        </div>

        {/* ── Volume Divergence Panel ── */}
        {history.length > 0 && (
          <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
            <div className="flex items-start justify-between mb-4 gap-4">
              <div>
                <h2 className="font-semibold text-white">Volume Divergence (30 days)</h2>
                <p className="text-xs text-gray-500 mt-0.5">
                  Bar colour = divergence zone · Market Cap (left axis) · Volume (right axis)
                </p>
              </div>
              {/* Legend */}
              <div className="flex flex-wrap gap-x-4 gap-y-1 justify-end text-xs text-gray-400 shrink-0">
                {[
                  { key: 'confirmed_bull', label: 'Confirmed Bull' },
                  { key: 'distribution',   label: 'Distribution'   },
                  { key: 'confirmed_bear', label: 'Confirmed Bear' },
                  { key: 'accumulation',   label: 'Accumulation'   },
                ].map((z) => (
                  <span key={z.key} className="flex items-center gap-1.5">
                    <span
                      className="w-2.5 h-2.5 rounded-sm inline-block"
                      style={{ background: ZONE_COLORS[z.key] }}
                    />
                    {z.label}
                  </span>
                ))}
              </div>
            </div>

            <ResponsiveContainer width="100%" height={280}>
              <ComposedChart data={history} margin={{ top: 4, right: 16, left: 4, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2a3a" />
                <XAxis
                  dataKey="date_iso"
                  tick={{ fill: '#6b7280', fontSize: 10 }}
                  tickFormatter={(d) => d.slice(5)}
                  interval="preserveStartEnd"
                />
                <YAxis
                  yAxisId="mc"
                  orientation="left"
                  tick={{ fill: '#6b7280', fontSize: 10 }}
                  tickFormatter={fmtAxis}
                  width={52}
                />
                <YAxis
                  yAxisId="vol"
                  orientation="right"
                  tick={{ fill: '#6b7280', fontSize: 10 }}
                  tickFormatter={fmtAxis}
                  width={52}
                />
                <Tooltip
                  contentStyle={{
                    background: '#111827',
                    border: '1px solid #374151',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  labelStyle={{ color: '#9ca3af', marginBottom: 4 }}
                  formatter={(value: number, name: string) => [
                    fmt(value),
                    name === 'market_cap_usd' ? 'Market Cap' : 'Volume',
                  ]}
                />
                <Area
                  yAxisId="mc"
                  type="monotone"
                  dataKey="market_cap_usd"
                  stroke="#6366f1"
                  strokeWidth={2}
                  fill="#6366f1"
                  fillOpacity={0.1}
                  name="market_cap_usd"
                  dot={false}
                  activeDot={{ r: 4 }}
                />
                <Bar
                  yAxisId="vol"
                  dataKey="volume_usd"
                  name="volume_usd"
                  maxBarSize={22}
                >
                  {history.map((entry, i) => (
                    <Cell
                      key={i}
                      fill={ZONE_COLORS[entry.divergence_zone] ?? '#6b7280'}
                      fillOpacity={0.8}
                    />
                  ))}
                </Bar>
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}

        {/* Loading state */}
        {loading && !overview && (
          <div className="text-center py-20 text-gray-500">
            <Activity className="w-8 h-8 mx-auto mb-3 animate-pulse" />
            <p className="text-sm">Fetching market data…</p>
          </div>
        )}
      </div>
    </>
  )
}
