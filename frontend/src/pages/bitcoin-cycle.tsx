/**
 * Bitcoin Cycle — the 1064-day calendar, on one page.
 *
 * The pattern: every completed BTC cycle has run ~1064 days from bottom to
 * top, then ~365 back down. This page draws those boxes on the price chart
 * exactly as the pattern is read, shows where today sits inside the live
 * box, and answers the only question that matters day to day: what did this
 * same day-of-cycle do in every previous cycle.
 *
 * Everything the agents see (`context["btc_cycle"]`) is computed from the
 * same snapshot this page renders — the board and the screen cannot disagree.
 */
import { useEffect, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import Head from 'next/head'
import {
  ArrowLeft,
  ArrowRight,
  CalendarDays,
  ChevronRight,
  Flag,
  Loader2,
  RefreshCw,
  Sparkles,
  TrendingUp,
  Fish as WhaleIcon,
} from 'lucide-react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import {
  useBtcCyclePage,
  useWhaleWatch,
  type CycleCalendarDay,
  type CycleState,
  type WhalePayload,
} from '@/hooks/useBtcCycle'
import { apiClient } from '@/services/api'
import type { IndicatorMarker, IndicatorOverlaySeries } from '@/components/TradingViewChart'
import { toCycleBoxes, phaseColor } from '@/utils/cycleOverlay'
import type { CycleWindowBox } from '@/utils/zonesOverlay'
import { toReasoningText } from '@/utils/reasoning'

const TradingViewChart = dynamic(() => import('@/components/TradingViewChart'), { ssr: false })

/** How many candles to ask for per TF to reach genesis (2010-07-17). */
export const TF_GENESIS_LIMIT: Record<string, number> = {
  MN1: 300,   // ~195 months + buffer
  W1: 1200,   // 842 weeks + buffer
  D1: 7000,   // 5.9k days
  H4: 40000,  // 35k 4h
  H1: 150000, // 141k hours
}
function tfToCycleApi(tf: string): string {
  const u = (tf || 'D1').toUpperCase()
  if (u === 'MN1' || u === '1M') return 'MN1'
  if (['W1', 'D1', 'H4', 'H1'].includes(u)) return u
  return 'D1'
}
function tfToPatternApi(tf: string): string {
  // Bitget pattern endpoint expects 1d rather than D1 for daily etc.
  const map: Record<string, string> = { MN1: 'MN1', W1: '1w', D1: '1d', H4: '4h', H1: '1h' }
  return map[tfToCycleApi(tf)] ?? '1d'
}
/** One candle shape shared by /cycle/candles and /cycle/chart. */
interface CycleChartCandle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume?: number
  synthetic?: boolean
}
/** Genesis-aware candle feed for every TF: MN1/W1/D1 via Yahoo+CoinMetrics, H1/H4 via Bitget+synthetic. */
function useCycleCandles(timeframe: string) {
  const tf = tfToCycleApi(timeframe)
  const [candles, setCandles] = useState<CycleChartCandle[]>([])
  const [markers, setMarkers] = useState<IndicatorMarker[]>([])
  const [fisherOverlays, setFisherOverlays] = useState<IndicatorOverlaySeries[]>([])
  const [count, setCount] = useState<number>(0)
  const [earliest, setEarliest] = useState<number | null>(null)
  const [earliestIso, setEarliestIso] = useState<string | null>(null)
  const [synthetic, setSynthetic] = useState<number>(0)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const ctrl = new AbortController()
    setLoaded(false)
    setError(null)
    void (async () => {
      try {
        const limit = TF_GENESIS_LIMIT[tf] ?? 7000
        const { data } = await apiClient.getCycleCandles(tf, limit, ctrl.signal)
        if (!alive) return
        setCandles(data?.candles ?? [])
        setCount(data?.count ?? 0)
        setEarliest(data?.earliest ?? null)
        setEarliestIso(data?.earliest_iso ?? null)
        setSynthetic(data?.synthetic_count ?? 0)
        // Pattern + Fisher for the same TF so markers align with the visible TF
        const reach = Math.max((data?.candles?.length ?? 0), limit)
        try {
          const pat = await apiClient.getPatternOverlay('bitget', 'BTCUSDT', {
            timeframe: tfToPatternApi(tf),
            limit: Math.min(reach, 3000),
          })
          if (!alive) return
          setMarkers(pat.data?.markers ?? [])
          setFisherOverlays(pat.data?.overlays ?? [])
        } catch {
          /* chart stands without pattern flags */
        }
      } catch (e: any) {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!alive) return
        setError(e?.response?.data?.detail || e?.message || 'Failed to load candles')
      } finally {
        if (alive) setLoaded(true)
      }
    })()
    return () => { alive = false; ctrl.abort() }
  }, [tf])

  return { candles, markers, fisherOverlays, count, earliest, earliestIso, synthetic, loaded, error }
}
// Backward compat alias — old page code used useCycleMonthly for MN1 only.
function useCycleMonthly() {
  return useCycleCandles('MN1')
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function fmtDate(iso: string): string {
  if (!iso) return '—'
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, {
    day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
  })
}

function fmtPrice(v?: number | null): string {
  return typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—'
}

/** Trend + momentum overlays for the chart, with per-group toggles — TF-aware. */
const INDICATOR_GROUPS = ['ema', 'rsi', 'macd'] as const
type IndicatorGroup = (typeof INDICATOR_GROUPS)[number]

function useIndicatorOverlays(chartTf?: string) {
  const tf = tfToCycleApi(chartTf ?? 'D1')
  const [overlays, setOverlays] = useState<IndicatorOverlaySeries[]>([])
  const [trend, setTrend] = useState<string | null>(null)
  const [enabled, setEnabled] = useState<Record<IndicatorGroup, boolean>>({
    ema: true, rsi: false, macd: false,
  })

  useEffect(() => {
    let alive = true
    const ctrl = new AbortController()
    void (async () => {
      try {
        const patTf = tfToPatternApi(tf)
        // Ask enough history for EMA200 warm-up: need >200 bars regardless of TF
        const want = Math.max(TF_GENESIS_LIMIT[tf] ?? 500, 500)
        const lim = Math.min(want, 1500) // indicator endpoint caps at 1500 for performance, EMA200 still covered
        const { data } = await apiClient.technical.indicatorOverlay('bitget', 'BTCUSDT', {
          timeframe: patTf, limit: lim, indicators: 'ema,rsi,macd',
        })
        if (alive) {
          setOverlays(data?.overlays ?? [])
          setTrend(data?.signal?.trend ?? null)
        }
      } catch {
        /* the chart stands without indicators */
      }
    })()
    return () => { alive = false; ctrl.abort() }
  }, [tf])

  const visible = useMemo(
    () => overlays.filter((o) => {
      const name = o.name.toLowerCase()
      if (name.startsWith('ema')) return enabled.ema
      if (name.startsWith('rsi')) return enabled.rsi
      if (name.startsWith('macd')) return enabled.macd
      return true
    }),
    [overlays, enabled],
  )

  return { visible, trend, enabled, setEnabled }
}

/** The header strip: season, position, countdowns, and how much to trust it. */
function CycleHeader({ state }: { state: CycleState }) {
  const color = phaseColor(state.phase)
  const hitRate = state.validation?.top_hit_rate
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className={`flex items-center gap-2 rounded-xl border px-4 py-2 ${color.border} ${color.bg}`}>
        <RefreshCw className={`h-5 w-5 ${color.text}`} />
        <div>
          <div className={`text-lg font-bold uppercase leading-none ${color.text}`}>
            {state.phase} market
          </div>
          <div className="mt-0.5 text-[11px] text-slate-400">
            day {state.day_of_cycle} of the cycle · anchor {fmtDate(state.anchor)}
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 px-4 py-2">
        <div className="text-[10px] uppercase tracking-wide text-slate-500">Projected top</div>
        <div className="font-mono text-sm text-slate-200">{fmtDate(state.projected_top)}</div>
        <div className="text-[11px] text-cyan-300">
          {state.days_to_top >= 0 ? `in ${state.days_to_top} days` : `${-state.days_to_top} days past`}
        </div>
      </div>

      <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 px-4 py-2">
        <div className="text-[10px] uppercase tracking-wide text-slate-500">Projected bottom</div>
        <div className="font-mono text-sm text-slate-200">{fmtDate(state.projected_bottom)}</div>
        <div className="text-[11px] text-amber-300">
          {state.days_to_bottom >= 0 ? `in ${state.days_to_bottom} days` : `${-state.days_to_bottom} days past`}
        </div>
      </div>

      {state.price != null && (
        <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 px-4 py-2">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">BTC</div>
          <div className="font-mono text-sm text-slate-200">${fmtPrice(state.price)}</div>
          {state.cycle_high != null && (
            <div className="text-[11px] text-slate-400">
              {(((state.price / state.cycle_high) - 1) * 100).toFixed(1)}% off cycle high
            </div>
          )}
        </div>
      )}

      {hitRate != null && (
        <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 px-4 py-2">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">Pattern hit rate</div>
          <div className="font-mono text-sm text-slate-200">
            {Math.round(hitRate * 100)}% of cycles
          </div>
          <div className="text-[11px] text-slate-400">
            tops within ±{state.validation?.tolerance_days ?? 45}d
          </div>
        </div>
      )}

      {state.late_phase && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300">
          Caution window — a phase turn is inside {Math.max(state.days_to_top, state.days_to_bottom)}d
        </div>
      )}
    </div>
  )
}

/** The month grid: every day painted its season, markers where they belong. */
function CycleCalendarGrid({
  days,
  year,
  month,
  onShift,
  onSelect,
  selected,
  loading,
}: {
  days: CycleCalendarDay[]
  year: number
  month: number
  onShift: (delta: number) => void
  onSelect: (d: CycleCalendarDay) => void
  selected: string | null
  loading: boolean
}) {
  const leading = days.length ? days[0].weekday : 0
  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
      <div className="mb-2 flex items-center gap-2">
        <CalendarDays className="h-4 w-4 text-cyan-300" />
        <h2 className="text-sm font-semibold text-slate-200">
          {MONTHS[month - 1]} {year}
        </h2>
        <div className="ml-auto flex items-center gap-1">
          {loading && <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-500" />}
          <button
            type="button"
            onClick={() => onShift(-1)}
            className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:border-slate-500 hover:text-slate-200"
            title="Previous month"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => onShift(1)}
            className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:border-slate-500 hover:text-slate-200"
            title="Next month"
          >
            <ArrowRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="grid grid-cols-7 gap-1 text-center">
        {WEEKDAYS.map((w) => (
          <div key={w} className="py-1 text-[10px] uppercase tracking-wide text-slate-500">
            {w}
          </div>
        ))}
        {Array.from({ length: leading }).map((_, i) => (
          <div key={`blank-${i}`} />
        ))}
        {days.map((d) => {
          const c = phaseColor(d.phase)
          const dim = d.projected ? 'opacity-60' : ''
          const selectedCls = selected === d.date ? 'ring-2 ring-cyan-400' : ''
          return (
            <button
              key={d.date}
              type="button"
              onClick={() => onSelect(d)}
              className={`relative rounded-md border px-1 py-1.5 text-left transition hover:brightness-125 ${c.border} ${c.bg} ${dim} ${selectedCls} ${
                d.is_today ? 'ring-2 ring-cyan-400/80' : ''
              }`}
              title={`${d.date} — ${d.phase} day ${d.day_of_cycle} of the cycle${d.projected ? ' (projected)' : ''}`}
            >
              <span className="block font-mono text-[11px] leading-none text-slate-200">
                {Number(d.date.slice(-2))}
              </span>
              <span className="mt-0.5 block text-[9px] leading-none text-slate-400">
                d{d.day_of_cycle}
              </span>
              {d.is_top && (
                <span title="Projected top" className="absolute right-0.5 top-0.5">
                  <Flag className="h-2.5 w-2.5 text-amber-400" />
                </span>
              )}
              {d.is_bottom && !d.is_top && (
                <span title="Projected bottom / anchor" className="absolute right-0.5 top-0.5">
                  <Flag className="h-2.5 w-2.5 text-cyan-300" />
                </span>
              )}
              {d.is_halving && (
                <span title="Halving" className="absolute left-0.5 top-0.5">
                  <Sparkles className="h-2.5 w-2.5 text-yellow-300" />
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-slate-800 pt-2 text-[10px] text-slate-500">
        <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-emerald-500/40" /> bull</span>
        <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-red-500/40" /> bear</span>
        <span className="flex items-center gap-1"><Flag className="h-2.5 w-2.5 text-amber-400" /> projected top</span>
        <span className="flex items-center gap-1"><Sparkles className="h-2.5 w-2.5 text-yellow-300" /> halving</span>
        <span>faded = projected, not yet lived</span>
      </div>
    </div>
  )
}

/** Past cycles: what the calendar projected vs where price actually turned. */
function ValidationTable({ state }: { state: CycleState }) {
  const rows = state.validation?.cycles ?? []
  if (!rows.length) return null
  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Every cycle since the pattern began</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-[11px]">
          <thead>
            <tr className="text-slate-500">
              <th className="pb-1 pr-3 font-medium">Bottom</th>
              <th className="pb-1 pr-3 font-medium">Top (proj → actual)</th>
              <th className="pb-1 pr-3 font-medium">Err</th>
              <th className="pb-1 pr-3 font-medium">Bottom (proj → actual)</th>
              <th className="pb-1 font-medium">Err</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-300">
            {rows.map((r) => (
              <tr key={r.bottom} className="border-t border-slate-800">
                <td className="py-1.5 pr-3 text-slate-400">{r.bottom}</td>
                <td className="py-1.5 pr-3">
                  {r.projected_top}
                  {r.actual_top ? ` → ${r.actual_top}` : ' → pending'}
                </td>
                <td className="py-1.5 pr-3">
                  {r.top_hit === undefined ? '—' : (
                    <span className={r.top_hit ? 'text-emerald-400' : 'text-red-400'}>
                      {r.top_error_days != null ? `${r.top_error_days > 0 ? '+' : ''}${r.top_error_days}d` : '—'}
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3">
                  {r.projected_bottom}
                  {r.actual_bottom ? ` → ${r.actual_bottom}` : ' → pending'}
                </td>
                <td className="py-1.5">
                  {r.bottom_hit === undefined ? '—' : (
                    <span className={r.bottom_hit ? 'text-emerald-400' : 'text-red-400'}>
                      {r.bottom_error_days != null ? `${r.bottom_error_days > 0 ? '+' : ''}${r.bottom_error_days}d` : '—'}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[10px] leading-snug text-slate-500">
        Green error = the turn landed inside the ±{state.validation?.tolerance_days ?? 45}-day tolerance;
        red = it missed. The hit rate in the header is what the agents quote as the pattern&apos;s confidence.
      </p>
    </div>
  )
}

/** Today's expectation, computed from the same offset in every prior cycle. */
function ExpectationCard({ state, expectation }: {
  state: CycleState
  expectation: { samples: number; avg_return_pct?: number; best_return_pct?: number; worst_return_pct?: number; avg_max_drawdown_pct?: number; median_return_pct?: number }
}) {
  if (!expectation.samples) {
    return (
      <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
        <h2 className="text-sm font-semibold text-slate-200">What today&apos;s position did before</h2>
        <p className="mt-1 text-[11px] text-slate-500">
          Not enough prior cycles cover day {state.day_of_cycle} — the live cycle is in
          territory history only reaches once per era.
        </p>
      </div>
    )
  }
  const avg = expectation.avg_return_pct ?? 0
  const tone = avg >= 0 ? 'text-emerald-400' : 'text-red-400'
  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
      <h2 className="text-sm font-semibold text-slate-200">
        Day {state.day_of_cycle} — what happened before
      </h2>
      <p className="mt-1 text-[11px] leading-snug text-slate-400">
        Across {expectation.samples} prior cycle{expectation.samples === 1 ? '' : 's'} at this exact
        day-of-cycle, BTC moved on average
        <span className={`font-mono font-semibold ${tone}`}> {avg > 0 ? '+' : ''}{avg}% </span>
        over the following 7 days.
      </p>
      <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
        <div className="rounded-lg bg-slate-800/50 px-2.5 py-1.5">
          <span className="text-slate-500">median</span>
          <span className="float-right font-mono text-slate-300">
            {expectation.median_return_pct != null ? `${expectation.median_return_pct > 0 ? '+' : ''}${expectation.median_return_pct}%` : '—'}
          </span>
        </div>
        <div className="rounded-lg bg-slate-800/50 px-2.5 py-1.5">
          <span className="text-slate-500">best</span>
          <span className="float-right font-mono text-emerald-400">
            {expectation.best_return_pct != null ? `${expectation.best_return_pct >= 0 ? '+' : ''}${expectation.best_return_pct}%` : '—'}
          </span>
        </div>
        <div className="rounded-lg bg-slate-800/50 px-2.5 py-1.5">
          <span className="text-slate-500">worst</span>
          <span className="float-right font-mono text-red-400">
            {expectation.worst_return_pct != null ? `${expectation.worst_return_pct}%` : '—'}
          </span>
        </div>
        <div className="rounded-lg bg-slate-800/50 px-2.5 py-1.5">
          <span className="text-slate-500">avg max DD</span>
          <span className="float-right font-mono text-amber-300">
            {expectation.avg_max_drawdown_pct != null ? `${expectation.avg_max_drawdown_pct}%` : '—'}
          </span>
        </div>
      </div>
    </div>
  )
}

/** Previous cycles vs the current one, aligned by day-of-cycle. */
function CyclesAlignedChart({ analogs, bullDays }: {
  analogs: { cycles: { bottom: string; points: [number, number][] }[]; current: { points: [number, number][] } | null } | null
  bullDays?: number
}) {
  const GHOST_COLORS = ['#64748b', '#0ea5e9', '#a78bfa', '#f59e0b']
  if (!analogs?.current) {
    return (
      <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
        <h2 className="text-sm font-semibold text-slate-200">Cycles aligned</h2>
        <p className="mt-1 text-[11px] text-slate-500">
          The aligned view needs the BTC daily feed — it will draw once the bars arrive.
        </p>
      </div>
    )
  }
  const maxOffset = Math.max(
    ...analogs.cycles.flatMap((c) => c.points.map((p) => p[0])),
    analogs.current.points.map((p) => p[0]).at(-1) ?? 0,
  )
  // One merged series per x so recharts draws every ghost on the same axis.
  const byOffset = new Map<number, Record<string, number | null>>()
  const put = (x: number, key: string, v: number) => {
    const row = byOffset.get(x) ?? {}
    row[key] = v
    byOffset.set(x, row)
  }
  analogs.cycles.forEach((c, i) => {
    c.points.forEach(([x, v]) => put(x, `ghost${i}`, v))
  })
  analogs.current.points.forEach(([x, v]) => put(x, 'current', v))
  const rows = Array.from(byOffset.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([x, vals]) => ({ x, ...vals }))

  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <TrendingUp className="h-4 w-4 text-cyan-300" />
        <h2 className="text-sm font-semibold text-slate-200">Cycles aligned — % from each bottom</h2>
        <span className="ml-auto flex flex-wrap items-center gap-2 text-[10px]">
          {analogs.cycles.map((c, i) => (
            <span key={c.bottom} className="flex items-center gap-1 text-slate-400">
              <span className="h-0.5 w-3" style={{ background: GHOST_COLORS[i % GHOST_COLORS.length] }} />
              {c.bottom}
            </span>
          ))}
          <span className="flex items-center gap-1 text-cyan-300">
            <span className="h-0.5 w-3 bg-cyan-400" /> current
          </span>
        </span>
      </div>
      <div className="mt-2 h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: -14 }}>
            <CartesianGrid stroke="rgba(148,163,184,0.12)" />
            <XAxis dataKey="x" tick={{ fill: '#64748b', fontSize: 10 }} stroke="rgba(148,163,184,0.2)" />
            <YAxis tick={{ fill: '#64748b', fontSize: 10 }} stroke="rgba(148,163,184,0.2)"
              tickFormatter={(v: number) => `${v}%`} width={52} />
            <Tooltip
              contentStyle={{ background: '#0b1526', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
              labelFormatter={(x) => `day ${x} of the cycle`}
              formatter={(v: number | string) => (typeof v === 'number' ? `${v > 0 ? '+' : ''}${v}%` : '—')}
            />
            {bullDays != null && bullDays > 0 && (
              <ReferenceLine x={bullDays} stroke="rgba(250,204,21,0.5)" strokeDasharray="4 4"
                label={{ value: 'projected top', fill: '#facc15', fontSize: 10, position: 'insideTopRight' }} />
            )}
            <ReferenceLine y={0} stroke="rgba(148,163,184,0.4)" />
            {analogs.cycles.map((c, i) => (
              <Line key={c.bottom} type="monotone" dataKey={`ghost${i}`} stroke={GHOST_COLORS[i % GHOST_COLORS.length]}
                strokeWidth={1} dot={false} connectNulls isAnimationActive={false} />
            ))}
            <Line type="monotone" dataKey="current" stroke="#22d3ee" strokeWidth={2.5} dot={false}
              connectNulls isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-[10px] leading-snug text-slate-500">
        Every cycle redrawn from its own bottom, day 0 → {maxOffset}. Where the cyan
        line bends away from the ghosts is where the pattern says attention is due.
      </p>
    </div>
  )
}

/** The next 30 days, as the same cycle-days scored in every prior cycle. */
function ExpectationTable({ rows, dayOfCycle }: {
  rows: { offset: number; samples: number; avg_return_pct?: number | null; best_return_pct?: number | null; worst_return_pct?: number | null }[]
  dayOfCycle: number
}) {
  if (!rows.length) return null
  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
      <h2 className="text-sm font-semibold text-slate-200">
        Next {rows.length} days — what this position did before
      </h2>
      <p className="mt-0.5 text-[10px] text-slate-500">
        Base rates, not promises: each row is the same day-of-cycle in every prior cycle,
        and what BTC did the following day.
      </p>
      <div className="mt-2 max-h-72 overflow-y-auto">
        <table className="w-full text-left text-[11px]">
          <thead className="sticky top-0 bg-slate-900">
            <tr className="text-slate-500">
              <th className="pb-1 pr-2 font-medium">Cycle day</th>
              <th className="pb-1 pr-2 font-medium">Avg next day</th>
              <th className="pb-1 pr-2 font-medium">Best</th>
              <th className="pb-1 pr-2 font-medium">Worst</th>
              <th className="pb-1 font-medium">N</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map((r) => {
              const avg = r.avg_return_pct
              return (
                <tr key={r.offset} className={`border-t border-slate-800 ${r.offset === dayOfCycle ? 'bg-cyan-500/10 text-cyan-200' : 'text-slate-300'}`}>
                  <td className="py-1 pr-2">{r.offset}{r.offset === dayOfCycle ? ' · today' : ''}</td>
                  <td className={`py-1 pr-2 ${avg == null ? 'text-slate-500' : avg >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {avg == null ? '—' : `${avg > 0 ? '+' : ''}${avg}%`}
                  </td>
                  <td className="py-1 pr-2 text-emerald-500/80">{r.best_return_pct != null ? `${r.best_return_pct >= 0 ? '+' : ''}${r.best_return_pct}%` : '—'}</td>
                  <td className="py-1 pr-2 text-red-500/80">{r.worst_return_pct != null ? `${r.worst_return_pct}%` : '—'}</td>
                  <td className="py-1 text-slate-500">{r.samples}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** The big money: monitored whale wallets, flows, and the aggregate read. */
function WhalePanel({ whale, loaded }: { whale: WhalePayload | null; loaded: boolean }) {
  if (!whale) {
    return (
      <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
          <WhaleIcon className="h-4 w-4 text-cyan-300" /> Whale watch
          {!loaded && <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-500" />}
        </h2>
        <p className="mt-1 text-[11px] text-slate-500">
          {loaded ? 'Whale feed unavailable — external blockchain APIs unreachable.' : 'Loading whale data…'}
        </p>
      </div>
    )
  }
  const score = String(whale.score ?? 'UNKNOWN').toUpperCase()
  const tone = score === 'ACCUMULATING' ? 'text-emerald-300 border-emerald-500/40 bg-emerald-500/10'
    : score === 'DISTRIBUTING' ? 'text-red-300 border-red-500/40 bg-red-500/10'
    : 'text-slate-300 border-slate-600/50 bg-slate-700/20'
  const fmt = (v: number | null) => (v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: 0 }))
  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <WhaleIcon className="h-4 w-4 text-cyan-300" />
        <h2 className="text-sm font-semibold text-slate-200">Whale watch</h2>
        <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase ${tone}`}>
          {score}
        </span>
        <span className="ml-auto font-mono text-[11px] text-slate-400">
          {whale.net_flow_7d_btc != null ? `${whale.net_flow_7d_btc > 0 ? '+' : ''}${fmt(whale.net_flow_7d_btc)} BTC / 7d` : ''}
        </span>
      </div>
      <p className="mt-1 text-[10px] text-slate-500">{whale.detail}</p>

      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-[11px]">
          <thead>
            <tr className="text-slate-500">
              <th className="pb-1 pr-2 font-medium">Wallet</th>
              <th className="pb-1 pr-2 font-medium">Balance</th>
              <th className="pb-1 font-medium">7d flow</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-300">
            {(whale.holders ?? []).slice(0, 10).map((h) => {
              const flow = h.net_flow_7d_btc
              return (
                <tr key={h.address} className="border-t border-slate-800">
                  <td className="py-1 pr-2">
                    <span className="text-slate-200">{h.label}</span>
                    <span className="ml-1.5 rounded bg-slate-700/40 px-1 text-[9px] uppercase text-slate-400">{h.category}</span>
                  </td>
                  <td className="py-1 pr-2">{fmt(h.balance_btc)}</td>
                  <td className={`py-1 ${flow == null ? 'text-slate-500' : flow >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {flow == null ? '—' : `${flow > 0 ? '+' : ''}${fmt(flow)}`}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {(whale.transfers?.length ?? 0) > 0 && (
        <div className="mt-2.5 border-t border-slate-800 pt-2">
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
            </span>
            Live transfers ≥ {50} BTC
          </h3>
          <ul className="mt-1.5 space-y-1">
            {whale.transfers.slice(0, 6).map((t) => (
              <li key={t.txid ?? `${t.address}-${t.time}`} className="flex items-center gap-2 font-mono text-[11px]">
                <span className={`rounded px-1 text-[9px] uppercase ${t.direction === 'in' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-red-500/15 text-red-300'}`}>
                  {t.direction === 'in' ? 'IN' : 'OUT'}
                </span>
                <span className="text-slate-200">{Math.abs(t.btc).toLocaleString(undefined, { maximumFractionDigits: 0 })} BTC</span>
                <span className="truncate text-slate-500">{t.label}</span>
                <span className="ml-auto shrink-0 text-slate-600">{fmtMoveAge(t.time)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="mt-1.5 text-[10px] leading-snug text-slate-500">
        Labels are community attributions of well-known addresses — exchange cold
        wallets reshuffle internally, so read flows as context, never alone.
      </p>
    </div>
  )
}

function fmtMoveAge(ts: number | null): string {
  if (!ts) return 'mempool'
  const mins = Math.round((Date.now() / 1000 - ts) / 60)
  if (mins < 60) return `${mins}m ago`
  if (mins < 60 * 48) return `${Math.round(mins / 60)}h ago`
  return `${Math.round(mins / (60 * 24))}d ago`
}

/** The exact context block every room seat receives. */
function AgentContextStrip({ state }: { state: CycleState }) {
  const lines = [
    `Cycle phase: ${state.phase.toUpperCase()} — day ${state.day_of_cycle} since the ${state.anchor} bottom (${Math.round(state.phase_pct * 100)}% through the ${state.phase === 'bull' ? 'accumulation/markup' : 'distribution'} phase).`,
    `Projected top: ${state.projected_top} (${state.days_to_top}d) · projected bottom: ${state.projected_bottom} (${state.days_to_bottom}d).`,
  ]
  if (state.price && state.cycle_high) {
    lines.push(`BTC ${fmtPrice(state.price)} — ${(((state.price / state.cycle_high) - 1) * 100).toFixed(1)}% off the cycle high.`)
  }
  const rate = state.validation?.top_hit_rate
  if (rate != null) {
    lines.push(`Pattern history: tops landed within ±${state.validation?.tolerance_days ?? 45}d of projection in ${Math.round(rate * 100)}% of scored cycles.`)
  }
  return (
    <div className="rounded-xl border border-cyan-500/30 bg-cyan-500/5 p-3">
      <div className="flex items-center gap-2">
        <ChevronRight className="h-3.5 w-3.5 text-cyan-300" />
        <h2 className="text-xs font-semibold uppercase tracking-wide text-cyan-300/90">
          What the agents see
        </h2>
        <span className="text-[10px] text-slate-500">context[&quot;btc_cycle&quot;] — every seat, every meeting</span>
      </div>
      <ul className="mt-1.5 space-y-1">
        {lines.map((l, i) => (
          <li key={i} className="text-[11px] leading-snug text-slate-300">{toReasoningText(l)}</li>
        ))}
      </ul>
    </div>
  )
}

const CHART_TFS = [
  { label: '1H', value: 'H1' },
  { label: '4H', value: 'H4' },
  { label: 'D1', value: 'D1' },
  { label: 'W1', value: 'W1' },
  { label: '1M', value: 'MN1' },
] as const

function CandleMetaStrip({ tf, count, earliestIso, synthetic, loaded, error }: { tf: string; count: number; earliestIso: string | null; synthetic: number; loaded: boolean; error: string | null }) {
  const label = tf === 'MN1' ? '1M' : tf
  if (!loaded) {
    return <div className="flex items-center gap-2 text-[11px] text-slate-500"><Loader2 className="h-3 w-3 animate-spin" /> Loading {label} full history…</div>
  }
  if (error) {
    return <div className="text-[11px] text-amber-300">{label}: {error}</div>
  }
  const start = earliestIso ? earliestIso.slice(0, 10) : '2010-07-17'
  const genesisOk = start <= '2010-08-01'
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span className={`rounded-full border px-2 py-0.5 text-[10px] ${genesisOk ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' : 'border-amber-500/40 bg-amber-500/10 text-amber-300'}`}>
        {genesisOk ? '✓ Genesis' : '◐ Limited history'}
      </span>
      <span className="font-mono text-slate-300">{count.toLocaleString()} {label} candles</span>
      <span className="text-slate-500">· from {start}</span>
      <span className="text-slate-500">· to now</span>
      {synthetic > 0 && <span className="rounded-full border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[10px] text-violet-300" title="Pre-Bitget H1/H4 (2010→exchange start) synthesized from daily closes; flat OHLC, volume 0">{synthetic.toLocaleString()} synthetic</span>}
      {genesisOk && <span className="text-emerald-400/70">· day 1 ✓</span>}
    </div>
  )
}

export default function BitcoinCyclePage() {
  const { state, loaded, windows, calendar, calLoading, analogs, expectation, month, shiftMonth } = useBtcCyclePage()
  // TF from ?tf= query so links/deep-links keep the selection and tests can drive each TF
  const initialTf = (() => {
    if (typeof window !== 'undefined') {
      const q = new URLSearchParams(window.location.search).get('tf')
      if (q) {
        const u = q.toUpperCase()
        if (u === '1M' || u === 'MN1') return 'MN1'
        if (['W1','D1','H4','H1'].includes(u)) return u
      }
    }
    return 'MN1'
  })()
  const [chartTf, setChartTf] = useState<string>(initialTf)
  const candleFeed = useCycleCandles(chartTf)
  const { visible: indicatorOverlays, enabled: indicatorsOn, setEnabled: setIndicatorEnabled } = useIndicatorOverlays(chartTf)
  const { whale: whales, loaded: whaleLoaded } = useWhaleWatch()
  const [selected, setSelected] = useState<CycleCalendarDay | null>(null)
  const [fitMode, setFitMode] = useState<'genesis' | 'recent'>('genesis')

  // Keep URL in sync when TF changes (without full reload)
  useEffect(() => {
    if (typeof window === 'undefined') return
    const url = new URL(window.location.href)
    url.searchParams.set('tf', chartTf === 'MN1' ? '1M' : chartTf)
    window.history.replaceState({}, '', url.toString())
  }, [chartTf])

  const boxes = useMemo<CycleWindowBox[]>(() => toCycleBoxes(windows), [windows])
  const chartLabel = chartTf === 'MN1' ? '1M' : chartTf
  // The cycle overlay boxes are TF-agnostic; fisher/pattern overlays now come TF-aware from useCycleCandles
  const candleSourceLabel = (() => {
    if (!candleFeed.loaded) return `BTC/USD · ${chartLabel} · loading…`
    if (candleFeed.synthetic > 0) return `BTC/USD · ${chartLabel} · CRYPTO (Bitget+Yahoo/CoinMetrics, ${candleFeed.synthetic} synth)`
    if (chartTf === 'H1' || chartTf === 'H4') return `BTC/USD · ${chartLabel} · CRYPTO (Bitget)`
    return `BTC/USD · ${chartLabel} · CRYPTO (Yahoo/CoinMetrics)`
  })()

  return (
    <>
      <Head><title>Bitcoin Cycle | TradeBot</title></Head>

      <div className="space-y-4 p-4">
        <div className="flex items-center gap-2">
          <RefreshCw className="h-6 w-6 text-cyan-300" />
          <div>
            <h1 className="text-xl font-bold text-slate-100">Bitcoin 1064-Day Cycle</h1>
            <p className="text-[11px] text-slate-500">
              Every cycle since launch: ≈1064 days up from the bottom, ≈365 back down.
              The agents and the signal engine read this same calendar. Every TF now loads from genesis.
            </p>
          </div>
        </div>

        {!loaded && <p className="text-sm text-slate-500">Loading the calendar…</p>}
        {loaded && !state?.ok && (
          <p className="text-sm text-amber-300">
            Cycle calendar unavailable — the BTC daily feed is unreachable. Everything else keeps working.
          </p>
        )}

        {state?.ok && (
          <>
            <CycleHeader state={state} />

            <div className="grid gap-4 xl:grid-cols-[2fr_1fr]">
              {/* Chart + alignment + daily prediction + validation history */}
              <div className="space-y-4">
                <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-2">
                  <div className="flex items-center gap-1.5 px-1 pb-1.5 flex-wrap">
                    <span className="text-[10px] uppercase tracking-wide text-slate-500">Indicators</span>
                    {INDICATOR_GROUPS.map((g) => (
                      <button
                        key={g}
                        type="button"
                        onClick={() => setIndicatorEnabled((prev) => ({ ...prev, [g]: !prev[g] }))}
                        className={`rounded-full border px-2 py-0.5 text-[10px] uppercase transition-colors ${
                          indicatorsOn[g]
                            ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-200'
                            : 'border-slate-600/40 text-slate-500 hover:text-slate-300'
                        }`}
                      >
                        {g}
                      </button>
                    ))}
                    <div className="ml-auto flex items-center gap-1">
                      <span className="text-[10px] uppercase tracking-wide text-slate-500 mr-1">Timeframe</span>
                      <div className="flex gap-0.5 bg-slate-800/70 rounded-lg p-0.5">
                        {CHART_TFS.map((tf) => (
                          <button
                            key={tf.value}
                            type="button"
                            onClick={() => setChartTf(tf.value)}
                            className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                              chartTf === tf.value
                                ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-700/50'
                            }`}
                          >
                            {tf.label}
                          </button>
                        ))}
                      </div>
                      <div className="ml-2 flex gap-0.5 bg-slate-800/70 rounded-lg p-0.5">
                        <button type="button" onClick={() => setFitMode('genesis')} className={`px-2 py-1 rounded text-[10px] ${fitMode==='genesis' ? 'bg-slate-700 text-slate-200' : 'text-slate-500 hover:text-slate-300'}`}>Genesis</button>
                        <button type="button" onClick={() => setFitMode('recent')} className={`px-2 py-1 rounded text-[10px] ${fitMode==='recent' ? 'bg-slate-700 text-slate-200' : 'text-slate-500 hover:text-slate-300'}`}>2 cycles</button>
                      </div>
                    </div>
                  </div>
                  <div className="px-1 pb-1.5">
                    <CandleMetaStrip tf={chartTf} count={candleFeed.count} earliestIso={candleFeed.earliestIso} synthetic={candleFeed.synthetic} loaded={candleFeed.loaded} error={candleFeed.error} />
                  </div>
                  <TradingViewChart
                    key={`${chartTf}-${fitMode}`}
                    symbol="BTC/USDT"
                    exchange="bitget"
                    timeframe={chartTf}
                    initialCandles={candleFeed.candles.length ? candleFeed.candles : undefined}
                    sourceLabel={candleSourceLabel}
                    cycleWindows={boxes}
                    overlays={[...candleFeed.fisherOverlays, ...indicatorOverlays]}
                    markers={candleFeed.markers}
                    showZones={false}
                  />
                  {candleFeed.synthetic > 0 && (
                    <p className="px-1 pt-1 text-[10px] leading-snug text-violet-300/70">
                      H1/H4 before {candleFeed.earliestIso ? new Date(candleFeed.earliestIso).getFullYear() : 2018}: Bitget had no data — shown as daily-close interpolated bars (flat, volume 0) to keep the line continuous from genesis. Real Bitget candles begin where the flat segment ends.
                    </p>
                  )}
                </div>
                <CyclesAlignedChart analogs={analogs} bullDays={analogs?.bull_days} />
                <ExpectationTable rows={expectation} dayOfCycle={state.day_of_cycle} />
                <ValidationTable state={state} />
              </div>

              {/* Calendar + whales + expectations */}
              <div className="space-y-4">
                <CycleCalendarGrid
                  days={calendar?.days ?? []}
                  year={calendar?.year ?? month.y}
                  month={calendar?.month ?? month.m}
                  onShift={shiftMonth}
                  onSelect={setSelected}
                  selected={selected?.date ?? null}
                  loading={calLoading}
                />

                {selected ? (
                  <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-3">
                    <h2 className="text-sm font-semibold text-slate-200">
                      {fmtDate(selected.date)}
                    </h2>
                    <p className="mt-1 text-[11px] leading-snug text-slate-400">
                      {selected.projected ? 'Projected' : 'Calendar'} ·{' '}
                      <span className={phaseColor(selected.phase).text}>{selected.phase}</span>{' '}
                      · day {selected.day_of_cycle} of the cycle
                      {selected.is_top && ' · projected top'}
                      {selected.is_anchor && ' · cycle bottom anchor'}
                      {selected.is_halving && ' · halving'}
                      . {selected.phase === 'bull'
                        ? `${selected.days_to_top}d to the projected top.`
                        : `${selected.days_to_bottom}d to the projected bottom.`}
                    </p>
                  </div>
                ) : (
                  <ExpectationCard
                    state={state}
                    expectation={calendar?.today_expectation ?? { samples: 0, offset: null }}
                  />
                )}

                <WhalePanel whale={whales} loaded={whaleLoaded} />

                <AgentContextStrip state={state} />
              </div>
            </div>
          </>
        )}
      </div>
    </>
  )
}
