import Head from 'next/head'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiClient } from '@/services/api'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Crosshair,
  Filter,
  Radio,
  RefreshCw,
  Sparkles,
  TrendingDown,
  TrendingUp,
  Zap,
  Play,
  ShieldCheck,
  ShieldAlert,
  BarChart2,
  Search,
} from 'lucide-react'

interface TelegramSignalMessage {
  id: number
  channel_source_id: number
  source_kind: 'signals' | 'news'
  telegram_message_id: string
  posted_at: string | null
  author_name: string | null
  raw_text: string
  extraction_json: Record<string, unknown> | null
  symbols_json: string[] | null
  confidence: number | null
  created_at: string
}

type SignalStatus = 'active' | 'filled' | 'tp_hit' | 'sl_hit' | 'closed'

interface ParsedSignal {
  id: number
  channel_source_id: number
  channel_title: string | null
  telegram_message_id: string
  symbol: string
  direction: string
  leverage: string | null
  entry: number | null
  entry_raw: string | null
  stop_loss: number | null
  stop_loss_raw: string | null
  trailing_sl: number | null
  tp_reached_count: number
  market_type: string
  take_profits: number[]
  status: SignalStatus | string
  confidence: number | null
  raw_text: string
  posted_at: string | null
  created_at: string
  updated_at: string
}

interface MonitorStatus {
  running: boolean
  interval_seconds: number
  last_run: string | null
  last_result: Record<string, number> | null
  last_error: string | null
}

interface SniperSettings {
  enabled: boolean
  mode: string
  trade_type: 'spot' | 'futures'
  position_size_usdt: number
  max_positions: number
  max_positions_sandbox: number
  max_positions_live: number
  leverage: number
  margin_mode: 'crossed' | 'isolated'
  sniper_offset_pct: number
  min_confidence: number
  min_risk_reward: number
  pending_ttl_minutes: number
  reanalyze: boolean
  execute_sandbox: boolean
  execute_live: boolean
  require_ai_confirmation: boolean
  execute_immediately: boolean
  skipped_reanalyze_minutes: number
  tp_trail_pct: number
  volume_channel_id: number | null
  allowed_channel_ids: number[] | null
}

type SniperTradeStatus = 'pending' | 'placed' | 'skipped' | 'missed' | 'failed'

interface SniperTrade {
  id: number
  signal_id: number
  channel_title: string | null
  symbol: string
  direction: string
  leverage: number | null
  signal_entry: number | null
  sniper_entry: number | null
  live_price_at_plan: number | null
  stop_loss: number | null
  take_profit: number | null
  position_size_usdt: number | null
  risk_reward: number | null
  status: SniperTradeStatus | string
  reason: string | null
  sim_order_id: number | null
  entry_strategy: string | null
  rsi: number | null
  support: number | null
  resistance: number | null
  volume_warning: boolean
  ai_confirmed: boolean | null
  ai_confirmation_note: string | null
  volume_confirmed: boolean | null
  executed_mode: string | null
  live_order_id: string | null
  created_at: string
  updated_at: string
}

interface TelegramChannelSource {
  id: number
  title: string
  channel_handle: string
  source_kind: 'signals' | 'news'
  is_enabled: boolean
}

interface ModelInfo {
  label: string
  context: number
  params: string
  speed: number
  strengths: string[]
  best_for: string
  vision: boolean
  reasoning: boolean
  json_mode: boolean
  cost: string
  notes: string
}

interface AIPreset {
  key: string
  label: string
  type: string
  base_url: string
  default_model: string
  models: string[]
  model_info: Record<string, ModelInfo>
  free_tier: boolean
  daily_limit: number | null
  monthly_limit: number | null
  signup_url: string
  notes: string
  editable_endpoint?: boolean
}

interface AIProvider {
  id: number
  provider_key: string
  label: string
  type: string
  api_key_set: boolean
  base_url: string | null
  default_model: string | null
  models: string[]
  model_info: Record<string, ModelInfo>
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
  monthly_calls: number
  daily_reset_at: string | null
  monthly_reset_at: string | null
}

const MAX_RENDERED_MESSAGES = 120
const MAX_MESSAGE_PREVIEW_CHARS = 1200

function toErrorMessage(error: unknown): string {
  if (typeof error === 'string') return error
  if (error && typeof error === 'object') {
    const errObj = error as {
      message?: string
      response?: { data?: { detail?: string } }
    }
    return errObj.response?.data?.detail || errObj.message || 'Request failed'
  }
  return 'Request failed'
}

const SOUTH_AFRICA_TIMEZONE = 'Africa/Johannesburg'

function formatDate(dateStr: string | null): string {
  if (!dateStr) return 'N/A'
  const date = new Date(dateStr)
  if (Number.isNaN(date.getTime())) return dateStr
  return date.toLocaleString('en-ZA', {
    timeZone: SOUTH_AFRICA_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout>
  const timeoutPromise = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('Request timed out')), ms)
  })

  return Promise.race([promise, timeoutPromise]).finally(() => {
    clearTimeout(timer)
  }) as Promise<T>
}

export default function TelegramSignalsPage() {
  const [messages, setMessages] = useState<TelegramSignalMessage[]>([])
  const [channels, setChannels] = useState<TelegramChannelSource[]>([])
  const [allChannels, setAllChannels] = useState<TelegramChannelSource[]>([])  // all kinds, for volume selector
  const [selectedChannelId, setSelectedChannelId] = useState('')
  const [limit, setLimit] = useState(50)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [polling, setPolling] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expandedMessageIds, setExpandedMessageIds] = useState<Set<number>>(new Set())

  const pollingInFlightRef = useRef(false)
  const loadingMessagesRef = useRef(false)

  // Active Signals tab state
  const [activeTab, setActiveTab] = useState<'signals' | 'forex' | 'trailing' | 'sniper' | 'execute' | 'volume' | 'ai' | 'strategies' | 'messages'>('signals')
  const [signals, setSignals] = useState<ParsedSignal[]>([])
  const [signalStatusFilter, setSignalStatusFilter] = useState<SignalStatus | ''>('active')
  const [signalPrices, setSignalPrices] = useState<Record<string, number | null>>({})
  const [busySignalId, setBusySignalId] = useState<number | null>(null)
  const [monitor, setMonitor] = useState<MonitorStatus | null>(null)
  const [loadingSignals, setLoadingSignals] = useState(false)

  // Forex signals tab state
  const [forexSignals, setForexSignals] = useState<ParsedSignal[]>([])
  const [forexStatusFilter, setForexStatusFilter] = useState<SignalStatus | ''>('active')
  const [forexPrices, setForexPrices] = useState<Record<string, number | null>>({})
  const [loadingForex, setLoadingForex] = useState(false)

  // Sniper tab state
  const [sniperSettings, setSniperSettings] = useState<SniperSettings | null>(null)
  const [sniperTrades, setSniperTrades] = useState<SniperTrade[]>([])
  const [savingSniper, setSavingSniper] = useState(false)
  const [runningSniper, setRunningSniper] = useState(false)

  // AI providers tab state
  const [aiPresets, setAiPresets] = useState<AIPreset[]>([])
  const [aiProviders, setAiProviders] = useState<AIProvider[]>([])

  const loadChannels = useCallback(async () => {
    try {
      const [sigRes, allRes] = await Promise.all([
        apiClient.telegram.getChannels({ user_id: '0', source_kind: 'signals' }),
        apiClient.telegram.getChannels({ user_id: '0' }),  // all kinds for volume selector
      ])
      setChannels(Array.isArray(sigRes.data) ? sigRes.data : [])
      setAllChannels(Array.isArray(allRes.data) ? allRes.data : [])
    } catch {
      // Channels are optional in this view.
    }
  }, [])

  const loadSignals = useCallback(async () => {
    setLoadingSignals(true)
    try {
      const params: { status?: SignalStatus; market_type: 'crypto'; channel_source_id?: number; limit: number } = { limit: 50, market_type: 'crypto' }
      if (signalStatusFilter) params.status = signalStatusFilter
      if (selectedChannelId) params.channel_source_id = Number.parseInt(selectedChannelId, 10)
      const res = await apiClient.telegram.getSignals(params)
      const nextSignals = Array.isArray(res.data) ? res.data : []
      // Guardrail: Active view must never include TP/SL/closed signals even if backend data is stale.
      if (signalStatusFilter === 'active') {
        setSignals(nextSignals.filter((s) => String(s.status).toLowerCase() === 'active'))
      } else {
        setSignals(nextSignals)
      }
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setLoadingSignals(false)
    }
  }, [signalStatusFilter, selectedChannelId])

  const loadForexSignals = useCallback(async () => {
    setLoadingForex(true)
    try {
      const params: { status?: SignalStatus; market_type: 'forex'; limit: number } = { limit: 50, market_type: 'forex' }
      if (forexStatusFilter) params.status = forexStatusFilter
      const res = await apiClient.telegram.getSignals(params)
      const next = Array.isArray(res.data) ? res.data : []
      if (forexStatusFilter === 'active') {
        setForexSignals(next.filter((s) => String(s.status).toLowerCase() === 'active'))
      } else {
        setForexSignals(next)
      }
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setLoadingForex(false)
    }
  }, [forexStatusFilter])

  const loadForexPrices = useCallback(async () => {
    const symbols = Array.from(new Set(forexSignals.filter((s) => String(s.status).toLowerCase() === 'active').map((s) => s.symbol)))
    if (symbols.length === 0) { setForexPrices({}); return }
    try {
      const res = await apiClient.telegram.getSignalPrices(symbols)
      // Backend returns { prices: { SYMBOL: price } }
      setForexPrices((res.data as { prices: Record<string, number | null> }).prices || {})
    } catch { /* non-fatal */ }
  }, [forexSignals])

  const loadSignalPrices = useCallback(async () => {
    const symbols = Array.from(new Set(signals.filter((s) => String(s.status).toLowerCase() === 'active').map((s) => s.symbol)))
    if (symbols.length === 0) {
      setSignalPrices({})
      return
    }
    try {
      const res = await apiClient.telegram.getSignalPrices(symbols)
      setSignalPrices((res.data as { prices: Record<string, number | null> }).prices || {})
    } catch {
      /* best-effort */
    }
  }, [signals])

  useEffect(() => {
    loadSignalPrices()
    const t = setInterval(loadSignalPrices, 15000)
    return () => clearInterval(t)
  }, [loadSignalPrices])

  const executeSignal = useCallback(
    async (signalId: number, symbol: string, direction: string, mode: 'sandbox' | 'live') => {
      if (mode === 'live') {
        const ok = window.confirm(`Place a REAL LIVE ${direction} order for ${symbol}? This uses real funds.`)
        if (!ok) return
      }
      setBusySignalId(signalId)
      setError(null)
      setMessage(null)
      try {
        const res = await apiClient.telegram.executeParsedSignal(signalId, mode, true)
        const d = res.data as { ok: boolean; executed_mode?: string; error?: string }
        if (d.ok) {
          setMessage(`${symbol} executed on ${d.executed_mode}. View it on /trading.`)
        } else {
          setError(`Execution failed: ${d.error}`)
        }
        await loadSignals()
      } catch (e: unknown) {
        setError(toErrorMessage(e))
      } finally {
        setBusySignalId(null)
      }
    },
    [loadSignals]
  )

  const closeSignal = useCallback(
    async (signalId: number, symbol: string, kind: 'close' | 'opposite') => {
      setBusySignalId(signalId)
      setError(null)
      setMessage(null)
      try {
        const text = kind === 'opposite'
          ? `#${symbol} Closed due to opposite direction signal ⚠`
          : `#${symbol} Closed`
        const res = await apiClient.telegram.processOutcome(text)
        const d = res.data as { ok: boolean; message?: string; cancelled_pending_trades?: number; error?: string }
        if (d.ok) {
          setMessage(d.message || `${symbol} ${kind === 'opposite' ? 'closed (opposite direction)' : 'closed'}.`)
        } else {
          setError(d.message || 'Close failed')
        }
        await loadSignals()
      } catch (e: unknown) {
        setError(toErrorMessage(e))
      } finally {
        setBusySignalId(null)
      }
    },
    [loadSignals]
  )

  const loadMonitor = useCallback(async () => {
    try {
      const res = await apiClient.telegram.getMonitorStatus()
      setMonitor(res.data as MonitorStatus)
    } catch {
      // monitor status is best-effort
    }
  }, [])

  const loadSniper = useCallback(async () => {
    try {
      const [settingsRes, tradesRes] = await Promise.all([
        apiClient.telegram.getSniperSettings(),
        apiClient.telegram.getSniperTrades({ limit: 100 }),
      ])
      setSniperSettings(settingsRes.data as SniperSettings)
      setSniperTrades(Array.isArray(tradesRes.data) ? tradesRes.data : [])
    } catch {
      // best-effort
    }
  }, [])

  const saveSniper = useCallback(
    async (patch: Partial<SniperSettings>) => {
      setSavingSniper(true)
      try {
        const res = await apiClient.telegram.updateSniperSettings(patch)
        setSniperSettings(res.data as SniperSettings)
        setMessage('Sniper settings saved.')
      } catch (e: unknown) {
        setError(toErrorMessage(e))
      } finally {
        setSavingSniper(false)
      }
    },
    []
  )

  const runSniperNow = useCallback(async () => {
    setRunningSniper(true)
    setMessage(null)
    setError(null)
    try {
      const res = await apiClient.telegram.runSniper()
      const r = res.data as Record<string, number>
      setMessage(
        `Sniper cycle: ${r.placed ?? 0} placed · ${r.pending ?? 0} pending · ${r.skipped ?? 0} skipped · ${r.missed ?? 0} missed.`
      )
      await loadSniper()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setRunningSniper(false)
    }
  }, [loadSniper])

  const loadAI = useCallback(async () => {
    try {
      const [presetsRes, providersRes] = await Promise.all([
        apiClient.aiAnalyst.getProviderPresets(),
        apiClient.aiAnalyst.getProviders(),
      ])
      setAiPresets(Array.isArray(presetsRes.data) ? presetsRes.data : [])
      setAiProviders(Array.isArray(providersRes.data) ? providersRes.data : [])
    } catch {
      // AI plugin may be unavailable; best-effort
    }
  }, [])

  const loadMessages = useCallback(async () => {
    if (loadingMessagesRef.current) {
      return
    }

    loadingMessagesRef.current = true
    setError(null)
    try {
      const params: {
        user_id: string
        source_kind: 'signals'
        limit: number
        channel_source_id?: number
      } = {
        user_id: '0',
        source_kind: 'signals',
        limit,
      }
      if (selectedChannelId) {
        params.channel_source_id = Number.parseInt(selectedChannelId, 10)
      }

      const res = await apiClient.telegram.getMessages(params)
      setMessages(Array.isArray(res.data) ? res.data : [])
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      loadingMessagesRef.current = false
    }
  }, [limit, selectedChannelId])

  useEffect(() => {
    const initialize = async () => {
      setLoading(true)
      try {
        // Keep first paint unblocked by bounding startup tasks.
        await Promise.allSettled([
          withTimeout(loadChannels(), 8000),
          withTimeout(loadMonitor(), 8000),
          withTimeout(loadSniper(), 8000),
          withTimeout(loadAI(), 8000),
        ])
      } finally {
        setLoading(false)
      }
    }
    initialize()
  }, [loadChannels, loadMonitor, loadSniper, loadAI])

  // Reload crypto signals when filters change.
  useEffect(() => {
    loadSignals()
  }, [loadSignals])

  // Reload forex signals when filters change.
  useEffect(() => {
    loadForexSignals()
  }, [loadForexSignals])

  // Load raw messages only when the Messages tab is active.
  useEffect(() => {
    if (activeTab !== 'messages') {
      return
    }
    loadMessages()
  }, [activeTab, loadMessages])

  // Auto-refresh active signals + monitor status every 30s (monitor polls every 60s)
  useEffect(() => {
    const id = setInterval(() => {
      loadSignals()
      loadForexSignals()
      loadMonitor()
      loadSniper()
    }, 30000)
    return () => clearInterval(id)
  }, [loadSignals, loadForexSignals, loadMonitor, loadSniper])

  const runPoll = useCallback(async (opts?: { silent?: boolean; refreshMessages?: boolean }) => {
    const silent = opts?.silent === true
    const refreshMessages = opts?.refreshMessages ?? true

    if (pollingInFlightRef.current) {
      return
    }

    pollingInFlightRef.current = true
    if (!silent) {
      setPolling(true)
      setMessage(null)
      setError(null)
    }

    try {
      const payload: { user_id: string; limit_per_channel: number; channel_source_ids?: number[] } = {
        user_id: '0',
        limit_per_channel: Math.max(1, Math.min(limit, 200)),
      }
      if (selectedChannelId) {
        payload.channel_source_ids = [Number.parseInt(selectedChannelId, 10)]
      }

      const res = await apiClient.telegram.poll(payload)
      if (!silent) {
        setMessage(`Poll completed: ${res.data.messages_saved} messages saved.`)
      }
      if (refreshMessages) {
        await loadMessages()
      }
    } catch (e: unknown) {
      if (!silent) {
        setError(toErrorMessage(e))
      }
    } finally {
      pollingInFlightRef.current = false
      if (!silent) {
        setPolling(false)
      }
    }
  }, [limit, selectedChannelId, loadMessages])

  // Auto-poll signals every 60s with overlap protection.
  useEffect(() => {
    const id = setInterval(() => {
      runPoll({ silent: true, refreshMessages: activeTab === 'messages' })
    }, 60000)
    return () => clearInterval(id)
  }, [activeTab, runPoll])

  const refresh = async () => {
    setRefreshing(true)
    await loadMessages()
    setRefreshing(false)
  }

  const sortedMessages = useMemo(
    () =>
      [...messages].sort((a, b) => {
        const aDate = new Date(a.posted_at || a.created_at).getTime()
        const bDate = new Date(b.posted_at || b.created_at).getTime()
        return bDate - aDate
      }),
    [messages]
  )

  const visibleMessages = useMemo(
    () => sortedMessages.slice(0, MAX_RENDERED_MESSAGES),
    [sortedMessages]
  )

  const toggleMessageExpansion = useCallback((id: number) => {
    setExpandedMessageIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }, [])

  return (
    <>
      <Head>
        <title>Telegram Signals - TradeBot</title>
      </Head>

      <div className="space-y-6">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white flex items-center gap-2">
              <Radio className="w-7 h-7 text-cyan-400" />
              Telegram Signals
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Review ingested Telegram signal messages and trigger polling from the sidebar menu.
            </p>
          </div>

          <div className="flex gap-2">
            <button
              onClick={refresh}
              disabled={refreshing}
              className="px-3 py-2 rounded-md bg-gray-700 hover:bg-gray-600 text-white text-sm disabled:opacity-60"
            >
              <span className="inline-flex items-center gap-2">
                <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                Refresh
              </span>
            </button>
            <button
              onClick={() => runPoll()}
              disabled={polling}
              className="px-3 py-2 rounded-md bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60"
            >
              <span className="inline-flex items-center gap-2">
                <RefreshCw className={`w-4 h-4 ${polling ? 'animate-spin' : ''}`} />
                Poll Signals
              </span>
            </button>
          </div>
        </div>

        {message && (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-300 flex items-start gap-2">
            <CheckCircle2 className="w-4 h-4 mt-0.5" />
            <span>{message}</span>
          </div>
        )}

        {error && (
          <div className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-300 flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {/* ── Tabs ──────────────────────────────────────────────── */}
        <div className="flex items-center gap-1 border-b border-gray-700">
          <button
            onClick={() => setActiveTab('signals')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'signals'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <Activity className="w-4 h-4" />
              Active Signals
              {signals.length > 0 && (
                <span className="ml-1 rounded-full bg-cyan-500/20 text-cyan-300 px-2 py-0.5 text-[10px]">
                  {signals.length}
                </span>
              )}
            </span>
          </button>
          <button
            onClick={() => setActiveTab('forex')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'forex'
                ? 'border-amber-400 text-amber-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <TrendingUp className="w-4 h-4" />
              Forex Signals
              {forexSignals.length > 0 && (
                <span className="ml-1 rounded-full bg-amber-500/20 text-amber-300 px-2 py-0.5 text-[10px]">
                  {forexSignals.length}
                </span>
              )}
            </span>
          </button>
          <button
            onClick={() => setActiveTab('trailing')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'trailing'
                ? 'border-emerald-400 text-emerald-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <TrendingUp className="w-4 h-4" />
              Trailing SL
              {(() => {
                const all = [...signals, ...forexSignals]
                const trailingCount = all.filter((s) => {
                  const isLong = s.direction?.toLowerCase() === 'long'
                  return s.trailing_sl != null && s.tp_reached_count > 0 &&
                    s.entry != null &&
                    (isLong ? s.trailing_sl >= s.entry * 0.999 : s.trailing_sl <= s.entry * 1.001)
                }).length
                return trailingCount > 0 ? (
                  <span className="ml-1 rounded-full bg-emerald-500/20 text-emerald-300 px-2 py-0.5 text-[10px] animate-pulse">
                    {trailingCount}
                  </span>
                ) : null
              })()}
            </span>
          </button>
          <button
            onClick={() => setActiveTab('sniper')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'sniper'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <Crosshair className="w-4 h-4" />
              Sniper Auto-Trade
              {sniperSettings?.enabled && (
                <span className="ml-1 w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              )}
            </span>
          </button>
          <button
            onClick={() => setActiveTab('execute')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'execute'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <Zap className="w-4 h-4" />
              Execute Signals
            </span>
          </button>
          <button
            onClick={() => setActiveTab('volume')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'volume'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <BarChart2 className="w-4 h-4" />
              Volume Monitor
            </span>
          </button>
          <button
            onClick={() => setActiveTab('ai')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'ai'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <Sparkles className="w-4 h-4" />
              Connect AI
              {aiProviders.some((p) => p.enabled && p.status === 'ok') && (
                <span className="ml-1 rounded-full bg-emerald-500/20 text-emerald-300 px-2 py-0.5 text-[10px]">
                  {aiProviders.filter((p) => p.enabled && p.status === 'ok').length}
                </span>
              )}
            </span>
          </button>
          <button
            onClick={() => setActiveTab('strategies')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'strategies'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <Crosshair className="w-4 h-4" />
              Strategy Scan
            </span>
          </button>
          <button
            onClick={() => setActiveTab('messages')}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === 'messages'
                ? 'border-cyan-400 text-cyan-300'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            <span className="inline-flex items-center gap-2">
              <Radio className="w-4 h-4" />
              Raw Messages
            </span>
          </button>
        </div>

        {/* ── Monitor status banner ─────────────────────────────── */}
        <div
          className={`rounded-md border px-4 py-2.5 text-xs flex flex-wrap items-center gap-x-4 gap-y-1 ${
            monitor?.running
              ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
              : 'border-gray-600/50 bg-gray-800/40 text-gray-400'
          }`}
        >
          <span className="inline-flex items-center gap-1.5 font-medium">
            <span
              className={`w-2 h-2 rounded-full ${monitor?.running ? 'bg-emerald-400 animate-pulse' : 'bg-gray-500'}`}
            />
            Monitor {monitor?.running ? 'running' : 'stopped'} — checks every {monitor?.interval_seconds ?? 60}s
          </span>
          {monitor?.last_run && (
            <span>Last tick: {formatDate(monitor.last_run)}</span>
          )}
          {monitor?.last_result && (
            <span>
              +{monitor.last_result.signals_created ?? 0} signals · {monitor.last_result.messages_saved ?? 0} msgs ·{' '}
              {monitor.last_result.outcomes_applied ?? 0} updates
            </span>
          )}
          {monitor?.last_error && <span className="text-red-300">⚠ {monitor.last_error}</span>}
        </div>

        {activeTab === 'signals' ? (
          <ActiveSignalsView
            signals={signals}
            channels={channels}
            loading={loadingSignals}
            statusFilter={signalStatusFilter}
            onStatusFilter={setSignalStatusFilter}
            selectedChannelId={selectedChannelId}
            onChannelFilter={setSelectedChannelId}
            onRefresh={loadSignals}
            prices={signalPrices}
            settings={sniperSettings}
            onExecute={executeSignal}
            busySignalId={busySignalId}
            onClose={closeSignal}
          />
        ) : activeTab === 'forex' ? (
          <ForexSignalsView
            signals={forexSignals}
            loading={loadingForex}
            statusFilter={forexStatusFilter}
            onStatusFilter={setForexStatusFilter}
            onRefresh={loadForexSignals}
            prices={forexPrices}
          />
        ) : activeTab === 'trailing' ? (
          <TrailingSlView
            cryptoSignals={signals}
            forexSignals={forexSignals}
            cryptoPrices={signalPrices}
            forexPrices={forexPrices}
            onRefresh={() => { loadSignals(); loadForexSignals() }}
            onClose={closeSignal}
          />
        ) : activeTab === 'sniper' ? (
          <SniperView
            settings={sniperSettings}
            trades={sniperTrades}
            saving={savingSniper}
            running={runningSniper}
            onSave={saveSniper}
            onRunNow={runSniperNow}
          />
        ) : activeTab === 'execute' ? (
          <ExecuteView
            trades={sniperTrades}
            settings={sniperSettings}
            onReload={loadSniper}
            onMessage={setMessage}
            onError={setError}
          />
        ) : activeTab === 'volume' ? (
          <VolumeMonitorView
            onMessage={setMessage}
            onError={setError}
            channels={allChannels}
            settings={sniperSettings}
            onSaveSettings={saveSniper}
          />
        ) : activeTab === 'ai' ? (
          <ConnectAIView
            presets={aiPresets}
            providers={aiProviders}
            onReload={loadAI}
            onMessage={setMessage}
            onError={setError}
          />
        ) : activeTab === 'strategies' ? (
          <StrategyScanView signals={signals} />
        ) : (
        <>
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
            <div>
              <label className="text-xs text-gray-400 block mb-1">Channel filter</label>
              <select
                value={selectedChannelId}
                onChange={(e) => setSelectedChannelId(e.target.value)}
                className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
              >
                <option value="">All signal channels</option>
                {channels.map((channel) => (
                  <option key={channel.id} value={String(channel.id)}>
                    {channel.title || channel.channel_handle}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="text-xs text-gray-400 block mb-1">Messages per channel</label>
              <input
                type="number"
                min={1}
                max={200}
                value={limit}
                onChange={(e) => setLimit(Math.max(1, Math.min(Number.parseInt(e.target.value || '50', 10), 200)))}
                className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
              />
            </div>

            <button
              onClick={loadMessages}
              disabled={loading}
              className="rounded bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 text-sm disabled:opacity-60"
            >
              <span className="inline-flex items-center gap-2">
                <Filter className="w-4 h-4" />
                Apply Filters
              </span>
            </button>
          </div>
        </div>

        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <h2 className="font-semibold text-white mb-4">Signal Messages</h2>

          {loading ? (
            <div className="text-sm text-gray-400">Loading signal messages...</div>
          ) : sortedMessages.length === 0 ? (
            <div className="text-sm text-gray-400">No Telegram signal messages found.</div>
          ) : (
            <div className="space-y-3">
              {sortedMessages.length > MAX_RENDERED_MESSAGES && (
                <div className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                  Showing newest {MAX_RENDERED_MESSAGES} of {sortedMessages.length} messages to keep the UI responsive.
                </div>
              )}
              {visibleMessages.map((item) => {
                const direction = String(item.extraction_json?.direction || 'unknown')
                const levels = item.extraction_json?.levels as Record<string, number> | undefined
                const isExpanded = expandedMessageIds.has(item.id)
                const isLong = item.raw_text.length > MAX_MESSAGE_PREVIEW_CHARS
                const displayedText = isExpanded || !isLong
                  ? item.raw_text
                  : `${item.raw_text.slice(0, MAX_MESSAGE_PREVIEW_CHARS)}…`
                return (
                  <div key={item.id} className="rounded-lg border border-gray-700/70 bg-gray-900/40 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-sm text-white font-medium">
                        {item.symbols_json?.join(', ') || 'No symbols detected'}
                      </div>
                      <div className="text-xs text-gray-400">
                        Channel #{item.channel_source_id} • Msg #{item.telegram_message_id}
                      </div>
                    </div>

                    <div className="mt-2 text-xs text-gray-400">
                      Direction: <span className="text-gray-200">{direction}</span>
                      {typeof item.confidence === 'number' && (
                        <>
                          {' '}• Confidence: <span className="text-gray-200">{(item.confidence * 100).toFixed(0)}%</span>
                        </>
                      )}
                    </div>

                    {levels && Object.keys(levels).length > 0 && (
                      <div className="mt-2 text-xs text-gray-400">
                        Levels:{' '}
                        {Object.entries(levels).map(([key, value]) => (
                          <span key={key} className="mr-3 text-gray-200">
                            {key.toUpperCase()}: {value}
                          </span>
                        ))}
                      </div>
                    )}

                    <div className="mt-3 text-sm text-gray-200 whitespace-pre-wrap">{displayedText}</div>
                    {isLong && (
                      <button
                        onClick={() => toggleMessageExpansion(item.id)}
                        className="mt-2 text-xs text-cyan-300 hover:text-cyan-200"
                      >
                        {isExpanded ? 'Show less' : 'Show full message'}
                      </button>
                    )}

                    <div className="mt-3 text-xs text-gray-500">
                      Posted: {formatDate(item.posted_at)} • Stored: {formatDate(item.created_at)}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
        </>
        )}
      </div>
    </>
  )
}

const STATUS_STYLES: Record<string, string> = {
  active: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40',
  filled: 'bg-blue-500/20 text-blue-300 border-blue-500/40',
  tp_hit: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
  sl_hit: 'bg-red-500/20 text-red-300 border-red-500/40',
  closed: 'bg-gray-600/30 text-gray-300 border-gray-600/50',
}

const STATUS_OPTIONS: Array<{ value: SignalStatus | ''; label: string }> = [
  { value: 'active', label: 'Active' },
  { value: 'filled', label: 'Filled' },
  { value: 'tp_hit', label: 'TP Hit' },
  { value: 'sl_hit', label: 'SL Hit' },
  { value: 'closed', label: 'Closed' },
]

function ActiveSignalsView(props: {
  signals: ParsedSignal[]
  channels: TelegramChannelSource[]
  loading: boolean
  statusFilter: SignalStatus | ''
  onStatusFilter: (s: SignalStatus | '') => void
  selectedChannelId: string
  onChannelFilter: (id: string) => void
  onRefresh: () => void
  prices: Record<string, number | null>
  settings: SniperSettings | null
  onExecute: (signalId: number, symbol: string, direction: string, mode: 'sandbox' | 'live') => Promise<void>
  busySignalId: number | null
  onClose: (signalId: number, symbol: string, kind: 'close' | 'opposite') => Promise<void>
}) {
  const { signals, channels, loading, statusFilter, onStatusFilter, selectedChannelId, onChannelFilter, onRefresh, prices, settings, onExecute, busySignalId, onClose } = props

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1 flex-wrap">
            {STATUS_OPTIONS.map((opt) => (
              <button
                key={opt.value || 'all'}
                onClick={() => onStatusFilter(opt.value)}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  statusFilter === opt.value
                    ? 'bg-cyan-600 text-white'
                    : 'bg-gray-900 text-gray-400 hover:text-white border border-gray-700'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>

          <select
            value={selectedChannelId}
            onChange={(e) => onChannelFilter(e.target.value)}
            className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
          >
            <option value="">All channels</option>
            {channels.map((c) => (
              <option key={c.id} value={String(c.id)}>
                {c.title || c.channel_handle}
              </option>
            ))}
          </select>

          <button
            onClick={onRefresh}
            disabled={loading}
            className="ml-auto px-3 py-1.5 rounded text-xs bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-60"
          >
            <span className="inline-flex items-center gap-1.5">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </span>
          </button>
        </div>
      </div>

      {/* Signal cards */}
      {loading && signals.length === 0 ? (
        <div className="text-sm text-gray-400">Loading signals…</div>
      ) : signals.length === 0 ? (
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-8 text-center text-sm text-gray-400">
          No signals for this filter yet. The monitor checks your channels every minute and creates signals automatically.
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {signals.map((s) => {
            const isLong = s.direction?.toLowerCase() === 'long'
            const statusKey = String(s.status).toLowerCase()
            const cur = statusKey === 'active' ? prices[s.symbol] : null
            // TP/SL hit detection against live price
            const tpHit = (tp: number) =>
              cur != null ? (isLong ? cur >= tp : cur <= tp) : false
            // A TP is only valid if it's on the profit side of the entry price.
            // TPs below entry for LONG (or above entry for SHORT) are parse artefacts.
            const isValidTp = (tp: number) =>
              s.entry != null
                ? (isLong ? tp > s.entry * 0.999 : tp < s.entry * 1.001)
                : true  // no entry price — show everything
            const slHit = () =>
              cur != null && s.stop_loss != null
                ? (isLong ? cur <= s.stop_loss : cur >= s.stop_loss)
                : false
            const slTriggered = slHit()
            // Trailing SL is only meaningful if it's on the PROFIT side of entry.
            const effectiveSl = s.trailing_sl ?? s.stop_loss
            const trailingActive =
              s.trailing_sl != null &&
              s.tp_reached_count > 0 &&
              (s.entry != null
                ? (isLong ? s.trailing_sl >= s.entry * 0.999 : s.trailing_sl <= s.entry * 1.001)
                : true)
            const trailSlTriggered =
              cur != null && effectiveSl != null
                ? (isLong ? cur <= effectiveSl : cur >= effectiveSl)
                : false
            return (
              <div key={s.id} className={`rounded-lg border bg-gray-900/40 p-4 transition ${
                trailSlTriggered ? 'border-red-500/60' : trailingActive ? 'border-emerald-500/40' : 'border-gray-700/70'
              }`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-bold ${
                        isLong ? 'bg-emerald-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'
                      }`}
                    >
                      {isLong ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                      {isLong ? 'LONG' : 'SHORT'}
                    </span>
                    <span className="text-base font-bold text-white">{s.symbol}</span>
                    {s.leverage && (
                      <span className="text-xs text-amber-300 bg-amber-500/10 px-1.5 py-0.5 rounded">{s.leverage}</span>
                    )}
                  </div>
                  <span className={`text-[10px] uppercase font-semibold px-2 py-1 rounded border ${STATUS_STYLES[statusKey] || STATUS_STYLES.closed}`}>
                    {statusKey.replace('_', ' ')}
                  </span>
                </div>

                <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Entry</div>
                    <div className="text-white font-medium">{s.entry ?? s.entry_raw ?? '—'}</div>
                  </div>
                  {/* Stop Loss — flashes red when price is at/past effective SL */}
                  <div className={`rounded border p-2 transition ${
                    trailSlTriggered ? 'bg-red-900/30 border-red-500/60'
                    : trailingActive ? 'bg-emerald-900/20 border-emerald-500/40'
                    : 'bg-gray-900/60 border-gray-700/50'
                  }`}>
                    <div className={`text-[11px] flex items-center gap-1 ${
                      trailSlTriggered ? 'text-red-300 font-semibold'
                      : trailingActive ? 'text-emerald-300 font-semibold'
                      : 'text-gray-500'
                    }`}>
                      {trailSlTriggered && <span className="animate-pulse">🔴</span>}
                      {trailingActive && !trailSlTriggered && <span>🔒</span>}
                      {trailingActive ? `Trailing SL (TP${s.tp_reached_count} locked)` : `Stop Loss${trailSlTriggered ? ' HIT' : ''}`}
                    </div>
                    <div className={`font-medium ${
                      trailSlTriggered ? 'text-red-300'
                      : trailingActive ? 'text-emerald-300'
                      : 'text-red-400'
                    }`}>
                      {trailingActive
                        ? (effectiveSl != null ? Number(effectiveSl).toPrecision(6) : '—')
                        : (s.stop_loss ?? s.stop_loss_raw ?? '—')}
                    </div>
                  </div>
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Targets</div>
                    <div className="text-emerald-300 font-medium">{s.take_profits?.length || 0}</div>
                  </div>
                </div>

                {/* Current price + distance to entry (active signals only) */}
                {statusKey === 'active' && (() => {
                  const entry = s.entry
                  const dist = cur != null && entry != null && entry > 0 ? ((cur - entry) / entry) * 100 : null
                  return (
                    <div className="mt-2 flex items-center justify-between rounded bg-gray-900/40 border border-gray-700/40 px-2.5 py-1.5 text-xs">
                      <span className="text-gray-400">Current price</span>
                      <span className="flex items-center gap-2">
                        <span className="text-white font-semibold">{cur != null ? cur : '…'}</span>
                        {dist != null && (
                          <span className={`text-[10px] ${Math.abs(dist) < 1 ? 'text-gray-400' : dist > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {dist > 0 ? '+' : ''}{dist.toFixed(2)}% vs entry
                          </span>
                        )}
                      </span>
                    </div>
                  )
                })()}

                {/* Take-profits — green when hit, amber when within 0.3%, grey otherwise */}
                {s.take_profits && s.take_profits.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {s.take_profits.map((tp, i) => {
                      const validTp = isValidTp(tp)
                      const locked = validTp && i < (s.tp_reached_count ?? 0)
                      const hit = tpHit(tp)
                      const near = !hit && cur != null && Math.abs((cur - tp) / tp) < 0.003
                      return (
                        <span
                          key={i}
                          className={`inline-flex items-center gap-0.5 text-[11px] rounded px-1.5 py-0.5 border font-medium transition ${
                            !validTp
                              ? 'bg-gray-900/40 text-gray-600 border-gray-800/50 line-through'
                              : locked || hit
                              ? 'bg-emerald-500/30 text-emerald-200 border-emerald-400/60'
                              : near
                              ? 'bg-amber-500/20 text-amber-200 border-amber-400/50 animate-pulse'
                              : 'bg-gray-700/30 text-gray-400 border-gray-700/50'
                          }`}
                          title={!validTp ? 'Invalid TP (parse error — wrong side of entry)' : hit ? 'TP reached ✓' : near ? 'Near TP' : `TP${i + 1}`}
                        >
                          {!validTp
                            ? <span className="text-gray-700 text-[9px]">⚠️</span>
                            : (locked || hit)
                            ? <span className="text-emerald-400">{locked ? '🔒' : '✓'}</span>
                            : null}
                          TP{i + 1}: {tp}
                        </span>
                      )
                    })}
                  </div>
                )}

                {/* Execute buttons (demo + live) */}
                {statusKey === 'active' && (
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <button
                      disabled={busySignalId === s.id}
                      onClick={() => onExecute(s.id, s.symbol, s.direction, 'sandbox')}
                      className="flex items-center gap-1 px-2.5 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 rounded text-xs font-medium transition"
                      title="Create a demo (sandbox) order on /trading"
                    >
                      <Play className="w-3 h-3" /> Demo
                    </button>
                    <button
                      disabled={busySignalId === s.id || !settings?.execute_live}
                      onClick={() => onExecute(s.id, s.symbol, s.direction, 'live')}
                      className="flex items-center gap-1 px-2.5 py-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-40 disabled:cursor-not-allowed rounded text-xs font-medium transition"
                      title={settings?.execute_live ? 'Place a REAL live order' : 'Enable live trading in the Sniper tab first'}
                    >
                      <Zap className="w-3 h-3" /> Live
                    </button>
                    {/* Close controls — mirror the channel "Closed due to opposite direction" messages */}
                    <div className="flex items-center gap-1.5 ml-auto">
                      <button
                        disabled={busySignalId === s.id}
                        onClick={() => onClose(s.id, s.symbol, 'opposite')}
                        className="flex items-center gap-1 px-2.5 py-1.5 bg-amber-700/60 hover:bg-amber-600/60 disabled:opacity-40 rounded text-xs font-medium transition"
                        title="Direction reversed — close this signal + cancel pending sniper (like channel's 'Closed due to opposite direction signal')"
                      >
                        ⚠ Opposite direction
                      </button>
                      <button
                        disabled={busySignalId === s.id}
                        onClick={() => onClose(s.id, s.symbol, 'close')}
                        className="flex items-center gap-1 px-2.5 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 rounded text-xs font-medium transition"
                        title="Mark this signal as closed"
                      >
                        ✕ Close
                      </button>
                    </div>
                    {busySignalId === s.id && <span className="text-[11px] text-gray-400">processing…</span>}
                  </div>
                )}

                <div className="mt-3 flex items-center justify-between text-[11px] text-gray-500">
                  <span>{s.channel_title || `Channel #${s.channel_source_id}`}</span>
                  <span>{formatDate(s.posted_at)}</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

const SNIPER_STATUS_STYLES: Record<string, string> = {
  pending: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  placed: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
  skipped: 'bg-gray-600/30 text-gray-300 border-gray-600/50',
  missed: 'bg-orange-500/20 text-orange-300 border-orange-500/40',
  failed: 'bg-red-500/20 text-red-300 border-red-500/40',
}

function SniperView(props: {
  settings: SniperSettings | null
  trades: SniperTrade[]
  saving: boolean
  running: boolean
  onSave: (patch: Partial<SniperSettings>) => void
  onRunNow: () => void
}) {
  const { settings, trades, saving, running, onSave, onRunNow } = props

  if (!settings) {
    return <div className="text-sm text-gray-400">Loading sniper settings…</div>
  }

  const counts = trades.reduce<Record<string, number>>((acc, t) => {
    acc[t.status] = (acc[t.status] || 0) + 1
    return acc
  }, {})

  const numberField = (
    label: string,
    key: keyof SniperSettings,
    step = 1,
    suffix?: string
  ) => (
    <div>
      <label className="block text-xs text-gray-400 mb-1">{label}</label>
      <div className="flex items-center gap-1">
        <input
          type="number"
          step={step}
          defaultValue={String(settings[key] as number)}
          onBlur={(e) => {
            const v = Number(e.target.value)
            if (!Number.isNaN(v) && v !== settings[key]) onSave({ [key]: v } as Partial<SniperSettings>)
          }}
          className="w-full rounded bg-gray-900 border border-gray-700 px-2.5 py-1.5 text-sm text-white"
        />
        {suffix && <span className="text-xs text-gray-500">{suffix}</span>}
      </div>
    </div>
  )

  return (
    <div className="space-y-4">
      {/* Master switch + explanation */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="font-semibold text-white flex items-center gap-2">
              <Crosshair className="w-5 h-5 text-cyan-400" />
              Sniper Auto-Trade
            </h2>
            <p className="text-xs text-gray-400 mt-1 max-w-2xl">
              Re-analyses every active Telegram signal against the live Bitget price, computes an
              optimised <strong className="text-gray-200">sniper entry</strong> (waits for a better fill),
              validates reward/risk, then places <strong className="text-gray-200">paper trades</strong> into
              your simulation account — visible on the <a href="/trading" className="text-cyan-400 underline">Trading</a> page.
            </p>
          </div>
          <button
            onClick={() => onSave({ enabled: !settings.enabled })}
            disabled={saving}
            className={`shrink-0 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              settings.enabled
                ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
            }`}
          >
            {settings.enabled ? '● Enabled' : '○ Disabled'}
          </button>
        </div>

        <div className="mt-4 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {numberField('Position size', 'position_size_usdt', 10, 'USDT')}
          {numberField('Leverage', 'leverage', 1, 'x')}
          {numberField('Max active (Demo)', 'max_positions_sandbox', 1)}
          {numberField('Max active (Live)', 'max_positions_live', 1)}
          {numberField('Sniper offset', 'sniper_offset_pct', 0.1, '%')}
          {numberField('Min confidence', 'min_confidence', 0.05)}
          {numberField('Min reward/risk', 'min_risk_reward', 0.1)}
          {numberField('Pending TTL', 'pending_ttl_minutes', 5, 'min')}
          {numberField('Re-check skipped', 'skipped_reanalyze_minutes', 5, 'min')}
          <div>
            <label className="block text-xs text-gray-400 mb-1">Margin mode</label>
            <select
              value={settings.margin_mode}
              onChange={(e) => onSave({ margin_mode: e.target.value as 'crossed' | 'isolated' })}
              className="w-full rounded bg-gray-900 border border-gray-700 px-2.5 py-1.5 text-sm text-white"
            >
              <option value="crossed">crossed</option>
              <option value="isolated">isolated</option>
            </select>
          </div>
        </div>

        {/* Execution targets + confirmation */}
        <div className="mt-4 flex flex-wrap items-center gap-4 pt-3 border-t border-gray-700">
          <button
            onClick={() => onSave({ execute_sandbox: !settings.execute_sandbox })}
            className="flex items-center gap-2 text-xs text-gray-300"
            title="Auto-place confirmed signals on the simulation account"
          >
            <span className={`relative inline-flex h-4 w-7 items-center rounded-full transition ${settings.execute_sandbox ? 'bg-emerald-600' : 'bg-gray-600'}`}>
              <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition ${settings.execute_sandbox ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
            </span>
            Auto-execute Sandbox
          </button>
          <button
            onClick={() => onSave({ execute_live: !settings.execute_live })}
            className="flex items-center gap-2 text-xs text-gray-300"
            title="Auto-place confirmed signals as REAL live orders (uses real funds)"
          >
            <span className={`relative inline-flex h-4 w-7 items-center rounded-full transition ${settings.execute_live ? 'bg-red-600' : 'bg-gray-600'}`}>
              <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition ${settings.execute_live ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
            </span>
            <span className="flex items-center gap-1">Auto-execute Live <span className="text-red-400">(real money)</span></span>
          </button>
          <button
            onClick={() => onSave({ require_ai_confirmation: !settings.require_ai_confirmation })}
            className="flex items-center gap-2 text-xs text-gray-300"
            title="Only auto-execute when the AI agents + exchange volume confirm the signal direction"
          >
            <span className={`relative inline-flex h-4 w-7 items-center rounded-full transition ${settings.require_ai_confirmation ? 'bg-cyan-600' : 'bg-gray-600'}`}>
              <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition ${settings.require_ai_confirmation ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
            </span>
            Require AI + volume confirmation
          </button>
          <button
            onClick={() => onSave({ execute_immediately: !settings.execute_immediately })}
            className="flex items-center gap-2 text-xs text-gray-300"
            title="Place confirmed signals immediately at market (so they appear on /trading) instead of waiting for the optimised limit entry"
          >
            <span className={`relative inline-flex h-4 w-7 items-center rounded-full transition ${settings.execute_immediately ? 'bg-emerald-600' : 'bg-gray-600'}`}>
              <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition ${settings.execute_immediately ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
            </span>
            Execute immediately (market)
          </button>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={onRunNow}
            disabled={running || !settings.enabled}
            className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60"
          >
            <span className="inline-flex items-center gap-2">
              <Crosshair className={`w-4 h-4 ${running ? 'animate-pulse' : ''}`} />
              {running ? 'Running…' : 'Run Sniper Now'}
            </span>
          </button>
          <span className="text-xs text-gray-500">Also runs automatically every minute with the monitor.</span>
        </div>
      </div>

      {/* Status summary */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
        {(['pending', 'placed', 'missed', 'skipped', 'failed'] as const).map((st) => (
          <div key={st} className="rounded-lg border border-gray-700 bg-gray-900/40 p-3 text-center">
            <div className="text-2xl font-bold text-white">{counts[st] || 0}</div>
            <div className="text-[11px] uppercase tracking-wide text-gray-500">{st}</div>
          </div>
        ))}
      </div>

      {/* Trades table */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
        <h3 className="font-semibold text-white mb-3 text-sm">Sniper Trades</h3>
        {trades.length === 0 ? (
          <div className="text-sm text-gray-400">
            No sniper trades yet. Enable the engine — it will plan optimised entries from your active signals.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-700">
                  <th className="text-left py-2 px-2">Symbol</th>
                  <th className="text-left py-2 px-2">Side</th>
                  <th className="text-right py-2 px-2">Signal Entry</th>
                  <th className="text-right py-2 px-2">Sniper Entry</th>
                  <th className="text-right py-2 px-2">SL</th>
                  <th className="text-right py-2 px-2">TP</th>
                  <th className="text-right py-2 px-2">R:R</th>
                  <th className="text-right py-2 px-2">RSI</th>
                  <th className="text-center py-2 px-2">Status</th>
                  <th className="text-left py-2 px-2">Strategy / Note</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => {
                  const isLong = t.direction?.toLowerCase() === 'long'
                  const statusKey = String(t.status).toLowerCase()
                  return (
                    <tr key={t.id} className="border-b border-gray-800/60 hover:bg-gray-900/30">
                      <td className="py-2 px-2 font-medium text-white">{t.symbol}</td>
                      <td className="py-2 px-2">
                        <span className={isLong ? 'text-emerald-300' : 'text-red-300'}>
                          {isLong ? 'LONG' : 'SHORT'}
                          {t.leverage ? ` ${t.leverage}x` : ''}
                        </span>
                      </td>
                      <td className="py-2 px-2 text-right text-gray-300">{t.signal_entry ?? '—'}</td>
                      <td className="py-2 px-2 text-right text-cyan-300 font-medium">{t.sniper_entry ?? '—'}</td>
                      <td className="py-2 px-2 text-right text-red-300">{t.stop_loss ?? '—'}</td>
                      <td className="py-2 px-2 text-right text-emerald-300">{t.take_profit ?? '—'}</td>
                      <td className="py-2 px-2 text-right text-gray-300">{t.risk_reward ?? '—'}</td>
                      <td className={`py-2 px-2 text-right ${
                        t.rsi == null ? 'text-gray-500'
                          : t.rsi >= 70 ? 'text-red-300'
                          : t.rsi <= 30 ? 'text-emerald-300'
                          : 'text-gray-300'
                      }`}>{t.rsi ?? '—'}</td>
                      <td className="py-2 px-2 text-center">
                        <span className={`text-[10px] uppercase font-semibold px-2 py-0.5 rounded border ${SNIPER_STATUS_STYLES[statusKey] || SNIPER_STATUS_STYLES.skipped}`}>
                          {statusKey}
                        </span>
                      </td>
                      <td className="py-2 px-2 text-gray-400 max-w-[260px] truncate" title={t.reason || ''}>
                        {t.volume_warning && <span className="text-orange-300 mr-1">⚠ vol</span>}
                        {t.entry_strategy || t.reason || ''}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Volume Monitor + Signal Analysis ─────────────────────────────────────
interface VolMonitorItem {
  signal_id: number
  symbol: string
  direction: string
  entry: number | null
  channel_title: string | null
  current_price: number | null
  distance_pct: number | null
  available: boolean
  buy_pct: number | null
  sell_pct: number | null
  opposing_pct: number | null
  volume_spike: boolean
  opposite_volume: boolean
  vol_ratio: number | null
}

function VolumeMonitorView(props: {
  onMessage: (m: string | null) => void
  onError: (e: string | null) => void
  channels: TelegramChannelSource[]
  settings: SniperSettings | null
  onSaveSettings: (patch: Partial<SniperSettings>) => void
}) {
  const { onMessage, onError, channels, settings, onSaveSettings } = props
  const [items, setItems] = useState<VolMonitorItem[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedSignalId, setSelectedSignalId] = useState<number | null>(null)
  const [analysis, setAnalysis] = useState<any>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [reanalyzingSkipped, setReanalyzingSkipped] = useState(false)
  const [volAlertText, setVolAlertText] = useState('')
  const [sendingAlert, setSendingAlert] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await apiClient.telegram.getVolumeMonitor(30)
      setItems((res.data as { items: VolMonitorItem[] }).items || [])
    } catch {
      /* best-effort */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 12000)
    return () => clearInterval(t)
  }, [load])

  const runAnalysis = async (signalId: number) => {
    setSelectedSignalId(signalId)
    setAnalyzing(true)
    setAnalysis(null)
    onError(null)
    try {
      const res = await apiClient.telegram.analyzeSignal(signalId)
      setAnalysis(res.data)
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    } finally {
      setAnalyzing(false)
    }
  }

  const reanalyzeSkipped = async () => {
    setReanalyzingSkipped(true)
    onError(null)
    try {
      const res = await apiClient.telegram.reanalyzeSkipped()
      const d = res.data as { promoted: number; reconsidered: number }
      onMessage(`Re-analysis complete: ${d.reconsidered} reconsidered, ${d.promoted} promoted to pending.`)
      await load()
    } catch (e: unknown) { onError(toErrorMessage(e)) }
    finally { setReanalyzingSkipped(false) }
  }

  const sendVolumeAlert = async () => {
    if (!volAlertText.trim()) return
    setSendingAlert(true)
    onError(null)
    try {
      const res = await apiClient.telegram.volumeAlert(volAlertText.trim())
      const d = res.data as { symbols_found: string[]; triggered: string[] }
      onMessage(`Volume alert: found ${d.symbols_found.length} symbols, triggered ${d.triggered.length} orders (${d.triggered.join(', ')}).`)
      setVolAlertText('')
      await load()
    } catch (e: unknown) { onError(toErrorMessage(e)) }
    finally { setSendingAlert(false) }
  }

  const VolBar = ({ pct, color }: { pct: number | null; color: string }) => (
    <div className="h-1.5 bg-gray-900 rounded-full overflow-hidden w-16 inline-block align-middle">
      <div className={`h-full ${color}`} style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
    </div>
  )

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="bg-gradient-to-br from-blue-500/10 to-transparent border border-blue-500/30 rounded-lg p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-semibold text-white flex items-center gap-2">
              <BarChart2 className="w-5 h-5 text-blue-400" /> Live Volume Monitor
            </h2>
            <p className="text-xs text-gray-400 mt-1">
              Real buy/sell order-flow for every active signal. Skipped signals re-analyse every
              {' '}<strong className="text-white">{settings?.skipped_reanalyze_minutes ?? 15}min</strong>
              {' '}and are promoted if conditions improve. Click <strong className="text-white">Analyse</strong> for a full AI report.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={reanalyzeSkipped}
              disabled={reanalyzingSkipped}
              className="px-3 py-1.5 bg-blue-700 hover:bg-blue-600 disabled:opacity-40 rounded text-xs font-medium transition"
            >
              {reanalyzingSkipped ? 'Checking…' : 'Re-check Skipped'}
            </button>
            <button onClick={load} className="p-2 bg-gray-800 rounded hover:bg-gray-700 transition">
              <RefreshCw className={`w-4 h-4 text-gray-400 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        {/* Volume-channel configuration */}
        <div className="mt-4 pt-4 border-t border-blue-500/20 space-y-3">
          <h3 className="text-xs font-semibold text-gray-300">Volume-Alert Channel (Telegram)</h3>
          <p className="text-[11px] text-gray-400">
            Select a channel that sends whale/volume alerts. When a message arrives for a token that
            matches an active or skipped signal <strong className="text-white">and the direction aligns</strong>,
            the system re-assesses and auto-executes. Supports the{' '}
            <span className="text-cyan-300">Binance Whale Volume Signals</span> format (💰 #SYMBOL LONG/SHORT · Price · Sequence).
          </p>

          {/* Active channel status */}
          {settings?.volume_channel_id && (() => {
            const ch = channels.find(c => c.id === settings.volume_channel_id)
            return ch ? (
              <div className="flex items-center gap-2 text-xs bg-cyan-900/20 border border-cyan-500/30 rounded px-3 py-2">
                <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
                <span className="text-cyan-300 font-medium">Active:</span>
                <span className="text-white">{ch.title || ch.channel_handle}</span>
                <span className="text-gray-500">— new messages auto-matched against your signals every monitor tick</span>
              </div>
            ) : null
          })()}

          <div className="flex flex-wrap items-center gap-2">
            <select
              value={settings?.volume_channel_id ?? ''}
              onChange={(e) => onSaveSettings({ volume_channel_id: e.target.value ? Number(e.target.value) : null })}
              className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white min-w-[220px]"
            >
              <option value="">— No volume channel selected —</option>
              {channels.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id === 7 ? '🐋 ' : ''}{c.title || c.channel_handle}
                </option>
              ))}
            </select>
            <div className="flex items-center gap-1 text-[11px]">
              <span className="text-gray-500">Re-check skipped every</span>
              <input
                type="number"
                min={0} max={1440} step={5}
                value={settings?.skipped_reanalyze_minutes ?? 15}
                onChange={(e) => onSaveSettings({ skipped_reanalyze_minutes: parseInt(e.target.value) || 0 })}
                className="w-16 rounded bg-gray-900 border border-gray-700 px-2 py-1 text-xs text-white text-center"
              />
              <span className="text-gray-500">min</span>
            </div>
          </div>

          {/* How it works */}
          <div className="text-[11px] text-gray-600 space-y-0.5">
            <div>💰 Each whale alert is matched to active/skipped signals by token symbol.</div>
            <div>↔ Direction check: LONG whale on a LONG signal ✓ · LONG whale on a SHORT signal ✗</div>
            <div>🔢 Sequence 3+ (🔴🔴🔴) = repeated strong pressure = higher confidence.</div>
            <div>⚡ If plan passes R:R check, the trade is auto-placed on your enabled targets.</div>
          </div>

          {/* Manual volume-alert test */}
          <div className="space-y-1.5">
            <div className="text-[11px] text-gray-500">Test manually (paste a whale message):</div>
            <div className="flex items-center gap-2">
              <input
                value={volAlertText}
                onChange={(e) => setVolAlertText(e.target.value)}
                placeholder="💰 #LTCUSDT LONG 🟢  Long Volume: $217k  Sequence: 3  Price: 41.97"
                className="flex-1 rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
              />
              <button
                onClick={sendVolumeAlert}
                disabled={sendingAlert || !volAlertText.trim()}
                className="px-3 py-1.5 bg-cyan-700 hover:bg-cyan-600 disabled:opacity-40 rounded text-xs font-medium transition whitespace-nowrap"
              >
                {sendingAlert ? 'Processing…' : 'Test Alert'}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-700">
              <th className="text-left py-2.5 px-3">Symbol</th>
              <th className="text-left py-2.5 px-3">Side</th>
              <th className="text-right py-2.5 px-3">Entry</th>
              <th className="text-right py-2.5 px-3">Price</th>
              <th className="text-right py-2.5 px-3">Dist%</th>
              <th className="text-center py-2.5 px-3">Buy%</th>
              <th className="text-center py-2.5 px-3">Sell%</th>
              <th className="text-center py-2.5 px-3">Vol×</th>
              <th className="text-center py-2.5 px-3">Status</th>
              <th className="py-2.5 px-3"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => {
              const long = it.direction?.toLowerCase() === 'long'
              const safe = !it.opposite_volume && it.available
              const warn = it.opposite_volume
              return (
                <tr key={it.signal_id} className="border-b border-gray-800/50 hover:bg-gray-800/20">
                  <td className="py-2 px-3 font-medium text-white">{it.symbol}</td>
                  <td className="py-2 px-3">
                    <span className={`flex items-center gap-1 ${long ? 'text-emerald-400' : 'text-red-400'}`}>
                      {long ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                      {it.direction?.toUpperCase()}
                    </span>
                  </td>
                  <td className="py-2 px-3 text-right text-gray-400">{it.entry ?? '—'}</td>
                  <td className="py-2 px-3 text-right text-white font-medium">{it.current_price ?? '…'}</td>
                  <td className={`py-2 px-3 text-right font-medium ${
                    it.distance_pct == null ? 'text-gray-500' :
                    it.distance_pct > 0 ? 'text-emerald-400' : 'text-red-400'
                  }`}>
                    {it.distance_pct != null ? `${it.distance_pct > 0 ? '+' : ''}${it.distance_pct}%` : '—'}
                  </td>
                  <td className="py-2 px-3 text-center">
                    {it.available ? (
                      <span className="flex items-center gap-1.5 justify-center">
                        <VolBar pct={it.buy_pct} color="bg-emerald-500" />
                        <span className="text-emerald-300">{it.buy_pct}%</span>
                      </span>
                    ) : <span className="text-gray-600">—</span>}
                  </td>
                  <td className="py-2 px-3 text-center">
                    {it.available ? (
                      <span className="flex items-center gap-1.5 justify-center">
                        <VolBar pct={it.sell_pct} color="bg-red-500" />
                        <span className="text-red-300">{it.sell_pct}%</span>
                      </span>
                    ) : <span className="text-gray-600">—</span>}
                  </td>
                  <td className="py-2 px-3 text-center text-gray-400">
                    {it.vol_ratio != null ? `${it.vol_ratio}×` : '—'}
                    {it.volume_spike && <span className="ml-1 text-amber-400">⚡</span>}
                  </td>
                  <td className="py-2 px-3 text-center">
                    {!it.available ? (
                      <span className="text-gray-600 text-[10px]">no data</span>
                    ) : warn ? (
                      <span className="flex items-center gap-1 text-amber-400 text-[10px]"><ShieldAlert className="w-3 h-3" />opp vol</span>
                    ) : (
                      <span className="flex items-center gap-1 text-emerald-400 text-[10px]"><ShieldCheck className="w-3 h-3" />ok</span>
                    )}
                  </td>
                  <td className="py-2 px-3">
                    <button
                      onClick={() => runAnalysis(it.signal_id)}
                      disabled={analyzing && selectedSignalId === it.signal_id}
                      className="flex items-center gap-1 px-2 py-1 bg-cyan-700 hover:bg-cyan-600 disabled:opacity-40 rounded text-[11px] font-medium transition whitespace-nowrap"
                    >
                      <Search className="w-3 h-3" />
                      {analyzing && selectedSignalId === it.signal_id ? 'Analysing…' : 'Analyse'}
                    </button>
                  </td>
                </tr>
              )
            })}
            {!loading && items.length === 0 && (
              <tr><td colSpan={10} className="py-8 text-center text-gray-500">No active signals to monitor.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Signal Analysis Panel */}
      {analysis && (
        <SignalAnalysisPanel
          analysis={analysis}
          onClose={() => setAnalysis(null)}
          onMessage={onMessage}
          onError={onError}
        />
      )}
    </div>
  )
}

function SignalAnalysisPanel({ analysis, onClose, onMessage, onError }: {
  analysis: any
  onClose: () => void
  onMessage: (m: string | null) => void
  onError: (e: string | null) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const sig = analysis.signal || {}
  const vol = analysis.volume || {}
  const ai = analysis.ai_agents || {}
  const sn = analysis.sniper_entries || {}
  const decision = analysis.decision as string
  const decisionColor = decision === 'execute' ? 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10'
    : decision === 'skip' ? 'text-red-400 border-red-500/40 bg-red-500/10'
    : 'text-amber-400 border-amber-500/40 bg-amber-500/10'
  const decisionIcon = decision === 'execute' ? '✓' : decision === 'skip' ? '✗' : '⊙'

  const exec = async (mode: 'sandbox' | 'live') => {
    if (mode === 'live') {
      const ok = window.confirm(`Place a REAL LIVE ${sig.direction} order for ${sig.symbol}? This uses real funds.`)
      if (!ok) return
    }
    setBusy(mode)
    onError(null)
    try {
      const res = await apiClient.telegram.executeParsedSignal(sig.id, mode, true)
      const d = res.data as { ok: boolean; executed_mode?: string; error?: string }
      if (d.ok) onMessage(`${sig.symbol} executed on ${d.executed_mode}. View it on /trading.`)
      else onError(`Execution failed: ${d.error}`)
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="bg-gray-900 border border-cyan-500/40 rounded-xl p-5 space-y-5">
      {/* Title */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-white font-bold text-base flex items-center gap-2">
            <Search className="w-4 h-4 text-cyan-400" />
            Signal Analysis — {sig.symbol}
            <span className={`text-xs font-medium ${sig.direction === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>
              {sig.direction?.toUpperCase()}
            </span>
          </h3>
          {sig.channel_title && <div className="text-[11px] text-gray-500 mt-0.5">{sig.channel_title}</div>}
        </div>
        <button onClick={onClose} className="text-gray-500 hover:text-white transition text-lg leading-none">✕</button>
      </div>

      {/* Decision banner */}
      <div className={`rounded-lg border px-4 py-3 ${decisionColor}`}>
        <div className="font-bold text-sm">{decisionIcon} {decision.toUpperCase()}</div>
        <div className="text-xs mt-0.5 opacity-90">{analysis.decision_reason}</div>
      </div>

      {/* Price grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          ['Current', analysis.current_price, 'text-white'],
          ['Signal Entry', sig.entry ?? '—', 'text-gray-200'],
          ['Stop Loss', sig.stop_loss ?? '—', 'text-red-300'],
          ['Target 1', (sig.take_profits || [])[0] ?? '—', 'text-emerald-300'],
        ].map(([label, val, color]) => (
          <div key={label as string} className="bg-gray-800/50 rounded-lg p-3">
            <div className="text-[11px] text-gray-400">{label as string}</div>
            <div className={`font-bold text-sm mt-0.5 ${color as string}`}>{val as string | number}</div>
          </div>
        ))}
      </div>

      {/* Volume + TA */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4 space-y-3">
        <h4 className="text-xs font-semibold text-gray-300 uppercase tracking-wide">Exchange Volume & TA</h4>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <div><div className="text-gray-500">RSI</div><div className="font-semibold text-white">{vol.rsi ?? '—'}</div></div>
          <div><div className="text-gray-500">Recommend</div>
            <div className={`font-semibold ${vol.recommend === 'enter' ? 'text-emerald-400' : vol.recommend === 'skip' ? 'text-red-400' : 'text-amber-400'}`}>{(vol.recommend || '—').toUpperCase()}</div>
          </div>
          <div><div className="text-gray-500">Support</div><div className="font-semibold text-gray-200">{vol.support ?? '—'}</div></div>
          <div><div className="text-gray-500">Resistance</div><div className="font-semibold text-gray-200">{vol.resistance ?? '—'}</div></div>
        </div>
        {vol.note && <div className="text-[11px] text-gray-400 italic">{vol.note}</div>}
        <div className={`flex items-center gap-2 text-xs ${vol.volume_confirms ? 'text-emerald-400' : 'text-amber-400'}`}>
          {vol.volume_confirms ? <ShieldCheck className="w-3.5 h-3.5" /> : <ShieldAlert className="w-3.5 h-3.5" />}
          Volume {vol.volume_confirms ? 'confirms' : 'opposes'} direction
          {vol.volume_ratio != null && ` (opposing ${Math.round(vol.volume_ratio * 100)}%)`}
        </div>
      </div>

      {/* AI Agents */}
      {ai && (
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4 space-y-3">
          <h4 className="text-xs font-semibold text-gray-300 uppercase tracking-wide">AI Agents ({ai.ai_calls ?? 0} calls)</h4>
          <div className="flex items-center gap-3 text-xs">
            <span className={`font-bold px-2 py-0.5 rounded ${ai.final_action === 'buy' ? 'bg-emerald-900/40 text-emerald-300' : ai.final_action === 'sell' ? 'bg-red-900/40 text-red-300' : 'bg-gray-700 text-gray-300'}`}>
              {(ai.final_action || 'hold').toUpperCase()}
            </span>
            <span className="text-gray-400">{Math.round((ai.final_confidence || 0) * 100)}% confidence</span>
            <span className={analysis.ai_confirms ? 'text-emerald-400' : 'text-amber-400'}>
              {analysis.ai_confirms ? '✓ confirms direction' : '⚠ not aligned'}
            </span>
          </div>
          {ai.reasoning && <div className="text-[11px] text-gray-400 italic">{ai.reasoning}</div>}
          {ai.decisions && ai.decisions.length > 0 && (
            <div className="space-y-1">
              {ai.decisions.map((d: any, i: number) => (
                <div key={i} className="flex items-center gap-2 text-[11px] text-gray-400">
                  <span className="text-gray-600 w-28 shrink-0">{d.role}</span>
                  <span className={`font-medium ${d.action === 'buy' ? 'text-emerald-400' : d.action === 'sell' ? 'text-red-400' : 'text-gray-400'}`}>{d.action}</span>
                  <span className="text-gray-600">{Math.round((d.confidence || 0) * 100)}%</span>
                  {d.provider && <span className="text-gray-700">· {d.provider}</span>}
                  <span className="truncate">{(d.reasoning || '').slice(0, 80)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Sniper entries */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4 space-y-3">
        <h4 className="text-xs font-semibold text-gray-300 uppercase tracking-wide">Sniper Entry Suggestions</h4>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
          {/* Primary (signal direction) */}
          <div className="bg-gray-900/50 rounded-lg p-3">
            <div className="text-[11px] text-gray-500 mb-1">Primary ({sig.direction?.toUpperCase() || '—'})</div>
            {sn.primary?.ok ? (
              <>
                <div className="font-bold text-white">{sn.primary.entry}</div>
                <div className="text-gray-500">SL {sn.primary.stop_loss ?? '—'} · TP {sn.primary.take_profit ?? '—'}</div>
                {sn.primary.risk_reward && <div className="text-cyan-300">R:R {sn.primary.risk_reward?.toFixed(2)}</div>}
                <div className={`mt-1 text-[10px] ${sn.primary.trigger_now ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {sn.primary.trigger_now ? '⚡ trigger now' : '⏳ wait for pull-back'}
                </div>
              </>
            ) : (
              <div className="text-amber-400 text-[10px]">{sn.primary?.reason || 'No plan'}</div>
            )}
          </div>
          {/* Buy sniper */}
          <div className="bg-emerald-900/10 border border-emerald-500/20 rounded-lg p-3">
            <div className="text-[11px] text-emerald-400 mb-1">Buy Sniper Entry</div>
            <div className="font-bold text-white">{sn.buy?.entry}</div>
            <div className="text-gray-500 text-[10px] mt-1">{sn.buy?.note}</div>
          </div>
          {/* Sell sniper */}
          <div className="bg-red-900/10 border border-red-500/20 rounded-lg p-3">
            <div className="text-[11px] text-red-400 mb-1">Sell Sniper Entry</div>
            <div className="font-bold text-white">{sn.sell?.entry}</div>
            <div className="text-gray-500 text-[10px] mt-1">{sn.sell?.note}</div>
          </div>
        </div>
      </div>

      {/* Execution buttons */}
      <div className="flex items-center gap-3 pt-2">
        <span className="text-xs text-gray-500">Execute this signal:</span>
        <button
          disabled={!!busy}
          onClick={() => exec('sandbox')}
          className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 rounded text-sm font-medium transition"
        >
          <Play className="w-3.5 h-3.5" /> {busy === 'sandbox' ? 'Executing…' : 'Demo'}
        </button>
        <button
          disabled={!!busy}
          onClick={() => exec('live')}
          className="flex items-center gap-1.5 px-3 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-40 rounded text-sm font-medium transition"
        >
          <Zap className="w-3.5 h-3.5" /> {busy === 'live' ? 'Executing…' : 'Live'}
        </button>
        <span className="text-[11px] text-gray-600">Live places a real order.</span>
      </div>
    </div>
  )
}

const AI_STATUS_STYLES: Record<string, string> = {
  ok: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
  error: 'bg-red-500/20 text-red-300 border-red-500/40',
  unknown: 'bg-gray-600/30 text-gray-300 border-gray-600/50',
}

// ── Execute Signals tab: manually fire telegram signals to sandbox/live ──
function ExecuteView(props: {
  trades: SniperTrade[]
  settings: SniperSettings | null
  onReload: () => void
  onMessage: (m: string | null) => void
  onError: (e: string | null) => void
}) {
  const { trades, settings, onReload, onMessage, onError } = props
  const [busyId, setBusyId] = useState<number | null>(null)

  // Manually executable = not yet placed (pending / skipped / failed / missed)
  const pendingTrades = trades.filter((t) => t.status !== 'placed')

  const execute = async (trade: SniperTrade, mode: 'sandbox' | 'live' | 'both', force: boolean) => {
    if (mode !== 'sandbox' && (trade.ai_confirmed === false || trade.volume_confirmed === false) && !force) {
      onError('Signal not confirmed by AI/volume — use Force to override.')
      return
    }
    if (mode === 'live' || mode === 'both') {
      const ok = window.confirm(
        `Place a REAL LIVE order for ${trade.symbol} (${trade.direction})? This uses real funds.`
      )
      if (!ok) return
    }
    setBusyId(trade.id)
    onError(null)
    onMessage(null)
    try {
      const res = await apiClient.telegram.executeSniperTrade(trade.id, mode, force)
      const d = res.data as { ok: boolean; executed_mode?: string; error?: string }
      if (d.ok) {
        onMessage(`${trade.symbol} executed on ${d.executed_mode}. View it on /trading.`)
      } else {
        onError(`Execution failed: ${d.error}`)
      }
      onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    } finally {
      setBusyId(null)
    }
  }

  const ConfBadge = ({ value, label }: { value: boolean | null; label: string }) => {
    if (value === null || value === undefined)
      return <span className="text-[10px] text-gray-500">{label}: n/a</span>
    return (
      <span className={`inline-flex items-center gap-1 text-[10px] ${value ? 'text-emerald-400' : 'text-amber-400'}`}>
        {value ? <ShieldCheck className="w-3 h-3" /> : <ShieldAlert className="w-3 h-3" />}
        {label}
      </span>
    )
  }

  return (
    <div className="space-y-4">
      {/* Explainer */}
      <div className="bg-gradient-to-br from-cyan-500/10 to-transparent border border-cyan-500/30 rounded-lg p-5">
        <h2 className="font-semibold text-white flex items-center gap-2">
          <Zap className="w-5 h-5 text-cyan-400" /> Execute Telegram Signals
        </h2>
        <p className="text-xs text-gray-300 mt-2 max-w-3xl">
          Every telegram signal is confirmed by the <strong className="text-cyan-200">AI agents</strong> (using your
          connected providers) and the <strong className="text-cyan-200">exchange volume</strong>. Confirmed signals
          auto-execute on the targets enabled below; anything not confirmed waits here for you to execute manually.
          Orders appear on <a href="/trading" className="underline text-cyan-300">/trading</a> (sandbox &amp; live).
        </p>
        <div className="flex flex-wrap gap-3 mt-3 text-[11px]">
          <span className={`px-2 py-1 rounded ${settings?.execute_sandbox ? 'bg-emerald-900/30 text-emerald-300' : 'bg-gray-800 text-gray-500'}`}>
            Sandbox auto-exec: {settings?.execute_sandbox ? 'ON' : 'OFF'}
          </span>
          <span className={`px-2 py-1 rounded ${settings?.execute_live ? 'bg-red-900/30 text-red-300' : 'bg-gray-800 text-gray-500'}`}>
            Live auto-exec: {settings?.execute_live ? 'ON (real money)' : 'OFF'}
          </span>
          <span className={`px-2 py-1 rounded ${settings?.require_ai_confirmation ? 'bg-cyan-900/30 text-cyan-300' : 'bg-gray-800 text-gray-500'}`}>
            Require AI confirmation: {settings?.require_ai_confirmation ? 'ON' : 'OFF'}
          </span>
        </div>
      </div>

      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-200">
          Signals awaiting execution ({pendingTrades.length})
        </h3>
        <button onClick={onReload} className="p-1.5 bg-gray-800 rounded hover:bg-gray-700 transition" title="Refresh">
          <RefreshCw className="w-4 h-4 text-gray-400" />
        </button>
      </div>

      {pendingTrades.length === 0 ? (
        <div className="text-center text-gray-500 text-sm py-8 bg-gray-800/20 rounded-lg border border-gray-700">
          No signals awaiting manual execution. Confirmed signals auto-execute to your enabled targets.
        </div>
      ) : (
        <div className="space-y-2">
          {pendingTrades.map((t) => {
            const dirLong = t.direction?.toLowerCase() === 'long'
            const blocked = t.ai_confirmed === false || t.volume_confirmed === false
            return (
              <div key={t.id} className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-white">{t.symbol}</span>
                      <span className={`inline-flex items-center gap-1 text-xs font-medium ${dirLong ? 'text-emerald-400' : 'text-red-400'}`}>
                        {dirLong ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                        {t.direction?.toUpperCase()}
                      </span>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${t.status === 'pending' ? 'bg-amber-900/30 text-amber-300' : 'bg-gray-700 text-gray-400'}`}>
                        {t.status}
                      </span>
                    </div>
                    <div className="text-[11px] text-gray-400 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                      <span>entry {t.sniper_entry ?? t.signal_entry ?? '—'}</span>
                      <span>SL {t.stop_loss ?? '—'}</span>
                      <span>TP {t.take_profit ?? '—'}</span>
                      {t.risk_reward != null && <span>R:R {t.risk_reward.toFixed(2)}</span>}
                      {t.channel_title && <span className="text-gray-600">{t.channel_title}</span>}
                    </div>
                    <div className="flex flex-wrap gap-3 mt-1.5">
                      <ConfBadge value={t.ai_confirmed} label="AI agents" />
                      <ConfBadge value={t.volume_confirmed} label="Volume" />
                    </div>
                    {t.reason && <div className="text-[11px] text-gray-500 mt-1 line-clamp-2">{t.reason}</div>}
                  </div>
                  <div className="flex flex-col items-end gap-1.5">
                    <div className="flex gap-1.5">
                      <button
                        disabled={busyId === t.id}
                        onClick={() => execute(t, 'sandbox', blocked)}
                        className="flex items-center gap-1 px-2.5 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 rounded text-xs font-medium transition"
                        title="Place on the simulation account"
                      >
                        <Play className="w-3 h-3" /> Sandbox
                      </button>
                      <button
                        disabled={busyId === t.id}
                        onClick={() => execute(t, 'live', blocked)}
                        className="flex items-center gap-1 px-2.5 py-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-40 rounded text-xs font-medium transition"
                        title="Place a REAL live order"
                      >
                        <Zap className="w-3 h-3" /> Live
                      </button>
                    </div>
                    {blocked && (
                      <span className="text-[10px] text-amber-400">⚠ unconfirmed — buttons force-execute</span>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

const COST_STYLES: Record<string, string> = {
  free: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
  cheap: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  paid: 'bg-purple-500/15 text-purple-300 border-purple-500/40',
}

function formatContext(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(0)}M`
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(0)}K`
  return `${tokens}`
}

function ModelInfoCard({ info, compact = false }: { info: ModelInfo; compact?: boolean }) {
  return (
    <div className={`rounded-lg border border-cyan-500/25 bg-cyan-500/[0.06] ${compact ? 'p-2.5' : 'p-3'}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-white">{info.label}</span>
        <span className={`text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded border ${COST_STYLES[info.cost] || COST_STYLES.free}`}>
          {info.cost}
        </span>
        <span className="text-[10px] text-gray-400 flex items-center gap-0.5" title={`Speed ${info.speed}/5`}>
          {Array.from({ length: 5 }).map((_, i) => (
            <span key={i} className={i < info.speed ? 'text-amber-400' : 'text-gray-600'}>▰</span>
          ))}
        </span>
      </div>
      <p className="text-xs text-cyan-100/80 mt-1">{info.best_for}</p>
      <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
        <span className="px-1.5 py-0.5 rounded bg-gray-700/60 text-gray-300">📐 {formatContext(info.context)} context</span>
        <span className="px-1.5 py-0.5 rounded bg-gray-700/60 text-gray-300">⚙ {info.params}</span>
        {info.reasoning && <span className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-300">🧠 Reasoning</span>}
        {info.vision && <span className="px-1.5 py-0.5 rounded bg-fuchsia-500/15 text-fuchsia-300">👁 Vision</span>}
        {info.json_mode && <span className="px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300">{'{ }'} JSON</span>}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {info.strengths.map((s) => (
          <span key={s} className="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-800/80 border border-gray-700 text-gray-300">{s}</span>
        ))}
      </div>
      {!compact && info.notes && <p className="text-[11px] text-gray-500 mt-2">{info.notes}</p>}
    </div>
  )
}

// ─── StrategyScanView ────────────────────────────────────────────────────────

interface MtpcTradeParams {
  entry: number; sl: number; tp1: number; tp2: number
  risk_pts: number; rr_tp1: number; rr_tp2: number; sl_valid: boolean
}
interface MtpcResult {
  symbol: string
  mtpc_state: string
  mtpc_action: string
  direction: string
  confluence: number
  confluence_ok: boolean
  min_confluence: number
  trigger?: { detected: boolean; pattern: string; rsi: number }
  factors?: Record<string, boolean>
  trade_params?: MtpcTradeParams
  reasons: string[]
  timeframes?: Record<string, { score: number; rsi: number; adx: number; direction?: string }>
  error?: string
}
interface ArAtrConfirmations {
  supertrend: boolean; adx: boolean; volume: boolean; macd_hist: boolean
}
interface ArAtrTradeParams {
  entry: number; sl: number; trail_stop_init: number; atr: number; risk_pts: number
}
interface ArAtrResult {
  symbol: string
  timeframe: string
  state: string
  action: string
  direction: string
  score: number
  confirmations: ArAtrConfirmations
  indicators?: { supertrend?: number; adx?: number; atr?: number; close?: number; vol_ratio?: number; macd_hist?: number }
  trade_params?: ArAtrTradeParams
  reasons: string[]
  error?: string
}
interface ScanRow {
  symbol: string
  busy: boolean
  result?: MtpcResult | ArAtrResult
  error?: string
}

function StrategyScanView({ signals }: { signals: ParsedSignal[] }) {
  const [strategy, setStrategy] = useState<'mtpc' | 'ar-atr'>('ar-atr')
  const [timeframe, setTimeframe] = useState('1h')
  const [adxThreshold, setAdxThreshold] = useState(25)
  const [rows, setRows] = useState<ScanRow[]>([])
  const [scanning, setScanning] = useState(false)

  // Unique symbols from signals the user has (active first)
  const symbols = useMemo(() => {
    const active = signals.filter((s) => String(s.status).toLowerCase() === 'active').map((s) => s.symbol)
    const all = signals.map((s) => s.symbol)
    return Array.from(new Set([...active, ...all])).slice(0, 30)
  }, [signals])

  const runOne = async (symbol: string): Promise<ScanRow> => {
    try {
      // Always use slug format (no slash) — both backends normalize DOGEUSDT → DOGE/USDT
      const slug = symbol.replace('/', '')
      if (strategy === 'mtpc') {
        const res = await apiClient.runMtpc(slug)
        return { symbol, busy: false, result: res.data as MtpcResult }
      } else {
        const res = await apiClient.runArAtr(slug, { timeframe, adx_threshold: adxThreshold })
        return { symbol, busy: false, result: res.data as ArAtrResult }
      }
    } catch (e: unknown) {
      const msg = typeof e === 'object' && e && 'response' in e
        ? ((e as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? 'Error')
        : 'Error'
      return { symbol, busy: false, error: msg }
    }
  }

  const scanAll = async () => {
    if (symbols.length === 0) return
    setScanning(true)
    setRows(symbols.map((s) => ({ symbol: s, busy: true })))
    const results = await Promise.all(symbols.map(runOne))
    setRows(results)
    setScanning(false)
  }

  const stateBadge = (state: string, action: string) => {
    if (state === 'signal') return <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">{action.toUpperCase()}</span>
    if (state === 'setup_only' || state === 'watch') return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">WATCH</span>
    if (state === 'blocked') return <span className="px-2 py-0.5 rounded text-[11px] text-gray-400 border border-gray-700">BLOCKED</span>
    return <span className="px-2 py-0.5 rounded text-[11px] text-gray-500 border border-gray-800">–</span>
  }

  const dirBadge = (dir: string) =>
    dir === 'bull'
      ? <span className="inline-flex items-center gap-1 text-emerald-400 text-xs"><TrendingUp className="w-3 h-3" /> Bull</span>
      : dir === 'bear'
        ? <span className="inline-flex items-center gap-1 text-red-400 text-xs"><TrendingDown className="w-3 h-3" /> Bear</span>
        : <span className="text-gray-500 text-xs">–</span>

  const isMtpc = (r: MtpcResult | ArAtrResult): r is MtpcResult => 'mtpc_state' in r
  const isSignal = (r: MtpcResult | ArAtrResult) =>
    isMtpc(r) ? r.mtpc_state === 'signal' : r.state === 'signal'
  const isWatch = (r: MtpcResult | ArAtrResult) =>
    isMtpc(r) ? r.mtpc_state === 'setup_only' : r.state === 'watch'

  // Sort: signals first, then watch, then others
  const sortedRows = useMemo(() => {
    return [...rows].sort((a, b) => {
      if (a.busy !== b.busy) return a.busy ? 1 : -1
      if (!a.result && !b.result) return 0
      if (!a.result) return 1
      if (!b.result) return -1
      const aS = isSignal(a.result) ? 2 : isWatch(a.result) ? 1 : 0
      const bS = isSignal(b.result) ? 2 : isWatch(b.result) ? 1 : 0
      return bS - aS
    })
  }, [rows])

  const signalCount = rows.filter((r) => r.result && isSignal(r.result)).length
  const watchCount  = rows.filter((r) => r.result && isWatch(r.result)).length

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="bg-gradient-to-br from-cyan-500/10 to-transparent border border-cyan-500/30 rounded-lg p-5">
        <h2 className="font-semibold text-white flex items-center gap-2">
          <Crosshair className="w-5 h-5 text-cyan-400" />
          Strategy Scan
        </h2>
        <p className="text-xs text-gray-400 mt-1 max-w-2xl">
          Run your custom strategies against the symbols from active Telegram signals.
          <span className="text-cyan-300"> MTPC</span> — Multi-Timeframe Pullback Confluence (4H→1H→15M, Fibonacci + S/R).
          <span className="text-purple-300"> AR-ATR</span> — Trend Multi-Confirmation (SuperTrend + ADX + Volume + MACD, single timeframe).
        </p>
      </div>

      {/* Controls */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
        <div className="flex flex-wrap items-end gap-4">
          {/* Strategy picker */}
          <div>
            <label className="block text-xs text-gray-400 mb-1">Strategy</label>
            <div className="flex rounded overflow-hidden border border-gray-700">
              <button
                onClick={() => setStrategy('mtpc')}
                className={`px-4 py-1.5 text-xs font-medium transition-colors ${strategy === 'mtpc' ? 'bg-cyan-600 text-white' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}`}
              >MTPC</button>
              <button
                onClick={() => setStrategy('ar-atr')}
                className={`px-4 py-1.5 text-xs font-medium transition-colors ${strategy === 'ar-atr' ? 'bg-purple-600 text-white' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}`}
              >AR-ATR</button>
            </div>
          </div>

          {/* AR-ATR options */}
          {strategy === 'ar-atr' && (
            <>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Timeframe</label>
                <select
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value)}
                  className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
                >
                  {['5m','15m','30m','1h','4h','1d'].map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">ADX threshold</label>
                <input
                  type="number" min={15} max={50} step={5}
                  value={adxThreshold}
                  onChange={(e) => setAdxThreshold(Number(e.target.value))}
                  className="w-20 rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
                />
              </div>
            </>
          )}

          {/* Symbols count */}
          <div className="text-xs text-gray-500">
            {symbols.length} symbol{symbols.length !== 1 ? 's' : ''} from signals
          </div>

          {/* Scan button */}
          <button
            onClick={scanAll}
            disabled={scanning || symbols.length === 0}
            className="ml-auto px-5 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-medium disabled:opacity-60 flex items-center gap-2"
          >
            <RefreshCw className={`w-4 h-4 ${scanning ? 'animate-spin' : ''}`} />
            {scanning ? 'Scanning…' : 'Scan All'}
          </button>
        </div>

        {/* Symbol chips */}
        {symbols.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {symbols.map((s) => (
              <span key={s} className="px-2 py-0.5 rounded-full bg-gray-800 border border-gray-700 text-xs text-gray-300">{s}</span>
            ))}
          </div>
        )}
        {symbols.length === 0 && (
          <p className="mt-3 text-xs text-gray-500">No active signals yet — add Telegram channels and let the monitor run to populate symbols.</p>
        )}
      </div>

      {/* Results */}
      {sortedRows.length > 0 && (
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg overflow-hidden">
          {/* Summary bar */}
          <div className="px-4 py-2 border-b border-gray-700 flex items-center gap-4 text-xs">
            <span className="text-gray-400">{sortedRows.length} scanned</span>
            {signalCount > 0 && <span className="text-emerald-400 font-semibold">{signalCount} signal{signalCount > 1 ? 's' : ''}</span>}
            {watchCount  > 0 && <span className="text-amber-400">{watchCount} watch</span>}
          </div>

          <div className="divide-y divide-gray-800">
            {sortedRows.map((row) => (
              <div key={row.symbol} className={`px-4 py-3 ${row.result && isSignal(row.result) ? 'bg-emerald-500/5' : ''}`}>
                {row.busy ? (
                  <div className="flex items-center gap-3">
                    <div className="w-20 h-4 bg-gray-800 rounded animate-pulse" />
                    <div className="w-16 h-4 bg-gray-800 rounded animate-pulse" />
                    <div className="w-24 h-4 bg-gray-800 rounded animate-pulse" />
                  </div>
                ) : row.error ? (
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-sm text-white w-24">{row.symbol}</span>
                    <span className="text-xs text-red-400">{row.error}</span>
                  </div>
                ) : row.result && isMtpc(row.result) ? (
                  /* ── MTPC row ── */
                  <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
                    <span className="font-mono text-sm text-white w-24 shrink-0">{row.symbol}</span>
                    <div className="flex items-center gap-2 shrink-0">
                      {stateBadge(row.result.mtpc_state, row.result.mtpc_action)}
                      {dirBadge(row.result.direction)}
                    </div>
                    {/* Confluence */}
                    <div className="flex items-center gap-1 shrink-0">
                      {[...Array(5)].map((_, i) => (
                        <div key={i} className={`w-2 h-2 rounded-full ${i < (row.result as MtpcResult).confluence ? 'bg-cyan-400' : 'bg-gray-700'}`} />
                      ))}
                      <span className="text-xs text-gray-400 ml-1">{(row.result as MtpcResult).confluence}/5</span>
                    </div>
                    {/* Timeframe scores */}
                    {row.result.timeframes && (
                      <div className="flex gap-2 text-[11px] text-gray-400">
                        {Object.entries(row.result.timeframes).map(([tf, d]) => (
                          <span key={tf}>
                            {tf}: <span className={d.score > 0 ? 'text-emerald-400' : d.score < 0 ? 'text-red-400' : 'text-gray-400'}>{d.score > 0 ? '+' : ''}{d.score.toFixed(2)}</span>
                            {' '}RSI {d.rsi?.toFixed(0)}
                          </span>
                        ))}
                      </div>
                    )}
                    {/* Trade params */}
                    {row.result.trade_params && (
                      <div className="flex gap-3 text-[11px] ml-auto text-right">
                        <span className="text-gray-400">Entry <span className="text-white font-mono">{row.result.trade_params.entry.toFixed(4)}</span></span>
                        <span className="text-gray-400">SL <span className="text-red-400 font-mono">{row.result.trade_params.sl.toFixed(4)}</span></span>
                        <span className="text-gray-400">TP1 <span className="text-emerald-400 font-mono">{row.result.trade_params.tp1.toFixed(4)}</span></span>
                        <span className="text-gray-400">TP2 <span className="text-emerald-300 font-mono">{row.result.trade_params.tp2.toFixed(4)}</span></span>
                        <span className="text-gray-500">{row.result.trade_params.rr_tp2}R</span>
                      </div>
                    )}
                    {/* Top reason */}
                    <div className="w-full text-[11px] text-gray-500 truncate">{row.result.reasons[0]}</div>
                  </div>
                ) : row.result && !isMtpc(row.result) ? (
                  /* ── AR-ATR row ── */
                  <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
                    <span className="font-mono text-sm text-white w-24 shrink-0">{row.symbol}</span>
                    <div className="flex items-center gap-2 shrink-0">
                      {stateBadge(row.result.state, row.result.action)}
                      {dirBadge(row.result.direction)}
                    </div>
                    {/* 4-confirmation dots */}
                    <div className="flex items-center gap-2 shrink-0">
                      {Object.entries((row.result as ArAtrResult).confirmations).map(([k, v]) => (
                        <span key={k} className={`text-[10px] px-1.5 py-0.5 rounded ${v ? 'bg-emerald-500/15 text-emerald-300' : 'bg-gray-800 text-gray-600'}`}>
                          {k === 'supertrend' ? 'ST' : k === 'adx' ? 'ADX' : k === 'volume' ? 'VOL' : 'MACD'}
                        </span>
                      ))}
                      <span className="text-xs text-gray-400">{(row.result as ArAtrResult).score}/4</span>
                    </div>
                    {/* Indicators */}
                    {row.result.indicators && (
                      <div className="flex gap-3 text-[11px] text-gray-400">
                        <span>ADX <span className="text-white">{row.result.indicators.adx?.toFixed(1)}</span></span>
                        <span>ATR <span className="text-white">{row.result.indicators.atr?.toFixed(5)}</span></span>
                        <span>MACD <span className={row.result.indicators.macd_hist && row.result.indicators.macd_hist > 0 ? 'text-emerald-400' : 'text-red-400'}>{row.result.indicators.macd_hist?.toFixed(6)}</span></span>
                      </div>
                    )}
                    {/* Trade params */}
                    {row.result.trade_params && (
                      <div className="flex gap-3 text-[11px] ml-auto text-right">
                        <span className="text-gray-400">Entry <span className="text-white font-mono">{row.result.trade_params.entry.toFixed(4)}</span></span>
                        <span className="text-gray-400">SL <span className="text-red-400 font-mono">{row.result.trade_params.sl.toFixed(4)}</span></span>
                        <span className="text-gray-400">Trail <span className="text-amber-400 font-mono">{row.result.trade_params.trail_stop_init.toFixed(4)}</span></span>
                      </div>
                    )}
                    {/* Top reason */}
                    <div className="w-full text-[11px] text-gray-500 truncate">{row.result.reasons[0]}</div>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty state */}
      {!scanning && sortedRows.length === 0 && symbols.length > 0 && (
        <div className="text-center py-10 text-gray-500 text-sm">
          Click <strong className="text-gray-300">Scan All</strong> to run the strategy on {symbols.length} symbols.
        </div>
      )}
    </div>
  )
}

function ConnectAIView(props: {
  presets: AIPreset[]
  providers: AIProvider[]
  onReload: () => void
  onMessage: (m: string | null) => void
  onError: (e: string | null) => void
}) {
  const { presets, providers, onReload, onMessage, onError } = props
  const [selectedKey, setSelectedKey] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [testingAll, setTestingAll] = useState(false)
  const [usage, setUsage] = useState<any>(null)

  const loadUsage = useCallback(async () => {
    try {
      const res = await apiClient.aiAnalyst.getAiUsage()
      setUsage(res.data)
    } catch {
      /* best-effort */
    }
  }, [])

  useEffect(() => {
    loadUsage()
    const t = setInterval(loadUsage, 20000)
    return () => clearInterval(t)
  }, [loadUsage, providers.length])

  const selectedPreset = presets.find((p) => p.key === selectedKey)

  const handleSelectPreset = (key: string) => {
    setSelectedKey(key)
    const preset = presets.find((p) => p.key === key)
    setSelectedModel(preset?.default_model || '')
    setBaseUrl(preset?.base_url || '')
  }

  const handleAdd = async () => {
    if (!selectedKey || !apiKey.trim()) {
      onError('Pick a provider and paste your API key.')
      return
    }
    if (selectedPreset?.editable_endpoint && !baseUrl.trim()) {
      onError('Enter the Base URL for this endpoint (e.g. http://localhost:3002/v1).')
      return
    }
    if (selectedPreset?.editable_endpoint && !selectedModel.trim()) {
      onError('Enter the model name for this endpoint.')
      return
    }
    setBusy(true)
    onError(null)
    onMessage(null)
    try {
      const res = await apiClient.aiAnalyst.addProvider({
        provider_key: selectedKey,
        api_key: apiKey.trim(),
        default_model: selectedModel || undefined,
        base_url: selectedPreset?.editable_endpoint ? baseUrl.trim() : undefined,
      })
      const p = res.data as AIProvider
      onMessage(
        p.status === 'ok'
          ? `${p.label} connected and verified ✓`
          : `${p.label} added but test failed: ${p.last_error || 'unknown error'}`
      )
      setApiKey('')
      setSelectedKey('')
      setSelectedModel('')
      setBaseUrl('')
      await onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const handleTest = async (id: number) => {
    setTestingId(id)
    onError(null)
    try {
      const res = await apiClient.aiAnalyst.testProvider(id)
      const r = res.data as { ok: boolean; error?: string; reply?: string }
      onMessage(r.ok ? `Provider OK (reply: ${r.reply || 'OK'})` : `Test failed: ${r.error}`)
      await onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    } finally {
      setTestingId(null)
    }
  }

  const handleTestAll = async () => {
    setTestingAll(true)
    onError(null)
    onMessage(null)
    try {
      const res = await apiClient.aiAnalyst.testAllProviders()
      const r = res.data as { tested: number; ok_count: number }
      onMessage(`Tested ${r.tested} provider${r.tested === 1 ? '' : 's'} — ${r.ok_count} OK, ${r.tested - r.ok_count} failed`)
      await onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    } finally {
      setTestingAll(false)
    }
  }

  const handleChangeModel = async (p: AIProvider, model: string) => {
    try {
      await apiClient.aiAnalyst.updateProvider(p.id, { default_model: model })
      onMessage(`${p.label} model set to ${model}`)
      await onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    }
  }

  const handleToggle = async (p: AIProvider) => {
    try {
      await apiClient.aiAnalyst.updateProvider(p.id, { enabled: !p.enabled })
      await onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await apiClient.aiAnalyst.deleteProvider(id)
      await onReload()
    } catch (e: unknown) {
      onError(toErrorMessage(e))
    }
  }

  return (
    <div className="space-y-4">
      {/* Instructions */}
      <div className="bg-gradient-to-br from-cyan-500/10 to-transparent border border-cyan-500/30 rounded-lg p-5">
        <div className="flex items-start justify-between gap-3">
          <h2 className="font-semibold text-white flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-cyan-400" />
            Connect AI Providers
          </h2>
          {/* Headroom proxy badge — always active */}
          <a
            href="http://127.0.0.1:8787/dashboard"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-[11px] font-mono whitespace-nowrap hover:bg-emerald-500/20 transition-colors"
            title="Headroom context-compression proxy is active — all AI calls are compressed before reaching the provider. Click to open the savings dashboard."
          >
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            headroom proxy active
          </a>
        </div>
        <p className="text-xs text-gray-300 mt-2 max-w-3xl">
          Add one or more AI accounts. When enabled, they power the whole app — <strong className="text-cyan-200">Telegram
          sniper entries</strong>, signal generation, trade decisions, and your AI agents — with automatic
          failover between providers. Add several <strong className="text-emerald-300">free-tier</strong> accounts and the
          load is spread across them so your free tokens last for a month.
        </p>
        <ol className="text-xs text-gray-400 mt-3 space-y-1 list-decimal list-inside">
          <li>Pick a provider below and open its key page (most are free to sign up).</li>
          <li>Create an API key, paste it here, and click <strong className="text-gray-200">Add &amp; Test</strong>.</li>
          <li>Green ✓ means it's verified and live across the app. Add more for resilience.</li>
          <li>Every call is automatically compressed by the <strong className="text-emerald-300">headroom</strong> proxy — saving 60-95% tokens before they hit your provider's quota.</li>
        </ol>
      </div>

      {/* Overall token usage + remaining (free-tier guard) */}
      {usage?.totals && (
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-semibold text-white flex items-center gap-2">
              <Sparkles className="w-4 h-4 text-emerald-400" />
              Token Usage &amp; Remaining (this month)
            </h3>
            <span className="text-[11px] text-gray-500">Agents + sniper + signals share these accounts</span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <div className="bg-gray-900/50 rounded-lg p-3">
              <div className="text-[11px] text-gray-400">Monthly calls used</div>
              <div className="text-lg font-bold text-white">
                {usage.totals.monthly_calls?.toLocaleString() ?? 0}
                {usage.totals.monthly_limit != null && (
                  <span className="text-xs text-gray-500"> / {usage.totals.monthly_limit.toLocaleString()}</span>
                )}
              </div>
            </div>
            <div className="bg-gray-900/50 rounded-lg p-3">
              <div className="text-[11px] text-gray-400">Monthly remaining</div>
              <div className="text-lg font-bold text-emerald-400">
                {usage.totals.monthly_remaining != null ? usage.totals.monthly_remaining.toLocaleString() : '∞'}
              </div>
            </div>
            <div className="bg-gray-900/50 rounded-lg p-3">
              <div className="text-[11px] text-gray-400">Tokens this month</div>
              <div className="text-lg font-bold text-cyan-300">{(usage.totals.month_tokens ?? 0).toLocaleString()}</div>
            </div>
            <div className="bg-gray-900/50 rounded-lg p-3">
              <div className="text-[11px] text-gray-400">Tokens today</div>
              <div className="text-lg font-bold text-cyan-300">{(usage.totals.today_tokens ?? 0).toLocaleString()}</div>
            </div>
          </div>
          {Array.isArray(usage.providers) && usage.providers.length > 0 && (
            <div className="space-y-2">
              {usage.providers.map((p: any) => {
                const used = p.monthly_calls || 0
                const limit = p.monthly_limit
                const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0
                const bar = pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-amber-500' : 'bg-emerald-500'
                return (
                  <div key={p.id}>
                    <div className="flex justify-between text-[11px] text-gray-400 mb-0.5">
                      <span className="flex items-center gap-1.5">
                        <span className={`w-1.5 h-1.5 rounded-full ${p.enabled ? 'bg-emerald-400' : 'bg-gray-600'}`} />
                        {p.label}
                        <span className="text-gray-600">· {(p.month_tokens ?? 0).toLocaleString()} tok</span>
                      </span>
                      <span>
                        {used.toLocaleString()}
                        {limit != null ? ` / ${limit.toLocaleString()}` : ' calls'}
                        {p.monthly_remaining != null && (
                          <span className="text-emerald-400"> · {p.monthly_remaining.toLocaleString()} left</span>
                        )}
                      </span>
                    </div>
                    {limit != null && (
                      <div className="h-1.5 bg-gray-900 rounded-full overflow-hidden">
                        <div className={`h-full ${bar} transition-all`} style={{ width: `${pct}%` }} />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* Add provider */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Provider</label>
            <select
              value={selectedKey}
              onChange={(e) => handleSelectPreset(e.target.value)}
              className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
            >
              <option value="">Select a provider…</option>
              {presets.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label} {p.free_tier ? '(free tier)' : '(paid)'}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Model {selectedPreset?.free_tier && <span className="text-emerald-400">(free)</span>}</label>
            {selectedPreset?.editable_endpoint ? (
              <input
                type="text"
                placeholder={selectedPreset.key === 'freellmapi' ? 'auto' : 'model id (e.g. llama3.1)'}
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
            ) : (
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                disabled={!selectedPreset}
                className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white disabled:opacity-50"
              >
                {!selectedPreset && <option value="">Select a provider first…</option>}
                {selectedPreset?.models.map((m) => {
                  const mi = selectedPreset.model_info[m]
                  return (
                    <option key={m} value={m}>
                      {mi ? `${mi.label} · ${formatContext(mi.context)} · ${mi.cost}` : m}
                    </option>
                  )
                })}
              </select>
            )}
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">API Key</label>
            <div className="flex gap-2">
              <input
                type="password"
                placeholder="Paste your API key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="flex-1 rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
              <button
                onClick={handleAdd}
                disabled={busy || !selectedKey || !apiKey.trim()}
                className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60 whitespace-nowrap"
              >
                {busy ? 'Testing…' : 'Add & Test'}
              </button>
            </div>
          </div>
        </div>
        {selectedPreset?.editable_endpoint && (
          <div className="mt-3">
            <label className="block text-xs text-gray-400 mb-1">Base URL</label>
            <input
              type="text"
              placeholder="http://localhost:3002/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white placeholder:text-gray-500 font-mono"
            />
            {selectedPreset.key === 'freellmapi' && (
              <p className="mt-1.5 text-[11px] text-gray-500">
                Run FreeLLMAPI yourself on a non-conflicting port (e.g. <span className="font-mono text-gray-400">PORT=3002 docker compose up -d</span>), add your
                provider keys in its dashboard, then paste its unified <span className="font-mono text-gray-400">freellmapi-…</span> key
                above with model <span className="font-mono text-gray-400">auto</span>.
              </p>
            )}
          </div>
        )}
        {selectedPreset && (
          <div className="mt-3 text-xs text-gray-400 flex flex-wrap items-center gap-x-4 gap-y-1">
            <span>
              Free-tier cap:{' '}
              <span className="text-gray-200">
                {selectedPreset.daily_limit ? `${selectedPreset.daily_limit}/day` : 'unlimited'}
                {selectedPreset.monthly_limit ? ` · ${selectedPreset.monthly_limit}/mo` : ''}
              </span>
            </span>
            <a href={selectedPreset.signup_url} target="_blank" rel="noopener noreferrer" className="text-cyan-400 underline">
              Get a free API key →
            </a>
            <span className="text-gray-500">{selectedPreset.notes}</span>
          </div>
        )}
        {/* Selected model capabilities */}
        {selectedPreset && selectedModel && selectedPreset.model_info[selectedModel] && (
          <div className="mt-3">
            <ModelInfoCard info={selectedPreset.model_info[selectedModel]} />
          </div>
        )}
      </div>

      {/* Configured providers */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-white text-sm">Configured AI Accounts</h3>
          {providers.length > 0 && (
            <button
              onClick={handleTestAll}
              disabled={testingAll}
              className="px-3 py-1.5 rounded text-xs bg-cyan-600/80 hover:bg-cyan-500 text-white disabled:opacity-60"
            >
              {testingAll ? 'Testing all…' : 'Test All Keys'}
            </button>
          )}
        </div>
        {providers.length === 0 ? (
          <div className="text-sm text-gray-400">No AI providers yet. Add one above to power AI features.</div>
        ) : (
          <div className="space-y-2">
            {providers.map((p) => {
              const dayPct = p.daily_limit ? Math.min(100, Math.round(((p.daily_calls || 0) / p.daily_limit) * 100)) : 0
              const moPct = p.monthly_limit ? Math.min(100, Math.round(((p.monthly_calls || 0) / p.monthly_limit) * 100)) : 0
              const barColor = (pct: number) => (pct >= 100 ? 'bg-red-500' : pct >= 80 ? 'bg-amber-500' : 'bg-emerald-500')
              return (
              <div key={p.id} className="rounded-lg border border-gray-700/70 bg-gray-900/40 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-white">{p.label}</span>
                    {p.free_tier && <span className="text-[10px] text-emerald-300 bg-emerald-500/10 px-1.5 py-0.5 rounded">free</span>}
                    <span className={`text-[10px] uppercase font-semibold px-2 py-0.5 rounded border ${AI_STATUS_STYLES[p.status] || AI_STATUS_STYLES.unknown}`}>
                      {p.status}
                    </span>
                    <span className="text-xs text-gray-500">prio {p.priority}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => handleTest(p.id)}
                      disabled={testingId === p.id}
                      className="px-2.5 py-1 rounded text-xs bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-60"
                    >
                      {testingId === p.id ? 'Testing…' : 'Test'}
                    </button>
                    <button
                      onClick={() => handleToggle(p)}
                      className={`px-2.5 py-1 rounded text-xs ${p.enabled ? 'bg-emerald-500/20 text-emerald-300' : 'bg-gray-700 text-gray-300'}`}
                    >
                      {p.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                    <button
                      onClick={() => handleDelete(p.id)}
                      className="px-2.5 py-1 rounded text-xs bg-red-500/20 text-red-300 hover:bg-red-500/30"
                    >
                      Delete
                    </button>
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-gray-500">
                  <label className="flex items-center gap-1">
                    <span>Model:</span>
                    {p.models && p.models.length > 0 ? (
                      <select
                        value={p.default_model || ''}
                        onChange={(e) => handleChangeModel(p, e.target.value)}
                        className="rounded bg-gray-900 border border-gray-700 px-2 py-0.5 text-xs text-gray-200"
                      >
                        {p.models.map((m) => {
                          const mi = p.model_info[m]
                          return (
                            <option key={m} value={m}>{mi ? `${mi.label} · ${formatContext(mi.context)} · ${mi.cost}` : m}</option>
                          )
                        })}
                      </select>
                    ) : (
                      <span className="text-gray-300">{p.default_model}</span>
                    )}
                  </label>
                  <span>Errors: <span className="text-gray-300">{p.total_errors}</span></span>
                  {p.last_error && <span className="text-red-300/80 truncate max-w-[300px]" title={p.last_error}>⚠ {p.last_error}</span>}
                </div>
                {/* Current model capabilities */}
                {p.default_model && p.model_info[p.default_model] && (
                  <div className="mt-2">
                    <ModelInfoCard info={p.model_info[p.default_model]} compact />
                  </div>
                )}
                {/* Usage bars — never exhaust the free tier */}
                <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <div className="flex justify-between text-[11px] text-gray-400 mb-0.5">
                      <span>Today</span>
                      <span className={dayPct >= 100 ? 'text-red-300' : 'text-gray-300'}>
                        {p.daily_calls || 0}{p.daily_limit ? ` / ${p.daily_limit}` : ' (∞)'}
                      </span>
                    </div>
                    <div className="h-1.5 rounded bg-gray-700/60 overflow-hidden">
                      <div className={`h-full ${barColor(dayPct)}`} style={{ width: `${p.daily_limit ? dayPct : 0}%` }} />
                    </div>
                  </div>
                  <div>
                    <div className="flex justify-between text-[11px] text-gray-400 mb-0.5">
                      <span>This month</span>
                      <span className={moPct >= 100 ? 'text-red-300' : 'text-gray-300'}>
                        {p.monthly_calls || 0}{p.monthly_limit ? ` / ${p.monthly_limit}` : ' (∞)'}
                      </span>
                    </div>
                    <div className="h-1.5 rounded bg-gray-700/60 overflow-hidden">
                      <div className={`h-full ${barColor(moPct)}`} style={{ width: `${p.monthly_limit ? moPct : 0}%` }} />
                    </div>
                  </div>
                </div>
              </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
// ── Utility: format a Forex symbol for display (EURUSD → EUR/USD) ─────────────
function fmtForexPair(sym: string): string {
  const s = sym.toUpperCase()
  if (s.length === 6 && !s.includes('/')) return `${s.slice(0, 3)}/${s.slice(3)}`
  return s
}

// Pip value: for JPY pairs, 1 pip = 0.01; for most others 0.0001; metals vary
function pipSize(sym: string): number {
  const s = sym.toUpperCase()
  if (s.includes('JPY')) return 0.01
  if (s.startsWith('XAU') || s.startsWith('XAUUSD')) return 0.1
  if (s.startsWith('XAG')) return 0.001
  return 0.0001
}

function toPips(diff: number, sym: string): string {
  const pip = pipSize(sym)
  return Math.abs(diff / pip).toFixed(1)
}

// ── ForexSignalsView component ───────────────────────────────────────────────
// ── TrailingSlView ─────────────────────────────────────────────────────────
function TrailingSlView(props: {
  cryptoSignals: ParsedSignal[]
  forexSignals: ParsedSignal[]
  cryptoPrices: Record<string, number | null>
  forexPrices: Record<string, number | null>
  onRefresh: () => void
  onClose: (signalId: number, symbol: string, kind: 'close' | 'opposite') => Promise<void>
}) {
  const { cryptoSignals, forexSignals, cryptoPrices, forexPrices, onRefresh, onClose } = props

  // Combine and filter to signals that have a valid profit-locked trailing SL
  const trailingSignals: Array<ParsedSignal & { isForex: boolean; cur: number | null }> = [
    ...cryptoSignals.map((s) => ({ ...s, isForex: false, cur: cryptoPrices[s.symbol] ?? null })),
    ...forexSignals.map((s) => ({ ...s, isForex: true, cur: forexPrices[s.symbol] ?? null })),
  ].filter((s) => {
    if (s.trailing_sl == null || s.tp_reached_count === 0) return false
    const isLong = s.direction?.toLowerCase() === 'long'
    if (s.entry == null) return false
    // Must be on profit side of entry
    return isLong ? s.trailing_sl >= s.entry * 0.999 : s.trailing_sl <= s.entry * 1.001
  })

  return (
    <div className="space-y-4">
      <div className="bg-gray-800/30 border border-emerald-700/30 rounded-lg p-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="text-xs text-emerald-400 font-semibold uppercase tracking-wider">🔒 Trailing Stop-Loss</span>
          <span className="text-xs text-gray-400">
            {trailingSignals.length} position{trailingSignals.length !== 1 ? 's' : ''} with locked profit
          </span>
        </div>
        <button
          onClick={onRefresh}
          className="flex items-center gap-1 px-3 py-1.5 bg-emerald-700/30 hover:bg-emerald-700/50 text-emerald-200 rounded text-xs transition"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Refresh
        </button>
      </div>

      {trailingSignals.length === 0 ? (
        <div className="bg-gray-800/30 border border-emerald-700/20 rounded-lg p-8 text-center text-sm text-gray-400">
          No positions with active trailing SL yet.<br />
          <span className="text-xs text-gray-500 mt-1 block">
            Trailing SL activates once a signal hits its first TP target above entry.
          </span>
        </div>
      ) : (
        <div className="space-y-2">
          {trailingSignals.map((s) => {
            const isLong = s.direction?.toLowerCase() === 'long'
            const cur = s.cur
            const pip = s.isForex ? pipSize(s.symbol) : null
            const trailSl = s.trailing_sl!
            // Distance from current price to trailing SL
            const distToTrail = cur != null ? Math.abs(cur - trailSl) : null
            const distPct = cur != null ? (Math.abs(cur - trailSl) / cur) * 100 : null
            const trailHit = cur != null
              ? (isLong ? cur <= trailSl : cur >= trailSl)
              : false
            // Distance from entry to trailing SL (locked profit)
            const lockedProfit = s.entry != null
              ? Math.abs(trailSl - s.entry)
              : null
            const lockedPct = s.entry != null && s.entry > 0
              ? (lockedProfit! / s.entry) * 100
              : null

            return (
              <div
                key={s.id}
                className={`rounded-lg border bg-gray-900/40 p-4 transition ${
                  trailHit
                    ? 'border-red-500/60 bg-red-900/10'
                    : 'border-emerald-500/30'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-bold ${isLong ? 'bg-emerald-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'}`}>
                      {isLong ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                      {isLong ? (s.isForex ? 'BUY' : 'LONG') : (s.isForex ? 'SELL' : 'SHORT')}
                    </span>
                    <span className="text-base font-bold text-white">
                      {s.isForex ? fmtForexPair(s.symbol) : s.symbol}
                    </span>
                    {s.isForex && <span className="text-[10px] text-amber-400 bg-amber-500/10 px-1.5 py-0.5 rounded">FOREX</span>}
                    <span className="text-emerald-400 text-sm font-semibold">🔒 TP{s.tp_reached_count} locked</span>
                    {trailHit && <span className="text-red-300 text-xs font-semibold animate-pulse">⚠ Trail SL reached!</span>}
                  </div>
                </div>

                <div className="mt-3 grid grid-cols-4 gap-2 text-xs">
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Entry</div>
                    <div className="text-white font-medium">{s.entry}</div>
                  </div>
                  <div className="rounded bg-emerald-900/20 border border-emerald-500/40 p-2">
                    <div className="text-emerald-400 text-[11px] font-semibold">🔒 Trailing SL</div>
                    <div className="text-emerald-300 font-semibold">{s.isForex ? Number(trailSl).toPrecision(6) : trailSl}</div>
                    {lockedPct != null && (
                      <div className="text-[10px] text-emerald-500">+{lockedPct.toFixed(2)}% locked</div>
                    )}
                  </div>
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Live price</div>
                    <div className={`font-medium ${cur == null ? 'text-gray-500' : trailHit ? 'text-red-300' : 'text-white'}`}>
                      {cur != null ? (s.isForex ? cur.toPrecision(6) : cur) : '…'}
                    </div>
                    {distToTrail != null && (
                      <div className={`text-[10px] mt-0.5 ${trailHit ? 'text-red-400' : 'text-gray-500'}`}>
                        {s.isForex && pip ? `${(distToTrail / pip).toFixed(1)} pips to trail` : `${distPct?.toFixed(2)}% to trail`}
                      </div>
                    )}
                  </div>
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Original SL</div>
                    <div className="text-red-400 font-medium">{s.stop_loss ?? s.stop_loss_raw ?? '—'}</div>
                  </div>
                </div>

                {/* TPs — show only valid ones */}
                {s.take_profits && s.take_profits.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {s.take_profits.map((tp, i) => {
                      const isValidTp = s.entry != null
                        ? (isLong ? tp > s.entry * 0.999 : tp < s.entry * 1.001)
                        : true
                      const locked = isValidTp && i < (s.tp_reached_count ?? 0)
                      if (!isValidTp) return null
                      return (
                        <span
                          key={i}
                          className={`inline-flex items-center gap-0.5 text-[11px] rounded px-1.5 py-0.5 border font-medium ${
                            locked
                              ? 'bg-emerald-500/30 text-emerald-200 border-emerald-400/60'
                              : 'bg-gray-700/30 text-gray-400 border-gray-700/50'
                          }`}
                        >
                          {locked ? '🔒' : ''} TP{i + 1}: {tp}
                        </span>
                      )
                    })}
                  </div>
                )}

                {/* Close button */}
                <div className="mt-3 flex justify-end">
                  <button
                    onClick={() => onClose(s.id, s.symbol, 'close')}
                    className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded text-xs transition"
                    title="Manually close this position"
                  >
                    ✕ Close position
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ForexSignalsView(props: {
  signals: ParsedSignal[]
  loading: boolean
  statusFilter: SignalStatus | ''
  onStatusFilter: (s: SignalStatus | '') => void
  onRefresh: () => void
  prices: Record<string, number | null>
}) {
  const { signals, loading, statusFilter, onStatusFilter, onRefresh, prices } = props

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="bg-gray-800/30 border border-amber-700/30 rounded-lg p-4 flex flex-wrap items-center gap-3">
        <span className="text-xs text-amber-400 font-semibold uppercase tracking-wider">Forex</span>
        <div className="flex items-center gap-1 flex-wrap">
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value || 'all'}
              onClick={() => onStatusFilter(opt.value)}
              className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                statusFilter === opt.value
                  ? 'bg-amber-600 text-white'
                  : 'bg-gray-900 text-gray-400 hover:text-white border border-gray-700'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="ml-auto flex items-center gap-1 px-3 py-1.5 bg-amber-700/30 hover:bg-amber-700/50 text-amber-200 rounded text-xs transition disabled:opacity-40"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {loading && signals.length === 0 ? (
        <div className="text-sm text-gray-400">Loading Forex signals…</div>
      ) : signals.length === 0 ? (
        <div className="bg-gray-800/30 border border-amber-700/30 rounded-lg p-8 text-center text-sm text-gray-400">
          <p>No Forex signals yet.</p>
          <p className="mt-2 text-xs text-gray-500">Add the <strong>Elevating Forex | GijsFX</strong> channel in the Raw Messages tab with <code className="bg-gray-900 px-1 rounded">source_kind=signals</code> and <code className="bg-gray-900 px-1 rounded">market_type=forex</code>.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {signals.map((s) => {
            const isLong = s.direction?.toLowerCase() === 'long'
            const statusKey = String(s.status).toLowerCase()
            const cur = statusKey === 'active' ? (prices[s.symbol] ?? null) : null
            const pip = pipSize(s.symbol)
            // A TP is only valid if it's on the profit side of the entry price.
            const isValidTp = (tp: number) =>
              s.entry != null
                ? (isLong ? tp > s.entry * 0.999 : tp < s.entry * 1.001)
                : true
            // Trailing SL must be on the PROFIT side of entry to be meaningful.
            const trailingActive =
              s.trailing_sl != null &&
              s.tp_reached_count > 0 &&
              (s.entry != null
                ? (isLong ? s.trailing_sl >= s.entry * 0.999 : s.trailing_sl <= s.entry * 1.001)
                : true)
            const effectiveSl = s.trailing_sl ?? s.stop_loss

            const slTriggered = cur != null && effectiveSl != null
              ? (isLong ? cur <= effectiveSl : cur >= effectiveSl)
              : false

            return (
              <div key={s.id} className={`rounded-lg border bg-gray-900/40 p-4 transition ${
                slTriggered ? 'border-red-500/60' : trailingActive ? 'border-emerald-500/40' : 'border-amber-700/40'
              }`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-bold ${
                      isLong ? 'bg-emerald-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'
                    }`}>
                      {isLong ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                      {isLong ? 'BUY' : 'SELL'}
                    </span>
                    <span className="text-base font-bold text-white">{fmtForexPair(s.symbol)}</span>
                    <span className="text-[10px] text-amber-400 bg-amber-500/10 px-1.5 py-0.5 rounded">FOREX</span>
                  </div>
                  <span className={`text-[10px] uppercase font-semibold px-2 py-1 rounded border ${STATUS_STYLES[statusKey] || STATUS_STYLES.closed}`}>
                    {statusKey.replace('_', ' ')}
                  </span>
                </div>

                {/* Entry / SL / Targets */}
                <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Entry</div>
                    <div className="text-white font-medium">{s.entry ?? s.entry_raw ?? '—'}</div>
                  </div>
                  <div className={`rounded border p-2 transition ${
                    slTriggered ? 'bg-red-900/30 border-red-500/60'
                    : trailingActive ? 'bg-emerald-900/20 border-emerald-500/40'
                    : 'bg-gray-900/60 border-gray-700/50'
                  }`}>
                    <div className={`text-[11px] flex items-center gap-1 ${
                      slTriggered ? 'text-red-300 font-semibold'
                      : trailingActive ? 'text-emerald-300 font-semibold'
                      : 'text-gray-500'
                    }`}>
                      {trailingActive ? '🔒' : slTriggered ? '🔴' : ''}
                      {trailingActive ? `Trailing SL` : `Stop Loss`}
                    </div>
                    <div className={`font-medium ${slTriggered ? 'text-red-300' : trailingActive ? 'text-emerald-300' : 'text-red-400'}`}>
                      {trailingActive && effectiveSl != null ? Number(effectiveSl).toPrecision(6) : (s.stop_loss ?? s.stop_loss_raw ?? '—')}
                    </div>
                    {s.entry != null && effectiveSl != null && (
                      <div className="text-[10px] text-gray-500 mt-0.5">
                        {toPips(Math.abs(s.entry - effectiveSl), s.symbol)} pips
                      </div>
                    )}
                  </div>
                  <div className="rounded bg-gray-900/60 border border-gray-700/50 p-2">
                    <div className="text-gray-500">Targets</div>
                    <div className="text-emerald-300 font-medium">{s.take_profits?.length || 0}</div>
                  </div>
                </div>

                {/* Current price / distance */}
                {statusKey === 'active' && cur != null && s.entry != null && (
                  <div className="mt-2 flex items-center justify-between rounded bg-gray-900/40 border border-amber-700/30 px-2.5 py-1.5 text-xs">
                    <span className="text-gray-400">Live price</span>
                    <span className="flex items-center gap-2">
                      <span className="text-white font-semibold">{cur}</span>
                      <span className="text-[10px] text-amber-300">
                        {toPips(cur - s.entry, s.symbol)} pips {cur > s.entry ? '↑' : '↓'}
                      </span>
                    </span>
                  </div>
                )}

                {/* TP ladder */}
                {s.take_profits && s.take_profits.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {s.take_profits.map((tp, i) => {
                      const validTp = isValidTp(tp)
                      const locked = validTp && i < (s.tp_reached_count ?? 0)
                      const hit = validTp && cur != null ? (isLong ? cur >= tp : cur <= tp) : false
                      const near = validTp && !hit && cur != null && Math.abs(cur - tp) < pip * 10
                      return (
                        <span
                          key={i}
                          className={`inline-flex items-center gap-0.5 text-[11px] rounded px-1.5 py-0.5 border font-medium ${
                            !validTp
                              ? 'bg-gray-900/40 text-gray-600 border-gray-800/50 line-through'
                              : locked || hit
                              ? 'bg-emerald-500/30 text-emerald-200 border-emerald-400/60'
                              : near
                              ? 'bg-amber-500/20 text-amber-200 border-amber-400/50 animate-pulse'
                              : 'bg-gray-700/30 text-gray-400 border-gray-700/50'
                          }`}
                          title={!validTp ? 'Invalid TP (parse error — wrong side of entry)' : locked ? `SL locked at TP${i + 1}` : hit ? 'TP reached ✓' : near ? 'Near TP' : `TP${i + 1}`}
                        >
                          {!validTp ? <span className="text-gray-700 text-[9px]">⚠️</span> : locked ? '🔒' : hit ? '✓' : ''} TP{i + 1}: {tp}
                          {validTp && s.entry != null && (
                            <span className="text-[9px] text-gray-400 ml-0.5">
                              ({toPips(Math.abs(tp - s.entry), s.symbol)}p)
                            </span>
                          )}
                        </span>
                      )
                    })}
                  </div>
                )}

                {/* Signal intelligence row */}
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-gray-500">
                  {s.channel_title && <span>📡 {s.channel_title}</span>}
                  {s.confidence != null && (
                    <span className={s.confidence >= 0.7 ? 'text-emerald-400' : s.confidence >= 0.5 ? 'text-amber-400' : 'text-gray-500'}>
                      Confidence {Math.round(s.confidence * 100)}%
                    </span>
                  )}
                  {trailingActive && s.tp_reached_count > 0 && (
                    <span className="text-emerald-400">🔒 TP{s.tp_reached_count} locked</span>
                  )}
                  {s.posted_at && <span>{formatDate(s.posted_at)}</span>}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}