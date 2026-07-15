import Head from 'next/head'
import { useState, useEffect, useCallback, useRef } from 'react'
import {
  FlaskConical,
  RefreshCw,
  Search,
  BarChart2,
  Zap,
  Users,
  Clock,
  ChevronDown,
  Play,
  Download,
  Loader2,
  AlertTriangle,
  CheckCircle,
  XCircle,
  TrendingUp,
  Activity,
} from 'lucide-react'
import { apiClient } from '@/services/api'

// ── Types ──────────────────────────────────────────────────────────────────

interface VibeStatus {
  reachable: boolean
  version?: string
  sidecar_running: boolean
  message?: string
}

interface RunRow {
  id: number
  remote_run_id?: string
  run_type: string
  symbol?: string
  prompt?: string
  status: string
  result_summary?: string
  pine_script?: string
  created_at: string
}

interface SwarmPreset {
  name: string
  description?: string
}

interface AlphaItem {
  id?: string
  name?: string
  family?: string
  [key: string]: unknown
}

type TabId = 'research' | 'backtest' | 'swarms' | 'alpha' | 'shadow'

const TABS: { id: TabId; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'research', label: '🔬 Research', icon: Search },
  { id: 'backtest', label: '📊 Backtest', icon: BarChart2 },
  { id: 'swarms', label: '🐝 Swarms', icon: Users },
  { id: 'alpha', label: '🧮 Alpha Zoo', icon: Zap },
  { id: 'shadow', label: '👥 Shadow Account', icon: Activity },
]

// ── Page ───────────────────────────────────────────────────────────────────

export default function VibeTradingPage() {
  const [activeTab, setActiveTab] = useState<TabId>('research')
  const [status, setStatus] = useState<VibeStatus | null>(null)
  const [runs, setRuns] = useState<RunRow[]>([])
  const [starting, setStarting] = useState(false)

  // Research tab
  const [researchPrompt, setResearchPrompt] = useState('')
  const [researchSymbol, setResearchSymbol] = useState('BTC/USDT')
  const [researchLoading, setResearchLoading] = useState(false)
  const [researchResult, setResearchResult] = useState<unknown>(null)

  // Backtest tab
  const [btSymbol, setBtSymbol] = useState('BTC/USDT')
  const [btStrategy, setBtStrategy] = useState('20/50-day moving average crossover')
  const [btTimeframe, setBtTimeframe] = useState('1d')
  const [btLoading, setBtLoading] = useState(false)
  const [btResult, setBtResult] = useState<unknown>(null)

  // Swarms tab
  const [swarmPresets, setSwarmPresets] = useState<SwarmPreset[]>([])
  const [swarmPreset, setSwarmPreset] = useState('crypto_trading_desk')
  const [swarmSymbol, setSwarmSymbol] = useState('BTC/USDT')
  const [swarmLoading, setSwarmLoading] = useState(false)
  const [swarmResult, setSwarmResult] = useState<unknown>(null)

  // Alpha Zoo tab
  const [alphaZoo, setAlphaZoo] = useState('gtja191')
  const [alphas, setAlphas] = useState<AlphaItem[]>([])
  const [alphaLoading, setAlphaLoading] = useState(false)

  const fetchStatus = useCallback(async () => {
    try {
      const r = await apiClient.vibeTradingPlugin.status()
      setStatus(r.data)
    } catch {
      setStatus({ reachable: false, sidecar_running: false })
    }
  }, [])

  const fetchRuns = useCallback(async () => {
    try {
      const r = await apiClient.vibeTradingPlugin.getRuns()
      setRuns(Array.isArray(r.data) ? r.data : [])
    } catch {}
  }, [])

  const fetchSwarmPresets = useCallback(async () => {
    try {
      const r = await apiClient.vibeTradingPlugin.getSwarmPresets()
      const presets = r.data
      if (Array.isArray(presets)) setSwarmPresets(presets)
      else if (presets && typeof presets === 'object') {
        setSwarmPresets(Object.entries(presets).map(([name, desc]) => ({ name, description: String(desc) })))
      }
    } catch {}
  }, [])

  useEffect(() => {
    fetchStatus()
    fetchRuns()
    fetchSwarmPresets()
    const t = setInterval(() => { fetchStatus(); fetchRuns() }, 30000)
    return () => clearInterval(t)
  }, [fetchStatus, fetchRuns, fetchSwarmPresets])

  const handleStart = async () => {
    setStarting(true)
    try {
      await apiClient.vibeTradingPlugin.startSidecar()
      await fetchStatus()
    } finally {
      setStarting(false)
    }
  }

  const handleResearch = async () => {
    if (!researchPrompt.trim()) return
    setResearchLoading(true)
    setResearchResult(null)
    try {
      const r = await apiClient.vibeTradingPlugin.research(researchPrompt, researchSymbol || undefined)
      setResearchResult(r.data)
      fetchRuns()
    } catch (e: unknown) {
      setResearchResult({ error: String(e) })
    } finally {
      setResearchLoading(false)
    }
  }

  const handleBacktest = async () => {
    if (!btSymbol.trim() || !btStrategy.trim()) return
    setBtLoading(true)
    setBtResult(null)
    try {
      const r = await apiClient.vibeTradingPlugin.backtest(btSymbol, btStrategy, btTimeframe)
      setBtResult(r.data)
      fetchRuns()
    } catch (e: unknown) {
      setBtResult({ error: String(e) })
    } finally {
      setBtLoading(false)
    }
  }

  const handleSwarm = async () => {
    if (!swarmPreset || !swarmSymbol) return
    setSwarmLoading(true)
    setSwarmResult(null)
    try {
      const r = await apiClient.vibeTradingPlugin.runSwarm(swarmPreset, swarmSymbol)
      setSwarmResult(r.data)
      fetchRuns()
    } catch (e: unknown) {
      setSwarmResult({ error: String(e) })
    } finally {
      setSwarmLoading(false)
    }
  }

  const handleAlphaList = async () => {
    setAlphaLoading(true)
    try {
      const r = await apiClient.vibeTradingPlugin.listAlphas({ zoo: alphaZoo, limit: 50 })
      const data = r.data
      setAlphas(Array.isArray(data) ? data : (data?.alphas || []))
    } finally {
      setAlphaLoading(false)
    }
  }

  const statusColor = status?.reachable ? 'text-green-400' : 'text-red-400'
  const statusIcon = status?.reachable ? <CheckCircle className="w-4 h-4 text-green-400" /> : <XCircle className="w-4 h-4 text-red-400" />

  return (
    <>
      <Head><title>Vibe Trading — TradeBot</title></Head>
      <div className="p-4 max-w-6xl mx-auto space-y-4">

        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <FlaskConical className="w-7 h-7 text-purple-400" />
            <div>
              <h1 className="text-xl font-bold text-white">Vibe Trading</h1>
              <p className="text-xs text-gray-400">AI research, backtesting, alpha zoo & swarm analysis</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {status && (
              <div className="flex items-center gap-2 text-sm">
                {statusIcon}
                <span className={statusColor}>
                  {status.reachable ? `Online${status.version ? ` v${status.version}` : ''}` : 'Offline'}
                </span>
              </div>
            )}
            {!status?.reachable && (
              <button
                onClick={handleStart}
                disabled={starting}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm disabled:opacity-50"
              >
                {starting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                Start Sidecar
              </button>
            )}
            <button onClick={() => { fetchStatus(); fetchRuns() }} className="p-1.5 rounded-lg hover:bg-gray-700 text-gray-400">
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Offline banner */}
        {status && !status.reachable && (
          <div className="flex items-start gap-2 p-3 rounded-lg bg-yellow-900/30 border border-yellow-700/50 text-yellow-300 text-sm">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
            <div>
              <p className="font-medium">Vibe-Trading sidecar not running</p>
              <p className="text-xs text-yellow-400 mt-0.5">
                {status.message || 'Click "Start Sidecar" or run: pip install vibe-trading-ai && vibe-trading serve --port 8899'}
              </p>
            </div>
          </div>
        )}

        {/* Tab selector */}
        <div className="flex gap-2 flex-wrap">
          {TABS.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                activeTab === tab.id
                  ? 'bg-purple-600 text-white'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* ── Research Tab ── */}
        {activeTab === 'research' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 space-y-3">
              <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                <Search className="w-4 h-4 text-purple-400" /> Natural-Language Research
              </h2>
              <div className="flex gap-2">
                <input
                  value={researchSymbol}
                  onChange={e => setResearchSymbol(e.target.value)}
                  placeholder="Symbol (optional)"
                  className="w-32 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
                />
                <input
                  value={researchPrompt}
                  onChange={e => setResearchPrompt(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleResearch()}
                  placeholder="Describe what you want to research…"
                  className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
                />
                <button
                  onClick={handleResearch}
                  disabled={researchLoading || !researchPrompt.trim()}
                  className="px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded-lg text-sm font-medium disabled:opacity-50 flex items-center gap-1.5"
                >
                  {researchLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                  Research
                </button>
              </div>
              <div className="flex gap-2 flex-wrap text-xs text-gray-500">
                {['BTC trend analysis', 'ETH on-chain metrics', 'RSI mean-reversion on crypto', 'Macro Fed impact on EM'].map(ex => (
                  <button key={ex} onClick={() => setResearchPrompt(ex)} className="px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-300">
                    {ex}
                  </button>
                ))}
              </div>
            </div>
            {researchResult != null && (
              <ResultCard title="Research Result" data={researchResult} />
            )}
          </div>
        )}

        {/* ── Backtest Tab ── */}
        {activeTab === 'backtest' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 space-y-3">
              <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                <BarChart2 className="w-4 h-4 text-blue-400" /> Strategy Backtesting
              </h2>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Symbol</label>
                  <input value={btSymbol} onChange={e => setBtSymbol(e.target.value)} className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500" />
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
                  <select value={btTimeframe} onChange={e => setBtTimeframe(e.target.value)} className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
                    {['1m','5m','15m','1h','4h','1d','1w'].map(tf => <option key={tf}>{tf}</option>)}
                  </select>
                </div>
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">Strategy Description</label>
                <textarea
                  value={btStrategy}
                  onChange={e => setBtStrategy(e.target.value)}
                  rows={2}
                  className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500 resize-none"
                />
              </div>
              <button
                onClick={handleBacktest}
                disabled={btLoading || !btSymbol.trim() || !btStrategy.trim()}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm font-medium disabled:opacity-50 flex items-center gap-1.5"
              >
                {btLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                Run Backtest
              </button>
            </div>
            {btResult != null && (
              <ResultCard title="Backtest Result" data={btResult} showPine />
            )}
          </div>
        )}

        {/* ── Swarms Tab ── */}
        {activeTab === 'swarms' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 space-y-3">
              <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                <Users className="w-4 h-4 text-orange-400" /> Multi-Agent Swarm Research
              </h2>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Preset</label>
                  <select
                    value={swarmPreset}
                    onChange={e => setSwarmPreset(e.target.value)}
                    className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-orange-500"
                  >
                    {(swarmPresets.length > 0 ? swarmPresets : [
                      { name: 'crypto_trading_desk' }, { name: 'investment_committee' },
                      { name: 'quant_strategy_desk' }, { name: 'macro_rates_fx_desk' },
                    ]).map(p => (
                      <option key={p.name} value={p.name}>{p.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Symbol</label>
                  <input value={swarmSymbol} onChange={e => setSwarmSymbol(e.target.value)} className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-orange-500" />
                </div>
              </div>
              <button
                onClick={handleSwarm}
                disabled={swarmLoading}
                className="px-4 py-2 bg-orange-600 hover:bg-orange-500 text-white rounded-lg text-sm font-medium disabled:opacity-50 flex items-center gap-1.5"
              >
                {swarmLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
                Launch Swarm
              </button>
            </div>
            {swarmResult != null && <ResultCard title="Swarm Result" data={swarmResult} />}
          </div>
        )}

        {/* ── Alpha Zoo Tab ── */}
        {activeTab === 'alpha' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 space-y-3">
              <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                <Zap className="w-4 h-4 text-yellow-400" /> Alpha Zoo (460 Pre-Built Alphas)
              </h2>
              <div className="flex gap-2">
                <select value={alphaZoo} onChange={e => setAlphaZoo(e.target.value)} className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-yellow-500">
                  {['gtja191','qlib158','kakushadze101','academic','fundamental'].map(z => <option key={z}>{z}</option>)}
                </select>
                <button onClick={handleAlphaList} disabled={alphaLoading} className="px-4 py-2 bg-yellow-600 hover:bg-yellow-500 text-white rounded-lg text-sm disabled:opacity-50 flex items-center gap-1.5">
                  {alphaLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                  Browse Alphas
                </button>
              </div>
              {alphas.length > 0 && (
                <div className="max-h-64 overflow-y-auto rounded-lg border border-gray-700">
                  <table className="w-full text-xs">
                    <thead className="bg-gray-900 sticky top-0">
                      <tr>
                        <th className="text-left px-3 py-2 text-gray-400">ID</th>
                        <th className="text-left px-3 py-2 text-gray-400">Name</th>
                        <th className="text-left px-3 py-2 text-gray-400">Family</th>
                      </tr>
                    </thead>
                    <tbody>
                      {alphas.map((a, i) => (
                        <tr key={i} className="border-t border-gray-700/50 hover:bg-gray-700/30">
                          <td className="px-3 py-2 text-gray-300 font-mono">{String(a.id || i)}</td>
                          <td className="px-3 py-2 text-white">{String(a.name || '—')}</td>
                          <td className="px-3 py-2 text-gray-400">{String(a.family || alphaZoo)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── Shadow Account Tab ── */}
        {activeTab === 'shadow' && (
          <div className="bg-gray-800/60 rounded-xl p-6 text-center space-y-4">
            <Activity className="w-10 h-10 text-gray-500 mx-auto" />
            <div>
              <h2 className="text-sm font-semibold text-white">Shadow Account Analysis</h2>
              <p className="text-xs text-gray-400 mt-1">
                Upload your broker trade export (CSV) to get behavior analysis, rule extraction, and a shadow strategy comparison.
              </p>
            </div>
            <p className="text-xs text-gray-500">
              Supported formats: 同花顺, 东方财富, 富途, generic CSV<br/>
              Run via CLI: <code className="bg-gray-900 px-1.5 py-0.5 rounded text-gray-300">vibe-trading --upload trades.csv && vibe-trading run -p "Analyze my trading behavior"</code>
            </p>
          </div>
        )}

        {/* ── Recent Runs ── */}
        {runs.length > 0 && (
          <div className="bg-gray-800/60 rounded-xl p-4 space-y-2">
            <h3 className="text-sm font-semibold text-white flex items-center gap-2">
              <Clock className="w-4 h-4 text-gray-400" /> Recent Runs
            </h3>
            <div className="max-h-48 overflow-y-auto space-y-1.5">
              {runs.slice(0, 20).map(r => (
                <div key={r.id} className="flex items-center gap-3 text-xs bg-gray-900/50 rounded-lg px-3 py-2">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${
                    r.status === 'completed' ? 'bg-green-500' :
                    r.status === 'failed' ? 'bg-red-500' : 'bg-yellow-500'
                  }`} />
                  <span className="text-purple-400 font-medium w-16 shrink-0">{r.run_type}</span>
                  <span className="text-gray-300 flex-1 truncate">{r.prompt || '—'}</span>
                  {r.symbol && <span className="text-gray-500 shrink-0">{r.symbol}</span>}
                  <span className="text-gray-600 shrink-0">{new Date(r.created_at).toLocaleTimeString()}</span>
                </div>
              ))}
            </div>
          </div>
        )}

      </div>
    </>
  )
}

// ── ResultCard ─────────────────────────────────────────────────────────────

function ResultCard({ title, data, showPine }: { title: string; data: unknown; showPine?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const text = typeof data === 'string' ? data : (JSON.stringify(data, null, 2) ?? String(data))
  const isError = typeof data === 'object' && data !== null && 'error' in (data as Record<string, unknown>)

  return (
    <div className={`rounded-xl border p-4 space-y-2 ${isError ? 'border-red-700/50 bg-red-900/10' : 'border-gray-700/50 bg-gray-800/60'}`}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">{title}</h3>
        {showPine && typeof data === 'object' && data !== null && 'pine_script' in (data as Record<string, unknown>) && (
          <button className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300">
            <Download className="w-3.5 h-3.5" /> Export Pine Script
          </button>
        )}
      </div>
      <pre className={`text-xs text-gray-300 whitespace-pre-wrap break-all max-h-${expanded ? '96' : '48'} overflow-y-auto font-mono leading-relaxed`}>
        {text}
      </pre>
      {text.length > 500 && (
        <button onClick={() => setExpanded(!expanded)} className="text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1">
          <ChevronDown className={`w-3.5 h-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}
