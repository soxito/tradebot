/**
 * useBtcCycle — the Bitcoin 1064-day calendar, live.
 *
 * One hook serves every surface: the cycle page (state + windows + calendar),
 * the compact badge (state only), and the chart overlays (windows). State
 * polls gently — the calendar moves a day at a time — and `cycle.transition`
 * SSE events refresh it the moment the phase turns.
 */
import { useCallback, useEffect, useState } from 'react'
import { apiClient } from '@/services/api'
import { eventStream } from '@/services/eventStream'

export type CyclePhase = 'bull' | 'bear'

export interface CycleValidationRow {
  bottom: string
  projected_top: string
  projected_bottom: string
  actual_top?: string
  top_error_days?: number
  top_hit?: boolean
  actual_bottom?: string
  bottom_error_days?: number
  bottom_hit?: boolean
}

export interface CycleState {
  ok: boolean
  phase: CyclePhase
  anchor: string
  day_of_cycle: number
  phase_day: number
  phase_days_total: number
  phase_pct: number
  projected_top: string
  projected_bottom: string
  days_to_top: number
  days_to_bottom: number
  late_phase: boolean
  price?: number | null
  cycle_high?: number | null
  cycle_low?: number | null
  validation?: {
    cycles: CycleValidationRow[]
    top_hit_rate: number | null
    bottom_hit_rate: number | null
    tolerance_days: number
  }
  as_of: string
}

export interface CycleWindow {
  start: string
  end: string
  phase: CyclePhase
  projected: boolean
}

export interface CycleCalendarDay {
  date: string
  weekday: number
  phase: CyclePhase
  day_of_cycle: number
  phase_pct: number
  projected: boolean
  is_top: boolean
  is_bottom: boolean
  is_anchor: boolean
  is_today: boolean
  is_halving: boolean
  days_to_top: number
  days_to_bottom: number
}

export interface CycleExpectation {
  offset: number | null
  horizon_days?: number
  samples: number
  avg_return_pct?: number
  median_return_pct?: number
  best_return_pct?: number
  worst_return_pct?: number
  avg_max_drawdown_pct?: number
}

export interface CycleCalendar {
  ok: boolean
  year: number
  month: number
  days: CycleCalendarDay[]
  today_expectation: CycleExpectation
  halvings: string[]
}

const STATE_POLL_MS = 5 * 60 * 1000

/** Cycle state only — the badge's diet. */
export function useBtcCycleState() {
  const [state, setState] = useState<CycleState | null>(null)
  const [loaded, setLoaded] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data } = await apiClient.getCycleState()
      if (data?.ok !== false) setState(data as CycleState)
    } catch {
      /* keep the last known phase — the calendar does not move fast */
    } finally {
      setLoaded(true)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(load, STATE_POLL_MS)
    const unsub = eventStream.subscribe('cycle.transition', () => { void load() })
    return () => {
      window.clearInterval(timer)
      unsub()
    }
  }, [load])

  return { state, loaded, refresh: load }
}

/** Windows only — chart overlays. Fetched once per mount; boxes move rarely. */
export function useBtcCycleWindows() {
  const [windows, setWindows] = useState<CycleWindow[]>([])

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const { data } = await apiClient.getCycleWindows()
        if (alive && data?.ok) setWindows(data.windows ?? [])
      } catch {
        /* no overlay is the correct degradation */
      }
    })()
    return () => { alive = false }
  }, [])

  return windows
}

/** Everything the dedicated cycle page renders. */
export function useBtcCyclePage() {
  const { state, loaded, refresh } = useBtcCycleState()
  const windows = useBtcCycleWindows()
  const [calendar, setCalendar] = useState<CycleCalendar | null>(null)
  const [calLoading, setCalLoading] = useState(false)
  const [analogs, setAnalogs] = useState<CycleAnalogs | null>(null)
  const [expectation, setExpectation] = useState<ExpectationRow[]>([])

  const now = new Date()
  const [month, setMonth] = useState<{ y: number; m: number }>({
    y: now.getFullYear(),
    m: now.getMonth() + 1,
  })

  const loadCalendar = useCallback(async (y: number, m: number) => {
    setCalLoading(true)
    try {
      const { data } = await apiClient.getCycleCalendar(y, m)
      setCalendar(data as CycleCalendar)
    } catch {
      /* the grid stays on the last good month */
    } finally {
      setCalLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadCalendar(month.y, month.m)
  }, [month, loadCalendar])

  // Analogs + the forward expectation table — fetched once; the ghosts of
  // cycles past do not move. Refreshed on a phase transition.
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const [a, e] = await Promise.all([
          apiClient.getCycleAnalogs(),
          apiClient.getCycleExpectation(undefined, 30),
        ])
        if (!alive) return
        if (a.data?.ok) setAnalogs(a.data as CycleAnalogs)
        if (e.data?.ok) setExpectation(e.data.rows ?? [])
      } catch {
        /* the tables just stay empty — the rest of the page stands */
      }
    })()
    return () => { alive = false }
  }, [])

  const shiftMonth = useCallback((delta: number) => {
    setMonth((prev) => {
      const d = new Date(prev.y, prev.m - 1 + delta, 1)
      return { y: d.getFullYear(), m: d.getMonth() + 1 }
    })
  }, [])

  return {
    state,
    loaded,
    windows,
    calendar,
    calLoading,
    analogs,
    expectation,
    month,
    shiftMonth,
    refresh,
  }
}

/** ── Cycle alignment + daily prediction ── */

export interface AnalogPath {
  bottom: string
  /** True for the cycle still being lived through. */
  live?: boolean
  points: [number, number][]  // [day_offset, pct_from_bottom]
}

export interface CycleAnalogs {
  ok: boolean
  cycles: AnalogPath[]
  current: AnalogPath | null
  bull_days?: number
  bear_days?: number
}

export interface ExpectationRow {
  offset: number
  horizon_days?: number
  samples: number
  avg_return_pct?: number | null
  median_return_pct?: number | null
  best_return_pct?: number | null
  worst_return_pct?: number | null
  avg_max_drawdown_pct?: number | null
}

/** ── Whale watch ── */

export interface WhaleHolder {
  address: string
  label: string
  category: string
  balance_btc: number | null
  net_flow_7d_btc: number | null
  tx_count: number | null
  source: string
}

export interface WhaleMove {
  txid: string
  label: string
  address: string
  category: string
  direction: 'in' | 'out'
  btc: number
  time: number | null
}

export interface WhalePayload {
  ok: boolean
  status?: string
  score: string
  net_flow_7d_btc: number | null
  detail?: string
  holders: WhaleHolder[]
  transfers: WhaleMove[]
}

/** The whale registry + score, polled gently and live via whale.move. */
export function useWhaleWatch() {
  const [data, setData] = useState<WhalePayload | null>(null)
  const [loaded, setLoaded] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data: d } = await apiClient.getWhaleHolders()
      if (d) setData(d as WhalePayload)
    } catch {
      /* keep the last read */
    } finally {
      setLoaded(true)
    }
  }, [])

  useEffect(() => {
    void load()
    // Real-time-ish: the backend refreshes its snapshot every 45s and pushes
    // whale.move SSE events on new transfers; this poll is the safety net.
    const timer = window.setInterval(load, 60 * 1000)
    const unsub = eventStream.subscribe('whale.move', () => { void load() })
    return () => {
      window.clearInterval(timer)
      unsub()
    }
  }, [load])

  return { whale: data, loaded }
}
