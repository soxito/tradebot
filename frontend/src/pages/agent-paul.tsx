import Head from 'next/head'
import { useCallback, useEffect, useState } from 'react'
import { apiClient } from '@/services/api'
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

type Tab = 'overview' | 'console' | 'queue' | 'history' | 'loop'

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
      </div>
    </>
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
