/**
 * cycleOverlay — turns the cycle API's windows (ISO date ranges) into the
 * full-height boxes the chart overlay draws, plus the shared palette the
 * calendar and badges use, so every surface reads the same season the same way.
 */
import type { CycleWindowBox } from '@/utils/zonesOverlay'
import type { CycleWindow } from '@/hooks/useBtcCycle'

/** ISO date (YYYY-MM-DD) → ms epoch at UTC midnight, matching the daily bars. */
export function isoToMs(iso: string): number {
  return new Date(`${iso}T00:00:00Z`).getTime()
}

export function toCycleBoxes(windows: CycleWindow[]): CycleWindowBox[] {
  return windows
    .filter((w) => w.start && w.end)
    .map((w) => ({
      startMs: isoToMs(w.start),
      endMs: isoToMs(w.end),
      phase: w.phase,
      projected: Boolean(w.projected),
    }))
    .sort((a, b) => a.startMs - b.startMs)
}

/** The one palette every cycle surface paints from. */
export const CYCLE_COLORS = {
  bull: {
    text: 'text-emerald-300',
    bg: 'bg-emerald-500/15',
    border: 'border-emerald-500/40',
    dot: 'bg-emerald-400',
    solid: '#22c55e',
    soft: 'rgba(34, 197, 94, 0.10)',
    softProjected: 'rgba(34, 197, 94, 0.05)',
  },
  bear: {
    text: 'text-red-300',
    bg: 'bg-red-500/15',
    border: 'border-red-500/40',
    dot: 'bg-red-400',
    solid: '#ef4444',
    soft: 'rgba(239, 68, 68, 0.10)',
    softProjected: 'rgba(239, 68, 68, 0.05)',
  },
} as const

export function phaseColor(phase: 'bull' | 'bear') {
  return CYCLE_COLORS[phase]
}
