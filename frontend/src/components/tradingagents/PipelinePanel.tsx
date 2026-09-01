/**
 * PipelinePanel — the live view of a TradingAgents run.
 *
 * Phase chips light up as the LangGraph advances (analyst team → bull/bear
 * debate → research manager → trader → risk debate → portfolio manager),
 * and the dialogue feed shows each agent message as it streams past.
 */
import { useEffect, useMemo, useRef } from 'react'
import { Loader2 } from 'lucide-react'

import { PHASE_LABELS, PHASE_ORDER, type TaPhase, type TaStreamEvent } from './types'

const PHASE_DONE = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40'
const PHASE_ACTIVE = 'bg-cyan-500/20 text-cyan-200 border-cyan-400/50 animate-pulse'
const PHASE_TODO = 'border-slate-700/70 text-slate-500'

function phaseIndex(phase: TaPhase | null): number {
  if (!phase) return -1
  const i = PHASE_ORDER.indexOf(phase)
  return i >= 0 ? i : phase === 'done' ? PHASE_ORDER.length : 0
}

interface Props {
  runId: string | null
  phase: TaPhase | null
  status: 'idle' | 'running' | 'done' | 'error'
  error?: string | null
  events: TaStreamEvent[]
}

export default function PipelinePanel({ runId, phase, status, error, events }: Props) {
  const feedRef = useRef<HTMLDivElement | null>(null)
  const pinnedRef = useRef(true)

  const messages = useMemo(
    () => events.filter((e) => e.type === 'message').slice(-80),
    [events],
  )

  useEffect(() => {
    const el = feedRef.current
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight
  }, [messages.length])

  const currentIdx = phaseIndex(phase)

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1">
        {PHASE_ORDER.map((p, i) => (
          <span
            key={p}
            className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${
              i < currentIdx ? PHASE_DONE : i === currentIdx && status === 'running' ? PHASE_ACTIVE : PHASE_TODO
            }`}
          >
            {PHASE_LABELS[p]}
          </span>
        ))}
        {status === 'done' && (
          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${PHASE_DONE}`}>
            Decision ready
          </span>
        )}
      </div>

      {runId && (
        <div className="flex items-center justify-between text-[10px] text-slate-600">
          <span className="font-mono">{runId}</span>
          <span>{status === 'running' ? 'streaming…' : `${messages.length} agent turns`}</span>
        </div>
      )}

      {status === 'running' && !messages.length && (
        <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-[11px] text-slate-400">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-cyan-400" />
          Gathering market data — first analyst waking up…
        </div>
      )}

      {error && (
        <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-red-500/30 bg-red-950/30 p-2 text-[11px] text-red-300">
          {error}
        </pre>
      )}

      {messages.length > 0 && (
        <div
          ref={feedRef}
          onScroll={(e) => {
            const el = e.currentTarget
            pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
          }}
          className="max-h-64 space-y-1.5 overflow-y-auto rounded-lg border border-slate-800 bg-slate-950/60 p-2"
        >
          {messages.map((m) => {
            const d = m.data as { role?: string; agent?: string; preview?: string; length?: number }
            return (
              <div key={m.seq} className="text-[11px] leading-snug">
                <span className="mr-1.5 rounded bg-slate-700/60 px-1 py-px font-mono text-[9px] uppercase text-slate-300">
                  {d.agent || d.role || 'agent'}
                </span>
                <span className="whitespace-pre-wrap text-slate-300">{d.preview}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
