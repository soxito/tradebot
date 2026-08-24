/**
 * CycleBadge — the compact season read for dashboards and trading pages.
 *
 * One line: which phase the 1064-day calendar is in and how far it is from
 * the next projected turn. Clicks through to the full cycle page.
 */
import Link from 'next/link'
import { RefreshCw } from 'lucide-react'
import { useBtcCycleState, type CycleState } from '@/hooks/useBtcCycle'
import { phaseColor } from '@/utils/cycleOverlay'

function headline(s: CycleState): string {
  if (s.phase === 'bull') {
    return s.days_to_top >= 0 ? `top in ${s.days_to_top}d` : 'top overdue'
  }
  return s.days_to_bottom >= 0 ? `bottom in ${s.days_to_bottom}d` : 'bottom overdue'
}

export default function CycleBadge({ compact = false }: { compact?: boolean }) {
  const { state } = useBtcCycleState()
  if (!state?.ok) return null

  const color = phaseColor(state.phase)
  return (
    <Link
      href="/bitcoin-cycle"
      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition hover:brightness-125 ${color.border} ${color.bg} ${color.text}`}
      title={`BTC cycle: ${state.phase} — day ${state.day_of_cycle} since the ${state.anchor} bottom. Projected top ${state.projected_top}, bottom ${state.projected_bottom}.`}
    >
      <RefreshCw className="h-3 w-3" />
      <span className="uppercase tracking-wide">{state.phase}</span>
      {!compact && <span className="text-slate-400">·</span>}
      {!compact && <span className="text-slate-300">{headline(state)}</span>}
    </Link>
  )
}
