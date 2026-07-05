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
}

const PHASE_LABEL: Record<string, string> = {
  analyzing: 'ANALYZING',
  waiting: 'WAITING',
  in_trade: 'IN TRADE',
  recovery: 'RECOVERY',
  stopped: 'STOPPED',
  error: 'ERROR',
}

const PHASE_COLOR: Record<string, string> = {
  analyzing: 'text-blue-400 bg-blue-500/15',
  waiting: 'text-amber-400 bg-amber-500/15',
  in_trade: 'text-green-400 bg-green-500/15',
  recovery: 'text-orange-400 bg-orange-500/15',
  stopped: 'text-gray-400 bg-gray-500/15',
  error: 'text-red-400 bg-red-500/15',
}

function pnlColor(v: number): string {
  if (v > 0) return 'text-green-400'
  if (v < 0) return 'text-red-400'
  return 'text-gray-300'
}

export default function MT5ScalpBotPanel({ accountId, serverSymbolDefault, chartSymbol, onSymbolChange }: Props) {
  const [symbol, setSymbol] = useState(chartSymbol || serverSymbolDefault || 'XAUUSD')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<{ symbol: string; description: string | null }[]>([])
  const [searching, setSearching] = useState(false)
  const [showResults, setShowResults] = useState(false)

  // Settings
  const [lotSize, setLotSize] = useState(0.01)
  const [autoLot, setAutoLot] = useState(false)
  const [maxDailyLoss, setMaxDailyLoss] = useState(3)
  const [targetProfit, setTargetProfit] = useState(1.5)
  const [recovery, setRecovery] = useState(true)
  const [useAi, setUseAi] = useState(true)
  const [useKronos, setUseKronos] = useState(true)
  const [showSettings, setShowSettings] = useState(false)

  const [session, setSession] = useState<ScalpStatus | null>(null)
  const [trades, setTrades] = useState<ScalpTradeRow[]>([])
  const [busy, setBusy] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applySuccess, setApplySuccess] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
    setBusy(true); setError(null)
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
        timeframe: 'M5',
      })
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to start scalp bot')
    } finally {
      setBusy(false)
    }
  }

  const handleStop = async () => {
    setBusy(true); setError(null)
    try {
      await apiClient.mt5.scalp.stop(accountId, symbol.trim().toUpperCase())
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to stop scalp bot')
    } finally {
      setBusy(false)
    }
  }

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
          <span>All TFs · M5 trigger</span>
        </div>
      </div>

      <div className="p-4 space-y-4">
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
          {showSettings && (
            <div className="mt-3 grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
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
