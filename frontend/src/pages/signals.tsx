import Head from 'next/head'
import { useState, useEffect, useCallback, useRef } from 'react'
import { apiClient } from '@/services/api'
import { formatPrice } from '@/utils/price'
import { useTradeStore } from '@/store/useTradeStore'
import { useApiBaseUrl } from '@/hooks/useApiUrl'
import SignalFeed from '@/components/SignalFeed'
import ResearchEntries, { ResearchVerdictBadge } from '@/components/research/ResearchEntries'
import { useResearchPlans, type ResearchPlan } from '@/hooks/useResearchPlans'
import {
  Activity,
  Zap,
  TrendingUp,
  TrendingDown,
  Minus,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  BarChart3,
  Brain,
  ArrowRight,
  Shield,
  CheckCircle,
  XCircle,
  Search,
  X,
  Plus,
} from 'lucide-react'

const DEFAULT_PAIRS = [
  'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT',
  'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT', 'LINK/USDT',
  'DOT/USDT', 'MATIC/USDT', 'ARB/USDT', 'OP/USDT',
]

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']

interface AnalysisResult {
  symbol: string
  timeframe: string
  exchange: string
  action: string
  score: number
  confidence: number
  strength: number
  reasons: string[]
  indicators: Record<string, any>
  sentiment: {
    score: number
    label: string
    has_data: boolean
  }
  signal_id?: number
  error?: string
}

interface BatchResult {
  total: number
  generated: number
  errors: number
  timeframe: string
  results: AnalysisResult[]
}

function IndicatorBadge({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-gray-900/60 rounded px-2.5 py-1.5 text-xs">
      <span className="text-gray-500">{label}</span>
      <span className={`ml-1.5 font-mono font-semibold ${color || 'text-gray-200'}`}>{value}</span>
    </div>
  )
}

function ActionIcon({ action }: { action: string }) {
  switch (action?.toLowerCase()) {
    case 'buy':
      return <TrendingUp className="w-5 h-5 text-green-400" />
    case 'sell':
      return <TrendingDown className="w-5 h-5 text-red-400" />
    default:
      return <Minus className="w-5 h-5 text-gray-400" />
  }
}

function ScoreBar({ score, label }: { score: number; label: string }) {
  const pct = ((score + 1) / 2) * 100
  const barColor =
    score > 0.25 ? 'bg-green-500' : score < -0.25 ? 'bg-red-500' : 'bg-yellow-500'
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-gray-400">{label}</span>
        <span className="font-mono">{score >= 0 ? '+' : ''}{score.toFixed(3)}</span>
      </div>
      <div className="h-1.5 bg-gray-700 rounded-full overflow-hidden">
        <div className={`h-full ${barColor} rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function AnalysisCard({ result, onExecute, executing, marketType, plan }: {
  result: AnalysisResult
  onExecute?: (result: AnalysisResult) => void
  executing?: boolean
  marketType: 'futures' | 'spot'
  /** Reconciled research across every live signal on this pair, if any. */
  plan?: ResearchPlan
}) {
  const [expanded, setExpanded] = useState(false)
  const ind = result.indicators || {}
  const actionColor =
    result.action === 'buy' ? 'text-green-400 border-green-500/30 bg-green-500/5'
      : result.action === 'sell' ? 'text-red-400 border-red-500/30 bg-red-500/5'
        : 'text-yellow-400 border-yellow-500/30 bg-yellow-500/5'
  const canExecute = result.action === 'buy' || result.action === 'sell'

  return (
    <div className={`border rounded-lg p-4 ${actionColor} transition-all`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <ActionIcon action={result.action} />
          <div>
            <span className="font-mono font-bold text-white">{result.symbol}</span>
            <span className="ml-2 text-xs text-gray-500">{result.timeframe}</span>
          </div>
          <span className={`text-sm font-bold uppercase ${
            result.action === 'buy' ? 'text-green-400' : result.action === 'sell' ? 'text-red-400' : 'text-yellow-400'
          }`}>
            {result.action}
          </span>
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
            marketType === 'futures' ? 'bg-orange-500/20 text-orange-400' : 'bg-blue-500/20 text-blue-400'
          }`}>
            {marketType === 'futures' ? 'Futures' : 'Spot'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {canExecute && onExecute && (
            <button
              onClick={() => onExecute(result)}
              disabled={executing}
              className={`flex items-center gap-1 px-3 py-1.5 rounded text-xs font-medium transition ${
                result.action === 'buy'
                  ? 'bg-green-600 hover:bg-green-500 text-white'
                  : 'bg-red-600 hover:bg-red-500 text-white'
              } disabled:opacity-50`}
            >
              {executing ? (
                <RefreshCw className="w-3 h-3 animate-spin" />
              ) : (
                <ArrowRight className="w-3 h-3" />
              )}
              Execute
            </button>
          )}
          <div className="text-right text-xs">
            <div className="text-gray-400">Confidence</div>
            <div className="font-bold text-white">{(result.confidence * 100).toFixed(0)}%</div>
          </div>
          <button onClick={() => setExpanded(!expanded)} className="text-gray-400 hover:text-white">
            {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
        </div>
      </div>

      {/* Score bar */}
      <div className="mt-3">
        <ScoreBar score={result.score} label="Combined Score" />
      </div>

      {/* Key indicators row */}
      <div className="mt-3 flex flex-wrap gap-2">
        {ind.price && <IndicatorBadge label="Price" value={formatPrice(Number(ind.price))} />}
        {ind.rsi !== undefined && (
          <IndicatorBadge
            label="RSI"
            value={ind.rsi.toFixed(1)}
            color={ind.rsi > 70 ? 'text-red-400' : ind.rsi < 30 ? 'text-green-400' : 'text-gray-200'}
          />
        )}
        {ind.macd_histogram !== undefined && (
          <IndicatorBadge
            label="MACD"
            value={ind.macd_histogram.toFixed(4)}
            color={ind.macd_histogram > 0 ? 'text-green-400' : 'text-red-400'}
          />
        )}
        {ind.bb_pct_b !== undefined && (
          <IndicatorBadge label="%B" value={ind.bb_pct_b.toFixed(2)} />
        )}
        {ind.buy_ratio !== undefined && (
          <IndicatorBadge
            label="Buy Vol"
            value={`${(ind.buy_ratio * 100).toFixed(0)}%`}
            color={ind.buy_ratio > 0.55 ? 'text-green-400' : ind.buy_ratio < 0.45 ? 'text-red-400' : 'text-gray-200'}
          />
        )}
        {result.sentiment.has_data && (
          <IndicatorBadge
            label="Sentiment"
            value={`${result.sentiment.score >= 0 ? '+' : ''}${result.sentiment.score.toFixed(2)} (${result.sentiment.label})`}
            color={result.sentiment.score > 0.1 ? 'text-green-400' : result.sentiment.score < -0.1 ? 'text-red-400' : 'text-gray-200'}
          />
        )}
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="mt-4 space-y-4 border-t border-gray-700/50 pt-4">
          {/* Research: every live signal on this pair reconciled into two
              costed entries. Above the indicators because it is the conclusion
              they feed into. */}
          <ResearchEntries plan={plan} defaultOpen />

          {/* Moving Averages */}
          <div>
            <h4 className="text-xs font-semibold text-gray-400 mb-2 flex items-center gap-1">
              <BarChart3 className="w-3 h-3" /> Moving Averages & Volume
            </h4>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
              {ind.ma5 !== undefined && <IndicatorBadge label="MA(5)" value={ind.ma5.toFixed(2)} />}
              {ind.ma10 !== undefined && <IndicatorBadge label="MA(10)" value={ind.ma10.toFixed(2)} />}
              {ind.ma20 !== undefined && <IndicatorBadge label="MA(20)" value={ind.ma20.toFixed(2)} />}
              {ind.vol_ma5 !== undefined && <IndicatorBadge label="Vol MA(5)" value={Number(ind.vol_ma5).toLocaleString(undefined, { maximumFractionDigits: 0 })} />}
              {ind.vol_ma10 !== undefined && <IndicatorBadge label="Vol MA(10)" value={Number(ind.vol_ma10).toLocaleString(undefined, { maximumFractionDigits: 0 })} />}
              {ind.volume !== undefined && <IndicatorBadge label="Volume" value={Number(ind.volume).toLocaleString(undefined, { maximumFractionDigits: 0 })} />}
            </div>
          </div>

          {/* MACD */}
          <div>
            <h4 className="text-xs font-semibold text-gray-400 mb-2">MACD</h4>
            <div className="grid grid-cols-3 gap-2">
              {ind.macd !== undefined && <IndicatorBadge label="Line" value={ind.macd.toFixed(4)} />}
              {ind.macd_signal !== undefined && <IndicatorBadge label="Signal" value={ind.macd_signal.toFixed(4)} />}
              {ind.macd_histogram !== undefined && (
                <IndicatorBadge
                  label="Histogram"
                  value={ind.macd_histogram.toFixed(4)}
                  color={ind.macd_histogram > 0 ? 'text-green-400' : 'text-red-400'}
                />
              )}
            </div>
          </div>

          {/* Bollinger Bands */}
          <div>
            <h4 className="text-xs font-semibold text-gray-400 mb-2">Bollinger Bands</h4>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {ind.bb_upper !== undefined && <IndicatorBadge label="Upper" value={ind.bb_upper.toFixed(2)} />}
              {ind.bb_middle !== undefined && <IndicatorBadge label="Middle" value={ind.bb_middle.toFixed(2)} />}
              {ind.bb_lower !== undefined && <IndicatorBadge label="Lower" value={ind.bb_lower.toFixed(2)} />}
              {ind.bb_pct_b !== undefined && <IndicatorBadge label="%B" value={ind.bb_pct_b.toFixed(3)} />}
            </div>
          </div>

          {/* Reasons */}
          <div>
            <h4 className="text-xs font-semibold text-gray-400 mb-2 flex items-center gap-1">
              <Brain className="w-3 h-3" /> Analysis Reasons
            </h4>
            <ul className="space-y-1">
              {result.reasons?.map((r, i) => (
                <li key={i} className="text-xs text-gray-300 flex items-start gap-2">
                  <span className="mt-1 w-1 h-1 rounded-full bg-gray-500 shrink-0" />
                  {r}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  )
}

export default function SignalsPage() {
  const apiBaseUrl = useApiBaseUrl()
  const { tradingMode, setSelectedSymbol } = useTradeStore()
  const [selectedPairs, setSelectedPairs] = useState<string[]>([])
  const [timeframe, setTimeframe] = useState('1h')
  const [marketType, setMarketType] = useState<'futures' | 'spot' | 'both'>('both')
  const [generating, setGenerating] = useState(false)
  const [batchResult, setBatchResult] = useState<BatchResult | null>(null)
  const [webhookOpen, setWebhookOpen] = useState(false)
  const [mounted, setMounted] = useState(false)
  const [executingSymbol, setExecutingSymbol] = useState<string | null>(null)
  const [executeResult, setExecuteResult] = useState<{ success: boolean; message: string } | null>(null)

  // AI Agents status
  const [aiStatus, setAiStatus] = useState<any>(null)
  const [aiAgents, setAiAgents] = useState<any[]>([])

  // Pair search state
  interface AvailablePair {
    symbol: string
    baseCoin: string
    quoteCoin: string
    market: string
    status: string
    delisting_ts: number | null
    delisting_date: string | null
    minLever: number | null
    maxLever: number | null
  }
  const [availablePairs, setAvailablePairs] = useState<AvailablePair[]>([])
  const [availablePairsLoading, setAvailablePairsLoading] = useState(false)
  const availablePairsFetched = useRef(false)
  const [pairSearch, setPairSearch] = useState('')
  const [showPairDropdown, setShowPairDropdown] = useState(false)
  const [savingPairs, setSavingPairs] = useState(false)
  const [showAllPairs, setShowAllPairs] = useState(false)
  const pairDropdownRef = useRef<HTMLDivElement>(null)

  // Load monitor pairs from backend on mount
  useEffect(() => {
    setMounted(true)
    apiClient.getSignalMonitorPairs()
      .then(res => {
        const pairs: string[] = res.data?.pairs || []
        if (pairs.length > 0) {
          setSelectedPairs(pairs)
          localStorage.setItem('tradebot_configured_pairs', JSON.stringify(pairs))
        } else {
          // Fall back to localStorage or defaults
          try {
            const saved = localStorage.getItem('tradebot_configured_pairs')
            if (saved) {
              const parsed = JSON.parse(saved)
              if (Array.isArray(parsed) && parsed.length > 0) {
                setSelectedPairs(parsed)
                return
              }
            }
          } catch { /* ignore */ }
          setSelectedPairs(DEFAULT_PAIRS)
        }
      })
      .catch(() => {
        // Fallback: localStorage
        try {
          const saved = localStorage.getItem('tradebot_configured_pairs')
          if (saved) {
            const parsed = JSON.parse(saved)
            if (Array.isArray(parsed) && parsed.length > 0) {
              setSelectedPairs(parsed)
              return
            }
          }
        } catch { /* ignore */ }
        setSelectedPairs(DEFAULT_PAIRS)
      })
  }, [])

  // Fetch AI agent status
  useEffect(() => {
    apiClient.getAgentStatus().then(res => setAiStatus(res.data)).catch(() => {})
    apiClient.getAgents().then(res => setAiAgents(res.data?.agents || [])).catch(() => {})
  }, [])

  // Fetch available pairs from Bitget
  useEffect(() => {
    if (availablePairsFetched.current) return
    availablePairsFetched.current = true
    setAvailablePairsLoading(true)
    apiClient.getBitgetAvailablePairs('USDT')
      .then(res => setAvailablePairs(res.data?.pairs || []))
      .catch(() => {})
      .finally(() => setAvailablePairsLoading(false))
  }, [])

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (pairDropdownRef.current && !pairDropdownRef.current.contains(e.target as Node)) {
        setShowPairDropdown(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Save pairs to backend
  const savePairsToBackend = useCallback(async (pairs: string[]) => {
    localStorage.setItem('tradebot_configured_pairs', JSON.stringify(pairs))
    setSavingPairs(true)
    try {
      await apiClient.setSignalMonitorPairs(pairs)
    } catch { /* silent — localStorage is fallback */ }
    finally { setSavingPairs(false) }
  }, [])

  const addPair = useCallback((pair: string) => {
    setSelectedPairs(prev => {
      if (prev.includes(pair)) return prev
      const next = [...prev, pair]
      savePairsToBackend(next)
      return next
    })
  }, [savePairsToBackend])

  const removePair = useCallback((pair: string) => {
    setSelectedPairs(prev => {
      const next = prev.filter(p => p !== pair)
      savePairsToBackend(next)
      return next
    })
  }, [savePairsToBackend])

  const addDefaults = useCallback(() => {
    setSelectedPairs(prev => {
      const set = new Set(prev)
      DEFAULT_PAIRS.forEach(p => set.add(p))
      const next = Array.from(set)
      savePairsToBackend(next)
      return next
    })
  }, [savePairsToBackend])

  const clearAllPairs = useCallback(() => {
    setSelectedPairs([])
    savePairsToBackend([])
  }, [savePairsToBackend])

  const generateSignals = async () => {
    if (selectedPairs.length === 0) return
    setGenerating(true)
    setBatchResult(null)
    try {
      const res = await apiClient.generateSignals({
        symbols: selectedPairs,
        timeframe,
        exchange: 'bitget',
      })
      setBatchResult(res.data)
    } catch (err: any) {
      console.error('Signal generation failed:', err)
      setBatchResult({
        total: selectedPairs.length,
        generated: 0,
        errors: selectedPairs.length,
        timeframe,
        results: [{ symbol: 'ERROR', error: err?.response?.data?.detail || err.message } as any],
      })
    } finally {
      setGenerating(false)
    }
  }

  const executeSignal = async (result: AnalysisResult) => {
    if (!result.action || result.action === 'hold') return
    setExecutingSymbol(result.symbol)
    setExecuteResult(null)
    try {
      // Get settings to determine min confidence threshold
      const simRes = await apiClient.getSimAccount()
      const settings = simRes.data
      let minConf = settings?.min_confidence ?? 0.90
      if (tradingMode === 'live') {
        try {
          const liveRes = await apiClient.getLiveTradeSettings()
          minConf = liveRes.data?.min_confidence ?? 0.90
        } catch {}
      }
      if ((result.confidence ?? 0) < minConf) {
        setExecuteResult({ success: false, message: `Confidence too low (${((result.confidence ?? 0) * 100).toFixed(0)}%). Min required: ${(minConf * 100).toFixed(0)}% (change in Settings).` })
        setExecutingSymbol(null)
        return
      }
      const leverage = settings?.auto_trade_leverage || 10
      const marginMode = settings?.auto_trade_margin_mode || 'crossed'
      const riskPct = settings?.auto_trade_risk_pct || 2

      if (tradingMode === 'sim') {
        // Sim execution
        const balance = settings?.balance || 10000
        const amount = (balance * (riskPct / 100)) / (result.indicators?.price || 1)
        await apiClient.placeSimOrder({
          symbol: result.symbol,
          side: result.action as 'buy' | 'sell',
          amount,
          order_type: 'market',
          trade_type: 'futures',
          leverage,
          margin_mode: marginMode,
        })
        setExecuteResult({ success: true, message: `[SIM] ${result.action.toUpperCase()} ${result.symbol} — futures ${leverage}x ${marginMode}` })
      } else {
        // Live futures execution
        const liveRes = await apiClient.getBitgetFuturesAccountSummary()
        const liveBalance = liveRes.data?.balance || 0
        const amount = (liveBalance * (riskPct / 100)) / (result.indicators?.price || 1)
        const productType = marginMode === 'crossed' ? 'USDT-FUTURES' : 'USDT-FUTURES'
        const side = result.action === 'buy' ? 'open_long' : 'open_short'
        await apiClient.createBitgetFuturesOrder({
          symbol: result.symbol.replace('/', ''),
          product_type: productType,
          margin_mode: marginMode === 'crossed' ? 'crossed' : 'isolated',
          margin_coin: 'USDT',
          size: amount.toString(),
          side,
          order_type: 'market',
          leverage,
        })
        setExecuteResult({ success: true, message: `[LIVE] ${result.action.toUpperCase()} ${result.symbol} — futures ${leverage}x ${marginMode}` })
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      let msg: string
      if (typeof detail === 'string') {
        msg = detail
      } else if (Array.isArray(detail)) {
        msg = detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ')
      } else if (detail && typeof detail === 'object') {
        msg = detail.msg || JSON.stringify(detail)
      } else {
        msg = err.message || 'Execution failed'
      }
      setExecuteResult({ success: false, message: msg })
    } finally {
      setExecutingSymbol(null)
    }
  }

  // Sort and filter results: futures-first, then by confidence
  const sortedResults = batchResult?.results
    ?.filter(r => !r.error)
    ?.sort((a, b) => {
      // Strong signals first (buy/sell before hold)
      const aActive = a.action === 'buy' || a.action === 'sell' ? 1 : 0
      const bActive = b.action === 'buy' || b.action === 'sell' ? 1 : 0
      if (bActive !== aActive) return bActive - aActive
      // Higher confidence first
      return b.confidence - a.confidence
    }) || []

  const errorResults = batchResult?.results?.filter(r => r.error) || []

  // One request covers every pair on the page; each pair's plan reconciles all
  // of that pair's live signals rather than one per row.
  const { planFor } = useResearchPlans(sortedResults.map((r) => r.symbol))

  if (!mounted) return null

  return (
    <>
      <Head><title>TradeBot - Signals</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        {/* Page header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white flex items-center gap-2">
              <Zap className="w-7 h-7 text-yellow-400" /> Autonomous Signals
              {tradingMode === 'sim' ? (
                <span className="text-sm font-medium bg-purple-500/20 border border-purple-500/40 text-purple-300 px-3 py-1 rounded-full">
                  SIM
                </span>
              ) : (
                <span className="text-sm font-medium bg-green-500/20 border border-green-500/40 text-green-300 px-3 py-1 rounded-full">
                  LIVE
                </span>
              )}
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Generate trading signals from technical analysis + sentiment — execute as futures or spot.
            </p>
          </div>
        </div>

        {/* Execute result toast */}
        {executeResult && (
          <div className={`border rounded-lg p-3 text-sm ${
            executeResult.success
              ? 'bg-green-500/10 border-green-500/30 text-green-300'
              : 'bg-red-500/10 border-red-500/30 text-red-300'
          }`}>
            <div className="flex items-start gap-2">
              {executeResult.success
                ? <CheckCircle className="w-4 h-4 shrink-0 mt-0.5" />
                : <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
              }
              <span>{executeResult.message}</span>
              <button onClick={() => setExecuteResult(null)} className="ml-auto text-gray-400 hover:text-white">×</button>
            </div>
          </div>
        )}

        {/* AI Agents Pipeline Status */}
        {aiStatus && (
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <Brain className="w-4 h-4 text-purple-400" /> AI Agents Pipeline
              </h3>
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500">Model: {aiStatus.model}</span>
                <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                  aiStatus.ai_enabled ? 'bg-green-500/20 text-green-400 border border-green-500/30' : 'bg-red-500/20 text-red-400 border border-red-500/30'
                }`}>
                  {aiStatus.ai_enabled ? 'AI Active' : 'AI Disabled'}
                </span>
              </div>
            </div>
            {aiStatus.ai_enabled && (
              <>
                <div className="flex items-center gap-1 mb-3 overflow-x-auto">
                  {['Market Analyst', 'Sentiment Analyst', 'Signal Generator', 'Risk Manager', 'Trade Executor'].map((name, i) => {
                    const agent = aiAgents.find(a => a.name === name)
                    const isActive = agent?.is_active
                    return (
                      <div key={name} className="flex items-center gap-1 shrink-0">
                        {i > 0 && <ArrowRight className="w-3 h-3 text-gray-600" />}
                        <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
                          isActive
                            ? 'bg-purple-500/15 border border-purple-500/30 text-purple-300'
                            : 'bg-gray-800 border border-gray-700 text-gray-500'
                        }`}>
                          <span className={`w-1.5 h-1.5 rounded-full ${isActive ? 'bg-green-400' : 'bg-gray-600'}`} />
                          {name}
                        </div>
                      </div>
                    )
                  })}
                </div>
                <div className="flex items-center gap-4 text-xs text-gray-500">
                  <span>{aiStatus.active_agents}/{aiStatus.total_agents} agents active</span>
                  {aiStatus.learning?.win_rate !== undefined && (
                    <span>Win rate: <span className="text-green-400 font-medium">{(aiStatus.learning.win_rate * 100).toFixed(0)}%</span></span>
                  )}
                  {aiStatus.learning?.total_decisions > 0 && (
                    <span>{aiStatus.learning.total_decisions} decisions</span>
                  )}
                </div>
              </>
            )}
          </div>
        )}

        {/* Generate Section */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5 space-y-4">
          <h2 className="font-semibold text-white flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-400" /> Signal Generator
          </h2>

          {/* Pair selection — searchable with chip list */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs text-gray-400">
                Monitoring Pairs ({selectedPairs.length})
                {savingPairs && <span className="ml-2 text-yellow-400">saving...</span>}
              </label>
              <div className="flex gap-2">
                <button
                  onClick={addDefaults}
                  className="text-[10px] px-2 py-0.5 rounded bg-blue-600/20 text-blue-300 border border-blue-500/30 hover:bg-blue-600/30"
                >
                  + Defaults
                </button>
                {selectedPairs.length > 0 && (
                  <button
                    onClick={clearAllPairs}
                    className="text-[10px] px-2 py-0.5 rounded bg-red-600/20 text-red-300 border border-red-500/30 hover:bg-red-600/30"
                  >
                    Clear All
                  </button>
                )}
              </div>
            </div>

            {/* Search input with dropdown */}
            <div className="relative mb-2" ref={pairDropdownRef}>
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                <input
                  type="text"
                  value={pairSearch}
                  onChange={e => {
                    setPairSearch(e.target.value)
                    setShowPairDropdown(true)
                  }}
                  onFocus={() => setShowPairDropdown(true)}
                  placeholder={availablePairsLoading ? 'Loading pairs...' : `Search ${availablePairs.length || ''} pairs to add (BTC, ETH, SOL...)`}
                  className="w-full bg-gray-900 border border-gray-700 rounded pl-8 pr-3 py-2 text-xs text-white focus:border-blue-500 outline-none"
                />
              </div>
              {showPairDropdown && (
                <div className="absolute z-50 w-full mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl max-h-60 overflow-y-auto">
                  {(() => {
                    const q = pairSearch.toUpperCase()
                    const selected = new Set(selectedPairs)
                    const filtered = (availablePairs.length > 0 ? availablePairs : DEFAULT_PAIRS.map(p => ({ symbol: p, baseCoin: p.split('/')[0], quoteCoin: 'USDT', market: 'spot', status: 'online', delisting_ts: null, delisting_date: null, minLever: null, maxLever: null })))
                      .filter(p => (!q || p.symbol.toUpperCase().includes(q) || p.baseCoin.toUpperCase().includes(q)) && !selected.has(p.symbol))
                      .slice(0, 100)
                    if (filtered.length === 0) {
                      return (
                        <div className="p-3 text-xs text-gray-500 text-center">
                          {pairSearch ? 'No matching pairs' : 'All pairs already added'}
                        </div>
                      )
                    }
                    return filtered.map(pair => (
                      <button
                        key={pair.symbol}
                        onClick={() => {
                          addPair(pair.symbol)
                          setPairSearch('')
                          setShowPairDropdown(false)
                        }}
                        className="w-full text-left px-3 py-2 text-xs transition flex items-center justify-between gap-2 hover:bg-gray-700 text-white"
                      >
                        <div className="flex items-center gap-2">
                          <Plus className="w-3 h-3 text-green-400" />
                          <span className="font-medium">{pair.symbol}</span>
                          <span className={`text-[10px] px-1 py-0.5 rounded ${
                            pair.market === 'both' ? 'bg-purple-500/20 text-purple-300' :
                            pair.market === 'futures' ? 'bg-orange-500/20 text-orange-300' :
                            'bg-blue-500/20 text-blue-300'
                          }`}>
                            {pair.market === 'both' ? 'S+F' : pair.market === 'futures' ? 'FUT' : 'SPOT'}
                          </span>
                        </div>
                        {pair.maxLever && (
                          <span className="text-[10px] text-gray-500">{pair.maxLever}x</span>
                        )}
                      </button>
                    ))
                  })()}
                </div>
              )}
            </div>

            {/* Selected pairs chip list */}
            {selectedPairs.length > 0 && (
              <div>
                <div className={`flex flex-wrap gap-1.5 ${!showAllPairs && selectedPairs.length > 20 ? 'max-h-24 overflow-hidden' : ''}`}>
                  {selectedPairs.map(pair => (
                    <span
                      key={pair}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-[11px] font-medium bg-blue-600/20 border border-blue-500/40 text-blue-300"
                    >
                      {pair.replace('/USDT', '')}
                      <button
                        onClick={() => removePair(pair)}
                        className="ml-0.5 hover:text-red-400 transition"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </span>
                  ))}
                </div>
                {selectedPairs.length > 20 && (
                  <button
                    onClick={() => setShowAllPairs(!showAllPairs)}
                    className="text-[10px] text-blue-400 mt-1 hover:text-blue-300"
                  >
                    {showAllPairs ? 'Show less' : `Show all ${selectedPairs.length} pairs`}
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Timeframe + Market Type + Generate */}
          <div className="flex items-center gap-3">
            <div>
              <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
              <select
                value={timeframe}
                onChange={e => setTimeframe(e.target.value)}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-white"
              >
                {TIMEFRAMES.map(tf => (
                  <option key={tf} value={tf}>{tf}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-400 block mb-1">Execute As</label>
              <div className="flex bg-gray-900 rounded overflow-hidden border border-gray-700">
                {(['futures', 'spot', 'both'] as const).map(mt => (
                  <button
                    key={mt}
                    onClick={() => setMarketType(mt)}
                    className={`px-3 py-1.5 text-xs font-semibold transition ${
                      marketType === mt
                        ? mt === 'futures' ? 'bg-orange-600 text-white'
                          : mt === 'spot' ? 'bg-blue-600 text-white'
                            : 'bg-gray-600 text-white'
                        : 'text-gray-400 hover:text-gray-200'
                    }`}
                  >
                    {mt.charAt(0).toUpperCase() + mt.slice(1)}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex-1" />
            <button
              onClick={generateSignals}
              disabled={generating || selectedPairs.length === 0}
              className="flex items-center gap-2 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500 text-white px-5 py-2 rounded-lg font-medium text-sm transition"
            >
              {generating ? (
                <RefreshCw className="w-4 h-4 animate-spin" />
              ) : (
                <Zap className="w-4 h-4" />
              )}
              {generating
                ? `Analyzing ${selectedPairs.length} pair${selectedPairs.length > 1 ? 's' : ''}…`
                : `Analyze & Generate (${selectedPairs.length})`}
            </button>
          </div>
        </div>

        {/* Analysis Results */}
        {batchResult && (
          <div className="space-y-4">
            <div className="flex items-center gap-4 text-sm">
              <span className="text-gray-400">
                {batchResult.timeframe} analysis
              </span>
              <span className="text-green-400">{batchResult.generated} signals</span>
              {batchResult.errors > 0 && (
                <span className="text-red-400">{batchResult.errors} errors</span>
              )}
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                marketType === 'futures' ? 'bg-orange-500/20 text-orange-400'
                  : marketType === 'spot' ? 'bg-blue-500/20 text-blue-400'
                    : 'bg-gray-500/20 text-gray-300'
              }`}>
                {marketType === 'both' ? 'Futures priority' : marketType}
              </span>
            </div>

            {/* Futures signals first */}
            {(marketType === 'futures' || marketType === 'both') && sortedResults.length > 0 && (
              <div className="space-y-3">
                {marketType === 'both' && (
                  <h3 className="text-sm font-semibold text-orange-400 flex items-center gap-2">
                    <Zap className="w-4 h-4" /> Futures Signals
                  </h3>
                )}
                <div className="grid gap-4 md:grid-cols-2">
                  {sortedResults.map((r, i) => (
                    <AnalysisCard
                      key={`futures-${r.symbol}`}
                      result={r}
                      marketType="futures"
                      plan={planFor(r.symbol)}
                      onExecute={executeSignal}
                      executing={executingSymbol === r.symbol}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Spot signals (only in 'spot' or 'both' mode) */}
            {(marketType === 'spot' || marketType === 'both') && sortedResults.length > 0 && (
              <div className="space-y-3">
                {marketType === 'both' && (
                  <h3 className="text-sm font-semibold text-blue-400 flex items-center gap-2 mt-4">
                    <Activity className="w-4 h-4" /> Spot Signals
                  </h3>
                )}
                <div className="grid gap-4 md:grid-cols-2">
                  {sortedResults.map((r, i) => (
                    <AnalysisCard
                      key={`spot-${r.symbol}`}
                      result={r}
                      marketType="spot"
                      plan={planFor(r.symbol)}
                      onExecute={marketType === 'spot' ? executeSignal : undefined}
                      executing={executingSymbol === r.symbol}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Errors */}
            {errorResults.length > 0 && (
              <div className="grid gap-4 md:grid-cols-2">
                {errorResults.map((r, i) => (
                  <div key={i} className="border border-red-500/30 bg-red-500/5 rounded-lg p-4">
                    <span className="font-mono text-white">{r.symbol}</span>
                    <p className="text-xs text-red-400 mt-1">{r.error}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Signal History */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <SignalFeed />
        </div>

        {/* TradingView Webhook (collapsed) */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg">
          <button
            onClick={() => setWebhookOpen(!webhookOpen)}
            className="w-full flex items-center justify-between p-4 text-left"
          >
            <span className="text-sm text-gray-400">TradingView Webhook (optional)</span>
            {webhookOpen ? <ChevronUp className="w-4 h-4 text-gray-500" /> : <ChevronDown className="w-4 h-4 text-gray-500" />}
          </button>
          {webhookOpen && (
            <div className="px-4 pb-4">
              <div className="bg-gray-900 rounded p-3 font-mono text-xs text-gray-300 break-all">
                POST {apiBaseUrl}/signals/tradingview/webhook
              </div>
              <p className="text-xs text-gray-500 mt-2">
                Configure this URL in your TradingView alerts. Payloads are validated with HMAC-SHA256.
              </p>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
