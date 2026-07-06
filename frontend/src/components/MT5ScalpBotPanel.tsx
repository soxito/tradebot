/**
 * MT5ScalpBotPanel — Autonomous scalp-bot control surface for /mt5-live.
 *
 * Per-account: rendered with key={accountId} in mt5-live.tsx so each account
 * gets its own independent state, poll loop, and session binding.
 *
 * Settings editable while active: lot size, loss limit, targets, and toggles
 * can all be changed on a running session via the "Apply" button (takes effect
 * on the next ~10s cycle — no restart required).
 *
 * Settings sync: when switching back to an account that already has an active
 * session the inputs are pre-filled with the session's current live settings.
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { apiClient } from '@/services/api'
import {
  Zap, Square, Search, Loader2, TrendingUp, TrendingDown, Activity,
  ShieldCheck, Brain, Sparkles, ChevronDown, ChevronUp, AlertTriangle, Check,
} from 'lucide-react'

interface ScalpTradeInfo {
  ticket: number | null
  side: string
  lot: number
  entry_price: number
  sl: number | null
  tp: number | null
  pnl: number
  is_recovery: boolean
  status: string
  confidence: number
  opened_at: string | null
}

interface ScalpStatus {
  session_id: number
  account_id: number
  symbol: string
  status: string
  phase: string
  lot_size: number
  auto_lot: boolean
  risk_per_trade_pct: number
  max_daily_loss_pct: number
  target_profit_pct: number
  recovery_enabled: boolean
  use_ai: boolean
  use_kronos: boolean
  timeframe: string
  strictness: string
  max_open_orders: number
  allowed_direction: string
  bias_direction: string | null
  bias_confidence: number
  session_pnl: number
  total_trades: number
  wins: number
  losses: number
  combined_pnl: number
  open_trades: ScalpTradeInfo[]
  last_cycle_at: string | null
  ai_note: string | null
  error_msg: string | null
  started_at: string | null
}

interface ScalpTradeRow {
  id: number
  symbol: string
  side: string
  lot: number
  entry_price: number
  close_price: number | null
  pnl: number
  is_recovery: boolean
  status: string
  confidence: number
  reason: string | null
  opened_at: string | null
  closed_at: string | null
}

interface Props {
  accountId: number
  serverSymbolDefault?: string
  /** Current chart symbol — when this changes the panel syncs (if not active). */
  chartSymbol?: string
  /** Fired when user picks a symbol so the chart can sync. */
  onSymbolChange?: (symbol: string) => void
  /** Account type — used to warn before launching on live accounts. */
  accountType?: string
}

const PHASE_LABEL: Record<string, string> = {
  analyzing: 'ANALYZING',
  waiting: 'WAITING',
  in_trade: 'IN TRADE',
  entry_pending: 'ENTRY PENDING',
  recovery: 'RECOVERY',
  stopped: 'STOPPED',
  paused: 'PAUSED',
  error: 'ERROR',
}

const PHASE_COLOR: Record<string, string> = {
  analyzing: 'text-blue-400 bg-blue-500/15',
  waiting: 'text-amber-400 bg-amber-500/15',
  in_trade: 'text-green-400 bg-green-500/15',
  entry_pending: 'text-cyan-400 bg-cyan-500/15',
  recovery: 'text-orange-400 bg-orange-500/15',
  stopped: 'text-gray-400 bg-gray-500/15',
  paused: 'text-yellow-400 bg-yellow-500/15',
  error: 'text-red-400 bg-red-500/15',
}

function pnlColor(v: number): string {
  if (v > 0) return 'text-green-400'
  if (v < 0) return 'text-red-400'
  return 'text-gray-300'
}

// ── All supported forex and commodity pairs for scalping ───────────────────
const PAIR_GROUPS = {
  '🥇 Commodities':  ['XAUUSD', 'XAGUSD', 'USOIL', 'UKOIL'],
  '💱 Forex Majors': ['EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD'],
  '🔀 Forex Minors': [
    'EURGBP', 'EURJPY', 'EURCAD', 'EURCHF', 'EURAUD', 'EURNZD',
    'GBPJPY', 'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPNZD',
    'AUDJPY', 'AUDCAD', 'AUDCHF', 'AUDNZD', 'CADJPY', 'CHFJPY', 'NZDJPY',
  ],
  '₿ Crypto':        ['BTCUSD', 'ETHUSD'],
}
const ALL_ACTIVE_PAIRS = Object.values(PAIR_GROUPS).flat()

// ── Scalp bot recommended defaults ─────────────────────────────────────────
// M5 is empirically the best timeframe for scalping: enough structure for
// clean entry signals while still being fast enough to capture intraday moves.
const SCALP_DEFAULTS = {
  lotSize:          0.01  as number,
  autoLot:          false as boolean,
  maxDailyLoss:     3     as number,
  targetProfit:     1.5   as number,
  recovery:         true  as boolean,
  useAi:            true  as boolean,
  useKronos:        true  as boolean,
  maxOpenOrders:    2     as number,
  scalpTf:          'M5'  as 'M1' | 'M5' | 'M15' | 'M30' | 'H1',
  allowedDirection: 'both' as 'buy' | 'sell' | 'both',
}

export default function MT5ScalpBotPanel({ accountId, serverSymbolDefault, chartSymbol, onSymbolChange, accountType }: Props) {
  const [symbol, setSymbol] = useState(chartSymbol || serverSymbolDefault || 'XAUUSD')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<{ symbol: string; description: string | null }[]>([])
  const [searching, setSearching] = useState(false)
  const [showResults, setShowResults] = useState(false)

  // Settings
  const [lotSize, setLotSize] = useState(SCALP_DEFAULTS.lotSize)
  const [autoLot, setAutoLot] = useState(SCALP_DEFAULTS.autoLot)
  const [maxDailyLoss, setMaxDailyLoss] = useState(SCALP_DEFAULTS.maxDailyLoss)
  const [targetProfit, setTargetProfit] = useState(SCALP_DEFAULTS.targetProfit)
  const [recovery, setRecovery] = useState(SCALP_DEFAULTS.recovery)
  const [useAi, setUseAi] = useState(SCALP_DEFAULTS.useAi)
  const [useKronos, setUseKronos] = useState(SCALP_DEFAULTS.useKronos)
  const [maxOpenOrders, setMaxOpenOrders] = useState(SCALP_DEFAULTS.maxOpenOrders)
  const [allowedDirection, setAllowedDirection] = useState<'buy' | 'sell' | 'both'>(SCALP_DEFAULTS.allowedDirection)
  const [scalpTf, setScalpTf] = useState<'M1' | 'M5' | 'M15' | 'M30' | 'H1'>(SCALP_DEFAULTS.scalpTf)
  const [showSettings, setShowSettings] = useState(false)

  const [session, setSession] = useState<ScalpStatus | null>(null)
  const [allSessions, setAllSessions] = useState<ScalpStatus[]>([])   // ALL active sessions for this account
  const [trades, setTrades] = useState<ScalpTradeRow[]>([])
  const [busy, setBusy] = useState(false)
  const [busySymbol, setBusySymbol] = useState<string | null>(null)   // which symbol is being started/stopped
  const [applying, setApplying] = useState(false)
  const [applySuccess, setApplySuccess] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [launchingPairs, setLaunchingPairs] = useState(false)   // launching pair bundle
  const [showPairPicker, setShowPairPicker] = useState(false)   // pair-picker panel open
  const [pairSearchQuery, setPairSearchQuery] = useState('')    // filter inside pair picker
  const [selectedPairs, setSelectedPairs] = useState<Set<string>>(  // user-selected pairs
    () => new Set(['XAUUSD', 'GBPUSD', 'EURUSD'])               // default 3 popular pairs
  )

  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Tracks which session id we last synced settings from so we don't
  // overwrite user edits on every 5s poll, only on first load per session.
  const syncedSessionId = useRef<number | null>(null)

  // ── Sync symbol from chart (when not running a live session) ──────────────
  useEffect(() => {
    if (!active && chartSymbol && chartSymbol !== symbol) {
      setSymbol(chartSymbol.toUpperCase())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartSymbol])

  // ── Poll live status for this account ────────────────────────────────────
  const refreshStatus = useCallback(async () => {
    try {
      const res = await apiClient.mt5.scalp.status(accountId)
      const list: ScalpStatus[] = res.data || []
      // Store ALL sessions for the multi-pair dashboard.
      setAllSessions(list)
      const match = list.find(s => s.symbol.toUpperCase() === symbol.toUpperCase()) || null
      setSession(prev => {
        if (!match) syncedSessionId.current = null
        return match
      })
      if (match) {
        const tr = await apiClient.mt5.scalp.trades(match.session_id)
        setTrades(tr.data || [])
      } else {
        setTrades([])
      }
    } catch {
      /* transient — keep last known state */
    }
  }, [accountId, symbol])

  useEffect(() => {
    refreshStatus()
    const id = setInterval(refreshStatus, 5000)
    return () => clearInterval(id)
  }, [refreshStatus])

  // ── Sync settings from live session (once per session load) ──────────────
  // When you switch to an account that already has a running session, the
  // inputs are pre-filled with the session's current settings.
  useEffect(() => {
    if (session && session.session_id !== syncedSessionId.current) {
      syncedSessionId.current = session.session_id
      setLotSize(session.lot_size)
      setAutoLot(session.auto_lot ?? false)
      setMaxDailyLoss(session.max_daily_loss_pct ?? 3)
      setTargetProfit(session.target_profit_pct ?? 1.5)
      setRecovery(session.recovery_enabled)
      setUseAi(session.use_ai)
      setUseKronos(session.use_kronos)
      setMaxOpenOrders(session.max_open_orders ?? 2)
      setAllowedDirection((session.allowed_direction as any) || 'both')
      setScalpTf((session.timeframe as any) || 'M5')
      setSymbol(session.symbol)
      // also tell chart to sync
      if (session.symbol && session.symbol !== symbol) {
        onSymbolChange?.(session.symbol)
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session])

  // ── Symbol search (debounced) ─────────────────────────────────────────────
  useEffect(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current)
    if (!query.trim()) { setResults([]); return }
    searchTimer.current = setTimeout(async () => {
      setSearching(true)
      try {
        const res = await apiClient.mt5.scalp.searchSymbols(accountId, query.trim())
        setResults(res.data || [])
        setShowResults(true)
      } catch {
        setResults([])
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => { if (searchTimer.current) clearTimeout(searchTimer.current) }
  }, [query, accountId])

  const active = session && (session.status === 'active' || session.status === 'paused')

  const handleActivate = async () => {
    if (!symbol.trim()) return
    setBusy(true); setBusySymbol(symbol); setError(null)
    try {
      await apiClient.mt5.scalp.start({
        account_id: accountId,
        symbol: symbol.trim().toUpperCase(),
        lot_size: lotSize,
        auto_lot: autoLot,
        max_daily_loss_pct: maxDailyLoss,
        target_profit_pct: targetProfit,
        recovery_enabled: recovery,
        use_ai: useAi,
        use_kronos: useKronos,
        timeframe: scalpTf,
        max_open_orders: maxOpenOrders,
        strictness: 'scalper',
        allowed_direction: allowedDirection,
      })
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to start scalp bot')
    } finally {
      setBusy(false); setBusySymbol(null)
    }
  }

  const handleStop = async () => {
    setBusy(true); setBusySymbol(symbol); setError(null)
    try {
      await apiClient.mt5.scalp.stop(accountId, symbol.trim().toUpperCase())
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to stop scalp bot')
    } finally {
      setBusy(false); setBusySymbol(null)
    }
  }

  // Stop a specific pair from the multi-session dashboard
  const handleStopSymbol = async (sym: string) => {
    setBusySymbol(sym); setError(null)
    try {
      await apiClient.mt5.scalp.stop(accountId, sym)
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || `Failed to stop ${sym}`)
    } finally {
      setBusySymbol(null)
    }
  }

  // Launch only the user-selected pairs
  const handleLaunchSelected = async () => {
    const activePairs = allSessions.filter(s => s.status === 'active').map(s => s.symbol)
    const pairsToLaunch = [...selectedPairs].filter(p => !activePairs.includes(p))

    if (pairsToLaunch.length === 0) {
      setError('All selected pairs are already running — choose different pairs')
      return
    }

    // Confirm on live/prop accounts
    if (accountType === 'live' || accountType === 'prop') {
      const confirmed = window.confirm(
        `⚠️ LIVE ACCOUNT WARNING\n\nStart ${pairsToLaunch.length} scalp bot(s) on a LIVE account:\n${pairsToLaunch.join(', ')}\n\nAccount: ${accountType.toUpperCase()}\n\nContinue?`
      )
      if (!confirmed) return
    }

    setLaunchingPairs(true); setError(null)
    const payload = {
      account_id: accountId,
      lot_size: lotSize,
      auto_lot: autoLot,
      max_daily_loss_pct: maxDailyLoss,
      target_profit_pct: targetProfit,
      recovery_enabled: recovery,
      use_ai: useAi,
      use_kronos: useKronos,
      timeframe: scalpTf,
      max_open_orders: maxOpenOrders,
      strictness: 'scalper' as const,
      allowed_direction: allowedDirection,
    }
    try {
      await Promise.allSettled(
        pairsToLaunch.map(sym => apiClient.mt5.scalp.start({ ...payload, symbol: sym }))
      )
      // Switch chart to first selected pair
      const first = pairsToLaunch[0]
      setSymbol(first); onSymbolChange?.(first)
      setShowPairPicker(false)
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to launch pairs')
    } finally {
      setLaunchingPairs(false)
    }
  }

  // Legacy alias kept for backward-compat
  const handleLaunchAllPairs = handleLaunchSelected
  const handleLaunch3Pairs  = handleLaunchSelected

  const handleCloseAll = async () => {
    setBusy(true); setError(null)
    try {
      await apiClient.mt5.scalp.closeAll(accountId)
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to close all')
    } finally {
      setBusy(false)
    }
  }

  // Push updated settings to the running session (takes effect next cycle ~10s)
  const handleApply = async () => {
    if (!session) return
    setApplying(true); setError(null)
    try {
      await (apiClient.mt5.scalp as any).update(session.session_id, {
        lot_size: lotSize,
        auto_lot: autoLot,
        max_daily_loss_pct: maxDailyLoss,
        target_profit_pct: targetProfit,
        recovery_enabled: recovery,
        use_ai: useAi,
        use_kronos: useKronos,
        max_open_orders: maxOpenOrders,
        allowed_direction: allowedDirection,
        // timeframe update requires session restart; only include when changed
      })
      setApplySuccess(true)
      setTimeout(() => setApplySuccess(false), 2000)
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to apply settings')
    } finally {
      setApplying(false)
    }
  }

  const handleReset = () => {
    setLotSize(SCALP_DEFAULTS.lotSize)
    setAutoLot(SCALP_DEFAULTS.autoLot)
    setMaxDailyLoss(SCALP_DEFAULTS.maxDailyLoss)
    setTargetProfit(SCALP_DEFAULTS.targetProfit)
    setRecovery(SCALP_DEFAULTS.recovery)
    setUseAi(SCALP_DEFAULTS.useAi)
    setUseKronos(SCALP_DEFAULTS.useKronos)
    setMaxOpenOrders(SCALP_DEFAULTS.maxOpenOrders)
    setAllowedDirection(SCALP_DEFAULTS.allowedDirection)
    setScalpTf(SCALP_DEFAULTS.scalpTf)
  }

  const phase = session?.phase || 'analyzing'
  const combined = session?.combined_pnl ?? 0

  return (
    <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-700/40 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <Zap className="w-4 h-4 text-tradebot-accent" />
          Scalp Bot
          {active && (
            <span className={`ml-1 px-2 py-0.5 rounded text-[10px] font-bold ${PHASE_COLOR[phase] || PHASE_COLOR.analyzing}`}>
              {PHASE_LABEL[phase] || phase.toUpperCase()}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-[11px] text-gray-400">
          <span>All TFs · {session ? session.timeframe : scalpTf} trigger</span>
        </div>
      </div>

      <div className="p-4 space-y-4">

        {/* ── Multi-Pair Dashboard: shows ALL running sessions ── */}
        {allSessions.length > 0 && (
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/40 overflow-hidden">
            <div className="px-3 py-2 border-b border-gray-700/40 flex items-center justify-between">
              <span className="text-[11px] font-semibold text-gray-300 uppercase tracking-wide">
                Active Pairs ({allSessions.length})
              </span>
              <div className="flex items-center gap-3">
                <span className="text-[11px] text-gray-500">
                  Total P&amp;L:{' '}
                  <span className={`font-bold ${
                    allSessions.reduce((s, x) => s + x.combined_pnl, 0) >= 0
                      ? 'text-green-400' : 'text-red-400'
                  }`}>
                    {allSessions.reduce((s, x) => s + x.combined_pnl, 0) >= 0 ? '+' : ''}
                    {allSessions.reduce((s, x) => s + x.combined_pnl, 0).toFixed(2)}
                  </span>
                </span>
                <button
                  onClick={handleCloseAll}
                  disabled={busy || launchingPairs}
                  className="text-[10px] text-red-300 hover:text-red-200 px-2 py-0.5 rounded border border-red-500/30 hover:border-red-500/60 transition disabled:opacity-40"
                >
                  Stop All
                </button>
              </div>
            </div>
            <div className="divide-y divide-gray-800/60">
              {allSessions.map(s => {
                const ph = s.phase || 'analyzing'
                const isBusy = busySymbol === s.symbol
                const isCurrentPair = s.symbol.toUpperCase() === symbol.toUpperCase()
                const dirLabel = s.allowed_direction && s.allowed_direction !== 'both'
                  ? s.allowed_direction.toUpperCase() : null
                return (
                  <div
                    key={s.session_id}
                    className={`flex items-center gap-2 px-3 py-2 text-xs cursor-pointer hover:bg-gray-800/30 transition ${isCurrentPair ? 'bg-tradebot-accent/5' : ''}`}
                    onClick={() => { setSymbol(s.symbol); setQuery(''); onSymbolChange?.(s.symbol) }}
                  >
                    <span className={`font-semibold w-16 shrink-0 ${isCurrentPair ? 'text-tradebot-accent' : 'text-white'}`}>{s.symbol}</span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold shrink-0 ${PHASE_COLOR[ph] || PHASE_COLOR.analyzing}`}>{PHASE_LABEL[ph] || ph.toUpperCase()}</span>
                    {dirLabel && (
                      <span className={`px-1 py-0.5 rounded text-[9px] font-bold shrink-0 ${dirLabel === 'BUY' ? 'bg-green-900/40 text-green-400' : 'bg-red-900/40 text-red-400'}`}>{dirLabel} ONLY</span>
                    )}
                    <span className={`shrink-0 ${s.bias_direction === 'buy' ? 'text-green-400' : s.bias_direction === 'sell' ? 'text-red-400' : 'text-gray-500'}`}>
                      {s.bias_direction === 'buy' ? '▲' : s.bias_direction === 'sell' ? '▼' : '—'}
                    </span>
                    <span className={`ml-auto font-bold tabular-nums ${pnlColor(s.combined_pnl)}`}>
                      {s.combined_pnl >= 0 ? '+' : ''}{s.combined_pnl.toFixed(2)}
                    </span>
                    {s.open_trades.length > 0 && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${s.open_trades[0].side === 'buy' ? 'bg-blue-900/40 text-blue-300' : 'bg-orange-900/40 text-orange-300'}`}>
                        {s.open_trades[0].side.toUpperCase()}
                      </span>
                    )}
                    <button
                      onClick={e => { e.stopPropagation(); handleStopSymbol(s.symbol) }}
                      disabled={isBusy}
                      className="shrink-0 text-gray-600 hover:text-red-400 transition disabled:opacity-40"
                      title={`Stop ${s.symbol}`}
                    >
                      {isBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Square className="w-3 h-3" />}
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* ── Pair Picker — select which pairs to scalp ── */}
        {(() => {
          const activePairs = new Set(allSessions.filter(s => s.status === 'active').map(s => s.symbol))
          const readyToLaunch = [...selectedPairs].filter(p => !activePairs.has(p)).length
          const pairFilter = pairSearchQuery.trim().toUpperCase()

          return (
            <div className="rounded-xl border border-gray-700/50 overflow-hidden">
              {/* Header row — always visible */}
              <button
                type="button"
                onClick={() => setShowPairPicker(v => !v)}
                className="w-full flex items-center justify-between px-3 py-2.5 bg-gray-900/50 hover:bg-gray-900/70 transition text-sm"
              >
                <div className="flex items-center gap-2">
                  <Zap className="w-4 h-4 text-tradebot-accent" />
                  <span className="font-semibold text-white">Choose Pairs to Scalp</span>
                  {selectedPairs.size > 0 && (
                    <span className="px-2 py-0.5 rounded-full bg-tradebot-accent/20 text-tradebot-accent text-[10px] font-bold">
                      {selectedPairs.size} selected
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {readyToLaunch > 0 && (
                    <span className="text-[10px] text-gray-400">{readyToLaunch} ready to launch</span>
                  )}
                  {showPairPicker ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
                </div>
              </button>

              {/* Expandable picker body */}
              {showPairPicker && (
                <div className="border-t border-gray-700/50 bg-gray-950/60">
                  {/* Search + Select all / Clear row */}
                  <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-800/60">
                    <div className="relative flex-1">
                      <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                      <input
                        type="text"
                        value={pairSearchQuery}
                        onChange={e => setPairSearchQuery(e.target.value)}
                        placeholder="Filter pairs…"
                        className="w-full pl-7 pr-2 py-1 rounded bg-gray-900 border border-gray-700 text-xs text-white placeholder-gray-500 focus:border-tradebot-accent/50 outline-none"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        const available = ALL_ACTIVE_PAIRS.filter(p => !activePairs.has(p) && (!pairFilter || p.includes(pairFilter)))
                        setSelectedPairs(prev => new Set([...prev, ...available]))
                      }}
                      className="text-[10px] px-2 py-1 rounded border border-gray-700 text-gray-300 hover:border-tradebot-accent/50 hover:text-tradebot-accent transition whitespace-nowrap"
                    >Select all</button>
                    <button
                      type="button"
                      onClick={() => setSelectedPairs(new Set())}
                      className="text-[10px] px-2 py-1 rounded border border-gray-700 text-gray-300 hover:border-red-500/40 hover:text-red-400 transition"
                    >Clear</button>
                  </div>

                  {/* Pair groups */}
                  <div className="max-h-64 overflow-y-auto px-3 py-2 space-y-3">
                    {Object.entries(PAIR_GROUPS).map(([group, pairs]) => {
                      const visible = pairs.filter(p =>
                        !pairFilter || p.includes(pairFilter) || group.toUpperCase().includes(pairFilter)
                      )
                      if (visible.length === 0) return null
                      return (
                        <div key={group}>
                          <div className="flex items-center gap-2 mb-1.5">
                            <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wide">{group}</span>
                            <div className="flex-1 h-px bg-gray-800/80" />
                            <button
                              type="button"
                              onClick={() => {
                                const allSelected = visible.every(p => selectedPairs.has(p))
                                setSelectedPairs(prev => {
                                  const next = new Set(prev)
                                  visible.forEach(p => allSelected ? next.delete(p) : next.add(p))
                                  return next
                                })
                              }}
                              className="text-[9px] text-gray-500 hover:text-gray-300 transition"
                            >
                              {visible.every(p => selectedPairs.has(p)) ? 'deselect' : 'select all'}
                            </button>
                          </div>
                          <div className="flex flex-wrap gap-1.5">
                            {visible.map(p => {
                              const isActive = activePairs.has(p)
                              const isSelected = selectedPairs.has(p)
                              return (
                                <button
                                  key={p}
                                  type="button"
                                  disabled={isActive}
                                  onClick={() => setSelectedPairs(prev => {
                                    const next = new Set(prev)
                                    isSelected ? next.delete(p) : next.add(p)
                                    return next
                                  })}
                                  className={`flex items-center gap-1 px-2 py-1 rounded text-[11px] font-semibold border transition ${
                                    isActive
                                      ? 'bg-green-900/20 border-green-700/40 text-green-400 cursor-default opacity-70'
                                      : isSelected
                                      ? 'bg-tradebot-accent/20 border-tradebot-accent/60 text-tradebot-accent'
                                      : 'bg-gray-900/60 border-gray-700/50 text-gray-300 hover:border-gray-500 hover:text-white'
                                  }`}
                                  title={isActive ? `${p} is already running` : isSelected ? `Deselect ${p}` : `Select ${p}`}
                                >
                                  {isActive ? (
                                    <span className="w-2 h-2 rounded-full bg-green-500 shrink-0" />
                                  ) : isSelected ? (
                                    <Check className="w-2.5 h-2.5 shrink-0" />
                                  ) : null}
                                  {p}
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      )
                    })}
                  </div>

                  {/* Launch selected footer */}
                  <div className="px-3 py-2 border-t border-gray-800/60 flex items-center justify-between gap-2">
                    <span className="text-[11px] text-gray-500">
                      {readyToLaunch > 0
                        ? `${readyToLaunch} pair${readyToLaunch === 1 ? '' : 's'} will start`
                        : 'No new pairs selected'}
                    </span>
                    <button
                      type="button"
                      onClick={handleLaunchSelected}
                      disabled={launchingPairs || busy || readyToLaunch === 0}
                      className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-tradebot-accent/20 border border-tradebot-accent/50 text-tradebot-accent text-xs font-bold hover:bg-tradebot-accent/30 disabled:opacity-40 transition"
                    >
                      {launchingPairs
                        ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Launching…</>
                        : <><Zap className="w-3.5 h-3.5" /> Launch {readyToLaunch > 0 ? readyToLaunch : ''} Selected</>
                      }
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        })()}

        {/* ── Symbol search + activate ── */}
        <div className="flex flex-col sm:flex-row gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
            <input
              type="text"
              value={query || symbol}
              onChange={e => { setSymbol(e.target.value.toUpperCase()); setQuery(e.target.value) }}
              onFocus={() => { setQuery(''); setShowResults(true) }}
              placeholder="Search pair (XAUUSD, EURUSD, US30, BTCUSD…)"
              disabled={!!active}
              className="w-full pl-9 pr-8 py-2 rounded-lg bg-gray-900/60 border border-gray-700/60 text-sm text-white placeholder-gray-500 focus:border-tradebot-accent/50 outline-none disabled:opacity-60"
            />
            {searching && <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500 animate-spin" />}
            {showResults && results.length > 0 && !active && (
              <div className="absolute z-20 mt-1 w-full max-h-56 overflow-y-auto rounded-lg bg-gray-900 border border-gray-700 shadow-xl">
                {results.map(r => (
                  <button
                    key={r.symbol}
                    onClick={() => {
                      setSymbol(r.symbol)
                      setQuery('')
                      setShowResults(false)
                      // sync to chart
                      onSymbolChange?.(r.symbol)
                    }}
                    className="w-full text-left px-3 py-2 text-sm text-gray-200 hover:bg-gray-800 flex items-center justify-between"
                  >
                    <span className="font-medium">{r.symbol}</span>
                    {r.description && <span className="text-xs text-gray-500 truncate ml-2">{r.description}</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
          {active ? (
            <button
              onClick={handleStop}
              disabled={busy}
              className="flex items-center justify-center gap-2 px-5 py-2 rounded-lg bg-red-500/20 text-red-300 hover:bg-red-500/30 font-semibold text-sm disabled:opacity-50 transition"
            >
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
              Stop
            </button>
          ) : (
            <button
              onClick={handleActivate}
              disabled={busy || !symbol.trim()}
              className="flex items-center justify-center gap-2 px-5 py-2 rounded-lg bg-green-500/25 text-green-300 hover:bg-green-500/35 font-semibold text-sm disabled:opacity-50 transition"
            >
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
              Activate {symbol && !active ? symbol : ''}
            </button>
          )}
        </div>

        {/* Always-visible Lot size row */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400 shrink-0">Lot:</span>
          <button
            onClick={() => setLotSize(v => Math.max(0.01, +(v - 0.01).toFixed(2)))}
            disabled={autoLot}
            className="px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300 text-xs disabled:opacity-40 hover:bg-gray-700"
          >−</button>
          <input
            type="number" step="0.01" min="0.01"
            value={lotSize}
            onChange={e => setLotSize(Math.max(0.01, +e.target.value || 0.01))}
            disabled={autoLot}
            className="w-20 text-center py-1 rounded bg-gray-900 border border-gray-700 text-white text-sm disabled:opacity-40"
          />
          <button
            onClick={() => setLotSize(v => +(v + 0.01).toFixed(2))}
            disabled={autoLot}
            className="px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300 text-xs disabled:opacity-40 hover:bg-gray-700"
          >+</button>
          <label className="flex items-center gap-1.5 text-[11px] text-gray-400 cursor-pointer ml-1">
            <input type="checkbox" checked={autoLot} onChange={e => setAutoLot(e.target.checked)} />
            Auto
          </label>
          {active && (
            <button
              onClick={handleApply}
              disabled={applying}
              className={`ml-auto flex items-center gap-1 px-2.5 py-1 rounded text-[11px] font-semibold transition ${
                applySuccess ? 'bg-green-500/20 text-green-300' : 'bg-tradebot-accent/20 text-tradebot-accent hover:bg-tradebot-accent/30'
              } disabled:opacity-50`}
            >
              {applying ? <Loader2 className="w-3 h-3 animate-spin" /> : applySuccess ? <Check className="w-3 h-3" /> : <Zap className="w-3 h-3" />}
              {applySuccess ? 'Saved!' : 'Apply'}
            </button>
          )}
        </div>

        {error && (
          <div className="flex items-start gap-2 text-xs text-red-300 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {/* ── Restart-pause warning — session was PAUSED after backend restart ── */}
        {session && session.phase === 'paused' && session.ai_note?.includes('backend restart') && (
          <div className="flex items-start gap-2 text-xs text-yellow-300 bg-yellow-500/10 border border-yellow-500/25 rounded-lg px-3 py-2.5">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5 text-yellow-400" />
            <div className="flex-1">
              <div className="font-semibold text-yellow-300 mb-0.5">Bot paused after backend restart</div>
              <div className="text-yellow-400/80">{session.ai_note}</div>
              <div className="mt-1.5 text-yellow-500/70">Click <strong className="text-yellow-300">Activate</strong> to resume trading on this session.</div>
            </div>
          </div>
        )}

        {/* ── Settings (collapsible, always editable even when active) ── */}
        <div>
          <div className="flex items-center justify-between">
            <button
              onClick={() => setShowSettings(s => !s)}
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition"
            >
              {showSettings ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
              Settings
              {active && <span className="ml-1 text-[10px] text-tradebot-accent/70">(editable while running)</span>}
            </button>
            <div className="flex items-center gap-2">
              {/* Reset to defaults — always available when settings panel is open */}
              {showSettings && !active && (
                <button
                  onClick={handleReset}
                  title={`Reset to defaults (M5 · 0.01 lot · 3% loss limit · 1.5% target)`}
                  className="flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] text-gray-400 border border-gray-700 hover:border-gray-500 hover:text-white transition"
                >
                  ↺ Defaults
                </button>
              )}
              {/* Apply button — visible when session is active and settings panel is open */}
              {active && showSettings && (
              <button
                onClick={handleApply}
                disabled={applying}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition ${
                  applySuccess
                    ? 'bg-green-500/20 text-green-300'
                    : 'bg-tradebot-accent/20 text-tradebot-accent hover:bg-tradebot-accent/30'
                } disabled:opacity-50`}
              >
                {applying ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : applySuccess ? (
                  <Check className="w-3.5 h-3.5" />
                ) : (
                  <Zap className="w-3.5 h-3.5" />
                )}
                {applySuccess ? 'Applied!' : 'Apply changes'}
              </button>
            )}
            </div>
          </div>
          {showSettings && (
            <div className="mt-3 grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
              {/* Scalp timeframe selector */}
              <div className="col-span-2 md:col-span-3">
                <label className="block text-gray-400 mb-1.5">
                  Scalp timeframe
                  <span className="ml-2 text-[10px] text-gray-600">(default: M5 — best for most markets)</span>
                  {active && <span className="text-yellow-400/70 ml-1 text-[10px]">(restart required to change)</span>}
                </label>
                <div className="flex gap-1.5 flex-wrap">
                  {(['M1', 'M5', 'M15', 'M30', 'H1'] as const).map(tf => (
                    <button
                      key={tf}
                      disabled={!!active}
                      onClick={() => setScalpTf(tf)}
                      className={`px-3 py-1 rounded-md text-xs font-semibold border transition ${
                        scalpTf === tf
                          ? 'bg-tradebot-accent text-black border-tradebot-accent'
                          : 'bg-gray-900 border-gray-700 text-gray-300 hover:border-tradebot-accent/50'
                      } disabled:opacity-50 disabled:cursor-not-allowed`}
                    >
                      {tf}{tf === SCALP_DEFAULTS.scalpTf ? <span className="ml-0.5 text-[9px] opacity-60">★</span> : null}
                    </button>
                  ))}
                </div>
                <p className="mt-0.5 text-[10px] text-gray-500">
                  Volume pressure &amp; entry signals are read from {scalpTf} candles. Higher TFs provide trend direction.
                </p>
              </div>
              {/* Lot size — always editable */}
              <div className="col-span-1">
                <label className="block text-gray-400 mb-1">
                  Lot size {autoLot ? '(risk-based)' : ''}
                  {active && <span className="text-tradebot-accent/70 ml-1">✎</span>}
                </label>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setLotSize(v => Math.max(0.01, +(v - 0.01).toFixed(2)))}
                    disabled={autoLot}
                    className="px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300 disabled:opacity-40"
                  >−</button>
                  <input
                    type="number" step="0.01" min="0.01" value={lotSize}
                    onChange={e => setLotSize(Math.max(0.01, +e.target.value || 0.01))}
                    disabled={autoLot}
                    className="w-full text-center py-1 rounded bg-gray-900 border border-gray-700 text-white disabled:opacity-40"
                  />
                  <button
                    onClick={() => setLotSize(v => +(v + 0.01).toFixed(2))}
                    disabled={autoLot}
                    className="px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300 disabled:opacity-40"
                  >+</button>
                </div>
              </div>
              {/* Auto lot */}
              <div className="col-span-1 flex items-end">
                <label className="flex items-center gap-2 text-gray-300 cursor-pointer">
                  <input type="checkbox" checked={autoLot} onChange={e => setAutoLot(e.target.checked)} />
                  Auto lot (risk-based)
                </label>
              </div>
              {/* Max open orders */}
              <div className="col-span-1">
                <label className="block text-gray-400 mb-1">
                  Max open orders
                  {active && <span className="text-tradebot-accent/70 ml-1">✎</span>}
                </label>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setMaxOpenOrders(v => Math.max(1, v - 1))}
                    className="px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300"
                  >−</button>
                  <input
                    type="number" step="1" min="1" max="10" value={maxOpenOrders}
                    onChange={e => setMaxOpenOrders(Math.min(10, Math.max(1, +e.target.value || 1)))}
                    className="w-full text-center py-1 rounded bg-gray-900 border border-gray-700 text-white"
                  />
                  <button
                    onClick={() => setMaxOpenOrders(v => Math.min(10, v + 1))}
                    className="px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300"
                  >+</button>
                </div>
                <p className="mt-0.5 text-[10px] text-gray-500">
                  {maxOpenOrders === 1 ? 'Primary only' : maxOpenOrders === 2 ? 'Primary + spike/recovery' : `Up to ${maxOpenOrders} concurrent orders`}
                </p>
              </div>
              {/* Direction restriction */}
              <div className="col-span-1">
                <label className="block text-gray-400 mb-1">
                  Trade direction
                  {active && <span className="text-tradebot-accent/70 ml-1">✎</span>}
                </label>
                <div className="flex gap-1">
                  {(['both', 'buy', 'sell'] as const).map(dir => (
                    <button
                      key={dir}
                      onClick={() => setAllowedDirection(dir)}
                      className={`flex-1 py-1.5 rounded text-xs font-semibold border transition ${
                        allowedDirection === dir
                          ? dir === 'buy'
                            ? 'bg-green-600/30 border-green-500/60 text-green-300'
                            : dir === 'sell'
                            ? 'bg-red-600/30 border-red-500/60 text-red-300'
                            : 'bg-tradebot-accent/20 border-tradebot-accent/60 text-tradebot-accent'
                          : 'bg-gray-900 border-gray-700 text-gray-400 hover:border-gray-500'
                      }`}
                    >
                      {dir === 'both' ? '↕ Both' : dir === 'buy' ? '▲ Buy only' : '▼ Sell only'}
                    </button>
                  ))}
                </div>
                <p className="mt-0.5 text-[10px] text-gray-500">
                  {allowedDirection === 'both' ? 'Bot trades both longs and shorts' :
                   allowedDirection === 'buy' ? 'Only BUY entries — bearish setups skipped' :
                   'Only SELL entries — bullish setups skipped'}
                </p>
              </div>
              {/* Max daily loss */}
              <div className="col-span-1">
                <label className="block text-gray-400 mb-1">Max daily loss: {maxDailyLoss}%</label>
                <input type="range" min={1} max={10} step={0.5} value={maxDailyLoss}
                  onChange={e => setMaxDailyLoss(+e.target.value)}
                  className="w-full accent-tradebot-accent" />
              </div>
              {/* Target profit */}
              <div className="col-span-1">
                <label className="block text-gray-400 mb-1">Target/trade: {targetProfit}%</label>
                <input type="range" min={0.5} max={5} step={0.1} value={targetProfit}
                  onChange={e => setTargetProfit(+e.target.value)}
                  className="w-full accent-tradebot-accent" />
              </div>
              {/* Toggles */}
              <div className="col-span-2 md:col-span-3 flex flex-wrap gap-4 pt-1">
                <label className="flex items-center gap-2 text-gray-300 cursor-pointer">
                  <input type="checkbox" checked={recovery} onChange={e => setRecovery(e.target.checked)} />
                  <ShieldCheck className="w-3.5 h-3.5 text-orange-400" /> Recovery order
                </label>
                <label className="flex items-center gap-2 text-gray-300 cursor-pointer">
                  <input type="checkbox" checked={useAi} onChange={e => setUseAi(e.target.checked)} />
                  <Brain className="w-3.5 h-3.5 text-purple-400" /> AI analysis
                </label>
                <label className="flex items-center gap-2 text-gray-300 cursor-pointer">
                  <input type="checkbox" checked={useKronos} onChange={e => setUseKronos(e.target.checked)} />
                  <Sparkles className="w-3.5 h-3.5 text-cyan-400" /> Kronos forecast
                </label>
              </div>
              {active && (
                <p className="col-span-2 md:col-span-3 text-[10px] text-gray-500 italic">
                  Changes take effect on the next cycle (~10s). Click &quot;Apply changes&quot; to push now.
                </p>
              )}
            </div>
          )}
        </div>

        {/* ── Live status ── */}
        {active && session && (
          <div className="space-y-3">
            {/* Bias + combined PnL */}
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400">Bias:</span>
                {session.bias_direction === 'buy' ? (
                  <span className="flex items-center gap-1 text-green-400 text-sm font-semibold"><TrendingUp className="w-4 h-4" /> BUY</span>
                ) : session.bias_direction === 'sell' ? (
                  <span className="flex items-center gap-1 text-red-400 text-sm font-semibold"><TrendingDown className="w-4 h-4" /> SELL</span>
                ) : (
                  <span className="text-gray-400 text-sm font-semibold">NEUTRAL</span>
                )}
                <div className="w-24 h-1.5 rounded-full bg-gray-700 overflow-hidden">
                  <div className="h-full bg-tradebot-accent" style={{ width: `${Math.round((session.bias_confidence || 0) * 100)}%` }} />
                </div>
                <span className="text-[11px] text-gray-500">{Math.round((session.bias_confidence || 0) * 100)}%</span>
              </div>
              <div className="text-right">
                <div className="text-[10px] text-gray-500 uppercase">Combined PnL</div>
                <div className={`text-lg font-bold ${pnlColor(combined)}`}>
                  {combined >= 0 ? '+' : ''}{combined.toFixed(2)}
                </div>
              </div>
            </div>

            {/* Open legs */}
            {session.open_trades.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {session.open_trades.map(t => (
                  <div key={t.ticket ?? Math.random()} className={`rounded-lg border p-2.5 text-xs ${t.is_recovery ? 'border-orange-500/30 bg-orange-500/5' : 'border-gray-700/50 bg-gray-900/40'}`}>
                    <div className="flex items-center justify-between mb-1">
                      <span className={`font-semibold ${t.side === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                        {t.is_recovery ? 'RECOVERY ' : ''}{t.side.toUpperCase()} {t.lot}
                      </span>
                      <span className={`font-bold ${pnlColor(t.pnl)}`}>{t.pnl >= 0 ? '+' : ''}{t.pnl.toFixed(2)}</span>
                    </div>
                    <div className="flex items-center gap-3 text-gray-400">
                      <span>Entry {t.entry_price}</span>
                      {t.sl != null && <span className="text-red-400/80">SL {t.sl}</span>}
                      {t.tp != null && <span className="text-green-400/80">TP {t.tp}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Session stats */}
            <div className="flex items-center justify-between text-[11px] text-gray-400 border-t border-gray-700/40 pt-2">
              <span className="flex items-center gap-1"><Activity className="w-3 h-3" /> {session.total_trades} trades</span>
              <span>W {session.wins} / L {session.losses}</span>
              <span>Session: <span className={pnlColor(session.session_pnl)}>{session.session_pnl >= 0 ? '+' : ''}{session.session_pnl.toFixed(2)}</span></span>
              <button onClick={handleCloseAll} disabled={busy} className="text-red-300 hover:text-red-200 disabled:opacity-50">Close all</button>
            </div>

            {session.ai_note && (
              <div className="text-[11px] text-gray-500 italic truncate" title={session.ai_note}>{session.ai_note}</div>
            )}
          </div>
        )}

        {/* ── Trade log ── */}
        {trades.length > 0 && (
          <div>
            <div className="text-[11px] text-gray-500 uppercase mb-1">Recent trades</div>
            <div className="max-h-40 overflow-y-auto rounded-lg border border-gray-700/40">
              <table className="w-full text-[11px]">
                <tbody>
                  {trades.slice(0, 10).map(t => (
                    <tr key={t.id} className="border-b border-gray-800/50 last:border-0">
                      <td className="px-2 py-1.5">
                        <span className={t.side === 'buy' ? 'text-green-400' : 'text-red-400'}>
                          {t.is_recovery ? '↺ ' : ''}{t.side.toUpperCase()}
                        </span>
                      </td>
                      <td className="px-2 py-1.5 text-gray-400">{t.lot}</td>
                      <td className="px-2 py-1.5 text-gray-400">{t.entry_price}</td>
                      <td className="px-2 py-1.5 text-gray-500">{t.status}</td>
                      <td className={`px-2 py-1.5 text-right font-medium ${pnlColor(t.pnl)}`}>
                        {t.pnl >= 0 ? '+' : ''}{t.pnl.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {!active && (
          <p className="text-[11px] text-gray-500">
            Searches the broker&apos;s symbols and scalps in both directions using Smart Money Concepts across all timeframes (M5 trigger), with Kronos + AI confirmation. Sets SL/TP automatically and adds a recovery leg if a trade goes against it.
          </p>
        )}
      </div>
    </div>
  )
}
