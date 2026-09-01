/**
 * LiveDebate — the meeting transcript as it happens.
 *
 * Every `agent.speaking` SSE event lands here in arrival order: who said it,
 * their vote and the argument they made. The chair's closing verdict is
 * highlighted. Auto-scrolls to the newest line while you're already reading
 * the bottom; stops following the moment you scroll up.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, Gavel, MessageSquareText } from 'lucide-react'
import type { DebateTurn } from '@/hooks/useTradingRoom'
import { toReasoningText } from '@/utils/reasoning'

const ACTION_STYLE: Record<string, string> = {
  buy: 'bg-emerald-500/20 text-emerald-300',
  sell: 'bg-red-500/20 text-red-300',
  hold: 'bg-slate-600/40 text-slate-300',
}

function bucket(action?: string): string {
  const a = (action ?? '').toLowerCase()
  if (a.includes('buy') || a === 'long' || a === 'approve' || a === 'execute') return 'buy'
  if (a.includes('sell') || a === 'short' || a === 'close') return 'sell'
  return 'hold'
}

function timeOf(at: number) {
  return new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

interface Props {
  turns: DebateTurn[]
  running: boolean
}

export default function LiveDebate({ turns, running }: Props) {
  const [collapsed, setCollapsed] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const pinnedRef = useRef(true)

  // Follow the tail while the reader is at (or near) the bottom; the moment
  // they scroll up to re-read an earlier seat, the view stays put.
  useEffect(() => {
    const el = scrollRef.current
    if (!el || !pinnedRef.current) return
    el.scrollTop = el.scrollHeight
  }, [turns])

  // A new meeting starts a fresh page of transcript.
  const visible = useMemo(() => {
    let start = 0
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].chair) { start = i + 1; break }
    }
    return turns.slice(start).slice(-40)
  }, [turns])

  return (
    <div className="shrink-0 overflow-hidden rounded-xl border border-slate-700/70 bg-slate-900/50">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="flex items-center gap-2 text-left"
        >
          <MessageSquareText className="h-3.5 w-3.5 shrink-0 text-cyan-300" />
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-300">Live debate</span>
          {collapsed ? <ChevronDown className="h-3.5 w-3.5 text-slate-500" /> : <ChevronUp className="h-3.5 w-3.5 text-slate-500" />}
        </button>
        {running && (
          <span className="ml-1 flex items-center gap-1.5 text-[10px] text-cyan-300">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" />
            on air
          </span>
        )}
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="ml-auto rounded p-1 text-slate-500 hover:bg-slate-800 hover:text-slate-300"
          title={collapsed ? 'Expand' : 'Collapse'}
          aria-label={collapsed ? 'Expand live debate' : 'Collapse live debate'}
        >
          {collapsed ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
        </button>
      </div>

      {collapsed ? null : (
        <div
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget
            pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
          }}
          className="max-h-[320px] space-y-2 overflow-y-auto border-t border-slate-800 px-3 py-2"
        >
        {!visible.length && (
          <p className="py-6 text-center text-[11px] text-slate-500">
            The transcript fills in as each agent presents its read.
          </p>
        )}
        {visible.map((turn, i) => {
          const b = bucket(turn.action)
          return (
            <div key={`${turn.session_id}-${turn.role}-${turn.at}-${i}`} className="flex gap-2">
              {turn.chair ? (
                <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-cyan-500/20">
                  <Gavel className="h-3 w-3 text-cyan-300" />
                </span>
              ) : (
                <span
                  className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                  style={{ backgroundColor: turn.color ?? '#94a3b8' }}
                />
              )}
              <div className={`min-w-0 flex-1 ${turn.chair ? 'rounded-lg border border-cyan-500/30 bg-cyan-500/5 px-2.5 py-1.5' : ''}`}>
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className={`text-[12px] font-medium ${turn.chair ? 'text-cyan-200' : 'text-slate-200'}`}>
                    {turn.human_name ?? turn.agent_name ?? turn.role}
                  </span>
                  {turn.chair && <span className="text-[10px] uppercase tracking-wide text-cyan-300/80">verdict</span>}
                  {turn.symbol && <span className="font-mono text-[10px] text-slate-500">{turn.symbol}</span>}
                  {turn.action && (
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${ACTION_STYLE[b]}`}>
                      {turn.action}
                    </span>
                  )}
                  {typeof turn.confidence === 'number' && turn.confidence > 0 && (
                    <span className="font-mono text-[10px] text-slate-500">
                      {Math.round(turn.confidence * (turn.confidence <= 1 ? 100 : 1))}%
                    </span>
                  )}
                  <span className="ml-auto font-mono text-[10px] text-slate-600">{timeOf(turn.at)}</span>
                </div>
                <p className="mt-0.5 whitespace-pre-line text-[11px] leading-snug text-slate-400">
                  {toReasoningText(turn.text)}
                </p>
              </div>
            </div>
          )
        })}
        </div>
      )}
    </div>
  )
}
