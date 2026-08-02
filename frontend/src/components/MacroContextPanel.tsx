/**
 * Macro context — the dollar and the fear gauge, alongside crypto market cap.
 *
 * These two series are inputs to the signal engines (see
 * backend/app/services/macro_context.py); this panel is the human-readable view
 * of the same data, so a trader can see the weather the scores were computed in.
 *
 * Colour is deliberately not green-up/red-down: for a crypto reader both gauges
 * are *inverse* risk, so a rising DXY is not "good". The change stays neutral in
 * tone and a one-word regime tag carries the meaning instead.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { apiClient } from '@/services/api'
import ChartErrorBoundary from './ChartErrorBoundary'

export interface MacroSeriesPoint {
  /** Milliseconds — the API returns seconds. */
  time: number
  close: number
}

export interface MacroStats {
  level: number | null
  changePct: number | null
  asOf: Date | null
  /** What the move means for crypto, not for the instrument itself. */
  regime: 'risk-on' | 'risk-off' | 'neutral'
}

type MacroKind = 'DXY' | 'VIX'

interface MacroInstrument {
  symbol: MacroKind
  title: string
  hint: string
  stroke: string
}

/** Adding a third gauge (US10Y, say) is a one-line change. */
const MACRO_INSTRUMENTS: MacroInstrument[] = [
  { symbol: 'DXY', title: 'US Dollar Index', hint: 'Dollar up = crypto headwind', stroke: '#f59e0b' },
  { symbol: 'VIX', title: 'Volatility Index', hint: 'Above 25 = risk-off', stroke: '#a78bfa' },
]

/** VIX above this reads as risk-off regardless of the day's direction. */
const VIX_STRESSED = 25

/** A move smaller than this is noise, not a regime. */
const DXY_MOVE_THRESHOLD = 0.15

export function deriveMacroStats(points: MacroSeriesPoint[], kind: MacroKind): MacroStats {
  if (!points.length) {
    return { level: null, changePct: null, asOf: null, regime: 'neutral' }
  }
  const last = points[points.length - 1]
  const prev = points.length >= 2 ? points[points.length - 2] : null
  // A single point has a level but no change — null, never NaN or Infinity.
  const changePct =
    prev && prev.close > 0 ? ((last.close / prev.close) - 1) * 100 : null

  let regime: MacroStats['regime'] = 'neutral'
  if (kind === 'VIX') {
    if (last.close >= VIX_STRESSED) regime = 'risk-off'
    else if (changePct !== null && changePct > 5) regime = 'risk-off'
    else if (changePct !== null && changePct < -5) regime = 'risk-on'
  } else if (changePct !== null) {
    // A bid dollar drains risk assets; an offered one feeds them.
    if (changePct > DXY_MOVE_THRESHOLD) regime = 'risk-off'
    else if (changePct < -DXY_MOVE_THRESHOLD) regime = 'risk-on'
  }

  return { level: last.close, changePct, asOf: new Date(last.time), regime }
}

/**
 * Y-axis bounds with breathing room.
 *
 * recharts defaults a numeric axis to [0, 'auto'], which renders DXY≈99.8 and
 * VIX≈16 as a flat line pinned to the top of the plot. This is what makes the
 * chart readable, not decoration.
 */
export function paddedDomain(points: MacroSeriesPoint[]): [number, number] {
  if (!points.length) return [0, 1]
  const values = points.map(p => p.close)
  const min = Math.min(...values)
  const max = Math.max(...values)
  // A flat session would otherwise collapse to a zero-height domain.
  const pad = Math.max((max - min) * 0.08, Math.abs(max) * 0.002, 0.01)
  return [min - pad, max + pad]
}

type Status = 'loading' | 'ok' | 'empty' | 'error'

function useMacroSeries(symbol: MacroKind, refreshNonce: number, lookbackDays: number) {
  const [points, setPoints] = useState<MacroSeriesPoint[]>([])
  const [status, setStatus] = useState<Status>('loading')
  const [lastGoodAt, setLastGoodAt] = useState<Date | null>(null)
  const [failedAt, setFailedAt] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    // Cancel whatever is in flight and take its place. This is also what stops
    // requests piling up behind the api client's retry/backoff when the 60s
    // tick outruns a slow response: there is at most one live request per card.
    //
    // Do NOT add an "already in flight, skip" guard here. React's development
    // double-effect runs mount → cleanup → mount, so the cleanup aborts the
    // first request and a skip-guard would then make the second invocation
    // return without fetching — leaving the card on "Loading…" forever.
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    try {
      const res = await apiClient.getMarketCandles(symbol, 'D1', lookbackDays, controller.signal)
      const candles: { time: number; close: number }[] = res.data?.candles ?? []
      if (!candles.length) {
        setStatus('empty')
        setFailedAt(new Date())
        return
      }
      setPoints(candles.map(c => ({ time: c.time * 1000, close: c.close })))
      setStatus('ok')
      setLastGoodAt(new Date())
      setFailedAt(null)
    } catch (e: any) {
      // An aborted request is a navigation, not a failure — rendering it as
      // "unavailable" would flash an error every time the tick collides.
      if (e?.name === 'CanceledError' || e?.code === 'ERR_CANCELED' || e?.message === 'canceled') return
      console.warn(`[Macro] ${symbol} candles failed`, e)
      setStatus('error')
      setFailedAt(new Date())
    }
  }, [symbol, lookbackDays])

  useEffect(() => {
    load()
    return () => abortRef.current?.abort()
  }, [load, refreshNonce])

  return { points, status, lastGoodAt, failedAt, retry: load }
}

const REGIME_STYLE: Record<MacroStats['regime'], string> = {
  'risk-on': 'bg-emerald-900/40 text-emerald-300',
  'risk-off': 'bg-rose-900/40 text-rose-300',
  neutral: 'bg-gray-700/50 text-gray-400',
}

function MacroSeriesCard({
  config,
  lookbackDays,
  refreshNonce,
}: {
  config: MacroInstrument
  lookbackDays: number
  refreshNonce: number
}) {
  const { points, status, lastGoodAt, failedAt, retry } = useMacroSeries(
    config.symbol, refreshNonce, lookbackDays,
  )
  const stats = deriveMacroStats(points, config.symbol)
  const degraded = (status === 'error' || status === 'empty') && points.length > 0

  return (
    <div className="bg-gray-800/40 border border-gray-700/60 rounded-xl p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500">{config.title}</div>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-xl font-bold leading-tight text-white">
              {stats.level === null ? '—' : stats.level.toFixed(config.symbol === 'VIX' ? 2 : 3)}
            </span>
            <span className="text-xs text-gray-400">
              {stats.changePct === null ? '' : `${stats.changePct >= 0 ? '+' : ''}${stats.changePct.toFixed(2)}%`}
            </span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${REGIME_STYLE[stats.regime]}`}>
              {stats.regime}
            </span>
          </div>
        </div>
        <span className="text-[10px] text-gray-600 font-mono">{config.symbol}</span>
      </div>

      <div className={`mt-3 ${degraded ? 'opacity-60' : ''}`}>
        {points.length > 0 ? (
          <ChartErrorBoundary label={`The ${config.symbol} chart hit a rendering error.`}>
            <ResponsiveContainer width="100%" height={160}>
              <AreaChart data={points} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id={`macro-${config.symbol}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={config.stroke} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={config.stroke} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                {/* Category axis: both are cash series with no weekend bars, and
                    a numeric time axis would render ragged holes. */}
                <XAxis
                  dataKey="time"
                  tick={{ fill: '#6b7280', fontSize: 10 }}
                  tickFormatter={(t: number) =>
                    new Date(t).toLocaleDateString(undefined, { month: '2-digit', day: '2-digit' })
                  }
                  interval="preserveStartEnd"
                  minTickGap={40}
                />
                <YAxis
                  domain={paddedDomain(points)}
                  tick={{ fill: '#6b7280', fontSize: 10 }}
                  width={48}
                  tickFormatter={(v: number) => v.toFixed(config.symbol === 'VIX' ? 1 : 2)}
                />
                <Tooltip
                  contentStyle={{
                    background: '#111827', border: '1px solid #374151',
                    borderRadius: 8, fontSize: 12,
                  }}
                  labelFormatter={(t: number) => new Date(t).toLocaleDateString()}
                  formatter={(v: number) => [v.toFixed(3), config.symbol]}
                />
                <Area
                  type="monotone"
                  dataKey="close"
                  stroke={config.stroke}
                  fill={`url(#macro-${config.symbol})`}
                  strokeWidth={1.6}
                  dot={false}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </ChartErrorBoundary>
        ) : status === 'loading' ? (
          <div className="h-[160px] flex items-center justify-center text-xs text-gray-600">
            Loading {config.symbol}…
          </div>
        ) : (
          // Only the chart region is replaced — the card frame and title stay,
          // so the page geometry does not jump.
          <div className="h-[160px] flex flex-col items-center justify-center gap-2 text-center">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            <div className="text-xs text-gray-400 px-2">
              {config.symbol} unavailable — the market data feed returned no bars.
            </div>
            <button
              onClick={retry}
              className="text-[11px] px-2 py-1 rounded bg-gray-700/60 text-gray-300 hover:bg-gray-700 inline-flex items-center gap-1"
            >
              <RefreshCw className="w-3 h-3" /> Retry
            </button>
          </div>
        )}
      </div>

      <div className="mt-2 text-[10px] text-gray-600">
        {degraded && failedAt ? (
          <span className="text-amber-500/80">
            Refresh failed at {failedAt.toLocaleTimeString()} — showing data from{' '}
            {lastGoodAt?.toLocaleTimeString() ?? 'earlier'}.
          </span>
        ) : (
          <>
            {stats.asOf ? `as of ${stats.asOf.toLocaleDateString()} · ${lookbackDays}d` : config.hint}
          </>
        )}
      </div>
    </div>
  )
}

export default function MacroContextPanel({
  refreshNonce,
  lookbackDays = 90,
}: {
  /** Monotonic counter from the page — each increment triggers one refetch. */
  refreshNonce: number
  lookbackDays?: number
}) {
  return (
    <section className="mb-6">
      <div className="flex items-center justify-between mb-2">
        <h2 className="font-semibold text-white">Macro Context</h2>
        <span className="text-[10px] text-gray-600">
          also scored into MT5 &amp; crypto signals
        </span>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {MACRO_INSTRUMENTS.map(config => (
          <MacroSeriesCard
            key={config.symbol}
            config={config}
            lookbackDays={lookbackDays}
            refreshNonce={refreshNonce}
          />
        ))}
      </div>
    </section>
  )
}
