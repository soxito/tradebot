/**
 * useTradingRoom — live state of the agent trading room.
 *
 * Hydrates from `/agents/room/state` (so a room opened mid-session, or after the
 * agents ran in the background, renders immediately) then tracks the SSE topics
 * the orchestrator publishes as it walks the pipeline.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'
import { eventStream } from '@/services/eventStream'

export type AgentState = 'idle' | 'analyzing' | 'presenting' | 'resting' | 'error'

export interface AgentDecisionSummary {
  action: string
  confidence: number
  reasoning: string
  ai_called: boolean
  completed_at: number
}

export interface RoomSeat {
  role: string
  human_name: string
  title: string
  color: string
  seat: number
  gender?: 'male' | 'female'
  agent_id?: number
  agent_name?: string
  description?: string | null
  pairs?: string | null
  state: AgentState
  symbol?: string
  session_id?: string
  phase?: number
  error?: string
  last_decision?: AgentDecisionSummary
  updated_at?: number
}

/** One leg of an execution — a single account or venue that took the trade. */
export interface ExecutionLeg {
  venue?: string
  /** 'demo' | 'live' for MT5, 'exchange' for crypto, 'paper' for the sim book. */
  role?: string
  status?: 'placed' | 'skipped' | 'error'
  ticket?: string
  reason?: string
}

export interface ExecutionReport {
  symbol: string
  action: string
  /**
   * 'dry_run' is no longer emitted: a dry run routes to the demo/paper account
   * and really fills there, so the outcome is 'placed' like any other. Kept in
   * the union only so events cached from before the change still render.
   */
  status: 'placed' | 'dry_run' | 'skipped' | 'error'
  reason: string
  order?: (Record<string, unknown> & { orders?: ExecutionLeg[] }) | null
  at: number
}

export interface RoomSession {
  session_id: string
  symbol: string
  timeframe: string
  trigger: string
  started_at: number
  finished_at?: number
  status: 'running' | 'complete'
  decisions: Array<{ role: string; agent_name: string; skill_used?: string; skill_asset_class?: string } & AgentDecisionSummary>
  final_action?: string
  final_confidence?: number
  final_reasoning?: string
  hermes_skill?: { symbol: string; asset_class: string; group?: string; linked_agents?: string[]; jarvis?: { role: string; human_name: string }; is_best_trader?: boolean; win_rate?: number | null; decisions_reviewed?: number; playbook_preview?: string } | null
  hermes_best_trader?: { symbol: string; asset_class: string } | null
  consensus?: {
    tally: { buy: number; sell: number; hold: number }
    leader: string
    agreement: number
    weighted_confidence: number
  }
}

/** One line of the live debate — an agent presenting its verdict out loud. */
export interface DebateTurn {
  role: string
  agent_name?: string
  human_name?: string
  title?: string
  color?: string
  seat?: number
  gender?: 'male' | 'female'
  session_id: string
  symbol?: string
  action?: string
  confidence?: number
  /** The spoken reasoning. */
  text: string
  /** Local receipt time (epoch seconds) — drives bubble TTL + ordering. */
  at: number
  /** True when JARVIS read the board's verdict to close the meeting. */
  chair?: boolean
}

const MAX_DEBATE_TURNS = 80

const MAX_SESSIONS = 40

export function useTradingRoom() {
  const [seats, setSeats] = useState<RoomSeat[]>([])
  const [sessions, setSessions] = useState<RoomSession[]>([])
  const [focusSymbol, setFocusSymbol] = useState<string | null>(null)
  const [focusSymbols, setFocusSymbols] = useState<string[]>([])
  const [ceo, setCeo] = useState<{ human_name: string; title: string; color: string } | null>(null)
  const [loaded, setLoaded] = useState(false)
  // Latest completed session, consumed by the notification/voice layer.
  const [lastCompleted, setLastCompleted] = useState<RoomSession | null>(null)
  const [executions, setExecutions] = useState<ExecutionReport[]>([])
  // Live debate — every speaking turn in arrival order (newest last).
  const [debate, setDebate] = useState<DebateTurn[]>([])

  const hydrate = useCallback(async () => {
    try {
      const { data } = await api.get('/agents/room/state')
      setSeats(data.seats ?? [])
      setSessions(data.sessions ?? [])
      setFocusSymbol(data.focus_symbol ?? null)
      setFocusSymbols(data.focus_symbols ?? (data.focus_symbol ? [data.focus_symbol] : []))
      setCeo(data.ceo ?? null)
    } catch {
      /* backend unreachable — SSE events will fill the room once it recovers */
    } finally {
      setLoaded(true)
    }
  }, [])

  useEffect(() => { void hydrate() }, [hydrate])

  // ── Live updates ──────────────────────────────────────────────────────────
  useEffect(() => {
    const patchSeat = (role: string, patch: Partial<RoomSeat>) => {
      setSeats((prev) => {
        const idx = prev.findIndex((s) => s.role === role)
        if (idx === -1) return prev
        const next = [...prev]
        next[idx] = { ...next[idx], ...patch }
        return next
      })
    }

    const unsubs = [
      eventStream.subscribe('agent.started', (raw) => {
        const e = raw as RoomSeat & { session_id: string }
        patchSeat(e.role, {
          state: 'analyzing',
          symbol: e.symbol,
          session_id: e.session_id,
          phase: e.phase,
          error: undefined,
        })
      }),

      eventStream.subscribe('agent.completed', (raw) => {
        const e = raw as RoomSeat & { session_id: string }
        patchSeat(e.role, {
          state: 'presenting',
          symbol: e.symbol,
          session_id: e.session_id,
          last_decision: e.last_decision,
        })
        if (!e.last_decision) return
        setSessions((prev) =>
          prev.map((s) =>
            s.session_id === e.session_id
              ? {
                  ...s,
                  decisions: [
                    ...s.decisions,
                    { role: e.role, agent_name: e.agent_name ?? e.role, ...e.last_decision! },
                  ],
                }
              : s,
          ),
        )
      }),

      eventStream.subscribe('agent.state', (raw) => {
        const e = raw as RoomSeat
        patchSeat(e.role, { state: e.state, error: e.error })
      }),

      eventStream.subscribe('session.started', (raw) => {
        const s = raw as RoomSession
        setSessions((prev) => [s, ...prev].slice(0, MAX_SESSIONS))
      }),

      eventStream.subscribe('session.completed', (raw) => {
        const e = raw as Partial<RoomSession> & { session_id: string }
        setSessions((prev) => {
          const next = prev.map((s) =>
            s.session_id === e.session_id
              ? { ...s, ...e, status: 'complete' as const, finished_at: Date.now() / 1000 }
              : s,
          )
          setLastCompleted(next.find((s) => s.session_id === e.session_id) ?? null)
          return next
        })
        setSeats((prev) =>
          prev.map((s) => (s.session_id === e.session_id ? { ...s, state: 'resting' } : s)),
        )
      }),

      eventStream.subscribe('room.focus', (raw) => {
        const e = raw as { symbol: string | null; symbols?: string[] }
        const list = e.symbols ?? (e.symbol ? [e.symbol] : [])
        setFocusSymbols(list)
        setFocusSymbol(list[0] ?? null)
      }),

      eventStream.subscribe('room.execution', (raw) => {
        const report = raw as ExecutionReport
        // A skip is routine (most sessions end in hold) — only surface the ones
        // where the room actually did something.
        if (report.status === 'skipped') return
        setExecutions((prev) => [report, ...prev].slice(0, 25))
      }),

      eventStream.subscribe('agent.speaking', (raw) => {
        const e = raw as Partial<DebateTurn> & { role: string; text: string; session_id: string }
        if (!e.text) return
        const turn: DebateTurn = {
          role: e.role,
          agent_name: e.agent_name,
          human_name: e.human_name ?? e.agent_name ?? e.role,
          title: e.title,
          color: e.color ?? '#94a3b8',
          session_id: e.session_id,
          symbol: e.symbol,
          action: e.action,
          confidence: e.confidence,
          text: e.text,
          // Stamped on receipt so the bubble TTL runs on browser time, not the
          // server clock — a skewed clock would pop bubbles instantly or never.
          at: Date.now() / 1000,
          chair: Boolean(e.chair),
        }
        setDebate((prev) => [...prev, turn].slice(-MAX_DEBATE_TURNS))
      }),
    ]

    return () => unsubs.forEach((u) => u())
  }, [])

  const applyFocus = useCallback(async (symbols: string[]) => {
    setFocusSymbols(symbols)
    setFocusSymbol(symbols[0] ?? null)
    try {
      await api.post('/agents/room/focus', { symbols })
    } catch {
      /* the SSE echo is authoritative; a failed post just leaves the old focus */
    }
  }, [])

  // Add/remove a pair from the focus set, or clear it entirely.
  const toggleFocus = useCallback((symbol: string | null) => {
    if (!symbol) { void applyFocus([]); return }
    const up = symbol.toUpperCase()
    const exists = focusSymbols.some((s) => s.toUpperCase() === up)
    const next = exists ? focusSymbols.filter((s) => s.toUpperCase() !== up) : [...focusSymbols, up]
    void applyFocus(next)
  }, [applyFocus, focusSymbols])

  const clearFocus = useCallback(() => { void applyFocus([]) }, [applyFocus])

  // Back-compat single-pair setter (replaces the whole focus set).
  const setFocus = useCallback((symbol: string | null) => {
    void applyFocus(symbol ? [symbol.toUpperCase()] : [])
  }, [applyFocus])

  // ── Always-on worker ──────────────────────────────────────────────────────
  const [workerRunning, setWorkerRunning] = useState(false)

  useEffect(() => {
    api.get('/agents/room/worker')
      .then(({ data }) => setWorkerRunning(Boolean(data.running)))
      .catch(() => {})
  }, [])

  const toggleWorker = useCallback(async () => {
    const next = !workerRunning
    setWorkerRunning(next)
    try {
      const { data } = await api.post(`/agents/room/worker/${next ? 'start' : 'stop'}`)
      setWorkerRunning(Boolean(data.running))
    } catch {
      setWorkerRunning(!next)
    }
  }, [workerRunning])

  return {
    seats,
    sessions,
    focusSymbol,
    focusSymbols,
    ceo,
    loaded,
    lastCompleted,
    executions,
    debate,
    workerRunning,
    setFocus,
    toggleFocus,
    clearFocus,
    toggleWorker,
    refresh: hydrate,
  }
}
