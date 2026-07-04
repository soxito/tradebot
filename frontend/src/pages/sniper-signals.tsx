import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import KronosForecastCard from '@/components/KronosForecastCard'
import {
  Crosshair,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  TrendingUp,
  TrendingDown,
  DollarSign,
  ShieldAlert,
  Target,
  Loader2,
  CheckCircle,
  XCircle,
  Activity,
  Zap,
} from 'lucide-react'
import { formatDateTimeZA } from '@/utils/datetime'

interface SniperSignal {
  id: number
  symbol: string
  action: string
  price: number
  confidence: number
  strength: string
  status: string
  created_at: string | null
  risk_score: number | null
  risk_reasons: string[]
  pump_pct: number | null
  market_cap: number | null
  volume_24h: number | null
  market_analysis: string | null
  ai_agents: {
    final_action?: string
    final_confidence?: number
    final_reasoning?: string
    agents_used?: number
    ai_calls?: number
    errors?: string[]
  } | null
  entry: number | null
  stop_loss: number | null
  take_profit: number | null
  enrichment: {
    pump_score?: number
    momentum_fading?: boolean
    sentiment_score?: number
    sentiment_label?: string
    cmc_community_score?: number
  }
  rug_token: {
    id: number
    symbol: string
    name: string
    status: string | null
    peak_price: number | null
    peak_change_pct: number | null
    current_price: number | null
  } | null
  trade: {
    id: number
    status: string
    side: string
    price: number | null
    average_price: number | null
    stop_loss: number | null
    take_profit: number | null
    pnl: number | null
    pnl_percentage: number | null
    leverage: number | null
    created_at: string | null
    closed_at: string | null
  } | null
}

interface SniperTrade {
  id: number
  symbol: string
  side: string
  trade_side: string | null
  status: string
  price: number | null
  average_price: number | null
  amount: number | null
  filled_amount: number | null
  stop_loss: number | null
  take_profit: number | null
  leverage: number | null
  margin_mode: string | null
  pnl: number | null
  pnl_percentage: number | null
  fee: number | null
  source: string
  signal_id: number | null
  created_at: string | null
  closed_at: string | null
  sniper_entry: any
  risk_score: number | null
  entry_method: string | null
}

interface SniperPosition {
  id: number
  symbol: string
  side: string
  status: string
  price: number | null
  average_price: number | null
  stop_loss: number | null
  take_profit: number | null
  pnl: number | null
  pnl_percentage: number | null
  source: string
  signal_id: number | null
  created_at: string | null
  closed_at: string | null
}

export default function SniperSignalsPage() {
  const [signals, setSignals] = useState<SniperSignal[]>([])
  const [trades, setTrades] = useState<SniperTrade[]>([])
  const [positions, setPositions] = useState<SniperPosition[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [lastRunSummary, setLastRunSummary] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'signals' | 'trades' | 'positions'>('signals')
  const [expandedSignal, setExpandedSignal] = useState<number | null>(null)
  const [expandedTrade, setExpandedTrade] = useState<number | null>(null)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const [sigRes, tradeRes, overviewRes] = await Promise.all([
        apiClient.getSniperSignals({ limit: 200 }),
        apiClient.getSniperTrades({ limit: 100 }),
        apiClient.getSmcOverview({ sniper_limit: 200 }),
      ])
      setSignals(sigRes.data.signals || [])
      setTrades(tradeRes.data.trades || [])
      setPositions(overviewRes.data?.sniper?.positions || [])
      setError(null)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to fetch sniper data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 15000)
    return () => clearInterval(interval)
  }, [fetchData])

  const handleGenerateSignals = useCallback(async () => {
    setGenerating(true)
    try {
      const res = await apiClient.runSniperOnce()
      const scanned = Number(res?.data?.scanned ?? res?.data?.scanned_tokens ?? 0)
      const created = Number(res?.data?.signals_created ?? 0)
      const executedCount = Number(res?.data?.trades_executed ?? 0)
      setLastRunSummary(
        `Scanned ${scanned} token(s), generated ${created} new signal(s), executed ${executedCount} trade(s).`
      )
      setError(null)
      await fetchData()
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to generate sniper signals')
    } finally {
      setGenerating(false)
    }
  }, [fetchData])

  const fmt = (n: number | null | undefined, dec = 2) =>
    n != null ? Number(n).toFixed(dec) : '—'
  const fmtUsd = (n: number | null | undefined) =>
    n != null ? `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'

  const riskColor = (score: number | null) => {
    if (score == null) return 'text-gray-400'
    if (score <= 0.3) return 'text-green-400'
    if (score <= 0.6) return 'text-yellow-400'
    return 'text-red-400'
  }

  const pnlColor = (pnl: number | null) => {
    if (pnl == null) return 'text-gray-400'
    return pnl >= 0 ? 'text-green-400' : 'text-red-400'
  }

  const totalSignals = signals.length
  const executed = signals.filter(s => s.trade).length
  const profitable = signals.filter(s => s.trade && s.trade.pnl != null && s.trade.pnl > 0).length
  const avgRisk = signals.length > 0
    ? signals.reduce((acc, s) => acc + (s.risk_score || 0), 0) / signals.length
    : 0

  return (
    <>
      <Head><title>Sniper Signals | TradeBot</title></Head>

      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Crosshair className="w-6 h-6 text-orange-400" />
            <h1 className="text-2xl font-bold text-white">Sniper Signals</h1>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleGenerateSignals}
              disabled={generating}
              className="flex items-center gap-2 px-4 py-2 bg-orange-600 hover:bg-orange-500 disabled:bg-orange-800/60 disabled:cursor-not-allowed rounded-lg text-sm text-white transition"
            >
              {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
              Generate New Signals
            </button>
            <button
              onClick={fetchData}
              disabled={loading}
              className="flex items-center gap-2 px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm text-gray-200 transition"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
          </div>
        </div>

        {error && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm flex items-center gap-2">
            <AlertTriangle className="w-4 h-4" /> {error}
          </div>
        )}

        {lastRunSummary && !error && (
          <div className="p-3 bg-emerald-900/30 border border-emerald-700/50 rounded-lg text-emerald-300 text-sm flex items-center gap-2">
            <CheckCircle className="w-4 h-4" /> {lastRunSummary}
          </div>
        )}

        {/* Stats Row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4">
            <div className="text-sm text-gray-400">Total Signals</div>
            <div className="text-2xl font-bold text-white">{totalSignals}</div>
          </div>
          <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4">
            <div className="text-sm text-gray-400">Executed</div>
            <div className="text-2xl font-bold text-blue-400">{executed}</div>
          </div>
          <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4">
            <div className="text-sm text-gray-400">Profitable</div>
            <div className="text-2xl font-bold text-green-400">{profitable}</div>
          </div>
          <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4">
            <div className="text-sm text-gray-400">Avg Risk Score</div>
            <div className={`text-2xl font-bold ${riskColor(avgRisk)}`}>{fmt(avgRisk)}</div>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 bg-gray-800/40 rounded-lg p-1 w-fit">
          <button
            onClick={() => setTab('signals')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition ${
              tab === 'signals' ? 'bg-orange-500/20 text-orange-400' : 'text-gray-400 hover:text-white'
            }`}
          >
            Signals ({signals.length})
          </button>
          <button
            onClick={() => setTab('trades')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition ${
              tab === 'trades' ? 'bg-blue-500/20 text-blue-400' : 'text-gray-400 hover:text-white'
            }`}
          >
            Trades ({trades.length})
          </button>
          <button
            onClick={() => setTab('positions')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition ${
              tab === 'positions' ? 'bg-emerald-500/20 text-emerald-400' : 'text-gray-400 hover:text-white'
            }`}
          >
            Positions ({positions.length})
          </button>
        </div>

        {loading && !signals.length ? (
          <div className="flex items-center justify-center py-20 text-gray-400">
            <Loader2 className="w-6 h-6 animate-spin mr-2" /> Loading sniper data...
          </div>
        ) : tab === 'signals' ? (
          /* ─── Signals List ─── */
          <div className="space-y-3">
            {signals.length === 0 ? (
              <div className="text-center py-16 text-gray-500">No sniper signals found</div>
            ) : signals.map(sig => {
              const expanded = expandedSignal === sig.id
              return (
                <div key={sig.id} className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden">
                  {/* Signal Header */}
                  <button
                    onClick={() => setExpandedSignal(expanded ? null : sig.id)}
                    className="w-full flex items-center justify-between p-4 hover:bg-gray-700/30 transition"
                  >
                    <div className="flex items-center gap-4">
                      <div className="flex items-center gap-2">
                        <Crosshair className="w-4 h-4 text-orange-400" />
                        <span className="font-bold text-white text-lg">{sig.symbol}</span>
                      </div>
                      <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                        sig.action === 'buy' ? 'bg-green-500/20 text-green-400' :
                        sig.action === 'sell' ? 'bg-red-500/20 text-red-400' :
                        'bg-gray-500/20 text-gray-400'
                      }`}>
                        {sig.action.toUpperCase()}
                      </span>
                      <span className={`text-sm ${riskColor(sig.risk_score)}`}>
                        Risk: {fmt(sig.risk_score)}
                      </span>
                      <span className="text-sm text-gray-400">
                        Conf: {fmt(sig.confidence ? sig.confidence * 100 : null, 0)}%
                      </span>
                    </div>
                    <div className="flex items-center gap-4">
                      {sig.trade ? (
                        <span className={`text-sm font-medium ${pnlColor(sig.trade.pnl)}`}>
                          {sig.trade.status === 'open' ? '● Open' : `PnL: ${fmt(sig.trade.pnl_percentage)}%`}
                        </span>
                      ) : (
                        <span className="text-sm text-gray-500">Not executed</span>
                      )}
                      <span className="text-xs text-gray-500">
                        {sig.created_at ? formatDateTimeZA(sig.created_at) : ''}
                      </span>
                      {expanded ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
                    </div>
                  </button>

                  {/* Expanded Details */}
                  {expanded && (
                    <div className="border-t border-gray-700/50 p-4 space-y-4">
                      <KronosForecastCard symbol={sig.symbol} timeframe="15m" predLen={16} />
                      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        {/* Entry Decision */}
                        <div className="bg-gray-900/50 rounded-lg p-4 space-y-2">
                          <div className="flex items-center gap-2 text-sm font-semibold text-orange-400">
                            <Target className="w-4 h-4" /> Entry Decision
                          </div>
                          <div className="grid grid-cols-2 gap-2 text-sm">
                            <span className="text-gray-400">Entry:</span>
                            <span className="text-white">{fmtUsd(sig.entry)}</span>
                            <span className="text-gray-400">Stop Loss:</span>
                            <span className="text-red-400">{fmtUsd(sig.stop_loss)}</span>
                            <span className="text-gray-400">Take Profit:</span>
                            <span className="text-green-400">{fmtUsd(sig.take_profit)}</span>
                            <span className="text-gray-400">Price:</span>
                            <span className="text-white">{fmtUsd(sig.price)}</span>
                          </div>
                        </div>

                        {/* Risk Analysis */}
                        <div className="bg-gray-900/50 rounded-lg p-4 space-y-2">
                          <div className="flex items-center gap-2 text-sm font-semibold text-red-400">
                            <ShieldAlert className="w-4 h-4" /> Risk Analysis
                          </div>
                          <div className="grid grid-cols-2 gap-2 text-sm">
                            <span className="text-gray-400">Risk Score:</span>
                            <span className={riskColor(sig.risk_score)}>{fmt(sig.risk_score)}</span>
                            <span className="text-gray-400">Pump %:</span>
                            <span className="text-yellow-400">{fmt(sig.pump_pct, 0)}%</span>
                            <span className="text-gray-400">Market Cap:</span>
                            <span className="text-white">{fmtUsd(sig.market_cap)}</span>
                            <span className="text-gray-400">24h Volume:</span>
                            <span className="text-white">{fmtUsd(sig.volume_24h)}</span>
                          </div>
                          {sig.risk_reasons.length > 0 && (
                            <div className="mt-2">
                              <span className="text-xs text-gray-500">Reasons:</span>
                              <ul className="mt-1 space-y-0.5">
                                {sig.risk_reasons.map((r, i) => (
                                  <li key={i} className="text-xs text-yellow-300 flex items-start gap-1">
                                    <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" /> {r}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>

                        {/* Enrichment Data */}
                        <div className="bg-gray-900/50 rounded-lg p-4 space-y-2">
                          <div className="flex items-center gap-2 text-sm font-semibold text-purple-400">
                            <Activity className="w-4 h-4" /> Enrichment
                          </div>
                          {sig.enrichment && Object.keys(sig.enrichment).length > 0 ? (
                            <div className="grid grid-cols-2 gap-2 text-sm">
                              {sig.enrichment.pump_score != null && (
                                <>
                                  <span className="text-gray-400">Pump Score:</span>
                                  <span className="text-yellow-400">{fmt(sig.enrichment.pump_score)}</span>
                                </>
                              )}
                              {sig.enrichment.momentum_fading != null && (
                                <>
                                  <span className="text-gray-400">Momentum Fading:</span>
                                  <span className={sig.enrichment.momentum_fading ? 'text-red-400' : 'text-green-400'}>
                                    {sig.enrichment.momentum_fading ? 'Yes' : 'No'}
                                  </span>
                                </>
                              )}
                              {sig.enrichment.sentiment_score != null && (
                                <>
                                  <span className="text-gray-400">Sentiment:</span>
                                  <span className="text-blue-400">{fmt(sig.enrichment.sentiment_score)} ({sig.enrichment.sentiment_label || '?'})</span>
                                </>
                              )}
                              {sig.enrichment.cmc_community_score != null && (
                                <>
                                  <span className="text-gray-400">CMC Community:</span>
                                  <span className="text-blue-400">{fmt(sig.enrichment.cmc_community_score)}</span>
                                </>
                              )}
                            </div>
                          ) : (
                            <div className="text-sm text-gray-500">No enrichment data</div>
                          )}
                        </div>
                      </div>

                      {/* Rug Token Context */}
                      {sig.rug_token && (
                        <div className="bg-gray-900/50 rounded-lg p-4">
                          <div className="flex items-center gap-2 text-sm font-semibold text-gray-300 mb-2">
                            <Zap className="w-4 h-4 text-yellow-400" /> Token Context
                          </div>
                          <div className="flex flex-wrap gap-4 text-sm">
                            <span><span className="text-gray-400">Name:</span> <span className="text-white">{sig.rug_token.name}</span></span>
                            <span><span className="text-gray-400">Status:</span> <span className={
                              sig.rug_token.status === 'confirmed' ? 'text-red-400' :
                              sig.rug_token.status === 'monitoring' ? 'text-yellow-400' :
                              'text-gray-400'
                            }>{sig.rug_token.status || '—'}</span></span>
                            <span><span className="text-gray-400">Peak Change:</span> <span className="text-green-400">{fmt(sig.rug_token.peak_change_pct, 0)}%</span></span>
                            <span><span className="text-gray-400">Peak:</span> <span className="text-white">{fmtUsd(sig.rug_token.peak_price)}</span></span>
                            <span><span className="text-gray-400">Current:</span> <span className="text-white">{fmtUsd(sig.rug_token.current_price)}</span></span>
                          </div>
                        </div>
                      )}

                      {(sig.ai_agents || sig.market_analysis) && (
                        <div className="bg-gray-900/50 rounded-lg p-4">
                          <div className="flex items-center gap-2 text-sm font-semibold text-emerald-300 mb-2">
                            <Activity className="w-4 h-4 text-emerald-400" /> Agent Market Analysis
                          </div>
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                            <div className="space-y-1">
                              <div>
                                <span className="text-gray-400">Final Action:</span>{' '}
                                <span className="text-white">{(sig.ai_agents?.final_action || 'hold').toUpperCase()}</span>
                              </div>
                              <div>
                                <span className="text-gray-400">Confidence:</span>{' '}
                                <span className="text-white">
                                  {sig.ai_agents?.final_confidence != null
                                    ? `${fmt(sig.ai_agents.final_confidence * 100, 0)}%`
                                    : '—'}
                                </span>
                              </div>
                              <div>
                                <span className="text-gray-400">Agents Used:</span>{' '}
                                <span className="text-white">{sig.ai_agents?.agents_used ?? '—'}</span>
                              </div>
                            </div>
                            <div className="text-gray-300 leading-relaxed">
                              {sig.market_analysis || sig.ai_agents?.final_reasoning || 'No AI reasoning available'}
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Linked Trade */}
                      {sig.trade && (
                        <div className="bg-gray-900/50 rounded-lg p-4">
                          <div className="flex items-center gap-2 text-sm font-semibold text-blue-400 mb-2">
                            <DollarSign className="w-4 h-4" /> Trade Result
                          </div>
                          <div className="flex flex-wrap gap-4 text-sm">
                            <span>
                              <span className="text-gray-400">Status:</span>{' '}
                              <span className={sig.trade.status === 'open' ? 'text-blue-400' : sig.trade.pnl != null && sig.trade.pnl > 0 ? 'text-green-400' : 'text-red-400'}>
                                {sig.trade.status}
                              </span>
                            </span>
                            <span><span className="text-gray-400">Side:</span> <span className="text-white">{sig.trade.side}</span></span>
                            <span><span className="text-gray-400">Entry:</span> <span className="text-white">{fmtUsd(sig.trade.average_price || sig.trade.price)}</span></span>
                            <span><span className="text-gray-400">SL:</span> <span className="text-red-400">{fmtUsd(sig.trade.stop_loss)}</span></span>
                            <span><span className="text-gray-400">TP:</span> <span className="text-green-400">{fmtUsd(sig.trade.take_profit)}</span></span>
                            {sig.trade.leverage && <span><span className="text-gray-400">Leverage:</span> <span className="text-white">{sig.trade.leverage}x</span></span>}
                            {sig.trade.pnl != null && (
                              <span>
                                <span className="text-gray-400">PnL:</span>{' '}
                                <span className={pnlColor(sig.trade.pnl)}>
                                  {fmtUsd(sig.trade.pnl)} ({fmt(sig.trade.pnl_percentage)}%)
                                </span>
                              </span>
                            )}
                            <span><span className="text-gray-400">Opened:</span> <span className="text-gray-300">{formatDateTimeZA(sig.trade.created_at)}</span></span>
                            {sig.trade.closed_at && (
                              <span><span className="text-gray-400">Closed:</span> <span className="text-gray-300">{formatDateTimeZA(sig.trade.closed_at)}</span></span>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ) : tab === 'trades' ? (
          /* ─── Trades Tab ─── */
          <div className="space-y-3">
            {trades.length === 0 ? (
              <div className="text-center py-16 text-gray-500">No sniper trades found</div>
            ) : trades.map(t => {
              const expanded = expandedTrade === t.id
              return (
                <div key={t.id} className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden">
                  <button
                    onClick={() => setExpandedTrade(expanded ? null : t.id)}
                    className="w-full flex items-center justify-between p-4 hover:bg-gray-700/30 transition"
                  >
                    <div className="flex items-center gap-4">
                      <span className="font-bold text-white">{t.symbol}</span>
                      <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                        t.side === 'buy' ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'
                      }`}>{(t.trade_side || t.side).toUpperCase()}</span>
                      <span className={`text-xs px-2 py-0.5 rounded ${
                        t.status === 'open' ? 'bg-blue-500/20 text-blue-400' : 'bg-gray-500/20 text-gray-400'
                      }`}>{t.status}</span>
                    </div>
                    <div className="flex items-center gap-4">
                      {t.pnl != null && (
                        <span className={`text-sm font-medium ${pnlColor(t.pnl)}`}>
                          {fmtUsd(t.pnl)} ({fmt(t.pnl_percentage)}%)
                        </span>
                      )}
                      <span className="text-xs text-gray-500">
                        {t.created_at ? formatDateTimeZA(t.created_at) : ''}
                      </span>
                      {expanded ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
                    </div>
                  </button>

                  {expanded && (
                    <div className="border-t border-gray-700/50 p-4">
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                        <div>
                          <span className="text-gray-400">Entry Price</span>
                          <div className="text-white">{fmtUsd(t.average_price || t.price)}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Amount</span>
                          <div className="text-white">{fmt(t.filled_amount || t.amount, 4)}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Stop Loss</span>
                          <div className="text-red-400">{fmtUsd(t.stop_loss)}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Take Profit</span>
                          <div className="text-green-400">{fmtUsd(t.take_profit)}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Leverage</span>
                          <div className="text-white">{t.leverage ? `${t.leverage}x` : '—'}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Margin Mode</span>
                          <div className="text-white">{t.margin_mode || '—'}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Fee</span>
                          <div className="text-yellow-400">{fmtUsd(t.fee)}</div>
                        </div>
                        <div>
                          <span className="text-gray-400">Entry Method</span>
                          <div className="text-orange-400">{t.entry_method || '—'}</div>
                        </div>
                        {t.risk_score != null && (
                          <div>
                            <span className="text-gray-400">Risk Score</span>
                            <div className={riskColor(t.risk_score)}>{fmt(t.risk_score)}</div>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ) : (
          <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden">
            {positions.length === 0 ? (
              <div className="text-center py-16 text-gray-500">No sniper positions found</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-900/60 border-b border-gray-700/50 text-gray-400">
                    <tr>
                      <th className="text-left px-4 py-3">Symbol</th>
                      <th className="text-left px-4 py-3">Side</th>
                      <th className="text-left px-4 py-3">Status</th>
                      <th className="text-left px-4 py-3">Entry</th>
                      <th className="text-left px-4 py-3">Stop Loss</th>
                      <th className="text-left px-4 py-3">Take Profit</th>
                      <th className="text-left px-4 py-3">PnL</th>
                      <th className="text-left px-4 py-3">Opened</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p) => (
                      <tr key={p.id} className="border-b border-gray-700/40 last:border-b-0 hover:bg-gray-700/20">
                        <td className="px-4 py-3 text-white font-medium">{p.symbol}</td>
                        <td className="px-4 py-3">
                          <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                            p.side === 'sell' ? 'bg-red-500/20 text-red-400' : 'bg-green-500/20 text-green-400'
                          }`}>
                            {(p.side || '—').toUpperCase()}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-gray-300">{p.status || '—'}</td>
                        <td className="px-4 py-3 text-white">{fmtUsd(p.average_price || p.price)}</td>
                        <td className="px-4 py-3 text-red-400">{fmtUsd(p.stop_loss)}</td>
                        <td className="px-4 py-3 text-green-400">{fmtUsd(p.take_profit)}</td>
                        <td className={`px-4 py-3 ${pnlColor(p.pnl)}`}>
                          {p.pnl != null ? `${fmtUsd(p.pnl)} (${fmt(p.pnl_percentage)}%)` : '—'}
                        </td>
                        <td className="px-4 py-3 text-gray-400">
                          {formatDateTimeZA(p.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}
