import Head from 'next/head'
import { useState, useEffect, useCallback, useRef } from 'react'
import { apiClient } from '@/services/api'
import { Settings, AlertTriangle, Shield, Target, Activity, Zap, Search, X, Plus, Bell } from 'lucide-react'
import { useStreamState } from '@/hooks/useEventStream'
import {
  notificationsSupported,
  notificationsEnabled,
  setNotificationsEnabled,
  vibrationEnabled,
  setVibrationEnabled,
} from '@/services/notifications'

const CONFIDENCE_OPTIONS = [
  { value: 0.50, label: '50%' },
  { value: 0.55, label: '55%' },
  { value: 0.60, label: '60%' },
  { value: 0.65, label: '65%' },
  { value: 0.70, label: '70%' },
  { value: 0.75, label: '75%' },
  { value: 0.80, label: '80%' },
  { value: 0.85, label: '85%' },
  { value: 0.90, label: '90%' },
  { value: 0.95, label: '95%' },
  { value: 1.00, label: '100%' },
]

const TIMEFRAMES = [
  { label: '1m', value: '1m' },
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
  { label: '4h', value: '4h' },
  { label: '1d', value: '1d' },
]

const SNIPER_ENTRIES_OPTIONS = [
  { value: 1, label: '1 entry' },
  { value: 2, label: '2 entries' },
  { value: 3, label: '3 entries' },
  { value: 4, label: '4 entries' },
  { value: 5, label: '5 entries' },
  { value: 6, label: '6 entries' },
  { value: 7, label: '7 entries' },
  { value: 8, label: '8 entries' },
  { value: 9, label: '9 entries' },
  { value: 10, label: '10 entries' },
]

const AI_PROVIDER_OPTIONS = [
  { value: 'orchestrator', label: 'Built-in Orchestrator' },
  { value: 'tradingagents', label: 'TradingAgents' },
]

const LLM_PROVIDER_OPTIONS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google', label: 'Google' },
]

const ROUND_OPTIONS = [1, 2, 3, 4, 5, 6]

const DEFAULT_PAIRS = [
  'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
  'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 'AVAX/USDT',
  'DOT/USDT', 'LINK/USDT', 'MATIC/USDT', 'NEAR/USDT',
  'ARB/USDT', 'OP/USDT', 'PEPE/USDT', 'SHIB/USDT',
]

interface AvailablePair {
  symbol: string; baseCoin: string; quoteCoin: string; market: string; status: string;
  delisting_ts: number | null; delisting_date: string | null;
  futures_adjustment?: string; maintain_time?: string; limit_open_time?: string;
  minLever: number | null; maxLever: number | null;
}

interface PineScriptItem {
  id: number; name: string; description: string; strategy_id: number | null;
  script_type: string; code: string; pairs: string[]; is_active: boolean;
}

export default function SettingsPage() {
  const [tradingStatus, setTradingStatus] = useState<any>(null)
  const [liveSettings, setLiveSettings] = useState<any>(null)
  const [simSettings, setSimSettings] = useState<any>(null)
  const [saving, setSaving] = useState<string | null>(null)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [configTab, setConfigTab] = useState<'live' | 'sim'>('live')

  // Live editable inputs (deferred save)
  const [liveMaxPositions, setLiveMaxPositions] = useState('')
  const [liveRiskPct, setLiveRiskPct] = useState('')
  const [liveMaxPosSize, setLiveMaxPosSize] = useState('')
  const [liveMaxExposure, setLiveMaxExposure] = useState('')
  const [liveMarginSize, setLiveMarginSize] = useState('')
  const [liveMinGap, setLiveMinGap] = useState('')
  const [liveMinPump, setLiveMinPump] = useState('')
  const [liveTradingAgentsBackendUrl, setLiveTradingAgentsBackendUrl] = useState('')
  const [liveDirty, setLiveDirty] = useState(false)

  // Sim editable inputs (deferred save)
  const [simMaxPositions, setSimMaxPositions] = useState('')
  const [simRiskPct, setSimRiskPct] = useState('')
  const [simMarginSize, setSimMarginSize] = useState('')
  const [simMinGap, setSimMinGap] = useState('')
  const [simMinPump, setSimMinPump] = useState('')
  const [simTradingAgentsBackendUrl, setSimTradingAgentsBackendUrl] = useState('')
  const [simDirty, setSimDirty] = useState(false)

  // Available pairs & pine scripts
  const [availablePairs, setAvailablePairs] = useState<AvailablePair[]>([])
  const [availablePairsLoading, setAvailablePairsLoading] = useState(false)
  const availablePairsFetched = useRef(false)
  const [pineScripts, setPineScripts] = useState<PineScriptItem[]>([])
  const [livePairSearch, setLivePairSearch] = useState('')
  const [showLivePairDropdown, setShowLivePairDropdown] = useState(false)
  const livePairDropdownRef = useRef<HTMLDivElement>(null)
  const [simPairSearch, setSimPairSearch] = useState('')
  const [showSimPairDropdown, setShowSimPairDropdown] = useState(false)
  const simPairDropdownRef = useRef<HTMLDivElement>(null)

  const fetchAll = useCallback(async () => {
    try { const r = await apiClient.getTradingStatus(); setTradingStatus(r.data) } catch {}
    try {
      const r = await apiClient.getLiveTradeSettings()
      setLiveSettings(r.data)
    } catch {}
    try {
      const r = await apiClient.getSimAccount()
      setSimSettings(r.data)
    } catch {}
  }, [])

  useEffect(() => { fetchAll() }, [fetchAll])

  // Fetch available pairs & pine scripts
  useEffect(() => {
    if (availablePairsFetched.current) return
    availablePairsFetched.current = true
    setAvailablePairsLoading(true)
    apiClient.getBitgetAvailablePairs('USDT')
      .then(res => { if (res.data?.pairs) setAvailablePairs(res.data.pairs) })
      .catch(() => {})
      .finally(() => setAvailablePairsLoading(false))
    apiClient.getPineScripts()
      .then(res => { if (Array.isArray(res.data)) setPineScripts(res.data) })
      .catch(() => {})
  }, [])

  // Click-outside for pair dropdowns
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (livePairDropdownRef.current && !livePairDropdownRef.current.contains(e.target as Node)) setShowLivePairDropdown(false)
      if (simPairDropdownRef.current && !simPairDropdownRef.current.contains(e.target as Node)) setShowSimPairDropdown(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Sync live inputs from fetched data
  useEffect(() => {
    if (!liveSettings) return
    setLiveMaxPositions(prev => prev === '' ? String(liveSettings.auto_trade_max_positions ?? 3) : prev)
    setLiveRiskPct(prev => prev === '' ? String(liveSettings.auto_trade_risk_pct ?? 1) : prev)
    setLiveMaxPosSize(prev => prev === '' ? String(liveSettings.max_position_size_usdt ?? 500) : prev)
    setLiveMaxExposure(prev => prev === '' ? String(liveSettings.max_total_exposure_usdt ?? 5000) : prev)
    setLiveMarginSize(prev => prev === '' ? String(liveSettings.margin_size_usdt ?? 10) : prev)
    setLiveMinGap(prev => prev === '' ? String(liveSettings.min_entry_gap_pct ?? 2) : prev)
    setLiveMinPump(prev => prev === '' ? String(liveSettings.min_pump_pct ?? 30) : prev)
    setLiveTradingAgentsBackendUrl(String(liveSettings.tradingagents_backend_url ?? ''))
  }, [liveSettings])

  // Sync sim inputs from fetched data
  useEffect(() => {
    if (!simSettings) return
    setSimMaxPositions(prev => prev === '' ? String(simSettings.auto_trade_max_positions ?? 5) : prev)
    setSimRiskPct(prev => prev === '' ? String(simSettings.auto_trade_risk_pct ?? 2) : prev)
    setSimMarginSize(prev => prev === '' ? String(simSettings.margin_size_usdt ?? 10) : prev)
    setSimMinGap(prev => prev === '' ? String(simSettings.min_entry_gap_pct ?? 2) : prev)
    setSimMinPump(prev => prev === '' ? String(simSettings.min_pump_pct ?? 30) : prev)
    setSimTradingAgentsBackendUrl(String(simSettings.tradingagents_backend_url ?? ''))
  }, [simSettings])

  const showMsg = (msg: string, isError = false) => {
    setSaveMsg(msg)
    setTimeout(() => setSaveMsg(null), 3000)
  }

  const updateLive = async (patch: any) => {
    setSaving('live')
    try {
      const res = await apiClient.updateLiveTradeSettings(patch)
      setLiveSettings(res.data)
      showMsg('Live settings updated')
    } catch {
      showMsg('Failed to save live settings', true)
    }
    setSaving(null)
  }

  const updateSim = async (patch: any) => {
    setSaving('sim')
    try {
      const res = await apiClient.updateSimSettings(patch)
      setSimSettings(res.data)
      showMsg('Sim settings updated')
    } catch {
      showMsg('Failed to save sim settings', true)
    }
    setSaving(null)
  }

  const saveLiveBatch = () => {
    const batch: any = {}
    const maxPos = parseInt(liveMaxPositions, 10)
    if (!isNaN(maxPos) && maxPos >= 1) batch.auto_trade_max_positions = Math.min(maxPos, 100)
    const riskPct = parseFloat(liveRiskPct)
    if (!isNaN(riskPct) && riskPct >= 0.5) batch.auto_trade_risk_pct = Math.min(riskPct, 10)
    const posSize = parseFloat(liveMaxPosSize)
    if (!isNaN(posSize) && posSize >= 10) batch.max_position_size_usdt = posSize
    const exposure = parseFloat(liveMaxExposure)
    if (!isNaN(exposure) && exposure >= 50) batch.max_total_exposure_usdt = exposure
    const marginSize = parseFloat(liveMarginSize)
    if (!isNaN(marginSize) && marginSize >= 1) batch.margin_size_usdt = marginSize
    const minGap = parseFloat(liveMinGap)
    if (!isNaN(minGap) && minGap >= 0.5) batch.min_entry_gap_pct = Math.min(minGap, 20)
    const minPump = parseFloat(liveMinPump)
    if (!isNaN(minPump) && minPump >= 1) batch.min_pump_pct = Math.min(minPump, 500)
    if (Object.keys(batch).length > 0) {
      updateLive(batch)
      setLiveDirty(false)
    }
  }

  const saveSimBatch = () => {
    const batch: any = {}
    const maxPos = parseInt(simMaxPositions, 10)
    if (!isNaN(maxPos) && maxPos >= 1) batch.auto_trade_max_positions = Math.min(maxPos, 100)
    const riskPct = parseFloat(simRiskPct)
    if (!isNaN(riskPct) && riskPct >= 0.5) batch.auto_trade_risk_pct = Math.min(riskPct, 10)
    const marginSize = parseFloat(simMarginSize)
    if (!isNaN(marginSize) && marginSize >= 1) batch.margin_size_usdt = marginSize
    const minGap = parseFloat(simMinGap)
    if (!isNaN(minGap) && minGap >= 0.5) batch.min_entry_gap_pct = Math.min(minGap, 20)
    const minPump = parseFloat(simMinPump)
    if (!isNaN(minPump) && minPump >= 1) batch.min_pump_pct = Math.min(minPump, 500)
    if (Object.keys(batch).length > 0) {
      updateSim(batch)
      setSimDirty(false)
    }
  }

  return (
    <>
      <Head><title>TradeBot - Settings</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        <div>
          <h1 className="text-3xl font-bold text-white">Settings</h1>
          <p className="text-sm text-gray-400 mt-1">
            Exchange configuration, risk parameters, and system settings
          </p>
        </div>

        {/* Save notification */}
        {saveMsg && (
          <div className={`px-4 py-2 rounded text-sm ${saveMsg.includes('Failed') ? 'bg-red-500/20 text-red-300 border border-red-500/30' : 'bg-green-500/20 text-green-300 border border-green-500/30'}`}>
            {saveMsg}
          </div>
        )}

        {/* ─── Realtime & Alerts ─── */}
        <RealtimeAlertsCard />

        {/* ─── Trading Configuration ─── */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold flex items-center gap-2">
              <Target className="w-4 h-4 text-blue-400" /> Trading Configuration
            </h2>
            <div className="flex gap-1 bg-gray-900 rounded-lg p-0.5">
              <button
                onClick={() => setConfigTab('live')}
                className={`px-3 py-1 text-xs font-semibold rounded transition ${
                  configTab === 'live'
                    ? 'bg-green-600 text-white'
                    : 'text-gray-400 hover:text-gray-200'
                }`}
              >
                Live
              </button>
              <button
                onClick={() => setConfigTab('sim')}
                className={`px-3 py-1 text-xs font-semibold rounded transition ${
                  configTab === 'sim'
                    ? 'bg-purple-600 text-white'
                    : 'text-gray-400 hover:text-gray-200'
                }`}
              >
                Simulation
              </button>
            </div>
          </div>

          {/* ── Live Tab ── */}
          {configTab === 'live' && liveSettings && (
            <div className="space-y-4">
              {/* Toggle row */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Live Trading</label>
                  <button
                    onClick={() => updateLive({ is_active: !liveSettings.is_active })}
                    disabled={saving === 'live'}
                    className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                      liveSettings.is_active ? 'bg-green-600 text-white' : 'bg-gray-800 text-gray-400 border border-gray-700'
                    }`}
                  >
                    {liveSettings.is_active ? 'Active' : 'Inactive'}
                  </button>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Auto-Trade</label>
                  <button
                    onClick={() => updateLive({ auto_trade: !liveSettings.auto_trade })}
                    disabled={saving === 'live'}
                    className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                      liveSettings.auto_trade ? 'bg-green-600 text-white' : 'bg-gray-800 text-gray-400 border border-gray-700'
                    }`}
                  >
                    {liveSettings.auto_trade ? 'Enabled' : 'Disabled'}
                  </button>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Dry Run</label>
                  <button
                    onClick={() => updateLive({ dry_run: !liveSettings.dry_run })}
                    disabled={saving === 'live'}
                    className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                      liveSettings.dry_run ? 'bg-amber-600 text-white' : 'bg-gray-800 text-gray-400 border border-gray-700'
                    }`}
                  >
                    {liveSettings.dry_run ? 'Planning Only' : 'Real Orders'}
                  </button>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">AI Agents</label>
                  <button
                    onClick={() => updateLive({ enable_ai: !liveSettings.enable_ai })}
                    disabled={saving === 'live'}
                    className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                      liveSettings.enable_ai ? 'bg-purple-600 text-white' : 'bg-gray-800 text-gray-400 border border-gray-700'
                    }`}
                  >
                    {liveSettings.enable_ai ? 'AI On' : 'AI Off'}
                  </button>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Trade Mode</label>
                  <div className="flex gap-1">
                    {(['spot', 'futures'] as const).map(mode => (
                      <button
                        key={mode}
                        onClick={() => updateLive({ auto_trade_mode: mode })}
                        disabled={saving === 'live'}
                        className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                          (liveSettings.auto_trade_mode || 'futures') === mode
                            ? mode === 'futures' ? 'bg-orange-600 text-white' : 'bg-blue-600 text-white'
                            : 'bg-gray-800 text-gray-400 border border-gray-700'
                        }`}
                      >
                        {mode.charAt(0).toUpperCase() + mode.slice(1)}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* AI provider config */}
              <div className="p-3 bg-purple-500/5 border border-purple-500/20 rounded space-y-3">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">AI Provider</label>
                    <select
                      value={liveSettings.auto_trade_ai_provider || 'orchestrator'}
                      onChange={e => updateLive({ auto_trade_ai_provider: e.target.value })}
                      disabled={saving === 'live' || !liveSettings.enable_ai}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                    >
                      {AI_PROVIDER_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                    </select>
                  </div>
                  {(liveSettings.auto_trade_ai_provider || 'orchestrator') === 'tradingagents' && (
                    <div>
                      <label className="text-xs text-gray-400 block mb-1">LLM Provider</label>
                      <select
                        value={liveSettings.tradingagents_llm_provider || 'openai'}
                        onChange={e => updateLive({ tradingagents_llm_provider: e.target.value })}
                        disabled={saving === 'live' || !liveSettings.enable_ai}
                        className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                      >
                        {LLM_PROVIDER_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                      </select>
                    </div>
                  )}
                </div>
                {(liveSettings.auto_trade_ai_provider || 'orchestrator') === 'tradingagents' && (
                  <>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Deep Think Model</label>
                        <input
                          value={liveSettings.tradingagents_deep_think_llm || 'gpt-5.4'}
                          onChange={e => updateLive({ tradingagents_deep_think_llm: e.target.value })}
                          disabled={saving === 'live' || !liveSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Quick Think Model</label>
                        <input
                          value={liveSettings.tradingagents_quick_think_llm || 'gpt-5.4-mini'}
                          onChange={e => updateLive({ tradingagents_quick_think_llm: e.target.value })}
                          disabled={saving === 'live' || !liveSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        />
                      </div>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Backend URL (optional)</label>
                        <input
                          value={liveTradingAgentsBackendUrl}
                          onChange={e => setLiveTradingAgentsBackendUrl(e.target.value)}
                          onBlur={() => updateLive({ tradingagents_backend_url: liveTradingAgentsBackendUrl.trim() || null })}
                          disabled={saving === 'live' || !liveSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                          placeholder="http://localhost:8000"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Debate Rounds</label>
                        <select
                          value={liveSettings.tradingagents_max_debate_rounds || 2}
                          onChange={e => updateLive({ tradingagents_max_debate_rounds: parseInt(e.target.value) })}
                          disabled={saving === 'live' || !liveSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        >
                          {ROUND_OPTIONS.map(round => <option key={round} value={round}>{round}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Risk Discuss Rounds</label>
                        <select
                          value={liveSettings.tradingagents_max_risk_discuss_rounds || 2}
                          onChange={e => updateLive({ tradingagents_max_risk_discuss_rounds: parseInt(e.target.value) })}
                          disabled={saving === 'live' || !liveSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        >
                          {ROUND_OPTIONS.map(round => <option key={round} value={round}>{round}</option>)}
                        </select>
                      </div>
                    </div>
                  </>
                )}
              </div>

              {/* Selects */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
                  <select
                    value={liveSettings.auto_trade_timeframe || '1h'}
                    onChange={e => updateLive({ auto_trade_timeframe: e.target.value })}
                    disabled={saving === 'live'}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {TIMEFRAMES.map(tf => <option key={tf.value} value={tf.value}>{tf.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Amount Mode</label>
                  <div className="flex bg-gray-900 rounded overflow-hidden border border-gray-700">
                    {(['quote', 'base'] as const).map(mode => (
                      <button
                        key={mode}
                        onClick={() => updateLive({ auto_trade_amount_mode: mode })}
                        disabled={saving === 'live'}
                        className={`flex-1 px-2 py-1.5 text-xs font-semibold transition ${
                          (liveSettings.auto_trade_amount_mode || 'quote') === mode
                            ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-gray-200'
                        }`}
                      >
                        {mode === 'quote' ? 'USDT' : 'Pair Qty'}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Min Confidence</label>
                  <select
                    value={liveSettings.min_confidence ?? 0.90}
                    onChange={e => updateLive({ min_confidence: parseFloat(e.target.value) })}
                    disabled={saving === 'live'}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {CONFIDENCE_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Sniper Entries</label>
                  <select
                    value={liveSettings.sniper_max_entries ?? 1}
                    onChange={e => updateLive({ sniper_max_entries: parseInt(e.target.value) })}
                    disabled={saving === 'live'}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {SNIPER_ENTRIES_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                  </select>
                </div>
                {(liveSettings.auto_trade_mode || 'futures') === 'futures' && (
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Margin Mode</label>
                    <div className="flex gap-1">
                      {(['crossed', 'isolated'] as const).map(mode => (
                        <button
                          key={mode}
                          onClick={() => updateLive({ auto_trade_margin_mode: mode })}
                          disabled={saving === 'live'}
                          className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                            (liveSettings.auto_trade_margin_mode || 'crossed') === mode
                              ? mode === 'crossed' ? 'bg-blue-600 text-white' : 'bg-purple-600 text-white'
                              : 'bg-gray-800 text-gray-400 border border-gray-700'
                          }`}
                        >
                          {mode === 'crossed' ? 'Cross' : 'Isolated'}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Futures leverage */}
              {(liveSettings.auto_trade_mode || 'futures') === 'futures' && (
                <div className="p-3 bg-orange-500/5 border border-orange-500/20 rounded">
                  <label className="text-xs text-gray-400 block mb-1">Leverage</label>
                  <div className="flex items-center gap-3">
                    <input
                      type="range"
                      min="1"
                      max="125"
                      value={liveSettings.auto_trade_leverage || 10}
                      onChange={e => updateLive({ auto_trade_leverage: Number(e.target.value) })}
                      disabled={saving === 'live'}
                      className="flex-1"
                    />
                    <span className="text-xs font-mono text-orange-300 w-10 text-right">
                      {liveSettings.auto_trade_leverage || 10}x
                    </span>
                  </div>
                </div>
              )}

              {/* Numeric inputs */}
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                <NumberInput label="Max Positions" value={liveMaxPositions} onChange={v => { setLiveMaxPositions(v); setLiveDirty(true) }} min={1} max={100} step={1} />
                <NumberInput label="Risk per Trade %" value={liveRiskPct} onChange={v => { setLiveRiskPct(v); setLiveDirty(true) }} min={0.5} max={10} step={0.5} />
                <NumberInput label="Max Position Size ($)" value={liveMaxPosSize} onChange={v => { setLiveMaxPosSize(v); setLiveDirty(true) }} min={10} step={10} />
                <NumberInput label="Max Total Exposure ($)" value={liveMaxExposure} onChange={v => { setLiveMaxExposure(v); setLiveDirty(true) }} min={50} step={50} />
                <NumberInput label="Margin Size ($)" value={liveMarginSize} onChange={v => { setLiveMarginSize(v); setLiveDirty(true) }} min={1} step={1} hint="Exact USDT margin per trade" />
                <NumberInput label="Entry Gap %" value={liveMinGap} onChange={v => { setLiveMinGap(v); setLiveDirty(true) }} min={0.5} max={20} step={0.5} hint="% distance between sniper/DCA entries" />
                <NumberInput label="Pump 24h %" value={liveMinPump} onChange={v => { setLiveMinPump(v); setLiveDirty(true) }} min={1} max={500} step={1} hint="Min 24h % gain to flag as rug pull" />
              </div>

              {/* Auto-Trade Pairs */}
              <PairSelector
                pairs={liveSettings.auto_trade_pairs || []}
                availablePairs={availablePairs}
                availablePairsLoading={availablePairsLoading}
                pairSearch={livePairSearch}
                setPairSearch={setLivePairSearch}
                showDropdown={showLivePairDropdown}
                setShowDropdown={setShowLivePairDropdown}
                dropdownRef={livePairDropdownRef}
                onAdd={pair => {
                  const updated = [...(liveSettings.auto_trade_pairs || []), pair]
                  updateLive({ auto_trade_pairs: updated })
                }}
                onRemove={pair => {
                  const updated = (liveSettings.auto_trade_pairs || []).filter((p: string) => p !== pair)
                  updateLive({ auto_trade_pairs: updated })
                }}
                accentColor="green"
              />

              {/* Pine Script for Entries */}
              <div>
                <label className="text-xs text-gray-400 block mb-1">Pine Script for Entries (50% weight)</label>
                <select
                  value={liveSettings.auto_trade_pine_script_id || ''}
                  onChange={e => updateLive({ auto_trade_pine_script_id: Number(e.target.value) || 0 })}
                  disabled={saving === 'live'}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                >
                  <option value="">None — TA + Sentiment only</option>
                  {pineScripts.map(ps => (
                    <option key={ps.id} value={ps.id}>
                      🌲 {ps.name} {ps.is_active ? '●' : ''} {ps.strategy_id ? '(linked)' : ''}
                    </option>
                  ))}
                </select>
              </div>

              {/* Save button */}
              <div className="flex items-center gap-3 pt-2 border-t border-gray-700/50">
                <button
                  onClick={saveLiveBatch}
                  disabled={!liveDirty || saving === 'live'}
                  className={`flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded transition ${
                    liveDirty
                      ? 'bg-blue-600 text-white hover:bg-blue-500 shadow-lg shadow-blue-600/20'
                      : 'bg-gray-800 text-gray-500 border border-gray-700 cursor-not-allowed'
                  }`}
                >
                  <Settings className="w-3.5 h-3.5" />
                  {liveDirty ? 'Save Settings' : 'Settings Saved'}
                </button>
                {liveDirty && (
                  <span className="text-xs text-amber-400 flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" /> Unsaved changes
                  </span>
                )}
              </div>
            </div>
          )}
          {configTab === 'live' && !liveSettings && (
            <div className="text-gray-400 text-sm">Loading live settings...</div>
          )}

          {/* ── Sim Tab ── */}
          {configTab === 'sim' && simSettings && (
            <div className="space-y-4">
              {/* Toggle row */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Auto-Trade</label>
                  <button
                    onClick={() => updateSim({ auto_trade: !simSettings.auto_trade })}
                    disabled={saving === 'sim'}
                    className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                      simSettings.auto_trade ? 'bg-green-600 text-white' : 'bg-gray-800 text-gray-400 border border-gray-700'
                    }`}
                  >
                    {simSettings.auto_trade ? 'Enabled' : 'Disabled'}
                  </button>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">AI Agents</label>
                  <button
                    onClick={() => updateSim({ enable_ai: !simSettings.enable_ai })}
                    disabled={saving === 'sim'}
                    className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                      simSettings.enable_ai ? 'bg-purple-600 text-white' : 'bg-gray-800 text-gray-400 border border-gray-700'
                    }`}
                  >
                    {simSettings.enable_ai ? 'AI On' : 'AI Off'}
                  </button>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Trade Mode</label>
                  <div className="flex gap-1">
                    {(['spot', 'futures'] as const).map(mode => (
                      <button
                        key={mode}
                        onClick={() => updateSim({ auto_trade_mode: mode })}
                        disabled={saving === 'sim'}
                        className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                          (simSettings.auto_trade_mode || 'spot') === mode
                            ? mode === 'futures' ? 'bg-orange-600 text-white' : 'bg-blue-600 text-white'
                            : 'bg-gray-800 text-gray-400 border border-gray-700'
                        }`}
                      >
                        {mode.charAt(0).toUpperCase() + mode.slice(1)}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
                  <select
                    value={simSettings.auto_trade_timeframe || '1h'}
                    onChange={e => updateSim({ auto_trade_timeframe: e.target.value })}
                    disabled={saving === 'sim'}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {TIMEFRAMES.map(tf => <option key={tf.value} value={tf.value}>{tf.label}</option>)}
                  </select>
                </div>
              </div>

              {/* AI provider config */}
              <div className="p-3 bg-purple-500/5 border border-purple-500/20 rounded space-y-3">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">AI Provider</label>
                    <select
                      value={simSettings.auto_trade_ai_provider || 'orchestrator'}
                      onChange={e => updateSim({ auto_trade_ai_provider: e.target.value })}
                      disabled={saving === 'sim' || !simSettings.enable_ai}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                    >
                      {AI_PROVIDER_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                    </select>
                  </div>
                  {(simSettings.auto_trade_ai_provider || 'orchestrator') === 'tradingagents' && (
                    <div>
                      <label className="text-xs text-gray-400 block mb-1">LLM Provider</label>
                      <select
                        value={simSettings.tradingagents_llm_provider || 'openai'}
                        onChange={e => updateSim({ tradingagents_llm_provider: e.target.value })}
                        disabled={saving === 'sim' || !simSettings.enable_ai}
                        className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                      >
                        {LLM_PROVIDER_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                      </select>
                    </div>
                  )}
                </div>
                {(simSettings.auto_trade_ai_provider || 'orchestrator') === 'tradingagents' && (
                  <>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Deep Think Model</label>
                        <input
                          value={simSettings.tradingagents_deep_think_llm || 'gpt-5.4'}
                          onChange={e => updateSim({ tradingagents_deep_think_llm: e.target.value })}
                          disabled={saving === 'sim' || !simSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Quick Think Model</label>
                        <input
                          value={simSettings.tradingagents_quick_think_llm || 'gpt-5.4-mini'}
                          onChange={e => updateSim({ tradingagents_quick_think_llm: e.target.value })}
                          disabled={saving === 'sim' || !simSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        />
                      </div>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Backend URL (optional)</label>
                        <input
                          value={simTradingAgentsBackendUrl}
                          onChange={e => setSimTradingAgentsBackendUrl(e.target.value)}
                          onBlur={() => updateSim({ tradingagents_backend_url: simTradingAgentsBackendUrl.trim() || null })}
                          disabled={saving === 'sim' || !simSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                          placeholder="http://localhost:8000"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Debate Rounds</label>
                        <select
                          value={simSettings.tradingagents_max_debate_rounds || 2}
                          onChange={e => updateSim({ tradingagents_max_debate_rounds: parseInt(e.target.value) })}
                          disabled={saving === 'sim' || !simSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        >
                          {ROUND_OPTIONS.map(round => <option key={round} value={round}>{round}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className="text-xs text-gray-400 block mb-1">Risk Discuss Rounds</label>
                        <select
                          value={simSettings.tradingagents_max_risk_discuss_rounds || 2}
                          onChange={e => updateSim({ tradingagents_max_risk_discuss_rounds: parseInt(e.target.value) })}
                          disabled={saving === 'sim' || !simSettings.enable_ai}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                        >
                          {ROUND_OPTIONS.map(round => <option key={round} value={round}>{round}</option>)}
                        </select>
                      </div>
                    </div>
                  </>
                )}
              </div>

              {/* Selects row 2 */}
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Amount Mode</label>
                  <div className="flex bg-gray-900 rounded overflow-hidden border border-gray-700">
                    {(['quote', 'base'] as const).map(mode => (
                      <button
                        key={mode}
                        onClick={() => updateSim({ auto_trade_amount_mode: mode })}
                        disabled={saving === 'sim'}
                        className={`flex-1 px-2 py-1.5 text-xs font-semibold transition ${
                          (simSettings.auto_trade_amount_mode || 'quote') === mode
                            ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-gray-200'
                        }`}
                      >
                        {mode === 'quote' ? 'USDT' : 'Pair Qty'}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Min Confidence</label>
                  <select
                    value={simSettings.min_confidence ?? 0.90}
                    onChange={e => updateSim({ min_confidence: parseFloat(e.target.value) })}
                    disabled={saving === 'sim'}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {CONFIDENCE_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Sniper Entries</label>
                  <select
                    value={simSettings.sniper_max_entries ?? 1}
                    onChange={e => updateSim({ sniper_max_entries: parseInt(e.target.value) })}
                    disabled={saving === 'sim'}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {SNIPER_ENTRIES_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                  </select>
                </div>
                {(simSettings.auto_trade_mode || 'spot') === 'futures' && (
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Margin Mode</label>
                    <div className="flex gap-1">
                      {(['crossed', 'isolated'] as const).map(mode => (
                        <button
                          key={mode}
                          onClick={() => updateSim({ auto_trade_margin_mode: mode })}
                          disabled={saving === 'sim'}
                          className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                            (simSettings.auto_trade_margin_mode || 'crossed') === mode
                              ? mode === 'crossed' ? 'bg-blue-600 text-white' : 'bg-purple-600 text-white'
                              : 'bg-gray-800 text-gray-400 border border-gray-700'
                          }`}
                        >
                          {mode === 'crossed' ? 'Cross' : 'Isolated'}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Futures leverage */}
              {(simSettings.auto_trade_mode || 'spot') === 'futures' && (
                <div className="p-3 bg-orange-500/5 border border-orange-500/20 rounded">
                  <label className="text-xs text-gray-400 block mb-1">Leverage</label>
                  <div className="flex items-center gap-3">
                    <input
                      type="range"
                      min="1"
                      max="125"
                      value={simSettings.auto_trade_leverage || 10}
                      onChange={e => updateSim({ auto_trade_leverage: Number(e.target.value) })}
                      disabled={saving === 'sim'}
                      className="flex-1"
                    />
                    <span className="text-xs font-mono text-orange-300 w-10 text-right">
                      {simSettings.auto_trade_leverage || 10}x
                    </span>
                  </div>
                </div>
              )}

              {/* Numeric inputs */}
              <div className="grid grid-cols-2 md:grid-cols-2 gap-3">
                <NumberInput label="Max Positions" value={simMaxPositions} onChange={v => { setSimMaxPositions(v); setSimDirty(true) }} min={1} max={100} step={1} />
                <NumberInput label="Risk per Trade %" value={simRiskPct} onChange={v => { setSimRiskPct(v); setSimDirty(true) }} min={0.5} max={10} step={0.5} />
                <NumberInput label="Margin Size ($)" value={simMarginSize} onChange={v => { setSimMarginSize(v); setSimDirty(true) }} min={1} step={1} hint="Exact USDT margin per trade" />
                <NumberInput label="Entry Gap %" value={simMinGap} onChange={v => { setSimMinGap(v); setSimDirty(true) }} min={0.5} max={20} step={0.5} hint="% distance between sniper/DCA entries" />
                  <NumberInput label="Pump 24h %" value={simMinPump} onChange={v => { setSimMinPump(v); setSimDirty(true) }} min={1} max={500} step={1} hint="Min 24h % gain to flag as rug pull" />
              </div>

              {/* Auto-Trade Pairs */}
              <PairSelector
                pairs={simSettings.auto_trade_pairs || []}
                availablePairs={availablePairs}
                availablePairsLoading={availablePairsLoading}
                pairSearch={simPairSearch}
                setPairSearch={setSimPairSearch}
                showDropdown={showSimPairDropdown}
                setShowDropdown={setShowSimPairDropdown}
                dropdownRef={simPairDropdownRef}
                onAdd={pair => {
                  const updated = [...(simSettings.auto_trade_pairs || []), pair]
                  updateSim({ auto_trade_pairs: updated })
                }}
                onRemove={pair => {
                  const updated = (simSettings.auto_trade_pairs || []).filter((p: string) => p !== pair)
                  updateSim({ auto_trade_pairs: updated })
                }}
                accentColor="purple"
              />

              {/* Pine Script for Entries */}
              <div>
                <label className="text-xs text-gray-400 block mb-1">Pine Script for Entries (50% weight)</label>
                <select
                  value={simSettings.auto_trade_pine_script_id || ''}
                  onChange={e => updateSim({ auto_trade_pine_script_id: Number(e.target.value) || 0 })}
                  disabled={saving === 'sim'}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white disabled:opacity-50"
                >
                  <option value="">None — TA + Sentiment only</option>
                  {pineScripts.map(ps => (
                    <option key={ps.id} value={ps.id}>
                      🌲 {ps.name} {ps.is_active ? '●' : ''} {ps.strategy_id ? '(linked)' : ''}
                    </option>
                  ))}
                </select>
              </div>

              {/* Save button */}
              <div className="flex items-center gap-3 pt-2 border-t border-gray-700/50">
                <button
                  onClick={saveSimBatch}
                  disabled={!simDirty || saving === 'sim'}
                  className={`flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded transition ${
                    simDirty
                      ? 'bg-purple-600 text-white hover:bg-purple-500 shadow-lg shadow-purple-600/20'
                      : 'bg-gray-800 text-gray-500 border border-gray-700 cursor-not-allowed'
                  }`}
                >
                  <Settings className="w-3.5 h-3.5" />
                  {simDirty ? 'Save Settings' : 'Settings Saved'}
                </button>
                {simDirty && (
                  <span className="text-xs text-amber-400 flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" /> Unsaved changes
                  </span>
                )}
              </div>
            </div>
          )}
          {configTab === 'sim' && !simSettings && (
            <div className="text-gray-400 text-sm">Loading sim settings...</div>
          )}
        </div>

        {/* Quick Links */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <h2 className="font-semibold mb-4">Quick Links</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <QuickLink
              href={`${process.env.NEXT_PUBLIC_API_URL?.replace('/api/v1', '')}/docs`}
              label="Swagger UI"
            />
            <QuickLink
              href={`${process.env.NEXT_PUBLIC_API_URL?.replace('/api/v1', '')}/health`}
              label="Health Check"
            />
            <QuickLink
              href={`${process.env.NEXT_PUBLIC_API_URL}/status`}
              label="API Status"
            />
            <QuickLink
              href={`${process.env.NEXT_PUBLIC_API_URL}/exchanges/status`}
              label="Exchange Status"
            />
            <QuickLink
              href="/telegram"
              label="Telegram Config"
            />
            <QuickLink
              href="/telegram-signals"
              label="Telegram Signals"
            />
          </div>
        </div>

        {/* Environment Info */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <h2 className="font-semibold mb-4">Environment</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
            <div className="flex justify-between py-2 border-b border-gray-700/50">
              <span className="text-gray-400">API URL</span>
              <span className="font-mono text-xs">{process.env.NEXT_PUBLIC_API_URL}</span>
            </div>
            <div className="flex justify-between py-2 border-b border-gray-700/50">
              <span className="text-gray-400">Frontend</span>
              <span className="font-mono text-xs">Next.js 14</span>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

function NumberInput({
  label, value, onChange, min, max, step, hint,
}: {
  label: string; value: string; onChange: (v: string) => void;
  min?: number; max?: number; step?: number; hint?: string;
}) {
  return (
    <div>
      <label className="text-xs text-gray-400 block mb-1">{label}</label>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={e => onChange(e.target.value)}
        className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
      />
      {hint && <p className="text-[10px] text-gray-500 mt-1">{hint}</p>}
    </div>
  )
}

function QuickLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="bg-gray-700/50 hover:bg-gray-700 border border-gray-600 rounded-lg p-3 text-center text-sm transition"
    >
      {label}
    </a>
  )
}

function PairSelector({
  pairs, availablePairs, availablePairsLoading,
  pairSearch, setPairSearch, showDropdown, setShowDropdown, dropdownRef,
  onAdd, onRemove, accentColor,
}: {
  pairs: string[]; availablePairs: AvailablePair[]; availablePairsLoading: boolean;
  pairSearch: string; setPairSearch: (v: string) => void;
  showDropdown: boolean; setShowDropdown: (v: boolean) => void;
  dropdownRef: React.RefObject<HTMLDivElement | null>;
  onAdd: (pair: string) => void; onRemove: (pair: string) => void;
  accentColor: 'green' | 'purple';
}) {
  const tagBg = accentColor === 'green' ? 'bg-green-600/20 border-green-500/30 text-green-300' : 'bg-blue-600/20 border-blue-500/30 text-blue-300'
  const focusColor = accentColor === 'green' ? 'focus:border-green-500' : 'focus:border-purple-500'
  return (
    <div>
      <label className="text-xs text-gray-400 block mb-1">Auto-Trade Pairs</label>
      {/* Delisting warnings */}
      {(() => {
        const warnings = pairs
          .map(p => availablePairs.find(ap => ap.symbol === p))
          .filter(ap => ap && (ap.delisting_ts || ap.futures_adjustment || ap.maintain_time || ap.limit_open_time || !['online', 'normal'].includes(ap.status)))
        if (warnings.length === 0) return null
        return (
          <div className="mb-2 space-y-1">
            {warnings.map(w => w && (
              <div key={w.symbol} className="flex items-start gap-1.5 px-2 py-1 text-[10px] rounded bg-red-500/10 border border-red-500/30 text-red-300">
                <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                <span>
                  <strong>{w.symbol}</strong>
                  {w.delisting_date && <> — Delisting: {w.delisting_date}</>}
                  {w.futures_adjustment && <> — Futures: {w.futures_adjustment}</>}
                  {w.maintain_time && <> — Maintenance: {w.maintain_time}</>}
                  {w.limit_open_time && <> — Restricted after: {w.limit_open_time}</>}
                  {!w.delisting_date && !w.futures_adjustment && !w.maintain_time && !w.limit_open_time && <> — Status: {w.status}</>}
                </span>
              </div>
            ))}
          </div>
        )
      })()}
      {/* Selected pairs as tags */}
      <div className="flex flex-wrap gap-1.5 mb-2 min-h-[28px]">
        {pairs.map(pair => {
          const pairInfo = availablePairs.find(ap => ap.symbol === pair)
          const isWarning = pairInfo && (pairInfo.delisting_ts || pairInfo.futures_adjustment || !['online', 'normal'].includes(pairInfo.status))
          return (
            <span
              key={pair}
              className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full border ${
                isWarning ? 'bg-red-600/20 border-red-500/30 text-red-300' : tagBg
              }`}
            >
              {isWarning && <AlertTriangle className="w-2.5 h-2.5" />}
              {pair}
              <button onClick={() => onRemove(pair)} className="hover:text-red-400 transition">
                <X className="w-3 h-3" />
              </button>
            </span>
          )
        })}
        {pairs.length === 0 && (
          <span className="text-xs text-gray-600 italic">No pairs selected — search below to add</span>
        )}
      </div>
      {/* Search input with dropdown */}
      <div className="relative" ref={dropdownRef}>
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
          <input
            type="text"
            value={pairSearch}
            onChange={e => { setPairSearch(e.target.value); setShowDropdown(true) }}
            onFocus={() => setShowDropdown(true)}
            placeholder={availablePairsLoading ? 'Loading pairs...' : `Search ${availablePairs.length || ''} pairs... (BTC, ETH, SOL)`}
            className={`w-full bg-gray-900 border border-gray-700 rounded pl-8 pr-3 py-1.5 text-xs text-white ${focusColor} outline-none`}
          />
        </div>
        {showDropdown && (
          <div className="absolute z-50 w-full mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl max-h-60 overflow-y-auto">
            {(() => {
              const q = pairSearch.toUpperCase()
              const selected = new Set(pairs)
              const filtered = (availablePairs.length > 0 ? availablePairs : DEFAULT_PAIRS.map(p => ({ symbol: p, baseCoin: p.split('/')[0], quoteCoin: 'USDT', market: 'spot', status: 'online', delisting_ts: null, delisting_date: null, minLever: null, maxLever: null })))
                .filter(p => (!q || p.symbol.toUpperCase().includes(q) || p.baseCoin.toUpperCase().includes(q)) && !selected.has(p.symbol))
                .slice(0, 100)
              if (filtered.length === 0) {
                return <div className="p-3 text-xs text-gray-500 text-center">{pairSearch ? 'No matching pairs' : 'All pairs already added'}</div>
              }
              return filtered.map(pair => {
                const isDelisting = !!pair.delisting_ts
                const isAbnormal = pair.status && !['online', 'normal'].includes(pair.status)
                return (
                  <button
                    key={pair.symbol}
                    onClick={() => { onAdd(pair.symbol); setPairSearch(''); setShowDropdown(false) }}
                    className={`w-full text-left px-3 py-2 text-xs transition flex items-center justify-between gap-2 ${
                      isDelisting ? 'hover:bg-red-900/30 bg-red-900/10' : 'hover:bg-gray-700'
                    } text-white`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-medium">{pair.symbol}</span>
                      <span className={`text-[10px] px-1 py-0.5 rounded ${
                        pair.market === 'both' ? 'bg-purple-500/20 text-purple-300' :
                        pair.market === 'futures' ? 'bg-orange-500/20 text-orange-300' :
                        'bg-blue-500/20 text-blue-300'
                      }`}>
                        {pair.market === 'both' ? 'S+F' : pair.market === 'futures' ? 'FUT' : 'SPOT'}
                      </span>
                      {pair.maxLever && <span className="text-[10px] text-gray-500">{pair.maxLever}x</span>}
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {isDelisting && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 flex items-center gap-0.5">
                          <AlertTriangle className="w-2.5 h-2.5" /> Delisting {pair.delisting_date}
                        </span>
                      )}
                      {isAbnormal && !isDelisting && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300">{pair.status}</span>
                      )}
                      <Plus className="w-3 h-3 text-gray-500" />
                    </div>
                  </button>
                )
              })
            })()}
          </div>
        )}
      </div>
    </div>
  )
}

const WAKELOCK_PREF_KEY = 'tradebot.wakelock.enabled'

function RealtimeAlertsCard() {
  const streamState = useStreamState()
  const [notifOn, setNotifOn] = useState(false)
  const [vibrateOn, setVibrateOn] = useState(true)
  const [wakeOn, setWakeOn] = useState(false)
  const [busy, setBusy] = useState(false)
  const supported = notificationsSupported()

  useEffect(() => {
    setNotifOn(notificationsEnabled())
    setVibrateOn(vibrationEnabled())
    try { setWakeOn(localStorage.getItem(WAKELOCK_PREF_KEY) === '1') } catch { /* ignore */ }
  }, [])

  const toggleNotif = async () => {
    setBusy(true)
    const next = !notifOn
    const ok = await setNotificationsEnabled(next)
    setNotifOn(ok && next)
    setBusy(false)
  }

  const toggleVibrate = () => {
    const next = !vibrateOn
    setVibrateOn(next)
    setVibrationEnabled(next)
  }

  const toggleWake = () => {
    const next = !wakeOn
    setWakeOn(next)
    try { localStorage.setItem(WAKELOCK_PREF_KEY, next ? '1' : '0') } catch { /* ignore */ }
  }

  const streamColor =
    streamState === 'live' ? 'text-emerald-400'
    : streamState === 'reconnecting' || streamState === 'connecting' ? 'text-yellow-400'
    : 'text-gray-500'

  const Row = ({ label, hint, checked, onClick, disabled }: {
    label: string; hint: string; checked: boolean; onClick: () => void; disabled?: boolean
  }) => (
    <div className="flex items-center justify-between py-2.5">
      <div>
        <div className="text-sm text-gray-200">{label}</div>
        <div className="text-xs text-gray-500">{hint}</div>
      </div>
      <button
        onClick={onClick}
        disabled={disabled}
        role="switch"
        aria-checked={checked}
        aria-label={label}
        className={`relative w-11 h-6 rounded-full transition ${checked ? 'bg-emerald-600' : 'bg-gray-600'} ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
      >
        <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-5' : ''}`} />
      </button>
    </div>
  )

  return (
    <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-semibold flex items-center gap-2">
          <Bell className="w-4 h-4 text-emerald-400" /> Realtime &amp; Alerts
        </h2>
        <span className={`text-xs flex items-center gap-1.5 ${streamColor}`}>
          <span className={`w-2 h-2 rounded-full ${streamState === 'live' ? 'bg-emerald-500 animate-pulse' : streamState === 'closed' ? 'bg-gray-500' : 'bg-yellow-500 animate-pulse'}`} />
          {streamState === 'live' ? 'Stream live' : streamState === 'closed' ? 'Polling (offline)' : 'Connecting…'}
        </span>
      </div>

      <div className="divide-y divide-gray-700/50">
        <Row
          label="Desktop notifications"
          hint={supported ? 'New signals, fills, and TP/SL hits (asks browser permission)' : 'Not supported in this browser'}
          checked={notifOn}
          onClick={toggleNotif}
          disabled={!supported || busy}
        />
        <Row
          label="Vibration on critical alerts"
          hint="Buzz on TP/SL hits and new signals (mobile)"
          checked={vibrateOn}
          onClick={toggleVibrate}
          disabled={!notifOn}
        />
        <Row
          label="Keep screen awake during live trading"
          hint="Holds a screen Wake Lock while the live trading tab is open"
          checked={wakeOn}
          onClick={toggleWake}
        />
      </div>
    </div>
  )
}
