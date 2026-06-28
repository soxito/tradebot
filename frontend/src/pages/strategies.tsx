import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  Plus,
  Trash2,
  Save,
  Copy,
  Play,
  Code,
  Settings,
  Zap,
  ChevronDown,
  ChevronUp,
  X,
  Check,
  FileCode2,
  Bot,
  RefreshCw,
  Download,
  ToggleLeft,
  ToggleRight,
  Brain,
  Sparkles,
  BarChart3,
  TrendingUp,
  TrendingDown,
  Minus,
  ArrowRight,
} from 'lucide-react'

// ──────────── Types ────────────

interface IndicatorConfig {
  name: string
  enabled: boolean
  params: Record<string, number>
  weight: number
}

interface Strategy {
  id: number
  name: string
  description: string
  pairs: string[]
  timeframe: string
  indicators: IndicatorConfig[]
  buy_threshold: number
  sell_threshold: number
  stop_loss_pct: number
  take_profit_pct: number
  trade_type: string
  leverage: number
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

interface PineScriptItem {
  id: number
  name: string
  description: string
  strategy_id: number | null
  script_type: string
  code: string
  pairs: string[]
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']

const AVAILABLE_INDICATORS: { name: string; label: string; defaultParams: Record<string, number> }[] = [
  { name: 'rsi', label: 'RSI', defaultParams: { period: 14, overbought: 70, oversold: 30 } },
  { name: 'macd', label: 'MACD', defaultParams: { fast: 12, slow: 26, signal: 9 } },
  { name: 'bollinger', label: 'Bollinger Bands', defaultParams: { period: 20, mult: 2 } },
  { name: 'ema_cross', label: 'EMA Cross', defaultParams: { fast: 50, slow: 200 } },
  { name: 'stoch_rsi', label: 'Stochastic RSI', defaultParams: { period: 14, overbought: 80, oversold: 20 } },
  { name: 'adx', label: 'ADX', defaultParams: { period: 14, threshold: 25 } },
  { name: 'volume', label: 'Volume Surge', defaultParams: { period: 20, mult: 1.5 } },
]

const DEFAULT_PAIRS = [
  'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
  'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 'AVAX/USDT',
  'DOT/USDT', 'LINK/USDT', 'ARB/USDT', 'OP/USDT',
]

export default function StrategiesPage() {
  const [tab, setTab] = useState<'bot' | 'pine'>('bot')

  // Bot strategies
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [editingStrategy, setEditingStrategy] = useState<Strategy | null>(null)
  const [showNewStrategy, setShowNewStrategy] = useState(false)
  const [stratLoading, setStratLoading] = useState(false)

  // Pine scripts
  const [pineScripts, setPineScripts] = useState<PineScriptItem[]>([])
  const [editingScript, setEditingScript] = useState<PineScriptItem | null>(null)
  const [showNewScript, setShowNewScript] = useState(false)
  const [pineLoading, setPineLoading] = useState(false)

  // Message
  const [msg, setMsg] = useState<{ text: string; type: 'ok' | 'err' } | null>(null)
  const flash = (text: string, type: 'ok' | 'err' = 'ok') => {
    setMsg({ text, type })
    setTimeout(() => setMsg(null), 4000)
  }

  // AI features state
  const [aiLoading, setAiLoading] = useState(false)
  const [aiPrefilledStrategy, setAiPrefilledStrategy] = useState<any>(null)
  const [aiStatus, setAiStatus] = useState<any>(null)
  const [chartAnalysis, setChartAnalysis] = useState<any>(null)
  const [chartSymbol, setChartSymbol] = useState('BTC/USDT')
  const [chartTimeframe, setChartTimeframe] = useState('1h')
  const [chartLoading, setChartLoading] = useState(false)

  // ──────────── Fetch ────────────

  const fetchStrategies = useCallback(async () => {
    try {
      const { data } = await apiClient.getStrategies()
      setStrategies(data)
    } catch { /* empty */ }
  }, [])

  const fetchPineScripts = useCallback(async () => {
    try {
      const { data } = await apiClient.getPineScripts()
      setPineScripts(data)
    } catch { /* empty */ }
  }, [])

  useEffect(() => {
    fetchStrategies()
    fetchPineScripts()
    apiClient.getAgentStatus().then(res => setAiStatus(res.data)).catch(() => {})
  }, [fetchStrategies, fetchPineScripts])

  // ──────────── AI Strategy Functions ────────────

  const mapAIIndicators = (aiIndicators: any[]) => {
    const allNames = AVAILABLE_INDICATORS.map(i => i.name)
    const aiMap = new Map((aiIndicators || []).map((ind: any) => [ind.name, ind]))
    return AVAILABLE_INDICATORS.map(avail => {
      const ai = aiMap.get(avail.name)
      if (ai) {
        return {
          name: avail.name,
          enabled: ai.enabled !== false,
          params: { ...avail.defaultParams, ...ai.params },
          weight: ai.weight || 1.0,
        }
      }
      return { name: avail.name, enabled: false, params: { ...avail.defaultParams }, weight: 1.0 }
    })
  }

  const generateAIStrategy = async () => {
    setAiLoading(true)
    try {
      const res = await apiClient.generateAIStrategy({
        symbol: 'BTC/USDT',
        timeframe: '1h',
        trade_type: 'futures',
        risk_level: 'medium',
      })
      const ai = res.data.strategy
      const newStrat = {
        ...newStrategyTemplate(),
        name: ai.name || 'AI Generated Strategy',
        description: ai.description || ai.reasoning || '',
        timeframe: ai.timeframe || '1h',
        indicators: mapAIIndicators(ai.indicators),
        buy_threshold: ai.buy_threshold ?? 0.25,
        sell_threshold: ai.sell_threshold ?? -0.25,
        stop_loss_pct: ai.stop_loss_pct ?? 2.0,
        take_profit_pct: ai.take_profit_pct ?? 4.0,
        trade_type: ai.trade_type || 'futures',
        leverage: ai.leverage ?? 5,
      }
      setAiPrefilledStrategy(newStrat)
      setShowNewStrategy(true)
      setEditingStrategy(null)
      flash('AI strategy generated! Review and save.')
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'AI generation failed', 'err')
    } finally {
      setAiLoading(false)
    }
  }

  const improveWithAI = async (strat: Strategy) => {
    setAiLoading(true)
    try {
      const res = await apiClient.improveAIStrategy({ strategy: strat })
      const ai = res.data.strategy
      const improved: Strategy = {
        ...strat,
        name: ai.name || strat.name,
        description: ai.description || ai.reasoning || strat.description,
        indicators: mapAIIndicators(ai.indicators),
        buy_threshold: ai.buy_threshold ?? strat.buy_threshold,
        sell_threshold: ai.sell_threshold ?? strat.sell_threshold,
        stop_loss_pct: ai.stop_loss_pct ?? strat.stop_loss_pct,
        take_profit_pct: ai.take_profit_pct ?? strat.take_profit_pct,
        leverage: ai.leverage ?? strat.leverage,
      }
      setEditingStrategy(improved)
      flash('AI improved the strategy! Review changes and save.')
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'AI improvement failed', 'err')
    } finally {
      setAiLoading(false)
    }
  }

  const copyImproveWithAI = async (strat: Strategy) => {
    setAiLoading(true)
    try {
      const res = await apiClient.improveAIStrategy({
        strategy: strat,
        goals: 'Create an improved copy with better risk-adjusted returns',
      })
      const ai = res.data.strategy
      const newStrat = {
        ...newStrategyTemplate(),
        name: (ai.name || strat.name) + ' (AI Copy)',
        description: ai.description || ai.reasoning || strat.description,
        pairs: strat.pairs,
        timeframe: ai.timeframe || strat.timeframe,
        indicators: mapAIIndicators(ai.indicators),
        buy_threshold: ai.buy_threshold ?? strat.buy_threshold,
        sell_threshold: ai.sell_threshold ?? strat.sell_threshold,
        stop_loss_pct: ai.stop_loss_pct ?? strat.stop_loss_pct,
        take_profit_pct: ai.take_profit_pct ?? strat.take_profit_pct,
        trade_type: ai.trade_type || strat.trade_type,
        leverage: ai.leverage ?? strat.leverage,
      }
      setAiPrefilledStrategy(newStrat)
      setShowNewStrategy(true)
      setEditingStrategy(null)
      flash('AI created an improved copy! Review and save.')
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'AI copy & improve failed', 'err')
    } finally {
      setAiLoading(false)
    }
  }

  const analyzeChartAI = async () => {
    setChartLoading(true)
    setChartAnalysis(null)
    try {
      const res = await apiClient.analyzeChart({ symbol: chartSymbol, timeframe: chartTimeframe })
      setChartAnalysis(res.data.analysis)
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'Chart analysis failed', 'err')
    } finally {
      setChartLoading(false)
    }
  }

  // ──────────── Strategy CRUD ────────────

  const newStrategyTemplate = (): Omit<Strategy, 'id' | 'created_at' | 'updated_at'> => ({
    name: '',
    description: '',
    pairs: ['BTC/USDT'],
    timeframe: '1h',
    indicators: AVAILABLE_INDICATORS.map(i => ({
      name: i.name,
      enabled: ['rsi', 'macd', 'ema_cross'].includes(i.name),
      params: { ...i.defaultParams },
      weight: 1.0,
    })),
    buy_threshold: 0.25,
    sell_threshold: -0.25,
    stop_loss_pct: 2.0,
    take_profit_pct: 4.0,
    trade_type: 'spot',
    leverage: 1,
    is_active: false,
  })

  const saveStrategy = async (strat: any) => {
    setStratLoading(true)
    try {
      if (strat.id) {
        await apiClient.updateStrategy(strat.id, strat)
        flash('Strategy updated')
      } else {
        await apiClient.createStrategy(strat)
        flash('Strategy created')
      }
      setEditingStrategy(null)
      setShowNewStrategy(false)
      await fetchStrategies()
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'Save failed', 'err')
    } finally {
      setStratLoading(false)
    }
  }

  const deleteStrategy = async (id: number) => {
    try {
      await apiClient.deleteStrategy(id)
      flash('Strategy deleted')
      await fetchStrategies()
    } catch { flash('Delete failed', 'err') }
  }

  const generatePine = async (stratId: number) => {
    try {
      await apiClient.generatePineScript(stratId)
      flash('Pine Script generated')
      await fetchPineScripts()
      setTab('pine')
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'Generation failed', 'err')
    }
  }

  // ──────────── Pine Script CRUD ────────────

  const newScriptTemplate = (): Omit<PineScriptItem, 'id' | 'created_at' | 'updated_at'> => ({
    name: '',
    description: '',
    strategy_id: null,
    script_type: 'indicator',
    code: `//@version=5
indicator("My Indicator", overlay=true)

// Your Pine Script code here
plot(close)
`,
    pairs: ['BTC/USDT'],
    is_active: false,
  })

  const savePineScript = async (script: any) => {
    setPineLoading(true)
    try {
      if (script.id) {
        await apiClient.updatePineScript(script.id, script)
        flash('Script saved')
      } else {
        await apiClient.createPineScript(script)
        flash('Script created')
      }
      setEditingScript(null)
      setShowNewScript(false)
      await fetchPineScripts()
    } catch (e: any) {
      flash(e?.response?.data?.detail || 'Save failed', 'err')
    } finally {
      setPineLoading(false)
    }
  }

  const deletePineScript = async (id: number) => {
    try {
      await apiClient.deletePineScript(id)
      flash('Script deleted')
      await fetchPineScripts()
    } catch { flash('Delete failed', 'err') }
  }

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
    flash('Copied to clipboard')
  }

  // ──────────── Render ────────────
  return (
    <>
      <Head>
        <title>Strategies & Pine Scripts | TradeBot</title>
      </Head>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <Zap className="w-7 h-7 text-yellow-400" />
            Strategies & Pine Scripts
          </h1>
        </div>

        {/* Toast */}
        {msg && (
          <div className={`px-4 py-2 rounded text-sm font-medium ${
            msg.type === 'ok' ? 'bg-green-500/20 text-green-400 border border-green-500/30' : 'bg-red-500/20 text-red-400 border border-red-500/30'
          }`}>
            {msg.text}
          </div>
        )}

        {/* Tab bar */}
        <div className="flex gap-1 bg-gray-800/50 p-1 rounded-lg w-fit">
          <button
            onClick={() => setTab('bot')}
            className={`flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold transition ${
              tab === 'bot' ? 'bg-purple-600 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-700/50'
            }`}
          >
            <Bot className="w-4 h-4" /> Signal Bot Config
          </button>
          <button
            onClick={() => setTab('pine')}
            className={`flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold transition ${
              tab === 'pine' ? 'bg-purple-600 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-700/50'
            }`}
          >
            <FileCode2 className="w-4 h-4" /> Pine Scripts
          </button>
        </div>

        {/* ════════════════════ BOT SIGNAL CONFIG TAB ════════════════════ */}
        {tab === 'bot' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-sm text-gray-400">
                Configure indicator combos, thresholds, and pairs for the auto-trade signal bot.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={generateAIStrategy}
                  disabled={aiLoading || !aiStatus?.ai_enabled}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-gradient-to-r from-purple-600 to-blue-600 hover:from-purple-500 hover:to-blue-500 disabled:opacity-50 text-white rounded text-sm font-semibold transition"
                  title={!aiStatus?.ai_enabled ? 'Enable AI agents first' : 'Generate strategy using AI'}
                >
                  {aiLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Brain className="w-4 h-4" />} AI Generate
                </button>
                <button
                  onClick={() => { setShowNewStrategy(true); setEditingStrategy(null); setAiPrefilledStrategy(null) }}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-purple-600 hover:bg-purple-500 text-white rounded text-sm font-semibold transition"
                >
                  <Plus className="w-4 h-4" /> New Strategy
                </button>
              </div>
            </div>

            {/* New strategy form */}
            {showNewStrategy && (
              <StrategyForm
                strategy={(aiPrefilledStrategy || newStrategyTemplate()) as any}
                onSave={(s: any) => { saveStrategy(s); setAiPrefilledStrategy(null) }}
                onCancel={() => { setShowNewStrategy(false); setAiPrefilledStrategy(null) }}
                loading={stratLoading}
              />
            )}

            {/* Strategy list */}
            {strategies.length === 0 && !showNewStrategy ? (
              <div className="text-center py-12 text-gray-500">
                <Bot className="w-10 h-10 mx-auto mb-3 opacity-40" />
                <p>No strategies yet. Create one to get started.</p>
              </div>
            ) : (
              <div className="space-y-3">
                {strategies.map(s => (
                  editingStrategy?.id === s.id ? (
                    <StrategyForm
                      key={s.id}
                      strategy={editingStrategy}
                      onSave={saveStrategy}
                      onCancel={() => setEditingStrategy(null)}
                      loading={stratLoading}
                    />
                  ) : (
                    <StrategyCard
                      key={s.id}
                      strategy={s}
                      onEdit={() => setEditingStrategy({ ...s })}
                      onDelete={() => deleteStrategy(s.id)}
                      onGeneratePine={() => generatePine(s.id)}
                      onImproveAI={() => improveWithAI(s)}
                      onCopyImproveAI={() => copyImproveWithAI(s)}
                      aiLoading={aiLoading}
                    />
                  )
                ))}
              </div>
            )}

            {/* AI Chart Analysis */}
            <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5 space-y-4">
              <h3 className="font-semibold text-white flex items-center gap-2">
                <Brain className="w-4 h-4 text-purple-400" /> AI Chart Analysis
                {!aiStatus?.ai_enabled && (
                  <span className="text-xs text-gray-500 font-normal">(Enable AI agents to use)</span>
                )}
              </h3>
              <p className="text-xs text-gray-400">
                Use AI to analyze chart data and get trading recommendations for better strategy decisions.
              </p>
              <div className="flex items-center gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Symbol</label>
                  <input
                    type="text"
                    value={chartSymbol}
                    onChange={e => setChartSymbol(e.target.value.toUpperCase())}
                    className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-white w-36 focus:border-purple-500 outline-none"
                    placeholder="BTC/USDT"
                  />
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
                  <select
                    value={chartTimeframe}
                    onChange={e => setChartTimeframe(e.target.value)}
                    className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-white focus:border-purple-500 outline-none"
                  >
                    {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
                  </select>
                </div>
                <div className="flex items-end">
                  <button
                    onClick={analyzeChartAI}
                    disabled={chartLoading || !aiStatus?.ai_enabled || !chartSymbol}
                    className="flex items-center gap-1.5 px-4 py-1.5 bg-gradient-to-r from-purple-600 to-blue-600 hover:from-purple-500 hover:to-blue-500 disabled:opacity-50 text-white rounded text-sm font-semibold transition mt-5"
                  >
                    {chartLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <BarChart3 className="w-4 h-4" />}
                    {chartLoading ? 'Analyzing…' : 'Analyze Chart'}
                  </button>
                </div>
              </div>

              {/* Chart Analysis Results */}
              {chartAnalysis && (
                <div className="bg-gray-900/60 border border-purple-500/20 rounded-lg p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-mono font-bold text-white">{chartAnalysis.symbol} <span className="text-xs text-gray-500">{chartAnalysis.timeframe}</span></span>
                    <div className="flex items-center gap-2">
                      <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                        chartAnalysis.recommended_action === 'buy' ? 'bg-green-500/20 text-green-400' :
                        chartAnalysis.recommended_action === 'sell' ? 'bg-red-500/20 text-red-400' :
                        'bg-yellow-500/20 text-yellow-400'
                      }`}>
                        {(chartAnalysis.recommended_action || 'wait').toUpperCase()}
                      </span>
                      <span className={`px-2 py-0.5 rounded text-xs ${
                        chartAnalysis.risk_assessment === 'low' ? 'bg-green-500/10 text-green-400' :
                        chartAnalysis.risk_assessment === 'high' ? 'bg-red-500/10 text-red-400' :
                        'bg-yellow-500/10 text-yellow-400'
                      }`}>
                        {chartAnalysis.risk_assessment} risk
                      </span>
                    </div>
                  </div>

                  <div className="text-xs text-gray-400">
                    <span className="text-gray-300 font-medium">Structure:</span> {chartAnalysis.market_structure}
                    <span className="ml-3 text-gray-300 font-medium">Confidence:</span> {((chartAnalysis.confidence || 0) * 100).toFixed(0)}%
                  </div>

                  {/* Key Levels */}
                  {chartAnalysis.key_levels && (
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <div className="bg-gray-800/50 rounded p-2">
                        <span className="text-green-400 font-medium">Support:</span>{' '}
                        {(chartAnalysis.key_levels.support || []).map((p: number) => p?.toLocaleString()).join(', ') || 'N/A'}
                      </div>
                      <div className="bg-gray-800/50 rounded p-2">
                        <span className="text-red-400 font-medium">Resistance:</span>{' '}
                        {(chartAnalysis.key_levels.resistance || []).map((p: number) => p?.toLocaleString()).join(', ') || 'N/A'}
                      </div>
                    </div>
                  )}

                  {/* Indicator Signals */}
                  {chartAnalysis.indicator_signals && (
                    <div className="flex flex-wrap gap-2">
                      {Object.entries(chartAnalysis.indicator_signals).map(([key, val]: [string, any]) => (
                        <span key={key} className="px-2 py-1 bg-gray-800/50 rounded text-xs">
                          <span className="text-gray-400">{key.toUpperCase()}:</span>{' '}
                          <span className={
                            String(val).includes('bullish') || String(val).includes('oversold') ? 'text-green-400' :
                            String(val).includes('bearish') || String(val).includes('overbought') ? 'text-red-400' :
                            'text-gray-300'
                          }>{String(val)}</span>
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Strategy Suggestions */}
                  {chartAnalysis.strategy_suggestions && chartAnalysis.strategy_suggestions.length > 0 && (
                    <div>
                      <span className="text-xs text-gray-400 font-medium">Strategy Suggestions:</span>
                      <ul className="mt-1 space-y-1">
                        {chartAnalysis.strategy_suggestions.map((s: string, i: number) => (
                          <li key={i} className="text-xs text-gray-300 flex items-start gap-2">
                            <Sparkles className="w-3 h-3 text-purple-400 mt-0.5 shrink-0" />
                            {s}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Reasoning */}
                  {chartAnalysis.reasoning && (
                    <p className="text-xs text-gray-400 border-t border-gray-700/50 pt-2">{chartAnalysis.reasoning}</p>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ════════════════════ PINE SCRIPT TAB ════════════════════ */}
        {tab === 'pine' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-sm text-gray-400">
                Create Pine Script indicators &amp; strategies to load on TradingView charts.
              </p>
              <button
                onClick={() => { setShowNewScript(true); setEditingScript(null) }}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-purple-600 hover:bg-purple-500 text-white rounded text-sm font-semibold transition"
              >
                <Plus className="w-4 h-4" /> New Script
              </button>
            </div>

            {/* New script form */}
            {showNewScript && (
              <PineScriptForm
                script={newScriptTemplate() as any}
                onSave={savePineScript}
                onCancel={() => setShowNewScript(false)}
                loading={pineLoading}
              />
            )}

            {/* Script list */}
            {pineScripts.length === 0 && !showNewScript ? (
              <div className="text-center py-12 text-gray-500">
                <FileCode2 className="w-10 h-10 mx-auto mb-3 opacity-40" />
                <p>No Pine Scripts yet. Create one or generate from a strategy.</p>
              </div>
            ) : (
              <div className="space-y-3">
                {pineScripts.map(p => (
                  editingScript?.id === p.id ? (
                    <PineScriptForm
                      key={p.id}
                      script={editingScript}
                      onSave={savePineScript}
                      onCancel={() => setEditingScript(null)}
                      loading={pineLoading}
                    />
                  ) : (
                    <PineScriptCard
                      key={p.id}
                      script={p}
                      onEdit={() => setEditingScript({ ...p })}
                      onDelete={() => deletePineScript(p.id)}
                      onCopy={() => copyToClipboard(p.code)}
                    />
                  )
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}


// ════════════════════════════════════════════════════════════════
// Strategy Card (read-only)
// ════════════════════════════════════════════════════════════════

function StrategyCard({
  strategy: s,
  onEdit,
  onDelete,
  onGeneratePine,
  onImproveAI,
  onCopyImproveAI,
  aiLoading,
}: {
  strategy: Strategy
  onEdit: () => void
  onDelete: () => void
  onGeneratePine: () => void
  onImproveAI?: () => void
  onCopyImproveAI?: () => void
  aiLoading?: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const enabledIndicators = s.indicators.filter(i => i.enabled)

  return (
    <div className="bg-gray-800/40 border border-gray-700 rounded-lg overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-gray-800/60 transition"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <span className={`w-2 h-2 rounded-full ${s.is_active ? 'bg-green-400 animate-pulse' : 'bg-gray-600'}`} />
          <span className="font-semibold text-white">{s.name}</span>
          <span className="text-xs text-gray-500">{s.timeframe} · {s.trade_type}</span>
          <div className="flex gap-1">
            {enabledIndicators.slice(0, 4).map(i => (
              <span key={i.name} className="px-1.5 py-0.5 bg-purple-500/20 text-purple-300 rounded text-[10px] font-medium">
                {i.name.toUpperCase()}
              </span>
            ))}
            {enabledIndicators.length > 4 && (
              <span className="text-[10px] text-gray-500">+{enabledIndicators.length - 4}</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{s.pairs.length} pair{s.pairs.length !== 1 ? 's' : ''}</span>
          {expanded ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
        </div>
      </div>

      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-700/50">
          {s.description && <p className="text-sm text-gray-400 pt-3">{s.description}</p>}

          {/* Indicator summary */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2 text-xs">
            {enabledIndicators.map(ind => (
              <div key={ind.name} className="bg-gray-900/50 rounded p-2">
                <span className="text-purple-300 font-semibold">{ind.name.toUpperCase()}</span>
                <span className="text-gray-500 ml-1">w:{ind.weight}</span>
                <div className="text-gray-500 mt-0.5">
                  {Object.entries(ind.params).map(([k, v]) => `${k}=${v}`).join(', ')}
                </div>
              </div>
            ))}
          </div>

          {/* Thresholds */}
          <div className="flex flex-wrap gap-4 text-xs">
            <span className="text-gray-400">Buy ≥ <span className="text-green-400 font-mono">{s.buy_threshold}</span></span>
            <span className="text-gray-400">Sell ≤ <span className="text-red-400 font-mono">{s.sell_threshold}</span></span>
            <span className="text-gray-400">SL: <span className="text-red-300 font-mono">{s.stop_loss_pct}%</span></span>
            <span className="text-gray-400">TP: <span className="text-green-300 font-mono">{s.take_profit_pct}%</span></span>
            {s.trade_type === 'futures' && <span className="text-orange-300">Lev: {s.leverage}x</span>}
          </div>

          {/* Pairs */}
          <div className="flex flex-wrap gap-1">
            {s.pairs.map(p => (
              <span key={p} className="px-1.5 py-0.5 bg-gray-700 text-gray-300 rounded text-[10px]">{p}</span>
            ))}
          </div>

          {/* Actions */}
          <div className="flex flex-wrap gap-2 pt-2">
            <button onClick={onEdit} className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-xs text-white transition">
              <Settings className="w-3 h-3" /> Edit
            </button>
            <button onClick={onGeneratePine} className="flex items-center gap-1 px-3 py-1.5 bg-blue-600/30 hover:bg-blue-600/50 border border-blue-500/30 rounded text-xs text-blue-300 transition">
              <Code className="w-3 h-3" /> Generate Pine Script
            </button>
            {onImproveAI && (
              <button
                onClick={onImproveAI}
                disabled={aiLoading}
                className="flex items-center gap-1 px-3 py-1.5 bg-purple-600/30 hover:bg-purple-600/50 border border-purple-500/30 rounded text-xs text-purple-300 transition disabled:opacity-50"
              >
                {aiLoading ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Brain className="w-3 h-3" />} Improve with AI
              </button>
            )}
            {onCopyImproveAI && (
              <button
                onClick={onCopyImproveAI}
                disabled={aiLoading}
                className="flex items-center gap-1 px-3 py-1.5 bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/20 rounded text-xs text-blue-300 transition disabled:opacity-50"
              >
                {aiLoading ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Copy className="w-3 h-3" />} Copy &amp; AI Improve
              </button>
            )}
            <button onClick={onDelete} className="flex items-center gap-1 px-3 py-1.5 bg-red-600/20 hover:bg-red-600/30 border border-red-500/30 rounded text-xs text-red-400 transition ml-auto">
              <Trash2 className="w-3 h-3" /> Delete
            </button>
          </div>
        </div>
      )}
    </div>
  )
}


// ════════════════════════════════════════════════════════════════
// Strategy Form (create/edit)
// ════════════════════════════════════════════════════════════════

function StrategyForm({
  strategy,
  onSave,
  onCancel,
  loading,
}: {
  strategy: Strategy | Omit<Strategy, 'id' | 'created_at' | 'updated_at'>
  onSave: (s: any) => void
  onCancel: () => void
  loading: boolean
}) {
  const [form, setForm] = useState<any>({ ...strategy })
  const [pairInput, setPairInput] = useState('')

  const update = (key: string, val: any) => setForm((f: any) => ({ ...f, [key]: val }))

  const updateIndicator = (idx: number, key: string, val: any) => {
    const inds = [...form.indicators]
    inds[idx] = { ...inds[idx], [key]: val }
    update('indicators', inds)
  }

  const updateIndicatorParam = (idx: number, paramKey: string, val: number) => {
    const inds = [...form.indicators]
    inds[idx] = { ...inds[idx], params: { ...inds[idx].params, [paramKey]: val } }
    update('indicators', inds)
  }

  const addPair = () => {
    const p = pairInput.trim().toUpperCase()
    if (p && !form.pairs.includes(p)) {
      update('pairs', [...form.pairs, p])
    }
    setPairInput('')
  }

  const removePair = (pair: string) => {
    update('pairs', form.pairs.filter((p: string) => p !== pair))
  }

  return (
    <div className="bg-gray-800/60 border border-purple-500/30 rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-white text-lg">
          {'id' in form ? 'Edit Strategy' : 'New Strategy'}
        </h3>
        <button onClick={onCancel} className="text-gray-400 hover:text-white"><X className="w-5 h-5" /></button>
      </div>

      {/* Name & description */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Name</label>
          <input
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.name}
            onChange={e => update('name', e.target.value)}
            placeholder="My Strategy"
          />
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Description</label>
          <input
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.description}
            onChange={e => update('description', e.target.value)}
            placeholder="Optional description"
          />
        </div>
      </div>

      {/* Timeframe, trade type, leverage */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Timeframe</label>
          <select
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.timeframe}
            onChange={e => update('timeframe', e.target.value)}
          >
            {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Trade Type</label>
          <select
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.trade_type}
            onChange={e => update('trade_type', e.target.value)}
          >
            <option value="spot">Spot</option>
            <option value="futures">Futures</option>
          </select>
        </div>
        {form.trade_type === 'futures' && (
          <div>
            <label className="text-xs text-gray-400 mb-1 block">Leverage</label>
            <input
              type="number"
              min={1}
              max={125}
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
              value={form.leverage}
              onChange={e => update('leverage', Math.min(125, Math.max(1, parseInt(e.target.value) || 1)))}
            />
          </div>
        )}
        <div className="flex items-end gap-2">
          <label className="text-xs text-gray-400 mb-1 block">Active</label>
          <button
            className="flex items-center gap-1 text-sm"
            onClick={() => update('is_active', !form.is_active)}
          >
            {form.is_active
              ? <><ToggleRight className="w-6 h-6 text-green-400" /> <span className="text-green-400 text-xs">On</span></>
              : <><ToggleLeft className="w-6 h-6 text-gray-500" /> <span className="text-gray-500 text-xs">Off</span></>
            }
          </button>
        </div>
      </div>

      {/* Thresholds & risk */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Buy Threshold</label>
          <input
            type="number"
            step={0.05}
            min={-1}
            max={1}
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.buy_threshold}
            onChange={e => update('buy_threshold', parseFloat(e.target.value) || 0)}
          />
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Sell Threshold</label>
          <input
            type="number"
            step={0.05}
            min={-1}
            max={1}
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.sell_threshold}
            onChange={e => update('sell_threshold', parseFloat(e.target.value) || 0)}
          />
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Stop Loss %</label>
          <input
            type="number"
            step={0.5}
            min={0}
            max={50}
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.stop_loss_pct}
            onChange={e => update('stop_loss_pct', parseFloat(e.target.value) || 0)}
          />
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Take Profit %</label>
          <input
            type="number"
            step={0.5}
            min={0}
            max={100}
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-purple-500 outline-none"
            value={form.take_profit_pct}
            onChange={e => update('take_profit_pct', parseFloat(e.target.value) || 0)}
          />
        </div>
      </div>

      {/* Indicators */}
      <div>
        <label className="text-xs text-gray-400 mb-2 block font-semibold">INDICATORS</label>
        <div className="space-y-2">
          {form.indicators.map((ind: IndicatorConfig, idx: number) => {
            const meta = AVAILABLE_INDICATORS.find(a => a.name === ind.name)
            return (
              <div key={ind.name} className={`rounded border p-3 transition ${
                ind.enabled ? 'bg-gray-900/60 border-purple-500/30' : 'bg-gray-900/30 border-gray-700/30 opacity-60'
              }`}>
                <div className="flex items-center justify-between mb-2">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={ind.enabled}
                      onChange={e => updateIndicator(idx, 'enabled', e.target.checked)}
                      className="accent-purple-500"
                    />
                    <span className="text-sm font-semibold text-white">{meta?.label || ind.name}</span>
                  </label>
                  <div className="flex items-center gap-2">
                    <label className="text-[10px] text-gray-500">Weight</label>
                    <input
                      type="number"
                      step={0.1}
                      min={0.1}
                      max={5}
                      className="w-16 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white outline-none"
                      value={ind.weight}
                      onChange={e => updateIndicator(idx, 'weight', parseFloat(e.target.value) || 1)}
                    />
                  </div>
                </div>
                {ind.enabled && (
                  <div className="flex flex-wrap gap-3">
                    {Object.entries(ind.params).map(([key, val]) => (
                      <div key={key} className="flex items-center gap-1.5">
                        <label className="text-[10px] text-gray-500">{key}</label>
                        <input
                          type="number"
                          step={key.includes('mult') ? 0.1 : 1}
                          className="w-16 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white outline-none"
                          value={val}
                          onChange={e => updateIndicatorParam(idx, key, parseFloat(e.target.value) || 0)}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* Pairs */}
      <div>
        <label className="text-xs text-gray-400 mb-2 block font-semibold">PAIRS</label>
        <div className="flex flex-wrap gap-1 mb-2">
          {form.pairs.map((p: string) => (
            <span key={p} className="flex items-center gap-1 px-2 py-0.5 bg-gray-700 text-gray-200 rounded text-xs">
              {p}
              <button onClick={() => removePair(p)} className="text-gray-400 hover:text-red-400"><X className="w-3 h-3" /></button>
            </span>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-white outline-none"
            placeholder="Add pair e.g. SOL/USDT"
            value={pairInput}
            onChange={e => setPairInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && addPair()}
          />
          <button onClick={addPair} className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-xs text-white">Add</button>
          <div className="relative group">
            <button className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-xs text-white">Quick Add ▾</button>
            <div className="hidden group-hover:block absolute right-0 top-full mt-1 bg-gray-800 border border-gray-700 rounded-lg shadow-xl z-20 p-2 w-48 max-h-52 overflow-y-auto">
              {DEFAULT_PAIRS.filter(p => !form.pairs.includes(p)).map(p => (
                <button
                  key={p}
                  onClick={() => update('pairs', [...form.pairs, p])}
                  className="w-full text-left px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 rounded"
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Save / Cancel */}
      <div className="flex gap-2 pt-2">
        <button
          onClick={() => onSave(form)}
          disabled={loading || !form.name.trim()}
          className="flex items-center gap-1.5 px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded text-sm font-semibold text-white transition"
        >
          <Save className="w-4 h-4" /> {loading ? 'Saving…' : 'Save Strategy'}
        </button>
        <button onClick={onCancel} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-sm text-gray-300 transition">
          Cancel
        </button>
      </div>
    </div>
  )
}


// ════════════════════════════════════════════════════════════════
// Pine Script Card (read-only)
// ════════════════════════════════════════════════════════════════

function PineScriptCard({
  script: p,
  onEdit,
  onDelete,
  onCopy,
}: {
  script: PineScriptItem
  onEdit: () => void
  onDelete: () => void
  onCopy: () => void
}) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="bg-gray-800/40 border border-gray-700 rounded-lg overflow-hidden">
      <div
        className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-gray-800/60 transition"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <FileCode2 className="w-4 h-4 text-blue-400" />
          <span className="font-semibold text-white">{p.name}</span>
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
            p.script_type === 'strategy' ? 'bg-orange-500/20 text-orange-400' : 'bg-blue-500/20 text-blue-400'
          }`}>
            {p.script_type}
          </span>
          {p.is_active && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-500/20 text-green-400">
              Active
            </span>
          )}
          {p.strategy_id && <span className="text-[10px] text-gray-500">from strategy #{p.strategy_id}</span>}
          {p.pairs && p.pairs.length > 0 && (
            <span className="text-[10px] text-cyan-500">{p.pairs.join(', ')}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-600">{p.code.split('\n').length} lines</span>
          {expanded ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
        </div>
      </div>

      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-700/50 pt-3">
          {p.description && <p className="text-sm text-gray-400">{p.description}</p>}

          {/* Code preview */}
          <div className="relative">
            <pre className="bg-gray-950 border border-gray-700 rounded-lg p-4 text-xs text-green-300 font-mono overflow-x-auto max-h-96 whitespace-pre">
              {p.code}
            </pre>
            <button
              onClick={(e) => { e.stopPropagation(); onCopy() }}
              className="absolute top-2 right-2 p-1.5 bg-gray-800/80 hover:bg-gray-700 rounded text-gray-400 hover:text-white transition"
              title="Copy to clipboard"
            >
              <Copy className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="bg-gray-900/50 border border-gray-700/50 rounded p-3 text-xs text-gray-400">
            <p className="font-semibold text-gray-300 mb-1">How to use in TradingView:</p>
            <ol className="list-decimal ml-4 space-y-0.5">
              <li>Copy the script above</li>
              <li>Open TradingView → Pine Editor (bottom panel)</li>
              <li>Paste and click <span className="text-green-400">Add to Chart</span></li>
              <li>For webhook alerts: right-click a signal → Create Alert → set webhook URL to your bot&apos;s <code className="text-purple-300">/api/v1/signals/tradingview/webhook</code></li>
            </ol>
          </div>

          {/* Actions */}
          <div className="flex gap-2">
            <button onClick={onCopy} className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-xs text-white transition">
              <Copy className="w-3 h-3" /> Copy Code
            </button>
            <button onClick={onEdit} className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-xs text-white transition">
              <Settings className="w-3 h-3" /> Edit
            </button>
            <button onClick={onDelete} className="flex items-center gap-1 px-3 py-1.5 bg-red-600/20 hover:bg-red-600/30 border border-red-500/30 rounded text-xs text-red-400 transition ml-auto">
              <Trash2 className="w-3 h-3" /> Delete
            </button>
          </div>
        </div>
      )}
    </div>
  )
}


// ════════════════════════════════════════════════════════════════
// Pine Script Form (create/edit)
// ════════════════════════════════════════════════════════════════

function PineScriptForm({
  script,
  onSave,
  onCancel,
  loading,
}: {
  script: PineScriptItem | Omit<PineScriptItem, 'id' | 'created_at' | 'updated_at'>
  onSave: (s: any) => void
  onCancel: () => void
  loading: boolean
}) {
  const [form, setForm] = useState<any>({ ...script })
  const [pairInput, setPairInput] = useState('')
  const update = (key: string, val: any) => setForm((f: any) => ({ ...f, [key]: val }))

  const addPair = () => {
    const p = pairInput.trim().toUpperCase()
    if (p && !(form.pairs || []).includes(p)) {
      update('pairs', [...(form.pairs || []), p])
    }
    setPairInput('')
  }

  const removePair = (pair: string) => {
    update('pairs', (form.pairs || []).filter((p: string) => p !== pair))
  }

  return (
    <div className="bg-gray-800/60 border border-blue-500/30 rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-white text-lg">
          {'id' in form ? 'Edit Pine Script' : 'New Pine Script'}
        </h3>
        <button onClick={onCancel} className="text-gray-400 hover:text-white"><X className="w-5 h-5" /></button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Name</label>
          <input
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
            value={form.name}
            onChange={e => update('name', e.target.value)}
            placeholder="My Pine Script"
          />
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Type</label>
          <select
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
            value={form.script_type}
            onChange={e => update('script_type', e.target.value)}
          >
            <option value="indicator">Indicator</option>
            <option value="strategy">Strategy</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-gray-400 mb-1 block">Description</label>
          <input
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
            value={form.description}
            onChange={e => update('description', e.target.value)}
            placeholder="Optional"
          />
        </div>
        <div className="flex items-end gap-2">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={form.is_active || false}
              onChange={e => update('is_active', e.target.checked)}
              className="w-4 h-4 rounded bg-gray-900 border-gray-600 text-green-500 focus:ring-green-500"
            />
            <span className="text-xs text-gray-300">Active in Signal Pipeline</span>
          </label>
        </div>
      </div>

      {/* Pairs */}
      <div>
        <label className="text-xs text-gray-400 mb-1 block">Trading Pairs (leave empty for all)</label>
        <div className="flex flex-wrap gap-1.5 mb-2">
          {(form.pairs || []).map((p: string) => (
            <span key={p} className="flex items-center gap-1 px-2 py-0.5 bg-cyan-500/20 text-cyan-300 text-xs rounded">
              {p}
              <button onClick={() => removePair(p)} className="hover:text-white">
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-xs text-white focus:border-cyan-500 outline-none"
            value={pairInput}
            onChange={e => setPairInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && (e.preventDefault(), addPair())}
            placeholder="BTC/USDT"
          />
          <button
            onClick={addPair}
            className="px-3 py-1 bg-cyan-600/30 border border-cyan-500/30 rounded text-xs text-cyan-300 hover:bg-cyan-600/50 transition"
          >
            Add
          </button>
          {/* Quick add buttons */}
          {['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT'].filter(p => !(form.pairs || []).includes(p)).slice(0, 3).map(p => (
            <button
              key={p}
              onClick={() => update('pairs', [...(form.pairs || []), p])}
              className="px-2 py-1 bg-gray-700 rounded text-[10px] text-gray-400 hover:text-white hover:bg-gray-600 transition"
            >
              +{p}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="text-xs text-gray-400 mb-1 block">Pine Script Code</label>
        <textarea
          className="w-full bg-gray-950 border border-gray-700 rounded-lg p-4 text-xs text-green-300 font-mono focus:border-blue-500 outline-none min-h-[350px] resize-y"
          value={form.code}
          onChange={e => update('code', e.target.value)}
          spellCheck={false}
          placeholder="//@version=5&#10;indicator(&quot;My Script&quot;, overlay=true)&#10;&#10;plot(close)"
        />
      </div>

      <div className="flex gap-2">
        <button
          onClick={() => onSave(form)}
          disabled={loading || !form.name.trim()}
          className="flex items-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded text-sm font-semibold text-white transition"
        >
          <Save className="w-4 h-4" /> {loading ? 'Saving…' : 'Save Script'}
        </button>
        <button onClick={onCancel} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-sm text-gray-300 transition">
          Cancel
        </button>
      </div>
    </div>
  )
}
