/**
 * SessionStream — the board-meeting log.
 *
 * Each session expands into the full argument: a vote bar showing how the
 * table split, JARVIS's closing reasoning, then every seat's contribution.
 * A verdict filter (all / buy / sell) keeps actionable meetings one click away
 * when the log fills with holds from a quiet market.
 */
import { useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, Cpu } from 'lucide-react'
import type { RoomSeat, RoomSession } from '@/hooks/useTradingRoom'
import { toReasoningText } from '@/utils/reasoning'

const ACTION_STYLE: Record<string, string> = {
  buy: 'text-emerald-400',
  sell: 'text-red-400',
  hold: 'text-slate-300',
}

type Filter = 'all' | 'buy' | 'sell'

function timeOf(ts?: number) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

interface Props {
  sessions: RoomSession[]
  seats: RoomSeat[]
}

export default function SessionStream({ sessions, seats }: Props) {
  const [open, setOpen] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const colorOf = (role: string) => seats.find((s) => s.role === role)?.color ?? '#94a3b8'
  const nameOf = (role: string) => seats.find((s) => s.role === role)?.human_name ?? role

  const visible = useMemo(() => {
    if (filter === 'all') return sessions
    return sessions.filter((s) => (s.final_action ?? s.consensus?.leader ?? '').toLowerCase() === filter)
  }, [sessions, filter])

  if (!sessions.length) {
    return (
      <p className="px-3 py-8 text-center text-sm text-slate-500">
        No meetings yet. Pick a pair to focus, or wait for the next signal.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1 px-1">
        {(['all', 'buy', 'sell'] as const).map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            className={`rounded-full px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-wide transition ${
              filter === f
                ? f === 'buy' ? 'bg-emerald-500/20 text-emerald-300'
                  : f === 'sell' ? 'bg-red-500/20 text-red-300'
                  : 'bg-slate-700/60 text-slate-200'
                : 'text-slate-500 hover:text-slate-300'
            }`}
          >
            {f}
          </button>
        ))}
        <span className="ml-auto font-mono text-[10px] text-slate-600">{visible.length}</span>
      </div>

      {!visible.length && (
        <p className="px-3 py-4 text-center text-[11px] text-slate-600">
          No {filter} calls in the log yet.
        </p>
      )}

      {visible.map((session) => {
        const expanded = open === session.session_id
        const running = session.status === 'running'
        const action = (session.final_action ?? session.consensus?.leader ?? '').toLowerCase()
        const tally = session.consensus?.tally
        const totalVotes = tally ? Math.max(1, tally.buy + tally.sell + tally.hold) : 1
        return (
          <div
            key={session.session_id}
            className="overflow-hidden rounded-xl border border-slate-700/70 bg-slate-900/50"
          >
            <button
              type="button"
              onClick={() => setOpen(expanded ? null : session.session_id)}
              className="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-slate-800/40"
            >
              {expanded ? (
                <ChevronDown className="h-4 w-4 shrink-0 text-slate-500" />
              ) : (
                <ChevronRight className="h-4 w-4 shrink-0 text-slate-500" />
              )}
              <span className="font-mono text-sm font-semibold text-slate-100">{session.symbol}</span>
              <span className="text-[11px] text-slate-500">{session.timeframe}</span>
              <span className="ml-auto flex items-center gap-2">
                {running ? (
                  <span className="animate-pulse rounded-full bg-cyan-500/20 px-2 py-0.5 text-[10px] text-cyan-300">
                    in session
                  </span>
                ) : action ? (
                  <span className={`text-sm font-semibold uppercase ${ACTION_STYLE[action] ?? 'text-slate-300'}`}>
                    {action}
                  </span>
                ) : (
                  <span className="text-sm text-slate-500">—</span>
                )}
                {session.trigger && (
                  <span className="rounded bg-slate-700/50 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-slate-400">
                    {session.trigger.replace('_', ' ')}
                  </span>
                )}
                <span className="font-mono text-[10px] text-slate-500">{timeOf(session.started_at)}</span>
              </span>
            </button>

            {expanded && (
              <div className="space-y-2 border-t border-slate-800 px-3 py-3">
                {/* Vote split as a stacked bar — reads faster than numbers alone. */}
                {session.consensus && tally && (
                  <div className="rounded-lg bg-slate-800/50 px-3 py-2">
                    <div className="flex h-1.5 overflow-hidden rounded-full bg-slate-700/50">
                      <div className="bg-emerald-400" style={{ width: `${(tally.buy / totalVotes) * 100}%` }} />
                      <div className="bg-red-400" style={{ width: `${(tally.sell / totalVotes) * 100}%` }} />
                      <div className="bg-slate-500" style={{ width: `${(tally.hold / totalVotes) * 100}%` }} />
                    </div>
                    <div className="mt-1.5 flex items-center gap-3 text-[11px]">
                      <span className="text-emerald-400">buy {tally.buy}</span>
                      <span className="text-red-400">sell {tally.sell}</span>
                      <span className="text-slate-300">hold {tally.hold}</span>
                      <span className="ml-auto text-slate-400">
                        led by {nameOf(session.consensus.leader)} ·{' '}
                        {Math.round(session.consensus.agreement * 100)}% agreement
                      </span>
                    </div>
                  </div>
                )}

                {session.trigger && (
                  <p className="text-[10px] uppercase tracking-wide text-slate-600">
                    convened by {session.trigger.replace('_', ' ')}
                  </p>
                )}

                {/* Best-trader skill the seats were prompted with (A+A+B) */}
                {(session as any).hermes_skill?.symbol && (
                  <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-violet-500/20 bg-violet-500/10 px-2.5 py-1.5 text-[11px]">
                    <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-bold text-amber-300">stock</span>
                    <span className="font-mono font-semibold text-violet-200">{(session as any).hermes_skill.symbol}</span>
                    <span className="text-violet-300/70">{(session as any).hermes_skill.asset_class}</span>
                    <span className="text-slate-500">·</span>
                    <span className="flex items-center gap-1 rounded bg-violet-500/20 px-1.5 py-0.5 text-[10px] text-violet-200">JARVIS chair</span>
                    <span className="text-[10px] text-violet-300/60">+ {(session as any).hermes_skill.linked_agents?.length ?? 7} seats</span>
                    {(session as any).hermes_skill.win_rate != null && (
                      <span className="ml-auto rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300">Learned win {Math.round((session as any).hermes_skill.win_rate * 100)}% over {(session as any).hermes_skill.decisions_reviewed ?? 0}</span>
                    )}
                  </div>
                )}

                {session.final_reasoning && (
                  <div className="rounded-lg border-l-2 border-cyan-500/50 bg-cyan-500/5 px-3 py-2">
                    <p className="text-[10px] font-semibold uppercase tracking-wide text-cyan-300/80">
                      JARVIS — verdict
                    </p>
                    <p className="mt-0.5 whitespace-pre-line text-[12px] leading-relaxed text-slate-200">
                      {toReasoningText(session.final_reasoning)}
                    </p>
                  </div>
                )}

                <div className="space-y-2 pt-1">
                  {session.decisions.map((d, i) => (
                    <div key={`${d.role}-${i}`} className="flex gap-2">
                      <span
                        className="mt-1 h-2 w-2 shrink-0 rounded-full"
                        style={{ backgroundColor: colorOf(d.role) }}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                          <span className="text-[12px] font-medium text-slate-200">{nameOf(d.role)}</span>
                          <span className={`text-[11px] uppercase ${ACTION_STYLE[(d.action ?? '').toLowerCase()] ?? 'text-slate-400'}`}>
                            {d.action}
                          </span>
                          <span className="font-mono text-[10px] text-slate-500">
                            {Math.round((d.confidence ?? 0) * 100)}%
                          </span>
                          {!d.ai_called && (
                            <span
                              className="flex items-center gap-0.5 rounded bg-slate-700/40 px-1 py-px text-[9px] uppercase tracking-wide text-slate-400"
                              title="Decided from stored memory without calling an AI model"
                            >
                              <Cpu className="h-2.5 w-2.5" /> local
                            </span>
                          )}
                          {(d as any).skill_used && (
                            <span className="rounded bg-amber-500/15 px-1 py-px text-[9px] font-mono text-amber-300" title={`Prompted with ${(d as any).skill_used} best-trader skill (${(d as any).skill_asset_class || ''})`}>
                              {(d as any).skill_used}
                            </span>
                          )}
                        </div>
                        <p className="whitespace-pre-line text-[11px] leading-snug text-slate-400">
                          {toReasoningText(d.reasoning)}
                        </p>
                      </div>
                    </div>
                  ))}
                  {!session.decisions.length && (
                    <p className="text-[11px] text-slate-500">Agents are still speaking…</p>
                  )}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
