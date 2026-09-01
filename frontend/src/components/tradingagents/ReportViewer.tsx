/**
 * ReportViewer — the finished dossier.
 *
 * Every section of a completed run: the four analyst reports, both debate
 * transcripts, the trader's plan and the final decision, with the verdict
 * card on top. Reports are markdown-ish; they render in pre-wrap blocks so
 * headings and structure survive without pulling in a markdown dependency.
 */
import { useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import type { TaResult } from './types'

function verdictOf(result?: TaResult | null): { label: string; cls: string } {
  const rec = result?.recommendation
  const raw = String(rec?.action ?? rec?.decision ?? '').toLowerCase()
  if (raw.includes('buy') || raw.includes('long')) return { label: 'BUY', cls: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' }
  if (raw.includes('sell') || raw.includes('short')) return { label: 'SELL', cls: 'bg-red-500/20 text-red-300 border-red-500/40' }
  return { label: raw ? raw.toUpperCase() : 'HOLD', cls: 'bg-slate-600/40 text-slate-200 border-slate-500/50' }
}

interface Section {
  key: string
  title: string
  body: string
}

function sectionsOf(result: TaResult): Section[] {
  const r = result.reports ?? {}
  const inv = result.investment_debate ?? {}
  const risk = result.risk_debate ?? {}
  return [
    { key: 'situation', title: 'Situation summary', body: result.situation_summary || '' },
    { key: 'market', title: 'Market / technical', body: r.market || '' },
    { key: 'sentiment', title: 'Sentiment', body: r.sentiment || '' },
    { key: 'news', title: 'News', body: r.news || '' },
    { key: 'fundamentals', title: 'Fundamentals', body: r.fundamentals || '' },
    {
      key: 'research_debate',
      title: `Bull vs Bear (${inv.turns ?? 0} turns)`,
      body:
        `— BULL —\n${inv.bull_history || '(silent)'}\n\n— BEAR —\n${inv.bear_history || '(silent)'}\n\n— JUDGE —\n${inv.judge_decision || '(pending)'}`,
    },
    { key: 'trader', title: "Trader's plan", body: result.trader_plan || '' },
    {
      key: 'risk_debate',
      title: `Risk debate (${risk.turns ?? 0} turns)`,
      body:
        `— AGGRESSIVE —\n${risk.aggressive_history || '(silent)'}\n\n— CONSERVATIVE —\n${risk.conservative_history || '(silent)'}\n\n— JUDGE —\n${risk.judge_decision || '(pending)'}`,
    },
    { key: 'final', title: 'Final decision', body: result.final_trade_decision || '' },
  ].filter((s) => s.body.trim().length > 0)
}

export default function ReportViewer({ result }: { result?: TaResult | null }) {
  const [openKey, setOpenKey] = useState<string | null>('final')
  const verdict = verdictOf(result)
  const sections = useMemo(() => (result ? sectionsOf(result) : []), [result])

  if (!result) {
    return (
      <p className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-[11px] text-slate-500">
        No completed run selected yet.
      </p>
    )
  }

  const confRaw = result.recommendation?.confidence
  const confPct =
    confRaw != null && !Number.isNaN(Number(confRaw))
      ? `${Math.round(Number(confRaw) * (Number(confRaw) <= 1 ? 100 : 1))}%`
      : null

  return (
    <div className="space-y-2">
      {/* Verdict card */}
      <div className="flex items-center gap-3 rounded-xl border border-slate-700/70 bg-slate-900/60 p-3">
        <span className={`rounded-lg border px-3 py-1.5 text-sm font-bold tracking-wide ${verdict.cls}`}>
          {verdict.label}
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-mono text-xs text-slate-200">
            {result.ticker ?? '—'} · {result.trade_date ?? '—'}
          </div>
          <div className="truncate text-[10px] text-slate-500" title={result.decision_summary || undefined}>
            {result.decision_summary || 'No summary returned.'}
          </div>
        </div>
        {confPct && (
          <span className="shrink-0 rounded-full bg-cyan-500/15 px-2.5 py-1 font-mono text-[11px] text-cyan-300">
            {confPct}
          </span>
        )}
      </div>

      {/* Collapsible report sections */}
      {sections.map((s) => {
        const open = openKey === s.key
        return (
          <div key={s.key} className="overflow-hidden rounded-lg border border-slate-800 bg-slate-950/50">
            <button
              type="button"
              onClick={() => setOpenKey(open ? null : s.key)}
              className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left text-[11px] font-semibold text-slate-300 hover:bg-slate-800/40"
            >
              {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
              {s.title}
            </button>
            {open && (
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words border-t border-slate-800 p-2.5 font-mono text-[10px] leading-relaxed text-slate-300">
                {s.body}
              </pre>
            )}
          </div>
        )
      })}
    </div>
  )
}
