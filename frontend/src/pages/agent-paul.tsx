import Head from 'next/head'
import { useCallback, useEffect, useState } from 'react'
import { apiClient, api } from '@/services/api'
import {
  Workflow,
  Play,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Clock,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  Power,
  Loader2,
  Zap,
  ListChecks,
  History,
  Settings as SettingsIcon,
  TrendingUp,
  TrendingDown,
  Minus,
  BrainCircuit,
  Webhook,
  Plus,
  Pencil,
  Trash2,
  ToggleLeft,
  ToggleRight,
  Mic,
  Cpu,
  Wifi,
  WifiOff,
  FlaskConical,
  ChevronDown,
  ChevronUp,
  BarChart2,
} from 'lucide-react'

type Mode = 'paper' | 'tradebot_execute' | 'paul_execute'

interface PaulSettings {
  enabled: boolean
  mode: Mode
  require_approval: boolean
  kill_switch: boolean
  default_timeframe: string
  min_confidence: number
  allowed_symbols: string | null
  risk_max_position_usdt: number
  risk_max_open_positions: number
  max_queue_size: number
  cooldown_minutes: number
  mt5_default_account_id: number | null
  mt5_default_volume: number
  mt5_timeframe: string
  mt5_min_rr: number
}

interface PaulStatus {
  enabled: boolean
  mode: Mode
  require_approval: boolean
  kill_switch: boolean
  ai_agents_enabled: boolean
  auto_trading_enabled: boolean
  min_confidence: number
  queued_count: number
  open_executed_count: number
  total_decisions: number
}

interface AcceptanceCriterion {
  id: string
  text: string
}

interface PaulDecision {
  id: number
  symbol: string
  timeframe: string
  mode: Mode
  market: 'crypto' | 'mt5'
  account_id: number | null
  volume: number | null
  provenance: 'ai' | 'heuristic'
  action: string
  confidence: number
  entry: number | null
  stop_loss: number | null
  take_profit: number | null
  risk_reward: number | null
  reasoning: string | null
  acceptance_criteria: AcceptanceCriterion[] | null
  qualify_status: 'pass' | 'concerns' | 'blocked'
  qualify_notes: string | null
  status: string
  signal_id: number | null
  execution_result: Record<string, unknown> | null
  error: string | null
  outcome: string | null
  outcome_pnl: number | null
  created_at: string | null
}

interface LoopInfo {
  framework: string
  source: string
  summary: string
  loop: { phase: string; trading: string }[]
  modes: Record<string, string>
  commands: string[]
}

interface Mt5Account {
  id: number
  name: string
  login: string
  server: string
}

type Tab = 'overview' | 'console' | 'queue' | 'history' | 'loop' | 'skills' | 'hooks' | 'providers'

interface AIProvider {
  id: number
  provider_key: string
  label: string
  type: string
  api_key_set: boolean
  base_url: string
  default_model: string
  models: string[]
  model_info: Record<string, any>
  enabled: boolean
  priority: number
  free_tier: boolean
  status: string
  last_error: string | null
  last_tested_at: string | null
  last_model_used: string | null
  total_calls: number
  total_errors: number
  daily_limit: number | null
  monthly_limit: number | null
  daily_calls: number
}

const MODE_LABEL: Record<Mode, string> = {
  paper: 'Paper (simulate)',
  tradebot_execute: 'TradeBot executes',
  paul_execute: 'PAUL executes (autonomous)',
}

function errText(e: unknown): string {
  const o = e as { response?: { data?: { detail?: string } }; message?: string }
  return o?.response?.data?.detail || o?.message || 'Request failed'
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    executed: 'bg-green-500/15 text-green-400',
    queued: 'bg-amber-500/15 text-amber-400',
    approved: 'bg-blue-500/15 text-blue-400',
    rejected: 'bg-gray-500/15 text-gray-400',
    failed: 'bg-red-500/15 text-red-400',
    skipped: 'bg-gray-500/15 text-gray-400',
    planned: 'bg-purple-500/15 text-purple-400',
    unified: 'bg-cyan-500/15 text-cyan-400',
  }
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-semibold ${map[status] || 'bg-gray-500/15 text-gray-400'}`}>
      {status}
    </span>
  )
}

function QualifyBadge({ q }: { q: string }) {
  if (q === 'pass') return <span className="inline-flex items-center gap-1 text-green-400 text-xs"><ShieldCheck className="w-3.5 h-3.5" />pass</span>
  if (q === 'concerns') return <span className="inline-flex items-center gap-1 text-amber-400 text-xs"><AlertTriangle className="w-3.5 h-3.5" />concerns</span>
  return <span className="inline-flex items-center gap-1 text-red-400 text-xs"><ShieldAlert className="w-3.5 h-3.5" />blocked</span>
}

function ActionBadge({ action }: { action: string }) {
  if (action === 'buy') return <span className="inline-flex items-center gap-1 text-green-400 font-semibold"><TrendingUp className="w-4 h-4" />BUY</span>
  if (action === 'sell') return <span className="inline-flex items-center gap-1 text-red-400 font-semibold"><TrendingDown className="w-4 h-4" />SELL</span>
  return <span className="inline-flex items-center gap-1 text-gray-400 font-semibold"><Minus className="w-4 h-4" />HOLD</span>
}

export default function AgentPaulPage() {
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<PaulStatus | null>(null)
  const [settings, setSettings] = useState<PaulSettings | null>(null)
  const [queue, setQueue] = useState<PaulDecision[]>([])
  const [decisions, setDecisions] = useState<PaulDecision[]>([])
  const [loopInfo, setLoopInfo] = useState<LoopInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  // Decision console
  const [symbol, setSymbol] = useState('BTC/USDT')
  const [timeframe, setTimeframe] = useState('1h')
  const [market, setMarket] = useState<'crypto' | 'mt5'>('crypto')
  const [mt5Accounts, setMt5Accounts] = useState<Mt5Account[]>([])
  const [mt5AccountId, setMt5AccountId] = useState<number | null>(null)
  const [deciding, setDeciding] = useState(false)
  const [lastDecision, setLastDecision] = useState<PaulDecision | null>(null)

  // Skills & Hooks
  const [skills, setSkills] = useState<any[]>([])
  const [hooks, setHooks] = useState<any[]>([])
  const [editSkill, setEditSkill] = useState<any | null>(null)
  const [editHook, setEditHook] = useState<any | null>(null)
  const [skillForm, setSkillForm] = useState({ name: '', description: '', trigger_keywords: '', system_prompt_addition: '', ai_provider_id: '', enabled: true })
  const [hookForm, setHookForm] = useState({ name: '', description: '', trigger_type: 'on_signal', condition: '{}', action_template: '', action_type: 'speak', ai_provider_id: '', enabled: true })
  const [skillsBusy, setSkillsBusy] = useState(false)
  const [hooksBusy, setHooksBusy] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [st, se, q, d] = await Promise.all([
        apiClient.agentPaul.getStatus(),
        apiClient.agentPaul.getSettings(),
        apiClient.agentPaul.getQueue(),
        apiClient.agentPaul.getDecisions(50),
      ])
      setStatus(st.data)
      setSettings(se.data)
      setQueue(q.data.queue || [])
      setDecisions(d.data.decisions || [])
      setError(null)
    } catch (e) {
      setError(errText(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    apiClient.agentPaul.listMt5Accounts()
      .then((r) => {
        const accts: Mt5Account[] = r.data || []
        setMt5Accounts(accts)
        if (accts.length && mt5AccountId == null) setMt5AccountId(accts[0].id)
      })
      .catch(() => {})
  }, [mt5AccountId])

  const switchMarket = (m: 'crypto' | 'mt5') => {
    setMarket(m)
    if (m === 'mt5') {
      setSymbol('XAUUSD')
      setTimeframe('H1')
    } else {
      setSymbol('BTC/USDT')
      setTimeframe('1h')
    }
  }

  useEffect(() => {
    if (tab === 'loop' && !loopInfo) {
      apiClient.agentPaul.getLoopInfo().then((r) => setLoopInfo(r.data)).catch(() => {})
    }
    if (tab === 'skills') {
      apiClient.jarvis.listSkills().then(r => setSkills(r.data?.skills || [])).catch(() => {})
    }
    if (tab === 'hooks') {
      apiClient.jarvis.listHooks().then(r => setHooks(r.data?.hooks || [])).catch(() => {})
    }
  }, [tab, loopInfo])

  const saveSettings = async (patch: Partial<PaulSettings>) => {
    if (!settings) return
    const next = { ...settings, ...patch }
    setSettings(next)
    try {
      const r = await apiClient.agentPaul.updateSettings(patch)
      setSettings(r.data)
      setNotice('Settings saved')
      setTimeout(() => setNotice(null), 1500)
      refresh()
    } catch (e) {
      setError(errText(e))
    }
  }

  const runDecide = async () => {
    if (!symbol.trim()) return
    if (market === 'mt5' && !mt5AccountId) {
      setError('Select an MT5 account first (add one on the MT5 Live page).')
      return
    }
    setDeciding(true)
    setLastDecision(null)
    try {
      const r = await apiClient.agentPaul.decide({
        symbol: symbol.trim(),
        timeframe,
        market,
        account_id: market === 'mt5' ? mt5AccountId ?? undefined : undefined,
      } as any)
      setLastDecision(r.data)
      refresh()
    } catch (e) {
      setError(errText(e))
    } finally {
      setDeciding(false)
    }
  }

  const queueAction = async (id: number, action: 'approve' | 'reject' | 'execute') => {
    setBusyId(id)
    try {
      if (action === 'approve') await apiClient.agentPaul.approve(id)
      else if (action === 'reject') await apiClient.agentPaul.reject(id)
      else await apiClient.agentPaul.execute(id)
      refresh()
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusyId(null)
    }
  }

  const unify = async (id: number, outcome: string) => {
    setBusyId(id)
    try {
      const pnlStr = window.prompt(`Closing PnL for decision #${id} (USDT, optional):`, '')
      const pnl = pnlStr && !Number.isNaN(Number(pnlStr)) ? Number(pnlStr) : undefined
      await apiClient.agentPaul.unify(id, { outcome, pnl })
      refresh()
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusyId(null)
    }
  }

  const tabs: { id: Tab; label: string; icon: any }[] = [
    { id: 'overview', label: 'Status & Controls', icon: SettingsIcon },
    { id: 'console', label: 'Decision Console', icon: Zap },
    { id: 'queue', label: `Execution Queue${queue.length ? ` (${queue.length})` : ''}`, icon: ListChecks },
    { id: 'history', label: 'Audit / History', icon: History },
    { id: 'loop', label: 'PAUL Loop', icon: Workflow },
    { id: 'skills', label: 'JARVIS Skills', icon: BrainCircuit },
    { id: 'hooks', label: 'JARVIS Hooks', icon: Webhook },
    { id: 'providers', label: 'AI Providers', icon: Cpu },
  ]

  return (
    <>
      <Head><title>Agent Paul | TradeBot</title></Head>
      <div className="max-w-6xl mx-auto space-y-5">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-tradebot-accent/15 flex items-center justify-center">
              <Workflow className="w-6 h-6 text-tradebot-accent" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-white">Agent Paul</h1>
              <p className="text-sm text-gray-400">PAUL loop — Plan, Apply &amp; Qualify, Unify</p>
            </div>
          </div>
          <button onClick={refresh} className="flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-sm text-gray-300">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} /> Refresh
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-sm">
            <XCircle className="w-4 h-4" /> {error}
          </div>
        )}
        {notice && (
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-green-500/10 border border-green-500/30 text-green-300 text-sm">
            <CheckCircle2 className="w-4 h-4" /> {notice}
          </div>
        )}

        {/* Status strip */}
        {status && (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <StatCard label="Status" value={status.enabled ? 'Enabled' : 'Disabled'} tone={status.enabled ? 'green' : 'gray'} />
            <StatCard label="Mode" value={MODE_LABEL[status.mode]} tone={status.mode === 'paper' ? 'blue' : 'amber'} />
            <StatCard label="Queued" value={String(status.queued_count)} tone={status.queued_count ? 'amber' : 'gray'} />
            <StatCard label="Open" value={String(status.open_executed_count)} tone="cyan" />
            <StatCard label="Total" value={String(status.total_decisions)} tone="gray" />
          </div>
        )}

        {/* Tabs */}
        <div className="flex flex-wrap gap-1 border-b border-gray-700/50">
          {tabs.map((t) => {
            const Icon = t.icon
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 -mb-px transition ${
                  tab === t.id ? 'border-tradebot-accent text-tradebot-accent' : 'border-transparent text-gray-400 hover:text-white'
                }`}
              >
                <Icon className="w-4 h-4" /> {t.label}
              </button>
            )
          })}
        </div>

        {/* ── Overview / Controls ── */}
        {tab === 'overview' && settings && status && (
          <div className="space-y-4">
            {status.kill_switch && (
              <div className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-sm">
                <ShieldAlert className="w-4 h-4" /> Kill switch is ON — all execution is blocked.
              </div>
            )}
            <div className="grid md:grid-cols-2 gap-4">
              <Panel title="Authority &amp; Safety">
                <Toggle label="Agent Paul enabled" checked={settings.enabled} onChange={(v) => saveSettings({ enabled: v })} />
                <Toggle label="Require approval (live modes)" checked={settings.require_approval} onChange={(v) => saveSettings({ require_approval: v })} />
                <Toggle label="Kill switch (block all)" checked={settings.kill_switch} danger onChange={(v) => saveSettings({ kill_switch: v })} />
                <div className="pt-2">
                  <label className="block text-xs text-gray-400 mb-1">Execution mode</label>
                  <select
                    value={settings.mode}
                    onChange={(e) => saveSettings({ mode: e.target.value as Mode })}
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white"
                  >
                    <option value="paper">Paper — simulate only (default)</option>
                    <option value="tradebot_execute">TradeBot executes — PAUL advises</option>
                    <option value="paul_execute">PAUL executes — autonomous</option>
                  </select>
                  <p className="text-xs text-gray-500 mt-1">
                    Live modes route orders through the core live-trade engine. Paper never touches the exchange.
                  </p>
                </div>
              </Panel>

              <Panel title="Risk Policy (Qualify gate)">
                <NumField label="Min confidence (0–1)" value={settings.min_confidence} step={0.05} min={0} max={1} onSave={(v) => saveSettings({ min_confidence: v })} />
                <NumField label="Max position (USDT)" value={settings.risk_max_position_usdt} step={10} min={0} onSave={(v) => saveSettings({ risk_max_position_usdt: v })} />
                <NumField label="Max open positions" value={settings.risk_max_open_positions} step={1} min={0} onSave={(v) => saveSettings({ risk_max_open_positions: v })} />
                <NumField label="Max approval queue" value={settings.max_queue_size} step={1} min={0} onSave={(v) => saveSettings({ max_queue_size: v })} />
                <NumField label="Cooldown (minutes)" value={settings.cooldown_minutes} step={1} min={0} onSave={(v) => saveSettings({ cooldown_minutes: v })} />
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Allowed symbols (CSV, blank = all)</label>
                  <input
                    defaultValue={settings.allowed_symbols || ''}
                    onBlur={(e) => saveSettings({ allowed_symbols: e.target.value })}
                    placeholder="BTC/USDT, ETH/USDT"
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white"
                  />
                </div>
              </Panel>
            </div>

            <Panel title="MT5 defaults (for /mt5-live trades)">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Default MT5 account</label>
                <select
                  value={settings.mt5_default_account_id ?? ''}
                  onChange={(e) => saveSettings({ mt5_default_account_id: e.target.value ? Number(e.target.value) : null } as any)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white"
                >
                  <option value="">None</option>
                  {mt5Accounts.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.login})</option>)}
                </select>
              </div>
              <NumField label="Default volume (lots)" value={settings.mt5_default_volume} step={0.01} min={0} onSave={(v) => saveSettings({ mt5_default_volume: v } as any)} />
              <NumField label="Min risk-reward (SMC)" value={settings.mt5_min_rr} step={0.5} min={0} onSave={(v) => saveSettings({ mt5_min_rr: v } as any)} />
              <div>
                <label className="block text-xs text-gray-400 mb-1">Default MT5 timeframe</label>
                <select
                  value={settings.mt5_timeframe}
                  onChange={(e) => saveSettings({ mt5_timeframe: e.target.value } as any)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white"
                >
                  {['M5', 'M15', 'M30', 'H1', 'H4', 'D1'].map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
            </Panel>

            <Panel title="Dependencies">
              <DepRow ok={status.ai_agents_enabled} label="AI agents enabled" hint="Plans use the AI orchestrator; otherwise a local RSI heuristic is used." />
              <DepRow ok={status.auto_trading_enabled} label="Auto-trading enabled (live)" hint="Required for tradebot_execute / paul_execute to place real orders." />
            </Panel>
          </div>
        )}

        {/* ── Decision Console ── */}
        {tab === 'console' && (
          <div className="space-y-4">
            <Panel title="Run a PAUL decision">
              <div className="flex flex-wrap gap-3 items-end">
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Market</label>
                  <div className="flex rounded-lg overflow-hidden border border-gray-700">
                    <button
                      onClick={() => switchMarket('crypto')}
                      className={`px-3 py-2 text-sm ${market === 'crypto' ? 'bg-tradebot-accent text-black font-semibold' : 'bg-gray-800 text-gray-300'}`}
                    >Crypto</button>
                    <button
                      onClick={() => switchMarket('mt5')}
                      className={`px-3 py-2 text-sm ${market === 'mt5' ? 'bg-tradebot-accent text-black font-semibold' : 'bg-gray-800 text-gray-300'}`}
                    >MT5</button>
                  </div>
                </div>
                {market === 'mt5' && (
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">MT5 account</label>
                    <select
                      value={mt5AccountId ?? ''}
                      onChange={(e) => setMt5AccountId(e.target.value ? Number(e.target.value) : null)}
                      className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white"
                    >
                      {mt5Accounts.length === 0 && <option value="">No accounts</option>}
                      {mt5Accounts.map((a) => (
                        <option key={a.id} value={a.id}>{a.name} ({a.login})</option>
                      ))}
                    </select>
                  </div>
                )}
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Symbol</label>
                  <input value={symbol} onChange={(e) => setSymbol(e.target.value)} className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white w-40" />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Timeframe</label>
                  <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white">
                    {(market === 'mt5' ? ['M5', 'M15', 'M30', 'H1', 'H4', 'D1'] : ['5m', '15m', '1h', '4h', '1d']).map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <button onClick={runDecide} disabled={deciding} className="flex items-center gap-2 px-4 py-2 rounded-lg bg-tradebot-accent text-black font-semibold text-sm disabled:opacity-60">
                  {deciding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />} Plan &amp; Apply
                </button>
              </div>
              {market === 'mt5' && (
                <p className="text-xs text-gray-500 mt-2">
                  MT5 plans use the SMC sniper engine on live broker candles. Live modes place a resting limit order on the selected account (shows on MT5 Live).
                </p>
              )}
            </Panel>

            {lastDecision && <DecisionCard d={lastDecision} onUnify={unify} busyId={busyId} />}
          </div>
        )}

        {/* ── Execution Queue ── */}
        {tab === 'queue' && (
          <div className="space-y-3">
            {queue.length === 0 && <Empty icon={ListChecks} text="No decisions awaiting approval." />}
            {queue.map((d) => (
              <div key={d.id} className="rounded-lg border border-gray-700/60 bg-gray-900/40 p-4">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-sm text-gray-300">#{d.id}</span>
                    <span className="font-semibold text-white">{d.symbol}</span>
                    <ActionBadge action={d.action} />
                    <span className="text-xs text-gray-400">conf {(d.confidence * 100).toFixed(0)}%</span>
                    <QualifyBadge q={d.qualify_status} />
                  </div>
                  <div className="flex items-center gap-2">
                    <button disabled={busyId === d.id} onClick={() => queueAction(d.id, 'approve')} className="flex items-center gap-1 px-3 py-1.5 rounded bg-green-600/80 hover:bg-green-600 text-white text-xs font-semibold disabled:opacity-50">
                      <CheckCircle2 className="w-3.5 h-3.5" /> Approve &amp; Execute
                    </button>
                    <button disabled={busyId === d.id} onClick={() => queueAction(d.id, 'reject')} className="flex items-center gap-1 px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs disabled:opacity-50">
                      <XCircle className="w-3.5 h-3.5" /> Reject
                    </button>
                  </div>
                </div>
                <DecisionDetail d={d} />
              </div>
            ))}
          </div>
        )}

        {/* ── Audit / History ── */}
        {tab === 'history' && (
          <div className="space-y-2">
            {decisions.length === 0 && <Empty icon={History} text="No decisions yet." />}
            {decisions.map((d) => (
              <div key={d.id} className="rounded-lg border border-gray-700/50 bg-gray-900/30 p-3">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="flex items-center gap-3 text-sm">
                    <span className="font-mono text-gray-500">#{d.id}</span>
                    <span className="font-semibold text-white">{d.symbol}</span>
                    <ActionBadge action={d.action} />
                    <span className="text-xs text-gray-400">{d.timeframe} · {d.provenance} · conf {(d.confidence * 100).toFixed(0)}%</span>
                    <StatusBadge status={d.status} />
                    <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">{MODE_LABEL[d.mode]}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {d.outcome && d.outcome !== 'open' && (
                      <span className={`text-xs ${d.outcome === 'win' ? 'text-green-400' : d.outcome === 'loss' ? 'text-red-400' : 'text-gray-400'}`}>
                        {d.outcome}{d.outcome_pnl != null ? ` ${d.outcome_pnl > 0 ? '+' : ''}${d.outcome_pnl}` : ''}
                      </span>
                    )}
                    {d.status === 'executed' && d.outcome === 'open' && (
                      <div className="flex items-center gap-1">
                        <button disabled={busyId === d.id} onClick={() => unify(d.id, 'win')} className="px-2 py-1 rounded bg-green-600/70 hover:bg-green-600 text-white text-xs">Win</button>
                        <button disabled={busyId === d.id} onClick={() => unify(d.id, 'loss')} className="px-2 py-1 rounded bg-red-600/70 hover:bg-red-600 text-white text-xs">Loss</button>
                        <button disabled={busyId === d.id} onClick={() => unify(d.id, 'break_even')} className="px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs">BE</button>
                      </div>
                    )}
                  </div>
                </div>
                <DecisionDetail d={d} compact />
              </div>
            ))}
          </div>
        )}

        {/* ── PAUL Loop reference ── */}
        {tab === 'loop' && (
          <div className="space-y-4">
            {!loopInfo && <div className="text-gray-400 text-sm flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>}
            {loopInfo && (
              <>
                <Panel title={loopInfo.framework}>
                  <p className="text-sm text-gray-300">{loopInfo.summary}</p>
                  <a href={loopInfo.source} target="_blank" rel="noreferrer" className="text-tradebot-accent text-sm hover:underline">{loopInfo.source}</a>
                </Panel>
                <div className="grid md:grid-cols-3 gap-3">
                  {loopInfo.loop.map((p) => (
                    <Panel key={p.phase} title={p.phase}>
                      <p className="text-sm text-gray-300">{p.trading}</p>
                    </Panel>
                  ))}
                </div>
                <Panel title="Execution modes">
                  {Object.entries(loopInfo.modes).map(([k, v]) => (
                    <div key={k} className="flex gap-2 text-sm py-1">
                      <span className="font-mono text-tradebot-accent w-40 shrink-0">{k}</span>
                      <span className="text-gray-300">{v}</span>
                    </div>
                  ))}
                </Panel>
                <Panel title="PAUL commands (Claude Code workflow)">
                  {loopInfo.commands.map((c) => (
                    <div key={c} className="font-mono text-xs text-gray-400 py-0.5">{c}</div>
                  ))}
                </Panel>
              </>
            )}
          </div>
        )}

        {/* ── JARVIS Skills ─────────────────────────────────────────── */}
        {tab === 'skills' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-sm text-gray-400">
                Skills are domain knowledge modules JARVIS auto-loads when your message matches the trigger keywords.
                Link a skill to an AI provider to use that provider for specialist responses.
              </p>
              <button
                onClick={() => { setEditSkill(null); setSkillForm({ name: '', description: '', trigger_keywords: '', system_prompt_addition: '', ai_provider_id: '', enabled: true }) }}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 rounded text-white text-xs font-medium"
              >
                <Plus className="w-3.5 h-3.5" /> New Skill
              </button>
            </div>

            {/* Skill editor */}
            {(editSkill !== undefined) && (
              <div className="rounded-lg border border-cyan-600/40 bg-gray-900/60 p-4 space-y-3">
                <h3 className="text-sm font-semibold text-cyan-300">{editSkill ? 'Edit Skill' : 'New Skill'}</h3>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">Name *</label>
                    <input value={skillForm.name} onChange={e => setSkillForm(p => ({ ...p, name: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">Trigger Keywords (comma-separated) *</label>
                    <input value={skillForm.trigger_keywords} onChange={e => setSkillForm(p => ({ ...p, trigger_keywords: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" placeholder="gold, xauusd, trade" />
                  </div>
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Description</label>
                  <input value={skillForm.description} onChange={e => setSkillForm(p => ({ ...p, description: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">System Prompt Addition * (injected into JARVIS context when triggered)</label>
                  <textarea rows={4} value={skillForm.system_prompt_addition} onChange={e => setSkillForm(p => ({ ...p, system_prompt_addition: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white font-mono" placeholder="You are a specialist in..." />
                </div>
                <div className="flex gap-3">
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-gray-500">AI Provider ID (optional)</label>
                    <input type="number" value={skillForm.ai_provider_id} onChange={e => setSkillForm(p => ({ ...p, ai_provider_id: e.target.value }))} className="w-20 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-white" placeholder="e.g. 1" />
                  </div>
                  <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
                    <input type="checkbox" checked={skillForm.enabled} onChange={e => setSkillForm(p => ({ ...p, enabled: e.target.checked }))} />
                    Enabled
                  </label>
                </div>
                <div className="flex gap-2">
                  <button disabled={skillsBusy} onClick={async () => {
                    setSkillsBusy(true)
                    try {
                      const kws = skillForm.trigger_keywords.split(',').map(k => k.trim()).filter(Boolean)
                      const data = { ...skillForm, trigger_keywords: kws, ai_provider_id: skillForm.ai_provider_id ? Number(skillForm.ai_provider_id) : null }
                      if (editSkill) await apiClient.jarvis.updateSkill(editSkill.id, data)
                      else await apiClient.jarvis.createSkill(data)
                      const r = await apiClient.jarvis.listSkills()
                      setSkills(r.data?.skills || [])
                      setEditSkill(undefined as any)
                    } catch { setError('Failed to save skill') } finally { setSkillsBusy(false) }
                  }} className="px-4 py-1.5 bg-cyan-600 hover:bg-cyan-500 rounded text-white text-sm">Save</button>
                  <button onClick={() => setEditSkill(undefined as any)} className="px-4 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-white text-sm">Cancel</button>
                </div>
              </div>
            )}

            <div className="grid gap-3">
              {skills.map(sk => (
                <div key={sk.id} className={`rounded-lg border ${sk.is_default ? 'border-cyan-600/30' : 'border-gray-700/50'} bg-gray-900/40 p-4`}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <BrainCircuit className="w-4 h-4 text-cyan-400 shrink-0" />
                        <span className="font-semibold text-white text-sm">{sk.name}</span>
                        {sk.is_default && <span className="text-[10px] bg-cyan-600/20 text-cyan-300 px-1.5 rounded">default</span>}
                        {!sk.enabled && <span className="text-[10px] bg-gray-600/30 text-gray-400 px-1.5 rounded">disabled</span>}
                        {sk.ai_provider_id && <span className="text-[10px] bg-purple-600/20 text-purple-300 px-1.5 rounded">AI #{sk.ai_provider_id}</span>}
                      </div>
                      {sk.description && <p className="text-xs text-gray-400 mt-1">{sk.description}</p>}
                      <div className="mt-2 flex flex-wrap gap-1">
                        {(sk.trigger_keywords || []).map((kw: string) => (
                          <span key={kw} className="text-[10px] bg-gray-800 border border-gray-700 text-gray-300 px-1.5 py-0.5 rounded">{kw}</span>
                        ))}
                      </div>
                      <p className="mt-2 text-xs text-gray-500 font-mono line-clamp-2">{sk.system_prompt_addition}</p>
                    </div>
                    <div className="flex gap-1 shrink-0">
                      <button onClick={() => { setEditSkill(sk); setSkillForm({ name: sk.name, description: sk.description || '', trigger_keywords: (sk.trigger_keywords || []).join(', '), system_prompt_addition: sk.system_prompt_addition, ai_provider_id: String(sk.ai_provider_id || ''), enabled: sk.enabled }) }} className="p-1.5 hover:bg-gray-700 rounded text-gray-400 hover:text-cyan-300"><Pencil className="w-3.5 h-3.5" /></button>
                      <button onClick={async () => {
                        await apiClient.jarvis.updateSkill(sk.id, { enabled: !sk.enabled })
                        const r = await apiClient.jarvis.listSkills(); setSkills(r.data?.skills || [])
                      }} className="p-1.5 hover:bg-gray-700 rounded text-gray-400">
                        {sk.enabled ? <ToggleRight className="w-4 h-4 text-cyan-400" /> : <ToggleLeft className="w-4 h-4 text-gray-500" />}
                      </button>
                      {!sk.is_default && (
                        <button onClick={async () => {
                          await apiClient.jarvis.deleteSkill(sk.id)
                          const r = await apiClient.jarvis.listSkills(); setSkills(r.data?.skills || [])
                        }} className="p-1.5 hover:bg-gray-700 rounded text-red-400 hover:text-red-300"><Trash2 className="w-3.5 h-3.5" /></button>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── JARVIS Hooks ──────────────────────────────────────────── */}
        {tab === 'hooks' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-sm text-gray-400">
                Hooks are automated reactions to events (new signal, price threshold, voice command, schedule).
                Say <span className="font-mono text-cyan-300">&quot;execute the best Gold signal&quot;</span> to trigger the voice command hook.
              </p>
              <button
                onClick={() => { setEditHook(null); setHookForm({ name: '', description: '', trigger_type: 'on_signal', condition: '{}', action_template: '', action_type: 'speak', ai_provider_id: '', enabled: true }) }}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-purple-600 hover:bg-purple-500 rounded text-white text-xs font-medium"
              >
                <Plus className="w-3.5 h-3.5" /> New Hook
              </button>
            </div>

            {/* Hook editor */}
            {(editHook !== undefined) && (
              <div className="rounded-lg border border-purple-600/40 bg-gray-900/60 p-4 space-y-3">
                <h3 className="text-sm font-semibold text-purple-300">{editHook ? 'Edit Hook' : 'New Hook'}</h3>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">Name *</label>
                    <input value={hookForm.name} onChange={e => setHookForm(p => ({ ...p, name: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">Trigger Type</label>
                    <select value={hookForm.trigger_type} onChange={e => setHookForm(p => ({ ...p, trigger_type: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white">
                      <option value="on_signal">On New Signal</option>
                      <option value="on_position_change">On Position P&L Change</option>
                      <option value="on_price_threshold">On Price Threshold</option>
                      <option value="on_schedule">On Schedule (cron)</option>
                      <option value="on_voice_command">On Voice Command</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Condition (JSON)</label>
                  <input value={hookForm.condition} onChange={e => setHookForm(p => ({ ...p, condition: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white font-mono" placeholder='{"min_confidence": 0.8}' />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Action Template * (use {'{{variable}}'} for substitution)</label>
                  <textarea rows={3} value={hookForm.action_template} onChange={e => setHookForm(p => ({ ...p, action_template: e.target.value }))} className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white font-mono" />
                </div>
                <div className="flex gap-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">Action Type</label>
                    <select value={hookForm.action_type} onChange={e => setHookForm(p => ({ ...p, action_type: e.target.value }))} className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white">
                      <option value="speak">Speak</option>
                      <option value="trade">Execute Trade</option>
                      <option value="alert">Alert</option>
                    </select>
                  </div>
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-gray-500">AI Provider ID</label>
                    <input type="number" value={hookForm.ai_provider_id} onChange={e => setHookForm(p => ({ ...p, ai_provider_id: e.target.value }))} className="w-20 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-white" />
                  </div>
                  <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
                    <input type="checkbox" checked={hookForm.enabled} onChange={e => setHookForm(p => ({ ...p, enabled: e.target.checked }))} />
                    Enabled
                  </label>
                </div>
                <div className="flex gap-2">
                  <button disabled={hooksBusy} onClick={async () => {
                    setHooksBusy(true)
                    try {
                      let cond: any = {}
                      try { cond = JSON.parse(hookForm.condition) } catch { cond = {} }
                      const data = { ...hookForm, condition: cond, ai_provider_id: hookForm.ai_provider_id ? Number(hookForm.ai_provider_id) : null }
                      if (editHook) await apiClient.jarvis.updateHook(editHook.id, data)
                      else await apiClient.jarvis.createHook(data)
                      const r = await apiClient.jarvis.listHooks()
                      setHooks(r.data?.hooks || [])
                      setEditHook(undefined as any)
                    } catch { setError('Failed to save hook') } finally { setHooksBusy(false) }
                  }} className="px-4 py-1.5 bg-purple-600 hover:bg-purple-500 rounded text-white text-sm">Save</button>
                  <button onClick={() => setEditHook(undefined as any)} className="px-4 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-white text-sm">Cancel</button>
                </div>
              </div>
            )}

            <div className="grid gap-3">
              {hooks.map(hk => (
                <div key={hk.id} className={`rounded-lg border ${hk.is_default ? 'border-purple-600/30' : 'border-gray-700/50'} bg-gray-900/40 p-4`}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        {hk.action_type === 'trade' ? <Zap className="w-4 h-4 text-amber-400 shrink-0" /> : hk.action_type === 'speak' ? <Mic className="w-4 h-4 text-cyan-400 shrink-0" /> : <Webhook className="w-4 h-4 text-purple-400 shrink-0" />}
                        <span className="font-semibold text-white text-sm">{hk.name}</span>
                        {hk.is_default && <span className="text-[10px] bg-purple-600/20 text-purple-300 px-1.5 rounded">default</span>}
                        <span className="text-[10px] bg-gray-700 text-gray-400 px-1.5 rounded font-mono">{hk.trigger_type}</span>
                        <span className="text-[10px] bg-amber-600/20 text-amber-300 px-1.5 rounded">{hk.action_type}</span>
                        {hk.ai_provider_id && <span className="text-[10px] bg-purple-600/20 text-purple-300 px-1.5 rounded">AI #{hk.ai_provider_id}</span>}
                        {!hk.enabled && <span className="text-[10px] bg-gray-600/30 text-gray-400 px-1.5 rounded">disabled</span>}
                        {hk.fire_count > 0 && <span className="text-[10px] text-gray-500">fired {hk.fire_count}×</span>}
                      </div>
                      {hk.description && <p className="text-xs text-gray-400 mt-1">{hk.description}</p>}
                      <p className="mt-2 text-xs text-gray-500 font-mono line-clamp-2">{hk.action_template}</p>
                    </div>
                    <div className="flex gap-1 shrink-0">
                      <button onClick={() => { setEditHook(hk); setHookForm({ name: hk.name, description: hk.description || '', trigger_type: hk.trigger_type, condition: JSON.stringify(hk.condition || {}), action_template: hk.action_template, action_type: hk.action_type, ai_provider_id: String(hk.ai_provider_id || ''), enabled: hk.enabled }) }} className="p-1.5 hover:bg-gray-700 rounded text-gray-400 hover:text-purple-300"><Pencil className="w-3.5 h-3.5" /></button>
                      <button onClick={async () => {
                        await apiClient.jarvis.updateHook(hk.id, { enabled: !hk.enabled })
                        const r = await apiClient.jarvis.listHooks(); setHooks(r.data?.hooks || [])
                      }} className="p-1.5 hover:bg-gray-700 rounded text-gray-400">
                        {hk.enabled ? <ToggleRight className="w-4 h-4 text-purple-400" /> : <ToggleLeft className="w-4 h-4 text-gray-500" />}
                      </button>
                      {!hk.is_default && (
                        <button onClick={async () => {
                          await apiClient.jarvis.deleteHook(hk.id)
                          const r = await apiClient.jarvis.listHooks(); setHooks(r.data?.hooks || [])
                        }} className="p-1.5 hover:bg-gray-700 rounded text-red-400 hover:text-red-300"><Trash2 className="w-3.5 h-3.5" /></button>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── AI Providers Tab ────────────────────────────────────────── */}
        {tab === 'providers' && <AIProvidersPanel />}

      </div>
    </>
  )
}

// ── AI Providers Panel ────────────────────────────────────────────────────────

function AIProvidersPanel() {
  const [providers, setProviders] = useState<AIProvider[]>([])
  const [loading, setLoading] = useState(true)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [testingAll, setTestingAll] = useState(false)
  const [testResults, setTestResults] = useState<Record<number, { ok: boolean; reply?: string; error?: string; model?: string }>>({})
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [updatingId, setUpdatingId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/plugins/ai-analyst/ai/providers')
      const items: AIProvider[] = Array.isArray(data) ? data : (data?.providers || [])
      setProviders(items.sort((a, b) => a.priority - b.priority))
    } catch { /* silent */ }
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  const testOne = async (id: number) => {
    setTestingId(id)
    try {
      const b = JSON.stringify({})
      const req_: any = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: b }
      const resp = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/plugins/ai-analyst/ai/providers/${id}/test`, req_)
      const d = await resp.json()
      setTestResults(prev => ({ ...prev, [id]: d }))
      setExpandedId(id)
    } catch (e: any) {
      setTestResults(prev => ({ ...prev, [id]: { ok: false, error: String(e) } }))
    }
    setTestingId(null)
  }

  const testAll = async () => {
    setTestingAll(true)
    try {
      const b = JSON.stringify({})
      const resp = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/plugins/ai-analyst/ai/providers/test-all`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: b })
      const d = await resp.json()
      const newResults: typeof testResults = {}
      for (const r of d?.results || []) {
        newResults[r.id] = { ok: r.ok, reply: r.reply, error: r.error, model: r.model }
      }
      setTestResults(newResults)
    } catch { /* silent */ }
    setTestingAll(false)
    await load()
  }

  const toggleEnabled = async (p: AIProvider) => {
    setUpdatingId(p.id)
    try {
      await api.put(`/plugins/ai-analyst/ai/providers/${p.id}`, { ...p, enabled: !p.enabled })
      await load()
    } catch { /* silent */ }
    setUpdatingId(null)
  }

  const changeModel = async (p: AIProvider, model: string) => {
    setUpdatingId(p.id)
    try {
      await api.put(`/plugins/ai-analyst/ai/providers/${p.id}`, { ...p, default_model: model })
      await load()
    } catch { /* silent */ }
    setUpdatingId(null)
  }

  const changePriority = async (p: AIProvider, delta: number) => {
    const newPrio = Math.max(1, (p.priority || 10) + delta)
    setUpdatingId(p.id)
    try {
      await api.put(`/plugins/ai-analyst/ai/providers/${p.id}`, { ...p, priority: newPrio })
      await load()
    } catch { /* silent */ }
    setUpdatingId(null)
  }

  const statusColor = (p: AIProvider) => {
    if (!p.enabled) return 'text-gray-500 bg-gray-700/30'
    const tr = testResults[p.id]
    if (tr) return tr.ok ? 'text-emerald-400 bg-emerald-400/10' : 'text-red-400 bg-red-400/10'
    if (p.status === 'ok') return 'text-emerald-400 bg-emerald-400/10'
    if (p.status === 'error') return 'text-red-400 bg-red-400/10'
    return 'text-amber-400 bg-amber-400/10'
  }

  const statusLabel = (p: AIProvider) => {
    if (!p.enabled) return 'Disabled'
    const tr = testResults[p.id]
    if (tr) return tr.ok ? 'OK' : 'ERROR'
    return (p.status || 'unknown').toUpperCase()
  }

  const usagePct = (used: number, limit: number | null) => {
    if (!limit) return 0
    return Math.min(100, Math.round((used / limit) * 100))
  }

  return (
    <div className="space-y-4">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-white flex items-center gap-2">
            <Cpu className="w-4 h-4 text-tradebot-accent" /> Configured AI Accounts
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            {providers.filter(p => p.enabled).length} of {providers.length} enabled · Jarvis selects best model per task
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={load}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-gray-800 border border-gray-700 rounded-lg text-gray-300 hover:bg-gray-700 transition"
          >
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </button>
          <button
            onClick={testAll}
            disabled={testingAll}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-tradebot-accent/20 border border-tradebot-accent/40 rounded-lg text-tradebot-accent hover:bg-tradebot-accent/30 disabled:opacity-50 transition"
          >
            {testingAll ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FlaskConical className="w-3.5 h-3.5" />}
            Test All Keys
          </button>
        </div>
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-500">
          <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" />
          Loading providers…
        </div>
      ) : providers.length === 0 ? (
        <div className="text-center py-12 text-gray-500">
          <Cpu className="w-8 h-8 mx-auto mb-2 opacity-40" />
          <p>No providers configured.</p>
          <p className="text-xs mt-1">
            Add providers on the <a href="/ai-analysis" className="text-tradebot-accent underline">AI Analyst</a> page.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {providers.map(p => {
            const tr = testResults[p.id]
            const isExpanded = expandedId === p.id
            const isTesting = testingId === p.id
            const isUpdating = updatingId === p.id
            const dailyPct = usagePct(p.daily_calls || 0, p.daily_limit)
            const monthlyPct = usagePct(/* infer monthly */ (p.total_calls || 0) % 10000, p.monthly_limit)

            return (
              <div key={p.id} className={`rounded-xl border transition-all ${
                p.enabled ? 'border-gray-700/60 bg-gray-900/50' : 'border-gray-800/40 bg-gray-900/20 opacity-60'
              }`}>
                {/* Main row */}
                <div className="flex items-center gap-3 p-4">
                  {/* Status dot */}
                  <div className={`w-2 h-2 rounded-full shrink-0 ${
                    !p.enabled ? 'bg-gray-600' :
                    tr ? (tr.ok ? 'bg-emerald-400' : 'bg-red-400') :
                    p.status === 'ok' ? 'bg-emerald-400 animate-pulse' :
                    p.status === 'error' ? 'bg-red-400' : 'bg-amber-400'
                  }`} />

                  {/* Name + badge */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-white text-sm">{p.label}</span>
                      {p.free_tier && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-600/20 text-emerald-400 border border-emerald-600/30">free</span>
                      )}
                      <span className={`text-[10px] px-2 py-0.5 rounded font-medium ${statusColor(p)}`}>
                        {statusLabel(p)}
                      </span>
                      <span className="text-[10px] text-gray-600">prio {p.priority}</span>
                      <span className="text-xs text-gray-500">
                        {p.total_calls || 0} calls · {p.total_errors || 0} errors
                      </span>
                    </div>

                    {/* Model + usage */}
                    <div className="flex items-center gap-3 mt-1.5 flex-wrap">
                      {/* Model selector */}
                      {p.models && p.models.length > 0 ? (
                        <select
                          value={p.default_model || ''}
                          onChange={e => changeModel(p, e.target.value)}
                          disabled={isUpdating || !p.enabled}
                          className="text-xs bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-gray-300 max-w-[200px] truncate"
                        >
                          {p.models.map(m => (
                            <option key={m} value={m}>{m}</option>
                          ))}
                        </select>
                      ) : (
                        <span className="text-xs text-gray-500 font-mono">{p.default_model || '—'}</span>
                      )}

                      {/* Daily usage bar */}
                      {p.daily_limit && (
                        <div className="flex items-center gap-1.5">
                          <span className="text-[10px] text-gray-600">Today</span>
                          <div className="w-20 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                            <div
                              className={`h-full rounded-full ${dailyPct > 80 ? 'bg-red-500' : dailyPct > 50 ? 'bg-amber-500' : 'bg-emerald-500'}`}
                              style={{ width: `${dailyPct}%` }}
                            />
                          </div>
                          <span className="text-[10px] text-gray-500">{p.daily_calls || 0}/{p.daily_limit}</span>
                        </div>
                      )}
                    </div>

                    {/* Last error */}
                    {p.last_error && (
                      <p className="mt-1 text-[10px] text-red-400 truncate max-w-md">
                        ⚠ {p.last_error}
                      </p>
                    )}

                    {/* Test result inline */}
                    {tr && (
                      <div className={`mt-1.5 text-xs px-2 py-1 rounded flex items-start gap-2 ${
                        tr.ok ? 'bg-emerald-900/20 text-emerald-300' : 'bg-red-900/20 text-red-300'
                      }`}>
                        {tr.ok ? <CheckCircle2 className="w-3.5 h-3.5 shrink-0 mt-0.5" /> : <XCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />}
                        <span>{tr.ok ? (tr.reply || 'Connection OK') : (tr.error || 'Failed')}</span>
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 shrink-0">
                    {/* Priority buttons */}
                    <div className="flex flex-col gap-0.5">
                      <button onClick={() => changePriority(p, -1)} disabled={isUpdating} className="p-0.5 hover:bg-gray-700 rounded text-gray-500 hover:text-white" title="Higher priority">
                        <ChevronUp className="w-3 h-3" />
                      </button>
                      <button onClick={() => changePriority(p, +1)} disabled={isUpdating} className="p-0.5 hover:bg-gray-700 rounded text-gray-500 hover:text-white" title="Lower priority">
                        <ChevronDown className="w-3 h-3" />
                      </button>
                    </div>

                    {/* Test button */}
                    <button
                      onClick={() => testOne(p.id)}
                      disabled={isTesting || !p.api_key_set}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border border-gray-600 bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40 transition"
                      title={!p.api_key_set ? 'No API key set' : 'Test connection'}
                    >
                      {isTesting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FlaskConical className="w-3.5 h-3.5" />}
                      Test
                    </button>

                    {/* Enabled toggle */}
                    <button
                      onClick={() => toggleEnabled(p)}
                      disabled={isUpdating}
                      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition ${
                        p.enabled
                          ? 'border-tradebot-accent/50 bg-tradebot-accent/10 text-tradebot-accent hover:bg-tradebot-accent/20'
                          : 'border-gray-600 bg-gray-800 text-gray-500 hover:bg-gray-700 hover:text-gray-300'
                      }`}
                    >
                      {isUpdating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                      {p.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                  </div>
                </div>

                {/* Model info tags (expanded when model has info) */}
                {p.model_info && p.default_model && p.model_info[p.default_model] && (() => {
                  const mi = p.model_info[p.default_model]
                  const tags = [
                    mi.context_window && `${(mi.context_window/1000).toFixed(0)}K context`,
                    mi.output_tokens && `${mi.output_tokens} out`,
                    mi.supports_reasoning && 'Reasoning',
                    mi.supports_vision && 'Vision',
                    mi.json_mode && 'JSON',
                    mi.free_tier && 'Free',
                  ].filter(Boolean)
                  if (!tags.length) return null
                  return (
                    <div className="px-4 pb-3 flex flex-wrap gap-1.5">
                      {tags.map(tag => (
                        <span key={tag} className="text-[10px] px-2 py-0.5 rounded-full bg-gray-800 text-gray-400 border border-gray-700/50">{tag}</span>
                      ))}
                      {mi.description && (
                        <span className="text-[10px] text-gray-600 ml-1">{mi.description}</span>
                      )}
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      )}

      {/* Summary bar */}
      {providers.length > 0 && (
        <div className="flex flex-wrap gap-4 pt-2 border-t border-gray-700/30 text-xs text-gray-500">
          <span>Total calls: <strong className="text-gray-300">{providers.reduce((s, p) => s + (p.total_calls||0), 0).toLocaleString()}</strong></span>
          <span>Total errors: <strong className="text-gray-300">{providers.reduce((s, p) => s + (p.total_errors||0), 0)}</strong></span>
          <span>Active today: <strong className="text-gray-300">{providers.filter(p=>p.enabled&&p.daily_calls>0).map(p=>p.label).join(', ') || 'None'}</strong></span>
          <span className="text-gray-600">Jarvis auto-selects best model: reasoning → analysis, fast → status checks</span>
        </div>
      )}
    </div>
  )
}

// ── Small presentational helpers ──────────────────────────

function StatCard({ label, value, tone }: { label: string; value: string; tone: string }) {
  const tones: Record<string, string> = {
    green: 'text-green-400', amber: 'text-amber-400', blue: 'text-blue-400', cyan: 'text-cyan-400', gray: 'text-gray-300',
  }
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 px-3 py-2.5">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-sm font-semibold ${tones[tone] || 'text-gray-300'} truncate`}>{value}</div>
    </div>
  )
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-4 space-y-3">
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      {children}
    </div>
  )
}

function Toggle({ label, checked, onChange, danger }: { label: string; checked: boolean; onChange: (v: boolean) => void; danger?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-gray-300">{label}</span>
      <button
        onClick={() => onChange(!checked)}
        className={`relative w-11 h-6 rounded-full transition ${checked ? (danger ? 'bg-red-500' : 'bg-tradebot-accent') : 'bg-gray-700'}`}
      >
        <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition ${checked ? 'translate-x-5' : ''}`} />
      </button>
    </div>
  )
}

function NumField({ label, value, step, min, max, onSave }: { label: string; value: number; step: number; min?: number; max?: number; onSave: (v: number) => void }) {
  const [v, setV] = useState(String(value))
  useEffect(() => setV(String(value)), [value])
  return (
    <div className="flex items-center justify-between gap-3">
      <label className="text-xs text-gray-400">{label}</label>
      <input
        type="number" value={v} step={step} min={min} max={max}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => { const n = Number(v); if (!Number.isNaN(n)) onSave(n) }}
        className="w-28 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-white text-right"
      />
    </div>
  )
}

function DepRow({ ok, label, hint }: { ok: boolean; label: string; hint: string }) {
  return (
    <div className="flex items-start gap-2">
      {ok ? <CheckCircle2 className="w-4 h-4 text-green-400 mt-0.5" /> : <AlertTriangle className="w-4 h-4 text-amber-400 mt-0.5" />}
      <div>
        <div className="text-sm text-gray-200">{label}</div>
        <div className="text-xs text-gray-500">{hint}</div>
      </div>
    </div>
  )
}

function Empty({ icon: Icon, text }: { icon: any; text: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-gray-500">
      <Icon className="w-8 h-8 mb-2 opacity-50" />
      <p className="text-sm">{text}</p>
    </div>
  )
}

function DecisionDetail({ d, compact }: { d: PaulDecision; compact?: boolean }) {
  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-400">
        {d.market === 'mt5' && (
          <span className="text-amber-400 font-semibold">
            MT5{d.account_id ? ` · acct ${d.account_id}` : ''}{d.volume ? ` · ${d.volume} lot` : ''}
          </span>
        )}
        {d.entry != null && <span>Entry <span className="text-gray-200">{d.entry}</span></span>}
        {d.stop_loss != null && <span>SL <span className="text-red-300">{d.stop_loss}</span></span>}
        {d.take_profit != null && <span>TP <span className="text-green-300">{d.take_profit}</span></span>}
        {d.risk_reward != null && <span>R:R <span className="text-gray-200">{d.risk_reward}</span></span>}
        {d.signal_id != null && <span>signal #{d.signal_id}</span>}
      </div>
      {d.reasoning && <p className="text-xs text-gray-400 italic">{d.reasoning}</p>}
      {d.qualify_notes && <p className="text-xs text-amber-400/80">Qualify: {d.qualify_notes}</p>}
      {d.error && <p className="text-xs text-red-400">Error: {d.error}</p>}
      {!compact && d.acceptance_criteria && (
        <div className="pt-1 space-y-0.5">
          {d.acceptance_criteria.map((ac) => (
            <div key={ac.id} className="text-xs text-gray-500"><span className="font-mono text-tradebot-accent">{ac.id}</span> {ac.text}</div>
          ))}
        </div>
      )}
    </div>
  )
}

function DecisionCard({ d, onUnify, busyId }: { d: PaulDecision; onUnify: (id: number, outcome: string) => void; busyId: number | null }) {
  return (
    <div className="rounded-lg border border-gray-700/60 bg-gray-900/50 p-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm text-gray-500">#{d.id}</span>
          <span className="font-semibold text-white">{d.symbol}</span>
          <ActionBadge action={d.action} />
          <span className="text-xs text-gray-400">conf {(d.confidence * 100).toFixed(0)}%</span>
          <QualifyBadge q={d.qualify_status} />
          <StatusBadge status={d.status} />
        </div>
        <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">{MODE_LABEL[d.mode]}</span>
      </div>
      <DecisionDetail d={d} />
      {d.status === 'executed' && d.outcome === 'open' && (
        <div className="flex items-center gap-1 mt-3">
          <span className="text-xs text-gray-500 mr-1">Unify (close loop):</span>
          <button disabled={busyId === d.id} onClick={() => onUnify(d.id, 'win')} className="px-2 py-1 rounded bg-green-600/70 hover:bg-green-600 text-white text-xs">Win</button>
          <button disabled={busyId === d.id} onClick={() => onUnify(d.id, 'loss')} className="px-2 py-1 rounded bg-red-600/70 hover:bg-red-600 text-white text-xs">Loss</button>
          <button disabled={busyId === d.id} onClick={() => onUnify(d.id, 'break_even')} className="px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs">Break-even</button>
        </div>
      )}
    </div>
  )
}
