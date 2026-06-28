import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  Brain,
  Search,
  ArrowRight,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Loader2,
  Target,
  Shield,
  TrendingUp,
  TrendingDown,
  Minus,
  Send,
} from 'lucide-react'

interface Decision {
  id: number
  symbol: string
  timeframe: string | null
  direction: string
  entry: number | null
  sl: number | null
  tp: number | null
  confidence: number | null
  rationale: string | null
  invalidation: string | null
  signals: { name: string; value: string; weight: number }[]
  status: string
  blocked_reasons: string[] | null
  warnings: string[]
  model: string | null
}

interface Overlay {
  proposed_entry: { price: number; color: string; title: string } | null
  sl_line: { price: number; color: string; title: string } | null
  tp_line: { price: number; color: string; title: string } | null
  direction: string
  confidence: number | null
  status: string
}

interface Settings {
  mode: string
  lot_mode: string
  lot_size: number | null
  paper_mode: boolean
  auto_place: boolean
  max_open_positions: number
  selected_agent_id: number | null
}

export default function AIAnalysisPage() {
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [timeframe, setTimeframe] = useState('H1')
  const [analyzing, setAnalyzing] = useState(false)
  const [proposing, setProposing] = useState(false)
  const [placing, setPlacing] = useState(false)
  const [decision, setDecision] = useState<Decision | null>(null)
  const [overlay, setOverlay] = useState<Overlay | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchSettings = useCallback(async () => {
    try {
      const res = await apiClient.aiAnalyst.getSettings()
      setSettings(res.data)
    } catch {}
  }, [])

  useEffect(() => { fetchSettings() }, [fetchSettings])

  const handleAnalyze = async () => {
    setAnalyzing(true)
    setError(null)
    try {
      const res = await apiClient.aiAnalyst.analyze({ symbol, timeframe })
      setDecision(res.data.decision)
      setOverlay(res.data.overlay)
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setAnalyzing(false)
    }
  }

  const handlePropose = async () => {
    setProposing(true)
    setError(null)
    try {
      const res = await apiClient.aiAnalyst.proposeLimit({ symbol, timeframe })
      setDecision(res.data.decision)
      setOverlay(res.data.overlay)
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setProposing(false)
    }
  }

  const handlePlace = async () => {
    if (!decision) return
    setPlacing(true)
    setError(null)
    try {
      await apiClient.aiAnalyst.placeLimit({ decision_id: decision.id })
      setDecision(prev => prev ? { ...prev, status: 'placed' } : null)
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setPlacing(false)
    }
  }

  const dirIcon = decision?.direction === 'buy'
    ? <TrendingUp className="w-5 h-5 text-green-400" />
    : decision?.direction === 'sell'
    ? <TrendingDown className="w-5 h-5 text-red-400" />
    : <Minus className="w-5 h-5 text-gray-400" />

  return (
    <>
      <Head><title>AI Analysis | TradeBot</title></Head>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center gap-3">
          <Brain className="w-6 h-6 text-purple-400" />
          <h1 className="text-2xl font-bold text-white">AI Market Analyst</h1>
          {settings?.paper_mode && (
            <span className="px-2 py-0.5 bg-yellow-900/30 text-yellow-400 rounded text-xs">PAPER MODE</span>
          )}
        </div>

        {error && (
          <div className="flex items-center gap-2 p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm">
            <AlertTriangle className="w-4 h-4" /> {error}
          </div>
        )}

        {/* Input */}
        <div className="flex items-end gap-3">
          <div>
            <label className="text-gray-400 text-xs mb-1 block">Symbol</label>
            <input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())}
              className="bg-gray-900 border border-gray-700 rounded-lg px-4 py-2.5 text-white w-48" />
          </div>
          <div>
            <label className="text-gray-400 text-xs mb-1 block">Timeframe</label>
            <select value={timeframe} onChange={e => setTimeframe(e.target.value)}
              className="bg-gray-900 border border-gray-700 rounded-lg px-4 py-2.5 text-white">
              {['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'].map(tf => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
          <button onClick={handleAnalyze} disabled={analyzing}
            className="flex items-center gap-2 px-5 py-2.5 bg-purple-600/20 text-purple-400 rounded-lg hover:bg-purple-600/30 transition-colors disabled:opacity-50">
            {analyzing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
            Analyze
          </button>
          <button onClick={handlePropose} disabled={proposing}
            className="flex items-center gap-2 px-5 py-2.5 bg-tradebot-accent/20 text-tradebot-accent rounded-lg hover:bg-tradebot-accent/30 transition-colors disabled:opacity-50">
            {proposing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Target className="w-4 h-4" />}
            Propose Limit
          </button>
        </div>

        {/* Decision Card */}
        {decision && (
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl p-6 space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                {dirIcon}
                <span className="text-white text-lg font-bold">{decision.symbol}</span>
                <span className={`text-sm font-medium ${
                  decision.direction === 'buy' ? 'text-green-400' : decision.direction === 'sell' ? 'text-red-400' : 'text-gray-400'
                }`}>{decision.direction.toUpperCase()}</span>
                {decision.timeframe && (
                  <span className="text-gray-500 text-sm">{decision.timeframe}</span>
                )}
              </div>
              <div className="flex items-center gap-3">
                {decision.confidence != null && (
                  <div className={`text-sm font-bold ${
                    decision.confidence >= 70 ? 'text-green-400' : decision.confidence >= 40 ? 'text-yellow-400' : 'text-red-400'
                  }`}>
                    {decision.confidence}% confidence
                  </div>
                )}
                <span className={`px-2 py-0.5 rounded text-xs ${
                  decision.status === 'proposed' ? 'bg-blue-900/30 text-blue-400' :
                  decision.status === 'placed' ? 'bg-green-900/30 text-green-400' :
                  decision.status === 'blocked' ? 'bg-red-900/30 text-red-400' :
                  'bg-gray-700 text-gray-400'
                }`}>{decision.status}</span>
              </div>
            </div>

            {/* Levels */}
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-gray-900/50 rounded-lg p-3">
                <span className="text-gray-500 text-xs">Entry</span>
                <div className="text-white font-mono font-medium">{decision.entry ?? '-'}</div>
              </div>
              <div className="bg-gray-900/50 rounded-lg p-3">
                <span className="text-red-400 text-xs">Stop Loss</span>
                <div className="text-red-400 font-mono font-medium">{decision.sl ?? '-'}</div>
              </div>
              <div className="bg-gray-900/50 rounded-lg p-3">
                <span className="text-green-400 text-xs">Take Profit</span>
                <div className="text-green-400 font-mono font-medium">{decision.tp ?? '-'}</div>
              </div>
            </div>

            {/* Rationale */}
            {decision.rationale && (
              <div>
                <span className="text-gray-400 text-xs">Rationale</span>
                <p className="text-gray-300 text-sm mt-1">{decision.rationale}</p>
              </div>
            )}
            {decision.invalidation && (
              <div>
                <span className="text-yellow-400 text-xs">Invalidation</span>
                <p className="text-gray-300 text-sm mt-1">{decision.invalidation}</p>
              </div>
            )}

            {/* Signals */}
            {decision.signals && decision.signals.length > 0 && (
              <div>
                <span className="text-gray-400 text-xs">Signals</span>
                <div className="flex flex-wrap gap-2 mt-1">
                  {decision.signals.map((s, i) => (
                    <span key={i} className="px-2 py-1 bg-gray-900 rounded text-xs text-gray-300">
                      {s.name}: {s.value} <span className="text-gray-500">({(s.weight * 100).toFixed(0)}%)</span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Blocked reasons */}
            {decision.blocked_reasons && decision.blocked_reasons.length > 0 && (
              <div className="p-3 bg-red-900/20 border border-red-700/30 rounded-lg">
                <div className="flex items-center gap-2 text-red-400 text-sm font-medium mb-1">
                  <Shield className="w-4 h-4" /> Risk Policy Blocked
                </div>
                <ul className="text-red-300 text-xs space-y-0.5">
                  {decision.blocked_reasons.map((r, i) => <li key={i}>- {r}</li>)}
                </ul>
              </div>
            )}

            {/* Place button */}
            {decision.status === 'proposed' && (
              <button onClick={handlePlace} disabled={placing}
                className="flex items-center gap-2 px-5 py-2.5 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors disabled:opacity-50">
                {placing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                Place Limit Order
              </button>
            )}
          </div>
        )}
      </div>
    </>
  )
}
