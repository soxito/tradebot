/**
 * RunsHistory — every TradingAgents run this desk has made.
 *
 * Backed by the backend's durable table, so history survives sidecar and
 * app restarts. Clicking a finished run loads its full dossier into the
 * report viewer above.
 */
import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'

import { apiClient } from '@/services/api'
import type { TaRunSummary } from './types'

const DECISION_CLS: Record<string, string> = {
  buy: 'bg-emerald-500/20 text-emerald-300',
  sell: 'bg-red-500/20 text-red-300',
  hold: 'bg-slate-600/40 text-slate-300',
}

function decisionCls(d?: string | null): string {
  if (!d) return DECISION_CLS.hold
  return DECISION_CLS[d.toLowerCase()] ?? DECISION_CLS.hold
}

interface Props {
  refreshKey: number
  selectedRunId: string | null
  onSelect: (runId: string) => void
}

export default function RunsHistory({ refreshKey, selectedRunId, onSelect }: Props) {
  const [runs, setRuns] = useState<TaRunSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const res = await apiClient.tradingAgents.getRuns(50)
        if (alive) setRuns(res.data?.runs ?? [])
      } catch {
        /* keep the previous list; the next refresh will retry */
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [refreshKey])

  if (loading && !runs.length) {
    return (
      <div className="flex items-center gap-2 p-3 text-[11px] text-slate-500">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading run history…
      </div>
    )
  }

  if (!runs.length) {
    return (
      <p className="p-3 text-[11px] text-slate-500">
        No runs yet. Convene the agents on any ticker to start the log.
      </p>
    )
  }

  return (
    <div className="divide-y divide-slate-800/70">
      {runs.map((r) => (
        <button
          key={r.run_id}
          type="button"
          onClick={() => r.status === 'done' && onSelect(r.run_id)}
          disabled={r.status !== 'done'}
          className={`flex w-full items-center gap-2 px-2.5 py-2 text-left text-[11px] transition ${
            r.run_id === selectedRunId ? 'bg-cyan-500/10' : 'hover:bg-slate-800/40'
          } ${r.status !== 'done' ? 'cursor-default opacity-80' : ''}`}
        >
          <span className={`rounded px-1.5 py-0.5 font-bold uppercase ${decisionCls(r.decision)}`}>
            {r.status === 'running' ? '…' : r.status === 'error' ? 'ERR' : (r.decision ?? '?')}
          </span>
          <span className="font-mono text-slate-200">{r.ticker}</span>
          <span className="text-slate-500">{r.trade_date}</span>
          <span className="ml-auto shrink-0 text-slate-600">
            {r.created_at ? new Date(r.created_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
            {r.source === 'trade_validation' && (
              <span className="ml-1 rounded bg-amber-500/15 px-1 py-px text-[9px] uppercase text-amber-300">
                auto
              </span>
            )}
          </span>
        </button>
      ))}
    </div>
  )
}
