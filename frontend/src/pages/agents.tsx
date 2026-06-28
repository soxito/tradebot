import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import AgentProvidersPanel from '@/components/AgentProvidersPanel'
import {
  Bot,
  Plus,
  Trash2,
  Edit3,
  Power,
  Play,
  RefreshCw,
  CheckCircle,
  XCircle,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Brain,
  Shield,
  TrendingUp,
  Radio,
  Zap,
  Eye,
  Loader2,
} from 'lucide-react'
import { formatTimeZA } from '@/utils/datetime'

interface AgentData {
  id: number
  name: string
  role: string
  description: string | null
  system_prompt: string
  model: string
  temperature: number
  max_tokens: number
  is_active: boolean
  pairs: string | null
  created_at: string | null
  updated_at: string | null
}

interface Decision {
  id: number
  agent_id: number
  agent_name: string
  agent_role: string
  symbol: string
  action: string
  confidence: number
  reasoning: string
  signal_id: number | null
  session_id: string | null
  outcome: string | null
  outcome_pnl: number | null
  ai_called: boolean
  memory_context_used: number
  created_at: string | null
}

interface LearningStats {
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

interface AgentStatus {
  ai_enabled: boolean
  openai_configured: boolean
  model: string
  total_agents: number
  active_agents: number
  roles: string[]
  memory_lookback: number
  min_memory_for_local: number
  local_confidence_threshold: number
  learning: LearningStats
}

const ROLE_META: Record<string, { icon: any; color: string; label: string }> = {
  market_analyst: { icon: TrendingUp, color: 'blue', label: 'Market Analyst' },
  signal_generator: { icon: Radio, color: 'green', label: 'Signal Generator' },
  risk_manager: { icon: Shield, color: 'red', label: 'Risk Manager' },
  sentiment_analyst: { icon: Brain, color: 'purple', label: 'Sentiment Analyst' },
  trade_executor: { icon: Zap, color: 'orange', label: 'Trade Executor' },
  position_reviewer: { icon: Eye, color: 'cyan', label: 'Position Reviewer' },
  custom: { icon: Bot, color: 'gray', label: 'Custom' },
}

const MODELS = [
  'gpt-5.1', 'gpt-5', 'gpt-4.1', 'gpt-4.1-mini', 'gpt-4.1-nano',
  'o4-mini', 'o3', 'o3-mini', 'o1', 'o1-mini',
  'gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo',
]

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentData[]>([])
  const [status, setStatus] = useState<AgentStatus | null>(null)
  const [decisions, setDecisions] = useState<Decision[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Form state
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [formData, setFormData] = useState({
    name: '',
    role: 'market_analyst',
    description: '',
    system_prompt: '',
    model: 'gpt-4o-mini',
    temperature: 0.3,
    max_tokens: 2000,
    is_active: true,
    pairs: '',
  })

  // Analysis state
  const [analyzeSymbol, setAnalyzeSymbol] = useState('BTC/USDT')
  const [analyzeTimeframe, setAnalyzeTimeframe] = useState('1h')
  const [analyzing, setAnalyzing] = useState(false)
  const [analysisResult, setAnalysisResult] = useState<any>(null)

  // Expand state for system prompts
  const [expandedAgent, setExpandedAgent] = useState<number | null>(null)
  const [expandedDecision, setExpandedDecision] = useState<number | null>(null)

  // Position monitor state
  const [posMonitorStatus, setPosMonitorStatus] = useState<any>(null)
  const [posMonitorRunning, setPosMonitorRunning] = useState(false)
  const [posReviewResult, setPosReviewResult] = useState<any>(null)
  const [posMonitorInterval, setPosMonitorInterval] = useState(900)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const [agentsRes, statusRes, decisionsRes] = await Promise.all([
        apiClient.getAgents(),
        apiClient.getAgentStatus(),
        apiClient.getAgentDecisions({ limit: 30 }),
      ])
      setAgents(agentsRes.data.agents || [])
      setStatus(statusRes.data)
      setDecisions(decisionsRes.data.decisions || [])
      setError(null)
      // Fetch position monitor status
      try {
        const pmRes = await apiClient.getPositionMonitorStatus()
        setPosMonitorStatus(pmRes.data)
        if (pmRes.data?.interval_seconds) setPosMonitorInterval(pmRes.data.interval_seconds)
      } catch { /* ignore if AI agents disabled */ }
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to fetch agents')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchAll() }, [fetchAll])

  const seedDefaults = async () => {
    try {
      await apiClient.seedDefaultAgents()
      await fetchAll()
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to seed agents')
    }
  }

  const toggleAgent = async (id: number) => {
    try {
      await apiClient.toggleAgent(id)
      setAgents(prev => prev.map(a => a.id === id ? { ...a, is_active: !a.is_active } : a))
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to toggle agent')
    }
  }

  const deleteAgent = async (id: number) => {
    if (!confirm('Delete this agent?')) return
    try {
      await apiClient.deleteAgent(id)
      setAgents(prev => prev.filter(a => a.id !== id))
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to delete agent')
    }
  }

  const openEdit = (agent: AgentData) => {
    setEditingId(agent.id)
    setFormData({
      name: agent.name,
      role: agent.role,
      description: agent.description || '',
      system_prompt: agent.system_prompt,
      model: agent.model,
      temperature: agent.temperature,
      max_tokens: agent.max_tokens,
      is_active: agent.is_active,
      pairs: agent.pairs || '',
    })
    setShowForm(true)
  }

  const openCreate = () => {
    setEditingId(null)
    setFormData({
      name: '',
      role: 'market_analyst',
      description: '',
      system_prompt: '',
      model: 'gpt-4o-mini',
      temperature: 0.3,
      max_tokens: 2000,
      is_active: true,
      pairs: '',
    })
    setShowForm(true)
  }

  const saveAgent = async () => {
    try {
      const payload = {
        ...formData,
        pairs: formData.pairs || null,
      }
      if (editingId) {
        await apiClient.updateAgent(editingId, payload)
      } else {
        await apiClient.createAgent(payload)
      }
      setShowForm(false)
      await fetchAll()
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to save agent')
    }
  }

  const runAnalysis = async () => {
    if (!analyzeSymbol) return
    setAnalyzing(true)
    setAnalysisResult(null)
    try {
      const res = await apiClient.analyzeSymbol({
        symbol: analyzeSymbol,
        timeframe: analyzeTimeframe,
      })
      setAnalysisResult(res.data)
      // Refresh decisions
      const dRes = await apiClient.getAgentDecisions({ limit: 30 })
      setDecisions(dRes.data.decisions || [])
    } catch (err: any) {
      setAnalysisResult({ error: err?.response?.data?.detail || err.message || 'Analysis failed' })
    } finally {
      setAnalyzing(false)
    }
  }

  const toggleAI = async () => {
    try {
      const res = await apiClient.toggleAiAgents()
      setStatus(prev => prev ? { ...prev, ai_enabled: res.data.ai_enabled } : prev)
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to toggle AI')
    }
  }

  const recordOutcome = async (decisionId: number, outcome: string) => {
    try {
      await apiClient.recordDecisionOutcome(decisionId, { outcome })
      setDecisions(prev =>
        prev.map(d => d.id === decisionId ? { ...d, outcome } : d)
      )
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to record outcome')
    }
  }

  const togglePositionMonitor = async () => {
    try {
      if (posMonitorStatus?.running) {
        await apiClient.stopPositionMonitor()
      } else {
        await apiClient.startPositionMonitor(posMonitorInterval)
      }
      const res = await apiClient.getPositionMonitorStatus()
      setPosMonitorStatus(res.data)
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to toggle position monitor')
    }
  }

  const runPositionReview = async () => {
    setPosMonitorRunning(true)
    setPosReviewResult(null)
    try {
      const res = await apiClient.runPositionReview(2.0)
      setPosReviewResult(res.data)
      const dRes = await apiClient.getAgentDecisions({ limit: 30 })
      setDecisions(dRes.data.decisions || [])
    } catch (err: any) {
      setPosReviewResult({ error: err?.response?.data?.detail || err.message || 'Position review failed' })
    } finally {
      setPosMonitorRunning(false)
    }
  }

  const getRoleMeta = (role: string) => ROLE_META[role] || ROLE_META.custom
  const actionColor = (action: string) => {
    if (['buy', 'bullish', 'approve', 'execute'].includes(action)) return 'text-green-400'
    if (['sell', 'bearish', 'reject', 'cancel', 'close'].includes(action)) return 'text-red-400'
    if (['modify', 'wait', 'adjust'].includes(action)) return 'text-yellow-400'
    if (['hold', 'neutral'].includes(action)) return 'text-blue-400'
    return 'text-gray-400'
  }

  return (
    <>
      <Head><title>AI Agents | TradeBot</title></Head>

      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Bot className="w-7 h-7 text-blue-400" /> AI Trading Agents
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Multi-agent system — uses your connected AI providers (Telegram Signals → Connect AI) with
              load-balancing &amp; monthly-tier protection, falling back to OpenAI if none are connected
            </p>
          </div>
          <div className="flex gap-2">
            <button onClick={fetchAll} className="p-2 bg-gray-800 rounded hover:bg-gray-700 transition" title="Refresh">
              <RefreshCw className="w-4 h-4 text-gray-400" />
            </button>
            <button onClick={openCreate} className="flex items-center gap-1 px-3 py-2 bg-blue-600 hover:bg-blue-500 rounded text-sm font-medium transition">
              <Plus className="w-4 h-4" /> New Agent
            </button>
          </div>
        </div>

        {/* AI providers + token budget (connected from Telegram Signals) */}
        <AgentProvidersPanel />

        {/* Status Bar */}
        {status && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
              {/* AI Toggle */}
              <div
                onClick={toggleAI}
                className={`p-3 rounded-lg border cursor-pointer transition hover:opacity-80 ${
                  status.ai_enabled
                    ? 'bg-green-900/20 border-green-500/30'
                    : 'bg-red-900/20 border-red-500/30'
                }`}
              >
                <span className="text-xs text-gray-400 block">AI Agents</span>
                <span className={`text-sm font-bold ${status.ai_enabled ? 'text-green-400' : 'text-red-400'}`}>
                  {status.ai_enabled ? 'ENABLED' : 'DISABLED'}
                </span>
                <span className="text-[10px] text-gray-500 block">click to toggle</span>
              </div>
              <div className={`p-3 rounded-lg border ${status.openai_configured ? 'bg-gray-800/50 border-gray-700/50' : 'bg-red-900/20 border-red-500/30'}`}>
                <span className="text-xs text-gray-400 block">OpenAI API</span>
                <span className={`text-sm font-bold ${status.openai_configured ? 'text-white' : 'text-red-400'}`}>
                  {status.openai_configured ? status.model : 'Not Set'}
                </span>
              </div>
              <div className="p-3 rounded-lg bg-gray-800/50 border border-gray-700/50">
                <span className="text-xs text-gray-400 block">Agents</span>
                <span className="text-sm font-bold text-white">{status.active_agents} / {status.total_agents}</span>
              </div>
              <div className="p-3 rounded-lg bg-gray-800/50 border border-gray-700/50">
                <span className="text-xs text-gray-400 block">Win Rate</span>
                <span className={`text-sm font-bold ${
                  status.learning.win_rate > 0.5 ? 'text-green-400' :
                  status.learning.win_rate > 0.3 ? 'text-yellow-400' : 'text-gray-400'
                }`}>
                  {status.learning.with_outcome > 0
                    ? `${(status.learning.win_rate * 100).toFixed(1)}%`
                    : 'N/A'
                  }
                </span>
                <span className="text-[10px] text-gray-500 block">{status.learning.with_outcome} rated</span>
              </div>
              <div className="p-3 rounded-lg bg-gray-800/50 border border-gray-700/50">
                <span className="text-xs text-gray-400 block">Local vs API</span>
                <span className="text-sm font-bold text-blue-400">
                  {status.learning.local_pct}% local
                </span>
                <span className="text-[10px] text-gray-500 block">
                  {status.learning.local_decisions} / {status.learning.total_decisions} decisions
                </span>
              </div>
            </div>

            {/* Learning summary bar */}
            {status.learning.total_decisions > 0 && (
              <div className="flex items-center gap-3 p-2 bg-gray-800/30 rounded text-xs text-gray-400">
                <Brain className="w-4 h-4 text-purple-400 shrink-0" />
                <span>
                  <strong className="text-white">{status.learning.total_decisions}</strong> total decisions |
                  <span className="text-green-400 ml-1">{status.learning.wins}W</span> /
                  <span className="text-red-400">{status.learning.losses}L</span> /
                  <span className="text-gray-400">{status.learning.break_even}BE</span>
                  {status.learning.total_pnl !== 0 && (
                    <span className={`ml-2 ${status.learning.total_pnl > 0 ? 'text-green-400' : 'text-red-400'}`}>
                      PnL: {status.learning.total_pnl > 0 ? '+' : ''}{status.learning.total_pnl.toFixed(2)} USDT
                    </span>
                  )}
                </span>
                <span className="ml-auto text-gray-500">
                  Memory needs {status.min_memory_for_local} rated decisions to go local
                </span>
              </div>
            )}
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="p-3 bg-red-900/30 border border-red-500/30 rounded-lg flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-red-400" />
            <span className="text-sm text-red-300">{error}</span>
            <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-300">
              <XCircle className="w-4 h-4" />
            </button>
          </div>
        )}

        {/* Seed button if no agents */}
        {!loading && agents.length === 0 && (
          <div className="text-center py-12 bg-gray-800/30 rounded-lg border border-gray-700/50">
            <Bot className="w-12 h-12 text-gray-600 mx-auto mb-3" />
            <p className="text-gray-400 mb-4">No agents configured yet</p>
            <button onClick={seedDefaults} className="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded text-sm font-medium transition">
              Create Default Agents (5)
            </button>
          </div>
        )}

        {/* Agent Cards */}
        {agents.length > 0 && (
          <div className="grid gap-3">
            {agents.map(agent => {
              const meta = getRoleMeta(agent.role)
              const Icon = meta.icon
              const isExpanded = expandedAgent === agent.id
              return (
                <div key={agent.id} className={`bg-gray-800/50 border rounded-lg transition ${agent.is_active ? 'border-gray-700/50' : 'border-gray-800 opacity-60'}`}>
                  <div className="flex items-center gap-3 p-4">
                    {/* Icon */}
                    <div className={`w-10 h-10 rounded-lg flex items-center justify-center bg-${meta.color}-500/15 border border-${meta.color}-500/30`}>
                      <Icon className={`w-5 h-5 text-${meta.color}-400`} />
                    </div>

                    {/* Info */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="font-semibold text-white">{agent.name}</h3>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded-full bg-${meta.color}-500/15 text-${meta.color}-300 border border-${meta.color}-500/30`}>
                          {meta.label}
                        </span>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-400">
                          {agent.model}
                        </span>
                      </div>
                      {agent.description && (
                        <p className="text-xs text-gray-400 mt-0.5 truncate">{agent.description}</p>
                      )}
                      {agent.pairs && (
                        <p className="text-xs text-gray-500 mt-0.5">Pairs: {agent.pairs}</p>
                      )}
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setExpandedAgent(isExpanded ? null : agent.id)}
                        className="p-1.5 hover:bg-gray-700 rounded transition"
                        title="View prompt"
                      >
                        <Eye className="w-4 h-4 text-gray-400" />
                      </button>
                      <button onClick={() => openEdit(agent)} className="p-1.5 hover:bg-gray-700 rounded transition" title="Edit">
                        <Edit3 className="w-4 h-4 text-gray-400" />
                      </button>
                      <button
                        onClick={() => toggleAgent(agent.id)}
                        className={`p-1.5 rounded transition ${agent.is_active ? 'hover:bg-red-900/30 text-green-400' : 'hover:bg-green-900/30 text-gray-500'}`}
                        title={agent.is_active ? 'Disable' : 'Enable'}
                      >
                        <Power className="w-4 h-4" />
                      </button>
                      <button onClick={() => deleteAgent(agent.id)} className="p-1.5 hover:bg-red-900/30 rounded transition" title="Delete">
                        <Trash2 className="w-4 h-4 text-gray-500 hover:text-red-400" />
                      </button>
                    </div>
                  </div>

                  {/* Expanded system prompt */}
                  {isExpanded && (
                    <div className="px-4 pb-4 border-t border-gray-700/50 mt-0">
                      <p className="text-xs text-gray-400 mt-3 mb-1">System Prompt:</p>
                      <pre className="text-xs text-gray-300 bg-gray-900/50 p-3 rounded max-h-48 overflow-auto whitespace-pre-wrap">{agent.system_prompt}</pre>
                      <div className="flex gap-4 mt-2 text-xs text-gray-500">
                        <span>Temperature: {agent.temperature}</span>
                        <span>Max tokens: {agent.max_tokens}</span>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {/* ─── Create / Edit Form ─── */}
        {showForm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-2xl max-h-[90vh] overflow-auto p-6 shadow-xl">
              <h2 className="text-lg font-bold mb-4">{editingId ? 'Edit Agent' : 'Create Agent'}</h2>

              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Name</label>
                    <input
                      value={formData.name}
                      onChange={e => setFormData(p => ({ ...p, name: e.target.value }))}
                      className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                      placeholder="My Agent"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Role</label>
                    <select
                      value={formData.role}
                      onChange={e => setFormData(p => ({ ...p, role: e.target.value }))}
                      className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                    >
                      <option value="market_analyst">Market Analyst</option>
                      <option value="signal_generator">Signal Generator</option>
                      <option value="risk_manager">Risk Manager</option>
                      <option value="sentiment_analyst">Sentiment Analyst</option>
                      <option value="trade_executor">Trade Executor</option>
                      <option value="custom">Custom</option>
                    </select>
                  </div>
                </div>

                <div>
                  <label className="text-xs text-gray-400 block mb-1">Description</label>
                  <input
                    value={formData.description}
                    onChange={e => setFormData(p => ({ ...p, description: e.target.value }))}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                    placeholder="What does this agent do?"
                  />
                </div>

                <div>
                  <label className="text-xs text-gray-400 block mb-1">System Prompt</label>
                  <textarea
                    value={formData.system_prompt}
                    onChange={e => setFormData(p => ({ ...p, system_prompt: e.target.value }))}
                    rows={10}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none font-mono"
                    placeholder="You are an expert crypto trading agent..."
                  />
                </div>

                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Model</label>
                    <select
                      value={formData.model}
                      onChange={e => setFormData(p => ({ ...p, model: e.target.value }))}
                      className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                    >
                      {MODELS.map(m => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Temperature ({formData.temperature})</label>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.1"
                      value={formData.temperature}
                      onChange={e => setFormData(p => ({ ...p, temperature: Number(e.target.value) }))}
                      className="w-full h-1.5 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-blue-500 mt-2"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Max Tokens</label>
                    <input
                      type="number"
                      value={formData.max_tokens}
                      onChange={e => setFormData(p => ({ ...p, max_tokens: Number(e.target.value) }))}
                      className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                    />
                  </div>
                </div>

                <div>
                  <label className="text-xs text-gray-400 block mb-1">Pairs (comma-separated, leave empty for all)</label>
                  <input
                    value={formData.pairs}
                    onChange={e => setFormData(p => ({ ...p, pairs: e.target.value }))}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                    placeholder="BTC/USDT, ETH/USDT"
                  />
                </div>
              </div>

              <div className="flex justify-end gap-2 mt-5">
                <button onClick={() => setShowForm(false)} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-sm transition">
                  Cancel
                </button>
                <button
                  onClick={saveAgent}
                  disabled={!formData.name || !formData.system_prompt}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded text-sm font-medium transition"
                >
                  {editingId ? 'Update' : 'Create'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ─── Analyze Panel ─── */}
        <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-4">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Play className="w-4 h-4 text-green-400" /> Run Agent Analysis
          </h2>
          <div className="flex flex-wrap gap-3 items-end">
            <div>
              <label className="text-xs text-gray-400 block mb-1">Symbol</label>
              <input
                value={analyzeSymbol}
                onChange={e => setAnalyzeSymbol(e.target.value.toUpperCase())}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none w-36"
                placeholder="BTC/USDT"
              />
            </div>
            <div>
              <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
              <select
                value={analyzeTimeframe}
                onChange={e => setAnalyzeTimeframe(e.target.value)}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
              >
                {['5m', '15m', '1h', '4h', '1d'].map(tf => <option key={tf} value={tf}>{tf}</option>)}
              </select>
            </div>
            <button
              onClick={runAnalysis}
              disabled={analyzing || !analyzeSymbol || agents.filter(a => a.is_active).length === 0 || !status?.ai_enabled}
              className="flex items-center gap-1.5 px-4 py-2 bg-green-600 hover:bg-green-500 disabled:opacity-50 rounded text-sm font-medium transition"
              title={!status?.ai_enabled ? 'Enable AI agents first' : ''}
            >
              {analyzing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              {analyzing ? 'Analyzing...' : 'Analyze'}
            </button>
          </div>

          {/* Analysis result */}
          {analysisResult && (
            <div className="mt-4 p-3 bg-gray-900/50 rounded-lg border border-gray-700/50">
              {analysisResult.error ? (
                <div className="text-red-400 text-sm">{analysisResult.error}</div>
              ) : (
                <div className="space-y-3">
                  <div className="flex items-center gap-3 flex-wrap">
                    <span className="text-sm font-bold text-white">{analysisResult.symbol}</span>
                    <span className={`text-sm font-bold ${actionColor(analysisResult.final_action)}`}>
                      {analysisResult.final_action?.toUpperCase()}
                    </span>
                    <span className="text-xs text-gray-500">Session: {analysisResult.session_id}</span>
                    <span className="text-xs text-gray-500">{analysisResult.agents_used} agents</span>
                    {analysisResult.ai_calls != null && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-300 border border-blue-500/30">
                        {analysisResult.ai_calls} API | {analysisResult.local_decisions} local
                      </span>
                    )}
                  </div>

                  {analysisResult.signal && (
                    <div className="p-2 bg-green-900/20 border border-green-500/30 rounded text-xs text-green-300">
                      Signal #{analysisResult.signal.id} created → {analysisResult.signal.action.toUpperCase()} {analysisResult.signal.symbol}
                    </div>
                  )}

                  {/* Decision chain */}
                  {analysisResult.decisions?.map((d: any, i: number) => {
                    const meta = getRoleMeta(d.agent_role)
                    return (
                      <div key={i} className="flex items-start gap-2 p-2 bg-gray-800/50 rounded">
                        <div className={`w-6 h-6 rounded flex items-center justify-center bg-${meta.color}-500/15 shrink-0 mt-0.5`}>
                          <meta.icon className={`w-3.5 h-3.5 text-${meta.color}-400`} />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-xs font-semibold text-white">{d.agent_name || d.agent_role}</span>
                            <span className={`text-xs font-bold ${actionColor(d.action)}`}>{d.action?.toUpperCase()}</span>
                            {d.confidence != null && (
                              <span className="text-[10px] text-gray-500">{(d.confidence * 100).toFixed(0)}% conf</span>
                            )}
                            <span className={`text-[10px] px-1 py-0.5 rounded ${
                              d.ai_called === false
                                ? 'bg-purple-500/15 text-purple-300 border border-purple-500/30'
                                : 'bg-gray-700 text-gray-400'
                            }`}>
                              {d.ai_called === false ? 'LOCAL' : 'API'}
                            </span>
                          </div>
                          {d.reasoning && (
                            <p className="text-xs text-gray-400 mt-0.5">{d.reasoning}</p>
                          )}
                        </div>
                      </div>
                    )
                  })}

                  {analysisResult.errors?.length > 0 && (
                    <div className="text-xs text-red-400">
                      Errors: {analysisResult.errors.join(', ')}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* ─── Position Monitor Panel ─── */}
        <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Eye className="w-4 h-4 text-cyan-400" /> Position Monitor
              {posMonitorStatus?.running && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-500/15 text-green-300 border border-green-500/30 animate-pulse">
                  ACTIVE
                </span>
              )}
            </h2>
            <div className="flex gap-2">
              <button
                onClick={togglePositionMonitor}
                disabled={!status?.ai_enabled}
                className={`flex items-center gap-1 px-3 py-1.5 rounded text-xs font-medium transition ${
                  posMonitorStatus?.running
                    ? 'bg-red-600 hover:bg-red-500'
                    : 'bg-cyan-600 hover:bg-cyan-500'
                } disabled:opacity-50`}
              >
                <Power className="w-3.5 h-3.5" />
                {posMonitorStatus?.running ? 'Stop' : 'Start'} Monitor
              </button>
              <button
                onClick={runPositionReview}
                disabled={posMonitorRunning || !status?.ai_enabled}
                className="flex items-center gap-1 px-3 py-1.5 bg-cyan-700 hover:bg-cyan-600 disabled:opacity-50 rounded text-xs font-medium transition"
              >
                {posMonitorRunning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                {posMonitorRunning ? 'Reviewing...' : 'Review Now'}
              </button>
            </div>
          </div>
          <p className="text-xs text-gray-400 mb-2">
            Analyzes open positions at the selected interval — decides whether to HOLD, CLOSE, or ADJUST (trail SL/TP).
          </p>
          <div className="flex items-center gap-3 mb-2">
            <label className="text-xs text-gray-400">Interval:</label>
            <select
              value={posMonitorInterval}
              onChange={e => setPosMonitorInterval(Number(e.target.value))}
              className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-white"
            >
              <option value={60}>1 min</option>
              <option value={180}>3 min</option>
              <option value={300}>5 min</option>
              <option value={600}>10 min</option>
              <option value={900}>15 min</option>
              <option value={1800}>30 min</option>
              <option value={3600}>1 hour</option>
              <option value={7200}>2 hours</option>
              <option value={10800}>3 hours</option>
              <option value={14400}>4 hours</option>
            </select>
            {posMonitorStatus?.running && posMonitorStatus.interval_seconds !== posMonitorInterval && (
              <span className="text-[10px] text-amber-400">Restart monitor to apply</span>
            )}
          </div>
          {posMonitorStatus && (
            <div className="flex gap-4 text-xs text-gray-400">
              <span>Interval: {posMonitorStatus.interval_seconds
                ? posMonitorStatus.interval_seconds < 3600
                  ? `${Math.round(posMonitorStatus.interval_seconds / 60)}m`
                  : `${(posMonitorStatus.interval_seconds / 3600).toFixed(1)}h`
                : 'N/A'}</span>
              {posMonitorStatus.started_at && <span>Started: {formatTimeZA(posMonitorStatus.started_at)}</span>}
              {posMonitorStatus.last_run && (
                <span>
                  Last: {posMonitorStatus.last_run.status === 'ok'
                    ? `${posMonitorStatus.last_run.positions_reviewed || 0} reviewed, ${posMonitorStatus.last_run.actions_taken || 0} actions`
                    : `Error: ${posMonitorStatus.last_run.error?.slice(0, 60)}`}
                </span>
              )}
            </div>
          )}

          {/* Position Review Result */}
          {posReviewResult && (
            <div className="mt-3 p-3 bg-gray-900/50 rounded-lg border border-gray-700/50">
              {posReviewResult.error ? (
                <div className="text-red-400 text-sm">{posReviewResult.error}</div>
              ) : posReviewResult.skipped ? (
                <div className="text-gray-400 text-sm">{posReviewResult.reason}</div>
              ) : (
                <div className="space-y-2">
                  <div className="text-xs text-gray-400">
                    Reviewed {posReviewResult.positions_reviewed} position(s) — {posReviewResult.actions_taken?.length || 0} action(s) taken
                  </div>
                  {posReviewResult.reviews?.map((r: any, i: number) => (
                    <div key={i} className="p-2 bg-gray-800/50 rounded border border-gray-700/30">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-bold text-white">{r.symbol}</span>
                        <span className={`text-xs font-bold ${actionColor(r.review_action)}`}>
                          {r.review_action?.toUpperCase()}
                        </span>
                        <span className={`text-xs ${r.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {r.pnl_pct >= 0 ? '+' : ''}{r.pnl_pct?.toFixed(2)}%
                        </span>
                        <span className="text-[10px] text-gray-500">held {r.hold_duration_hours?.toFixed(1)}h</span>
                        <span className={`text-[10px] px-1 py-0.5 rounded ${
                          r.urgency === 'high' ? 'bg-red-500/15 text-red-300 border border-red-500/30'
                            : r.urgency === 'medium' ? 'bg-yellow-500/15 text-yellow-300 border border-yellow-500/30'
                            : 'bg-gray-700 text-gray-400'
                        }`}>{r.urgency}</span>
                      </div>
                      {r.reasoning && <p className="text-xs text-gray-400 mt-1">{r.reasoning}</p>}
                      {r.adjusted_sl && <span className="text-[10px] text-yellow-300">New SL: {r.adjusted_sl}</span>}
                      {r.adjusted_tp && <span className="text-[10px] text-yellow-300 ml-2">New TP: {r.adjusted_tp}</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* ─── Recent Decisions ─── */}
        <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg">
          <div className="p-4 border-b border-gray-700/50">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Brain className="w-4 h-4 text-purple-400" /> Recent Agent Decisions
            </h2>
          </div>
          {decisions.length === 0 ? (
            <div className="p-8 text-center text-gray-500 text-sm">
              No decisions yet — run an analysis to see agent reasoning
            </div>
          ) : (
            <div className="divide-y divide-gray-700/50">
              {decisions.map(d => {
                const meta = getRoleMeta(d.agent_role)
                const Icon = meta.icon
                const isExp = expandedDecision === d.id
                return (
                  <div key={d.id} className="p-3 hover:bg-gray-800/30 transition">
                    <div className="flex items-center gap-2 cursor-pointer" onClick={() => setExpandedDecision(isExp ? null : d.id)}>
                      <Icon className={`w-4 h-4 text-${meta.color}-400 shrink-0`} />
                      <span className="text-xs font-semibold text-white">{d.agent_name}</span>
                      <span className="text-xs text-gray-500">{d.symbol}</span>
                      <span className={`text-xs font-bold ${actionColor(d.action)}`}>{d.action?.toUpperCase()}</span>
                      <span className="text-xs text-gray-600">{(d.confidence * 100).toFixed(0)}%</span>
                      {/* AI/Local badge */}
                      <span className={`text-[10px] px-1 py-0.5 rounded ${
                        d.ai_called === false
                          ? 'bg-purple-500/15 text-purple-300'
                          : 'bg-gray-700/50 text-gray-500'
                      }`}>
                        {d.ai_called === false ? 'LOCAL' : 'API'}
                      </span>
                      {/* Outcome badge */}
                      {d.outcome && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${
                          d.outcome === 'win' ? 'bg-green-500/15 text-green-300' :
                          d.outcome === 'loss' ? 'bg-red-500/15 text-red-300' :
                          'bg-gray-700 text-gray-400'
                        }`}>
                          {d.outcome.toUpperCase()}
                          {d.outcome_pnl != null && ` ${d.outcome_pnl > 0 ? '+' : ''}${d.outcome_pnl.toFixed(2)}`}
                        </span>
                      )}
                      {d.session_id && <span className="text-[10px] text-gray-700 ml-auto font-mono">{d.session_id}</span>}
                      <span className="text-[10px] text-gray-600">{d.created_at}</span>
                      {isExp ? <ChevronUp className="w-3 h-3 text-gray-500" /> : <ChevronDown className="w-3 h-3 text-gray-500" />}
                    </div>
                    {isExp && (
                      <div className="pl-6 mt-2 space-y-2">
                        {d.reasoning && (
                          <p className="text-xs text-gray-400">{d.reasoning}</p>
                        )}
                        {/* Outcome controls */}
                        {!d.outcome && (
                          <div className="flex items-center gap-2">
                            <span className="text-[10px] text-gray-500">Rate this decision:</span>
                            <button
                              onClick={(e) => { e.stopPropagation(); recordOutcome(d.id, 'win') }}
                              className="text-[10px] px-2 py-1 bg-green-900/30 hover:bg-green-900/50 text-green-400 rounded transition"
                            >
                              Win
                            </button>
                            <button
                              onClick={(e) => { e.stopPropagation(); recordOutcome(d.id, 'loss') }}
                              className="text-[10px] px-2 py-1 bg-red-900/30 hover:bg-red-900/50 text-red-400 rounded transition"
                            >
                              Loss
                            </button>
                            <button
                              onClick={(e) => { e.stopPropagation(); recordOutcome(d.id, 'break_even') }}
                              className="text-[10px] px-2 py-1 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded transition"
                            >
                              Break Even
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </>
  )
}
