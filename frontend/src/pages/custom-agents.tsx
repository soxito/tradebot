import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  Cpu,
  Power,
  RefreshCw,
  Play,
  AlertTriangle,
  CheckCircle,
  XCircle,
  ChevronDown,
  ChevronUp,
  TrendingUp,
  Radio,
  Shield,
  Brain,
  Zap,
  Eye,
  Loader2,
  BarChart3,
  Activity,
} from 'lucide-react'

interface RoleLearning {
  total_decisions: number
  with_outcome: number
  wins: number
  losses: number
  break_even: number
  win_rate: number
  accuracy: number
  total_pnl: number
  ai_calls: number
  local_decisions: number
  local_pct: number
}

interface CustomAgent {
  name: string
  role: string
  type: string
}

interface CustomStatus {
  enabled: boolean
  total_custom_decisions: number
  role_learning: Record<string, RoleLearning>
  agents: CustomAgent[]
}

interface TestResult {
  approved: boolean
  reasoning: string
  confidence?: number
  risk_level?: string
  order_type?: string
  amount_usdt?: number
  stop_loss?: number
  take_profit?: number
  [key: string]: any
}

const ROLE_META: Record<string, { icon: any; color: string; bg: string; label: string }> = {
  market_analyst: { icon: TrendingUp, color: 'text-blue-400', bg: 'bg-blue-500/15', label: 'Market Analyst' },
  signal_generator: { icon: Radio, color: 'text-green-400', bg: 'bg-green-500/15', label: 'Signal Generator' },
  risk_manager: { icon: Shield, color: 'text-red-400', bg: 'bg-red-500/15', label: 'Risk Manager' },
  sentiment_analyst: { icon: Brain, color: 'text-purple-400', bg: 'bg-purple-500/15', label: 'Sentiment Analyst' },
  trade_executor: { icon: Zap, color: 'text-orange-400', bg: 'bg-orange-500/15', label: 'Trade Executor' },
  position_reviewer: { icon: Eye, color: 'text-cyan-400', bg: 'bg-cyan-500/15', label: 'Position Reviewer' },
}

export default function CustomAgentsPage() {
  const [status, setStatus] = useState<CustomStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [toggling, setToggling] = useState(false)

  // Test state
  const [testSymbol, setTestSymbol] = useState('BTC/USDT')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [expandedRole, setExpandedRole] = useState<string | null>(null)

  const fetchStatus = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiClient.getCustomAgentStatus()
      setStatus(res.data)
      setError(null)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to fetch custom agent status')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    const interval = setInterval(fetchStatus, 30000)
    return () => clearInterval(interval)
  }, [fetchStatus])

  const handleToggle = async () => {
    setToggling(true)
    try {
      const res = await apiClient.toggleCustomAgents()
      setStatus(prev => prev ? { ...prev, enabled: res.data.custom_agents_enabled } : prev)
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to toggle custom agents')
    } finally {
      setToggling(false)
    }
  }

  const runTest = async () => {
    if (!testSymbol.trim()) return
    setTesting(true)
    setTestResult(null)
    try {
      const res = await apiClient.testCustomAgents(testSymbol)
      setTestResult(res.data)
    } catch (err: any) {
      setTestResult({ approved: false, reasoning: err?.response?.data?.detail || err.message || 'Test failed' })
    } finally {
      setTesting(false)
    }
  }

  const fmt = (n: number | null | undefined, dec = 2) =>
    n != null ? Number(n).toFixed(dec) : '—'

  if (loading && !status) {
    return (
      <div className="flex items-center justify-center py-20 text-gray-400">
        <Loader2 className="w-6 h-6 animate-spin mr-2" /> Loading custom agents...
      </div>
    )
  }

  return (
    <>
      <Head><title>Custom Agents | TradeBot</title></Head>

      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Cpu className="w-6 h-6 text-cyan-400" />
            <h1 className="text-2xl font-bold text-white">Custom Agents</h1>
            <span className="text-sm text-gray-400">(Rule-Based AI Replacement)</span>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={fetchStatus}
              disabled={loading}
              className="flex items-center gap-2 px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm text-gray-200 transition"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
            <button
              onClick={handleToggle}
              disabled={toggling}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition ${
                status?.enabled
                  ? 'bg-green-500/20 text-green-400 hover:bg-green-500/30 border border-green-500/50'
                  : 'bg-gray-700 text-gray-300 hover:bg-gray-600 border border-gray-600'
              }`}
            >
              <Power className="w-4 h-4" />
              {toggling ? 'Toggling...' : status?.enabled ? 'Enabled' : 'Disabled'}
            </button>
          </div>
        </div>

        {error && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm flex items-center gap-2">
            <AlertTriangle className="w-4 h-4" /> {error}
          </div>
        )}

        {/* Status Banner */}
        <div className={`p-4 rounded-xl border ${
          status?.enabled
            ? 'bg-green-900/15 border-green-700/50'
            : 'bg-gray-800/60 border-gray-700/50'
        }`}>
          <div className="flex items-center justify-between">
            <div>
              <div className="flex items-center gap-2">
                {status?.enabled ? (
                  <CheckCircle className="w-5 h-5 text-green-400" />
                ) : (
                  <XCircle className="w-5 h-5 text-gray-500" />
                )}
                <span className="text-white font-semibold">
                  {status?.enabled ? 'Custom Agents Active' : 'Custom Agents Inactive'}
                </span>
              </div>
              <p className="text-sm text-gray-400 mt-1">
                {status?.enabled
                  ? 'Custom agents will handle trade validation when AI (OpenAI) is unavailable or disabled.'
                  : 'Enable custom agents to use rule-based trade validation as an AI fallback.'}
              </p>
            </div>
            <div className="text-right">
              <div className="text-2xl font-bold text-white">{status?.total_custom_decisions || 0}</div>
              <div className="text-xs text-gray-400">Total Decisions</div>
            </div>
          </div>
        </div>

        {/* Agent Cards */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {status?.agents?.map(agent => {
            const meta = ROLE_META[agent.role] || { icon: Cpu, color: 'text-gray-400', bg: 'bg-gray-500/15', label: agent.role }
            const Icon = meta.icon
            const learning = status.role_learning?.[agent.role]
            const expanded = expandedRole === agent.role

            return (
              <div key={agent.role} className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden">
                <button
                  onClick={() => setExpandedRole(expanded ? null : agent.role)}
                  className="w-full p-4 text-left hover:bg-gray-700/30 transition"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className={`p-2 rounded-lg ${meta.bg}`}>
                        <Icon className={`w-5 h-5 ${meta.color}`} />
                      </div>
                      <div>
                        <div className="font-semibold text-white text-sm">{agent.name}</div>
                        <div className="text-xs text-gray-400">{agent.type}</div>
                      </div>
                    </div>
                    {expanded ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
                  </div>

                  {/* Quick Stats */}
                  {learning && (
                    <div className="mt-3 grid grid-cols-3 gap-2">
                      <div className="text-center">
                        <div className="text-lg font-bold text-white">{learning.total_decisions}</div>
                        <div className="text-xs text-gray-500">Decisions</div>
                      </div>
                      <div className="text-center">
                        <div className={`text-lg font-bold ${learning.win_rate > 50 ? 'text-green-400' : learning.win_rate > 0 ? 'text-yellow-400' : 'text-gray-400'}`}>
                          {fmt(learning.win_rate, 0)}%
                        </div>
                        <div className="text-xs text-gray-500">Win Rate</div>
                      </div>
                      <div className="text-center">
                        <div className={`text-lg font-bold ${learning.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          ${fmt(learning.total_pnl)}
                        </div>
                        <div className="text-xs text-gray-500">PnL</div>
                      </div>
                    </div>
                  )}
                </button>

                {/* Expanded Learning Details */}
                {expanded && learning && (
                  <div className="border-t border-gray-700/50 p-4 space-y-3">
                    <div className="flex items-center gap-2 text-sm font-semibold text-gray-300 mb-2">
                      <BarChart3 className="w-4 h-4" /> Learning Stats
                    </div>
                    <div className="grid grid-cols-2 gap-3 text-sm">
                      <div className="flex justify-between">
                        <span className="text-gray-400">Total Decisions</span>
                        <span className="text-white">{learning.total_decisions}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">With Outcome</span>
                        <span className="text-white">{learning.with_outcome}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Wins</span>
                        <span className="text-green-400">{learning.wins}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Losses</span>
                        <span className="text-red-400">{learning.losses}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Break Even</span>
                        <span className="text-gray-300">{learning.break_even}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Win Rate</span>
                        <span className={learning.win_rate > 50 ? 'text-green-400' : 'text-yellow-400'}>
                          {fmt(learning.win_rate, 1)}%
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Accuracy</span>
                        <span className="text-white">{fmt(learning.accuracy, 1)}%</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Total PnL</span>
                        <span className={learning.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                          ${fmt(learning.total_pnl)}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">AI Calls</span>
                        <span className="text-blue-400">{learning.ai_calls}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-400">Local Decisions</span>
                        <span className="text-cyan-400">{learning.local_decisions} ({fmt(learning.local_pct, 0)}%)</span>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {/* Test Panel */}
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-6">
          <div className="flex items-center gap-2 mb-4">
            <Play className="w-5 h-5 text-green-400" />
            <h2 className="text-lg font-semibold text-white">Test Custom Pipeline</h2>
            <span className="text-sm text-gray-400 ml-2">Run the full custom agent pipeline without executing a trade</span>
          </div>

          <div className="flex items-center gap-3">
            <input
              type="text"
              value={testSymbol}
              onChange={e => setTestSymbol(e.target.value)}
              placeholder="BTC/USDT"
              className="bg-gray-900 border border-gray-700 rounded-lg px-4 py-2 text-white text-sm w-48 focus:outline-none focus:border-cyan-500"
            />
            <button
              onClick={runTest}
              disabled={testing || !testSymbol.trim()}
              className="flex items-center gap-2 px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:bg-gray-700 disabled:text-gray-500 rounded-lg text-sm text-white font-medium transition"
            >
              {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              {testing ? 'Running...' : 'Run Test'}
            </button>
          </div>

          {/* Test Result */}
          {testResult && (
            <div className={`mt-4 p-4 rounded-lg border ${
              testResult.approved
                ? 'bg-green-900/15 border-green-700/50'
                : 'bg-red-900/15 border-red-700/50'
            }`}>
              <div className="flex items-center gap-2 mb-2">
                {testResult.approved ? (
                  <CheckCircle className="w-5 h-5 text-green-400" />
                ) : (
                  <XCircle className="w-5 h-5 text-red-400" />
                )}
                <span className={`font-semibold ${testResult.approved ? 'text-green-400' : 'text-red-400'}`}>
                  {testResult.approved ? 'APPROVED' : 'REJECTED'}
                </span>
                {testResult.confidence != null && (
                  <span className="text-sm text-gray-400 ml-2">
                    Confidence: {fmt(typeof testResult.confidence === 'number' ? testResult.confidence * 100 : testResult.confidence, 0)}%
                  </span>
                )}
              </div>
              <p className="text-sm text-gray-300 whitespace-pre-wrap">{testResult.reasoning}</p>

              {testResult.approved && (
                <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                  {testResult.risk_level && (
                    <div>
                      <span className="text-gray-400">Risk Level</span>
                      <div className="text-white">{testResult.risk_level}</div>
                    </div>
                  )}
                  {testResult.order_type && (
                    <div>
                      <span className="text-gray-400">Order Type</span>
                      <div className="text-white">{testResult.order_type}</div>
                    </div>
                  )}
                  {testResult.amount_usdt != null && (
                    <div>
                      <span className="text-gray-400">Amount</span>
                      <div className="text-white">${fmt(testResult.amount_usdt)}</div>
                    </div>
                  )}
                  {testResult.stop_loss != null && (
                    <div>
                      <span className="text-gray-400">Stop Loss</span>
                      <div className="text-red-400">${fmt(testResult.stop_loss)}</div>
                    </div>
                  )}
                  {testResult.take_profit != null && (
                    <div>
                      <span className="text-gray-400">Take Profit</span>
                      <div className="text-green-400">${fmt(testResult.take_profit)}</div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* How It Works */}
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-6">
          <h2 className="text-lg font-semibold text-white mb-3 flex items-center gap-2">
            <Activity className="w-5 h-5 text-gray-400" /> How Custom Agents Work
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm text-gray-400">
            <div className="space-y-2">
              <p className="text-gray-300 font-medium">Pipeline (same as AI agents):</p>
              <ol className="list-decimal list-inside space-y-1">
                <li><span className="text-blue-400">Market Analyst</span> — RSI, MACD, EMA, Bollinger Bands, ADX scoring</li>
                <li><span className="text-purple-400">Sentiment Analyst</span> — DB sentiment scores + CMC community data</li>
                <li><span className="text-green-400">Signal Generator</span> — Combines TA + sentiment + learning from past trades</li>
                <li><span className="text-red-400">Risk Manager</span> — Position limits, exposure, losing streaks, historical win rate</li>
                <li><span className="text-orange-400">Trade Executor</span> — Order type selection, SL/TP calculation, amount sizing</li>
              </ol>
            </div>
            <div className="space-y-2">
              <p className="text-gray-300 font-medium">Learning Features:</p>
              <ul className="list-disc list-inside space-y-1">
                <li>Queries past trade outcomes per symbol and role</li>
                <li>Adapts confidence thresholds based on win rate</li>
                <li>Detects losing streaks and reduces position sizes</li>
                <li>Identifies best-performing actions per asset</li>
                <li>Falls back automatically when OpenAI is unavailable</li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}
