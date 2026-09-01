/**
 * useRoomSettings — the trading room's agent roster and execution policy.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'

export interface RoomProfile {
  agent_id: number
  name: string
  role: string
  description: string | null
  system_prompt: string
  model: string
  is_active: boolean
  pairs: string | null
  human_name: string
  title: string
  color: string
  seat: number
  /** Picks the body build and hair in the 3D room. Rendering only. */
  gender: 'male' | 'female'
  tasks: string | null
  customised: boolean
}

export interface Mt5AccountOption {
  id: number
  name: string
  login: string
  balance: number
  equity: number
  currency: string
}

export interface ExecutionPolicy {
  execution_enabled: boolean
  dry_run: boolean
  allow_sim: boolean
  allow_crypto: boolean
  allow_mt5: boolean
  mt5_account_id: number | null
  /** False = Signal Generator routes to mt5_demo_account_id (safe default). True = live account. */
  mt5_live_mode: boolean
  mt5_demo_account_id: number | null
  risk_pct: number
  max_open_positions: number
  min_consensus: number
  min_confidence: number
  max_trades_per_day: number
  max_leverage: number
  /** Seconds between re-analyses of a pinned pair. One of FOCUS_INTERVALS. */
  focus_interval_s: number
  /** The timeframe the board analyses on, and the one the room's chart draws. */
  focus_timeframe: string
  /** Whether the room worker keeps meeting (and re-arms after a restart). */
  worker_enabled: boolean
  // ── Bitcoin 1064-day cycle ──
  /** Cycle bottoms as ISO dates — the calendar's spine. */
  cycle_anchors: string[]
  cycle_bull_days: number
  cycle_bear_days: number
  /** Auto risk reduction inside the projected-bear / late-bull window. */
  cycle_auto_risk: boolean
  cycle_risk_multiplier: number
  /** Years of monthly candles the cycle chart reaches back for. */
  cycle_history_years: number
  /** When on, the room supervisor reviews and manages all copy profiles. */
  manage_copy_profiles?: boolean
  copy_max_drawdown_pct?: number
  trades_today?: number
  mt5_accounts?: Mt5AccountOption[]
  global_auto_trading_enabled?: boolean
}

export function useRoomSettings() {
  const [profiles, setProfiles] = useState<RoomProfile[]>([])
  const [policy, setPolicy] = useState<ExecutionPolicy | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [p, s] = await Promise.all([
        api.get('/agents/room/profiles'),
        api.get('/agents/room/settings'),
      ])
      setProfiles(p.data.profiles ?? [])
      setPolicy(s.data)
    } catch {
      setError('Could not reach the backend. Is it running?')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const saveProfiles = useCallback(async (updates: Partial<RoomProfile>[]) => {
    await api.put('/agents/room/profiles', updates)
    await load()
  }, [load])

  /** Returns an error string when the backend refuses (e.g. arming live). */
  const savePolicy = useCallback(async (patch: Partial<ExecutionPolicy>): Promise<string | null> => {
    try {
      const { data } = await api.put('/agents/room/settings', patch)
      setPolicy((prev) => ({ ...(prev as ExecutionPolicy), ...data }))
      return null
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      return detail ?? 'Could not save that setting.'
    }
  }, [])

  return { profiles, policy, loading, error, reload: load, saveProfiles, savePolicy }
}
