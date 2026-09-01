/**
 * Desk brief — everything the `/room` Telegram command sends, in the room.
 *
 * The web room showed who was speaking and what they concluded; the Telegram
 * command sent the conclusion plus the structural read, the plan's levels, the
 * forecast, a copyable signal card and the chart those levels were drawn on.
 * Two surfaces of one desk should not hand out different amounts of the same
 * analysis, so this renders the same builder's output.
 */
import { useCallback, useState } from 'react'
import Link from 'next/link'
import { ChevronDown, ChevronUp, Loader2, RefreshCw } from 'lucide-react'

import { api } from '@/services/api'
import { useBtcCycleState } from '@/hooks/useBtcCycle'

interface Momentum {
  direction?: string
  strength?: string
  change_pct_60_bars?: number
  path_efficiency?: number
  range_position_pct?: number
}

interface Forecast {
  engine?: string
  direction?: string
  pct_change?: number
  confidence?: number
  horizon?: string
}

interface Brief {
  symbol: string
  timeframe: string
  result?: { final_action?: string | null; final_confidence?: number | null; final_reasoning?: string | null }
  momentum?: Momentum | null
  forecast?: Forecast | null
  market_read?: string
  plan_levels_text?: string
  signal_card?: string | null
  scenario_follow_up?: string
  chart_png_base64?: string | null
  candles_available?: number
}

const TONE: Record<string, string> = {
  buy: 'bg-emerald-500/20 text-emerald-300',
  sell: 'bg-red-500/20 text-red-300',
  up: 'bg-emerald-500/20 text-emerald-300',
  down: 'bg-red-500/20 text-red-300',
}

/** Telegram-flavoured HTML, reduced to plain text for the panel. */
function plain(text?: string | null): string {
  return (text ?? '').replace(/<[^>]+>/g, '').trim()
}

function Pill({ label, tone }: { label: string; tone?: string }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${tone ?? 'bg-slate-700 text-slate-300'}`}>
      {label}
    </span>
  )
}

export default function DeskBrief({ symbol }: { symbol: string | null }) {
  const [brief, setBrief] = useState<Brief | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  // The season the pair is trading in — same snapshot the seats receive.
  // Called before the early return below: hooks run on every render.
  const { state: cycle } = useBtcCycleState()

  const load = useCallback(async (analyse: boolean) => {
    if (!symbol) return
    setBusy(true)
    setError(null)
    try {
      const { data } = await api.post('/agents/room/brief', { symbol, analyse })
      setBrief(data)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail ?? 'The desk could not produce a brief for that pair.')
    } finally {
      setBusy(false)
    }
  }, [symbol])

  if (!symbol) return null

  const verdict = (brief?.result?.final_action ?? '').toLowerCase()
  const momentum = brief?.momentum
  const forecast = brief?.forecast

  return (
    <div className="shrink-0 overflow-hidden rounded-xl border border-slate-700/70 bg-slate-900/50">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="flex items-center gap-2 text-left"
        >
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Desk brief</h2>
          <span className="font-mono text-[11px] text-slate-500">{symbol}</span>
          {collapsed ? <ChevronDown className="h-3.5 w-3.5 text-slate-500" /> : <ChevronUp className="h-3.5 w-3.5 text-slate-500" />}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void load(false)}
          className="ml-auto rounded-lg border border-slate-700 px-2 py-1 text-[11px] text-slate-300 transition hover:border-slate-500 disabled:opacity-50"
          title="Describe the room's last meeting on this pair — no new agent calls"
        >
          Last meeting
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void load(true)}
          className="flex items-center gap-1 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-2 py-1 text-[11px] font-medium text-cyan-300 transition hover:border-cyan-400 disabled:opacity-50"
          title="Convene the room on this pair now"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          Convene
        </button>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="rounded p-1 text-slate-500 hover:bg-slate-800 hover:text-slate-300"
          title={collapsed ? 'Expand' : 'Collapse'}
          aria-label={collapsed ? 'Expand desk brief' : 'Collapse desk brief'}
        >
          {collapsed ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
        </button>
      </div>
      {collapsed ? null : (
        <div className="max-h-[38vh] overflow-y-auto border-t border-slate-800 px-3 pb-3 pt-2">

      {error && <p className="text-[11px] text-red-400">{error}</p>}
      {cycle?.ok && (
        <p className="mb-2 flex items-center gap-1.5 text-[11px] text-slate-400">
          <span className={`h-1.5 w-1.5 rounded-full ${cycle.phase === 'bull' ? 'bg-emerald-400' : 'bg-red-400'}`} />
          BTC cycle <span className={`font-semibold uppercase ${cycle.phase === 'bull' ? 'text-emerald-300' : 'text-red-300'}`}>{cycle.phase}</span>
          · day {cycle.day_of_cycle}
          · {cycle.phase === 'bull' ? 'top' : 'bottom'} {Math.max(0, cycle.phase === 'bull' ? cycle.days_to_top : cycle.days_to_bottom)}d
          <Link href="/bitcoin-cycle" className="ml-auto text-cyan-400 hover:text-cyan-300">cycle page</Link>
        </p>
      )}
      {!brief && !error && (
        <p className="text-[11px] text-slate-500">
          The full read — verdict, forecast, levels and chart — for the pair on the board.
        </p>
      )}

      {brief && (
        <div className="space-y-2.5">
          <div className="flex flex-wrap items-center gap-1.5">
            {verdict && <Pill label={verdict} tone={TONE[verdict]} />}
            {typeof brief.result?.final_confidence === 'number' && (
              <span className="text-[11px] text-slate-400">
                confidence {Math.round(brief.result.final_confidence * 100)}%
              </span>
            )}
            <span className="ml-auto font-mono text-[10px] text-slate-500">
              {brief.timeframe} · {brief.candles_available ?? 0} bars
            </span>
          </div>

          {(momentum?.direction || forecast?.direction) && (
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
              {momentum?.direction && (
                <>
                  <span className="text-slate-500">momentum</span>
                  <Pill label={`${momentum.direction} · ${momentum.strength ?? ''}`} tone={TONE[momentum.direction]} />
                </>
              )}
              {forecast?.direction && (
                <>
                  <span className="text-slate-500">forecast</span>
                  <Pill
                    label={`${forecast.direction}${
                      typeof forecast.pct_change === 'number' ? ` ${forecast.pct_change.toFixed(2)}%` : ''
                    }`}
                    tone={TONE[forecast.direction]}
                  />
                </>
              )}
            </div>
          )}

          {brief.result?.final_reasoning && (
            <p className="text-[11px] leading-snug text-slate-300">{plain(brief.result.final_reasoning)}</p>
          )}

          {brief.plan_levels_text && (
            <pre className="whitespace-pre-wrap break-words rounded-lg bg-slate-950/60 p-2 font-mono text-[10px] leading-relaxed text-cyan-200">
              {plain(brief.plan_levels_text)}
            </pre>
          )}

          {brief.chart_png_base64 && (
            /* eslint-disable-next-line @next/next/no-img-element */
            <img
              src={`data:image/png;base64,${brief.chart_png_base64}`}
              alt={`${brief.symbol} ${brief.timeframe} — the desk's plan`}
              className="w-full rounded-lg border border-slate-700"
            />
          )}

          {brief.market_read && (
            <p className="whitespace-pre-wrap text-[11px] leading-snug text-slate-400">
              {plain(brief.market_read)}
            </p>
          )}

          {brief.signal_card && (
            <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950/60 p-2 font-mono text-[10px] leading-relaxed text-slate-300">
              {plain(brief.signal_card)}
            </pre>
          )}

          {brief.scenario_follow_up && (
            <p className="text-[11px] leading-snug text-amber-300/80">{plain(brief.scenario_follow_up)}</p>
          )}
        </div>
      )}
        </div>
      )}
    </div>
  )
}
