import { useState } from 'react'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Layers,
  Minus,
  ShieldCheck,
  Telescope,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'

import type { ResearchEntry, ResearchPlan } from '@/hooks/useResearchJobs'

const VERDICT: Record<string, { cls: string; Icon: any; label: string }> = {
  bullish:     { cls: 'text-emerald-300 border-emerald-700/40 bg-emerald-900/30', Icon: TrendingUp,    label: 'Bullish' },
  bearish:     { cls: 'text-red-300 border-red-700/40 bg-red-900/30',             Icon: TrendingDown,  label: 'Bearish' },
  neutral:     { cls: 'text-gray-300 border-gray-600/40 bg-gray-800/60',          Icon: Minus,         label: 'Neutral' },
  stand_aside: { cls: 'text-amber-300 border-amber-700/40 bg-amber-900/30',       Icon: AlertTriangle, label: 'Stand aside' },
}

const num = (v: number) => (Math.abs(v) >= 1000 ? v.toFixed(2) : String(Number(v.toPrecision(6))))

/**
 * A pair's research verdict as a single chip.
 *
 * Sized to sit inside a table row or a card header on the signal, trending,
 * sniper and rug-pull pages, so the reconciled view travels with the pair
 * wherever it is listed.
 */
export function ResearchVerdictBadge({ plan }: { plan?: ResearchPlan | null }) {
  if (!plan || !plan.verdict) return null
  const v = VERDICT[plan.verdict] || VERDICT.neutral
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] whitespace-nowrap ${v.cls}`}
      title={
        `${plan.signal_count} signal(s) reconciled · ` +
        `${plan.entries.length} entry plan(s) · ` +
        `researched ${plan.age_hours.toFixed(1)}h ago` +
        (plan.rationale ? `\n\n${plan.rationale}` : '')
      }
    >
      <v.Icon className="w-3 h-3" />
      {v.label}
      {plan.confidence != null && ` ${(plan.confidence * 100).toFixed(0)}%`}
      {plan.signal_count > 1 && (
        <span className="opacity-70">· {plan.signal_count} sigs</span>
      )}
    </span>
  )
}

function EntryRow({ entry }: { entry: ResearchEntry }) {
  const buy = entry.side === 'buy'
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5 border-b border-gray-700/30 last:border-0">
      <span className="text-[10px] uppercase tracking-wide text-gray-500 w-16 shrink-0">
        {entry.label}
      </span>
      <span
        className={`px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0 ${
          buy ? 'bg-emerald-900/40 text-emerald-300' : 'bg-red-900/40 text-red-300'
        }`}
      >
        {entry.side.toUpperCase()}
      </span>
      <span className="text-xs text-white font-mono">{num(entry.entry)}</span>
      <span className="text-[11px] text-red-400/90 font-mono" title="stop loss">
        SL {num(entry.stop_loss)}
      </span>
      <span className="text-[11px] text-emerald-400/90 font-mono" title="take profit">
        TP {num(entry.take_profit)}
      </span>
      {entry.rr != null && (
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded ${
            entry.rr >= 2 ? 'bg-emerald-900/30 text-emerald-300' : 'bg-gray-800 text-gray-400'
          }`}
          title="reward ÷ risk, computed from these levels"
        >
          {entry.rr}R
        </span>
      )}
      <span className="text-[10px] text-gray-500 ml-auto shrink-0">
        {(entry.confidence * 100).toFixed(0)}%
      </span>
      {entry.trigger && (
        <div className="w-full text-[10px] text-gray-500 pl-16">↳ {entry.trigger}</div>
      )}
    </div>
  )
}

/**
 * The two entry plans a pair's research produced, with the verdict above them.
 *
 * Every live signal on the instrument is researched together, so this is one
 * reconciled answer rather than one per signal — `signal_count` says how many
 * went in. Collapsed by default inside dense lists.
 */
export default function ResearchEntries({
  plan,
  defaultOpen = false,
  compact = false,
}: {
  plan?: ResearchPlan | null
  defaultOpen?: boolean
  compact?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  if (!plan) return null
  if (!plan.entries.length) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <ResearchVerdictBadge plan={plan} />
        <span>no tradeable entry from this research</span>
      </div>
    )
  }

  return (
    <div className={compact ? '' : 'bg-gray-900/40 border border-gray-700/40 rounded-lg p-3'}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex flex-wrap items-center gap-2 w-full text-left"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5 text-gray-500 shrink-0" />
              : <ChevronRight className="w-3.5 h-3.5 text-gray-500 shrink-0" />}
        <Telescope className="w-3.5 h-3.5 text-tradebot-accent shrink-0" />
        <span className="text-xs text-gray-300">
          {plan.entries.length} researched {plan.entries.length === 1 ? 'entry' : 'entries'}
        </span>
        <ResearchVerdictBadge plan={plan} />
        {plan.signal_count > 1 && (
          <span
            className="inline-flex items-center gap-1 text-[10px] text-gray-500"
            title={`combined from: ${plan.signal_sources}`}
          >
            <Layers className="w-3 h-3" />
            {plan.signal_count} signals combined
          </span>
        )}
        {plan.speculative ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-amber-300/90">
            <AlertTriangle className="w-3 h-3" /> unverified
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[10px] text-emerald-300/90">
            <ShieldCheck className="w-3 h-3" /> sourced
          </span>
        )}
      </button>

      {open && (
        <div className="mt-2">
          {plan.entries.map((e) => <EntryRow key={e.label} entry={e} />)}
          {plan.rationale && (
            <p className="text-[11px] text-gray-500 leading-relaxed mt-2">{plan.rationale}</p>
          )}
          <div className="text-[10px] text-gray-600 mt-1">
            researched {plan.age_hours.toFixed(1)}h ago
            {plan.horizon_hours ? ` · ${plan.horizon_hours}h horizon` : ''}
            {plan.provider_used ? ` · via ${plan.provider_used}` : ''}
          </div>
        </div>
      )}
    </div>
  )
}
