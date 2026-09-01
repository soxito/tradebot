/**
 * AgentPanel — one seat's live status card.
 */
import type { RoomSeat } from '@/hooks/useTradingRoom'
import { toReasoningText } from '@/utils/reasoning'

const STATE_LABEL: Record<string, string> = {
  idle: 'Idle',
  analyzing: 'Analyzing',
  presenting: 'Reporting',
  resting: 'Standing by',
  error: 'Error',
}

const STATE_STYLE: Record<string, string> = {
  idle: 'bg-slate-700/60 text-slate-300',
  analyzing: 'bg-cyan-500/20 text-cyan-300 animate-pulse',
  presenting: 'bg-emerald-500/20 text-emerald-300',
  resting: 'bg-slate-600/40 text-slate-400',
  error: 'bg-red-500/20 text-red-300',
}

const ACTION_STYLE: Record<string, string> = {
  buy: 'text-emerald-400',
  bullish: 'text-emerald-400',
  approve: 'text-emerald-400',
  execute: 'text-emerald-400',
  sell: 'text-red-400',
  bearish: 'text-red-400',
  reject: 'text-red-400',
  close: 'text-red-400',
}

interface Props {
  seat: RoomSeat
  focused: boolean
  onSelect: (role: string) => void
}

export default function AgentPanel({ seat, focused, onSelect }: Props) {
  const decision = seat.last_decision
  const confidence = decision ? Math.round((decision.confidence ?? 0) * 100) : 0
  const action = decision?.action?.toLowerCase() ?? ''

  return (
    <button
      type="button"
      onClick={() => onSelect(seat.role)}
      className={`flex h-44 w-full flex-col overflow-hidden rounded-xl border p-3 text-left transition ${
        focused
          ? 'border-cyan-400/70 bg-cyan-500/5 shadow-[0_0_24px_-8px_rgba(34,211,238,0.6)]'
          : 'border-slate-700/70 bg-slate-900/50 hover:border-slate-500'
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-semibold text-slate-950"
          style={{ backgroundColor: seat.color }}
        >
          {seat.human_name.slice(0, 2).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-slate-100">{seat.human_name}</div>
          <div className="truncate text-[11px] text-slate-400">{seat.title}</div>
        </div>
        <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${STATE_STYLE[seat.state] ?? STATE_STYLE.idle}`}>
          {STATE_LABEL[seat.state] ?? seat.state}
        </span>
      </div>

      <div className="mt-2 min-h-[16px] font-mono text-[11px] text-slate-400">
        {seat.symbol ?? ''}
      </div>

      <div className="mt-2 flex-1">
        {decision && (
          <div className="space-y-1.5">
            <div className="flex items-baseline justify-between">
              <span className={`text-sm font-semibold uppercase ${ACTION_STYLE[action] ?? 'text-slate-300'}`}>
                {decision.action}
              </span>
              <span className="font-mono text-[11px] text-slate-400">{confidence}%</span>
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-slate-800">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{ width: `${confidence}%`, backgroundColor: seat.color }}
              />
            </div>
            <p className="text-[11px] leading-snug text-slate-400">{toReasoningText(decision.reasoning)}</p>
            {!decision.ai_called && (
              <span className="text-[10px] text-slate-500">from memory — no AI call</span>
            )}
          </div>
        )}

        {seat.state === 'error' && seat.error && (
          <p className="mt-2 line-clamp-2 text-[11px] text-red-400">{seat.error}</p>
        )}
      </div>
    </button>
  )
}
