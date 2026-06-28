import Head from 'next/head'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Brain,
  Crosshair,
  Plus,
  RefreshCw,
  ShieldCheck,
  Skull,
  Target,
  TrendingUp,
  X,
  Zap,
} from 'lucide-react'
import { apiClient } from '@/services/api'

const DEFAULT_PAIR = 'BTC/USDT'
const MAX_OVERVIEW_PAIRS = 20
const TERMINAL_POSITION_STATUSES = new Set(['closed', 'cancelled', 'canceled', 'failed', 'rejected', 'stopped', 'exited'])

const normalizePair = (raw: string): string => {
  const value = (raw || '').trim().toUpperCase()
  if (!value) return ''
  if (value.includes('/')) return value
  if (value.endsWith('USDT') && value.length > 4) {
    return `${value.slice(0, -4)}/USDT`
  }
  return value
}

const isActivePositionStatus = (status?: string | null): boolean => {
  const key = (status || '').trim().toLowerCase()
  if (!key) return true
  return !TERMINAL_POSITION_STATUSES.has(key)
}

interface EntryQualityDetails {
  label?: string
  reasons?: string[]
}

interface VolumeContext {
  latest_volume?: number | null
  volume_ma?: number | null
  volume_ratio?: number | null
  buy_ratio?: number | null
  volume_confirmed?: boolean | null
  directional_confirmed?: boolean | null
}

interface BtcNewsContext {
  confirms?: boolean | null
  score?: number | null
  label?: string | null
  summary?: string | null
  is_stale?: boolean | null
  article_count?: number | null
}

interface EntryCandidate {
  label: string
  title?: string
  price: number
  score?: number
  is_limit?: boolean
  distance_pct?: number
  reason?: string
  rank?: number
}

interface SelectedEntry {
  label?: string
  title?: string
  price?: number | null
  mode?: string
  is_limit?: boolean
  reason?: string
}

interface SmcSignalRecord {
  id: number
  source: string
  symbol: string
  action: string
  status?: string
  confidence: number
  strength?: number
  timeframe?: string | null
  price?: number | null
  entry_price?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  entry_quality?: EntryQualityDetails | string | null
  decision_reasons?: string[] | null
  volume_context?: VolumeContext | null
  btc_news_context?: BtcNewsContext | null
  order_flow_confirmed?: boolean | null
  volume_ratio?: number | null
  raw_data?: Record<string, unknown> | null
  created_at?: string | null
}

interface SniperPosition {
  id: number
  symbol: string
  side: string
  status: string
  price?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  pnl?: number | null
  pnl_percentage?: number | null
  signal_id?: number | null
  source: 'smc_signal' | 'sniper_engine'
  created_at?: string | null
}

interface RugToken {
  id: number
  symbol: string
  name: string
  status: string
  risk_score?: number | null
  price_change_24h?: number | null
  recommended_entry?: number | null
  recommended_sl?: number | null
  recommended_tp?: number | null
  updated_at?: string | null
}

interface SmcGenerateResponse {
  source: string
  symbol: string
  timeframe: string
  action: string
  confidence: number
  score: number
  price?: number | null
  market_price?: number | null
  entry_price?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  entry_mode?: string
  selected_entry?: SelectedEntry | null
  entry_candidates?: EntryCandidate[] | null
  entry_quality?: EntryQualityDetails | null
  volume_context?: VolumeContext | null
  btc_news_context?: BtcNewsContext | null
  order_flow_confirmed?: boolean | null
  decision_reasons?: string[] | null
  raw_data?: Record<string, unknown> | null
  script?: { id: number; name: string }
  ai_agents?: {
    final_action?: string
    final_confidence?: number
    final_reasoning?: string
    [key: string]: unknown
  } | null
  insights?: {
    score?: number
    classification?: string
    summary?: string
    [key: string]: unknown
  } | null
  signal?: SmcSignalRecord | null
  remove_scope?: string
  cleanup_removed?: number
}

interface SmcOverviewResponse {
  smc?: { signals?: SmcSignalRecord[]; count?: number }
  sniper?: { positions?: SniperPosition[]; count?: number }
  rug_pulls?: { tokens?: RugToken[]; count?: number }
}

const TIMEFRAMES = ['15m', '1h', '4h', '1d']
const EXCHANGES = ['bitget', 'binance', 'bybit', 'okx', 'kucoin', 'coinbase']
const REMOVE_SCOPES = [
  { value: 'symbol_timeframe', label: 'Current Pair + Timeframe' },
  { value: 'symbol', label: 'Current Pair (all timeframes)' },
  { value: 'all', label: 'All SMC signals' },
] as const
const ENTRY_MODES = [
  { value: 'best_limit', label: 'Best Sniper Limit (Recommended)' },
  { value: 'conservative', label: 'Conservative Limit' },
  { value: 'balanced', label: 'Balanced Limit' },
  { value: 'aggressive', label: 'Aggressive Limit' },
  { value: 'market', label: 'Market Entry' },
  { value: 'candidate', label: 'Choose Candidate Entry' },
  { value: 'custom', label: 'Custom Entry Price' },
] as const

const actionBadge = (action: string): string => {
  const key = action.toLowerCase()
  if (key === 'buy') return 'bg-green-500/10 text-green-400 border border-green-500/30'
  if (key === 'sell') return 'bg-red-500/10 text-red-400 border border-red-500/30'
  return 'bg-yellow-500/10 text-yellow-400 border border-yellow-500/30'
}

const sourceBadge = (source: string): string => {
  if (source === 'smc_signal') return 'bg-cyan-500/10 text-cyan-300 border border-cyan-500/30'
  return 'bg-gray-500/10 text-gray-300 border border-gray-500/30'
}

const fmt = (value?: number | null, digits = 6): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits })
}

const fmtPct = (value?: number | null): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

const fmtDate = (value?: string | null): string => {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '-'
  return d.toLocaleString()
}

type DecisionSignalLike = {
  entry_quality?: EntryQualityDetails | string | null
  decision_reasons?: string[] | null
  volume_context?: VolumeContext | null
  btc_news_context?: BtcNewsContext | null
  order_flow_confirmed?: boolean | null
  raw_data?: Record<string, unknown> | null
} | null | undefined

const asRecord = (value: unknown): Record<string, unknown> | null => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

const asNullableBoolean = (value: unknown): boolean | null => {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (['true', '1', 'yes', 'y'].includes(normalized)) return true
    if (['false', '0', 'no', 'n'].includes(normalized)) return false
  }
  return null
}

const asStringArray = (value: unknown): string[] => {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => String(item || '').trim())
    .filter((item) => Boolean(item))
}

const readRawData = (signal: DecisionSignalLike): Record<string, unknown> | null => asRecord(signal?.raw_data)

const resolveVolumeContext = (signal: DecisionSignalLike): VolumeContext | null => {
  if (signal?.volume_context && typeof signal.volume_context === 'object') {
    return signal.volume_context
  }
  const rawData = readRawData(signal)
  const fromRaw = asRecord(rawData?.volume_context)
  return (fromRaw as VolumeContext | null) || null
}

const resolveBtcNewsContext = (signal: DecisionSignalLike): BtcNewsContext | null => {
  if (signal?.btc_news_context && typeof signal.btc_news_context === 'object') {
    return signal.btc_news_context
  }
  const rawData = readRawData(signal)
  const fromRaw = asRecord(rawData?.btc_news_context)
  return (fromRaw as BtcNewsContext | null) || null
}

const resolveOrderFlowConfirmed = (signal: DecisionSignalLike): boolean | null => {
  const direct = asNullableBoolean(signal?.order_flow_confirmed)
  if (direct !== null) return direct

  const volumeContext = resolveVolumeContext(signal)
  const directional = asNullableBoolean(volumeContext?.directional_confirmed)
  if (directional !== null) return directional

  const rawData = readRawData(signal)
  return asNullableBoolean(rawData?.order_flow_confirmed)
}

const resolveDecisionReasons = (signal: DecisionSignalLike): string[] => {
  if (!signal) return []

  const directReasons = asStringArray(signal.decision_reasons)
  if (directReasons.length) return directReasons

  if (signal.entry_quality && typeof signal.entry_quality === 'object') {
    const qualityReasons = asStringArray((signal.entry_quality as EntryQualityDetails).reasons)
    if (qualityReasons.length) return qualityReasons
  }

  const rawData = readRawData(signal)
  const rawEntryQuality = asRecord(rawData?.entry_quality)
  const rawReasons = asStringArray(rawEntryQuality?.reasons)
  if (rawReasons.length) return rawReasons

  return []
}

const getOrderFlowBadge = (confirmed: boolean | null): { label: string; className: string } => {
  if (confirmed === true) {
    return {
      label: 'Order flow confirmed',
      className: 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/30',
    }
  }
  if (confirmed === false) {
    return {
      label: 'Order flow conflict',
      className: 'bg-rose-500/10 text-rose-300 border border-rose-500/30',
    }
  }
  return {
    label: 'Order flow unavailable',
    className: 'bg-slate-500/10 text-slate-300 border border-slate-500/30',
  }
}

const getBtcNewsBadge = (context: BtcNewsContext | null): { label: string; className: string } => {
  const confirms = asNullableBoolean(context?.confirms)
  const score = typeof context?.score === 'number' ? context.score : null

  if (confirms === true || (confirms === null && score !== null && score > 0.15)) {
    return {
      label: 'BTC news aligned',
      className: 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/30',
    }
  }

  if (confirms === false || (confirms === null && score !== null && score < -0.15)) {
    return {
      label: 'BTC news conflict',
      className: 'bg-rose-500/10 text-rose-300 border border-rose-500/30',
    }
  }

  if (score !== null) {
    return {
      label: 'BTC news neutral',
      className: 'bg-amber-500/10 text-amber-300 border border-amber-500/30',
    }
  }

  return {
    label: 'BTC news unavailable',
    className: 'bg-slate-500/10 text-slate-300 border border-slate-500/30',
  }
}

export default function SmartMoneyConceptsPage() {
  const [symbol, setSymbol] = useState(DEFAULT_PAIR)
  const [pairInput, setPairInput] = useState('')
  const [monitoredPairs, setMonitoredPairs] = useState<string[]>([])
  const [timeframe, setTimeframe] = useState('1h')
  const [exchange, setExchange] = useState('bitget')
  const [stopLossPct, setStopLossPct] = useState(2)
  const [takeProfitPct, setTakeProfitPct] = useState(4)
  const [removeOldSignals, setRemoveOldSignals] = useState(true)
  const [removeOldScope, setRemoveOldScope] = useState<'symbol_timeframe' | 'symbol' | 'all'>('symbol_timeframe')
  const [entryMode, setEntryMode] = useState<'best_limit' | 'conservative' | 'balanced' | 'aggressive' | 'market' | 'custom' | 'candidate'>('best_limit')
  const [selectedEntryLabel, setSelectedEntryLabel] = useState('')
  const [customEntryPrice, setCustomEntryPrice] = useState<number | ''>('')

  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)

  const [latestSignal, setLatestSignal] = useState<SmcGenerateResponse | null>(null)
  const [smcSignals, setSmcSignals] = useState<SmcSignalRecord[]>([])
  const [sniperPositions, setSniperPositions] = useState<SniperPosition[]>([])
  const [rugTokens, setRugTokens] = useState<RugToken[]>([])
  const [selectedSignal, setSelectedSignal] = useState<SmcSignalRecord | null>(null)

  const smcLinkedSniperCount = useMemo(
    () => sniperPositions.filter((p) => p.source === 'smc_signal').length,
    [sniperPositions],
  )
  const activeSmcPositions = useMemo(
    () => sniperPositions.filter((position) => position.source === 'smc_signal' && isActivePositionStatus(position.status)),
    [sniperPositions],
  )

  const getLinkedLivePosition = useCallback(
    (signal: SmcSignalRecord): SniperPosition | null => {
      const bySignalId = activeSmcPositions.find((position) => position.signal_id && position.signal_id === signal.id)
      if (bySignalId) return bySignalId
      return activeSmcPositions.find((position) => position.symbol === signal.symbol) || null
    },
    [activeSmcPositions],
  )

  const inferDirectionalAction = useCallback(
    (signal: SmcSignalRecord): 'buy' | 'sell' => {
      const entry = signal.entry_price ?? signal.price ?? null
      const stopLoss = signal.stop_loss ?? null
      const takeProfit = signal.take_profit ?? null

      if (entry !== null && stopLoss !== null && takeProfit !== null) {
        if (takeProfit > entry && stopLoss < entry) return 'buy'
        if (takeProfit < entry && stopLoss > entry) return 'sell'
      }

      const recentDirectional = smcSignals.find(
        (item) => item.id !== signal.id && item.symbol === signal.symbol && ['buy', 'sell'].includes(item.action.toLowerCase()),
      )
      if (recentDirectional) {
        return recentDirectional.action.toLowerCase() as 'buy' | 'sell'
      }

      return 'buy'
    },
    [smcSignals],
  )

  const getDisplayAction = useCallback(
    (signal: SmcSignalRecord): 'buy' | 'sell' | 'hold' => {
      const rawAction = signal.action.toLowerCase()
      if (rawAction === 'buy' || rawAction === 'sell') return rawAction
      if (rawAction === 'hold' && getLinkedLivePosition(signal)) return 'hold'
      return inferDirectionalAction(signal)
    },
    [getLinkedLivePosition, inferDirectionalAction],
  )

  const selectedSignalLivePosition = useMemo(() => {
    if (!selectedSignal) return null
    return getLinkedLivePosition(selectedSignal)
  }, [getLinkedLivePosition, selectedSignal])

  const selectedSignalAction = useMemo(() => {
    if (!selectedSignal) return 'buy'
    return getDisplayAction(selectedSignal)
  }, [getDisplayAction, selectedSignal])

  const selectedSignalOrderFlow = useMemo(() => resolveOrderFlowConfirmed(selectedSignal), [selectedSignal])
  const selectedSignalBtcNews = useMemo(() => resolveBtcNewsContext(selectedSignal), [selectedSignal])
  const selectedSignalReasons = useMemo(() => resolveDecisionReasons(selectedSignal), [selectedSignal])

  const latestEntryCandidates = useMemo(() => latestSignal?.entry_candidates || [], [latestSignal])
  const latestSignalOrderFlow = useMemo(() => resolveOrderFlowConfirmed(latestSignal), [latestSignal])
  const latestSignalBtcNews = useMemo(() => resolveBtcNewsContext(latestSignal), [latestSignal])
  const latestSignalReasons = useMemo(() => resolveDecisionReasons(latestSignal), [latestSignal])

  const loadMonitorPairs = useCallback(async () => {
    try {
      const response = await apiClient.getSignalMonitorPairs()
      const apiPairs: string[] = Array.isArray(response.data?.pairs)
        ? (response.data.pairs as unknown[])
            .map((p) => normalizePair(String(p || '')))
            .filter((p): p is string => Boolean(p))
        : []

      const nextPairs = apiPairs.length ? Array.from(new Set(apiPairs)) : [DEFAULT_PAIR]
      setMonitoredPairs(nextPairs)
      setSymbol((prev) => {
        const normalizedPrev = normalizePair(prev || DEFAULT_PAIR)
        return nextPairs.includes(normalizedPrev) ? normalizedPrev : nextPairs[0]
      })
    } catch {
      setMonitoredPairs((prev) => (prev.length ? prev : [DEFAULT_PAIR]))
      setSymbol((prev) => normalizePair(prev || DEFAULT_PAIR) || DEFAULT_PAIR)
    }
  }, [])

  const fetchOverview = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const pairs = Array.from(
        new Set(
          [symbol, ...monitoredPairs]
            .map((pair) => normalizePair(pair))
            .filter(Boolean),
        ),
      ).slice(0, MAX_OVERVIEW_PAIRS)

      const targetPairs = pairs.length ? pairs : [DEFAULT_PAIR]

      const mergedSmc: SmcSignalRecord[] = []
      const mergedSniper: SniperPosition[] = []
      const mergedRug: RugToken[] = []
      let successfulRequests = 0

      for (const pair of targetPairs) {
        try {
          const response = await apiClient.getSmcOverview({
            symbol: pair,
            timeframe,
            limit: 80,
            rug_limit: 40,
            sniper_limit: 80,
          })
          const data = (response.data || {}) as SmcOverviewResponse
          mergedSmc.push(...(data.smc?.signals || []))
          mergedSniper.push(...(data.sniper?.positions || []))
          mergedRug.push(...(data.rug_pulls?.tokens || []))
          successfulRequests += 1
        } catch {
          // Keep going: partial failures should not hide other monitored pairs.
        }
      }

      if (!successfulRequests) {
        throw new Error('Failed to load Smart Money Concepts overview for monitored pairs')
      }

      const sortByDateDesc = <T extends { created_at?: string | null; updated_at?: string | null }>(
        a: T,
        b: T,
      ) => {
        const left = new Date(a.created_at || a.updated_at || 0).getTime()
        const right = new Date(b.created_at || b.updated_at || 0).getTime()
        return right - left
      }

      const uniqSmc = new Map<string, SmcSignalRecord>()
      for (const signal of mergedSmc) {
        uniqSmc.set(`${signal.id}:${signal.symbol}`, signal)
      }

      const uniqSniper = new Map<string, SniperPosition>()
      for (const position of mergedSniper) {
        uniqSniper.set(`${position.id}:${position.symbol}:${position.created_at || ''}`, position)
      }

      const uniqRug = new Map<string, RugToken>()
      for (const token of mergedRug) {
        uniqRug.set(`${token.id}:${token.symbol}`, token)
      }

      setSmcSignals(Array.from(uniqSmc.values()).sort(sortByDateDesc))
      setSniperPositions(Array.from(uniqSniper.values()).sort(sortByDateDesc))
      setRugTokens(Array.from(uniqRug.values()).sort(sortByDateDesc))
      setLastUpdated(new Date().toISOString())
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || 'Failed to load Smart Money Concepts overview'
      setError(detail)
    } finally {
      setLoading(false)
    }
  }, [monitoredPairs, symbol, timeframe])

  const generateForPair = useCallback(
    async (targetPair: string, options?: { removeOldSignals?: boolean }) => {
      const effectiveEntryMode = entryMode === 'candidate' && !selectedEntryLabel ? 'best_limit' : entryMode
      const response = await apiClient.generateSmcSignal({
        symbol: targetPair,
        timeframe,
        exchange,
        limit: 220,
        use_ai_agents: true,
        use_insights: true,
        persist_signal: true,
        stop_loss_pct: stopLossPct,
        take_profit_pct: takeProfitPct,
        refresh_news_if_stale: true,
        remove_old_signals: options?.removeOldSignals ?? removeOldSignals,
        remove_old_scope: removeOldScope,
        entry_mode: effectiveEntryMode,
        selected_entry_label: effectiveEntryMode === 'candidate' ? selectedEntryLabel : undefined,
        custom_entry_price: effectiveEntryMode === 'custom' && customEntryPrice !== '' ? Number(customEntryPrice) : undefined,
      })
      return (response.data || null) as SmcGenerateResponse | null
    },
    [customEntryPrice, entryMode, exchange, removeOldScope, removeOldSignals, selectedEntryLabel, stopLossPct, takeProfitPct, timeframe],
  )

  const handleGenerateSignal = useCallback(async () => {
    setGenerating(true)
    setError(null)
    try {
      const normalized = normalizePair(symbol)
      if (!normalized || !normalized.includes('/')) {
        throw new Error('Select a valid pair in BASE/QUOTE format (example: BTC/USDT)')
      }
      const generated = await generateForPair(normalized)
      setLatestSignal(generated)
      await fetchOverview()
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || 'Failed to generate Smart Money Concepts signal'
      setError(detail)
    } finally {
      setGenerating(false)
    }
  }, [fetchOverview, generateForPair, symbol])

  const handleGenerateMonitoredSignals = useCallback(async () => {
    setGenerating(true)
    setError(null)
    try {
      const targets = Array.from(
        new Set(
          (monitoredPairs.length ? monitoredPairs : [symbol])
            .map((pair) => normalizePair(pair))
            .filter(Boolean),
        ),
      )

      if (!targets.length) {
        throw new Error('Add at least one monitored pair before generating signals')
      }

      const failed: string[] = []
      let generatedCount = 0
      let latest: SmcGenerateResponse | null = null

      for (let index = 0; index < targets.length; index += 1) {
        const pair = targets[index]
        const allowCleanupForPair =
          removeOldSignals && (removeOldScope !== 'all' || index === 0)
        try {
          const generated = await generateForPair(pair, { removeOldSignals: allowCleanupForPair })
          if (generated) {
            latest = generated
            generatedCount += 1
          }
        } catch {
          failed.push(pair)
        }
      }

      if (!generatedCount) {
        throw new Error('Failed to generate Smart Money Concepts signals for monitored pairs')
      }

      setLatestSignal(latest)
      if (failed.length) {
        setError(`Generated ${generatedCount}/${targets.length} pairs. Failed: ${failed.join(', ')}`)
      }
      await fetchOverview()
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || 'Failed to generate monitored pair signals'
      setError(detail)
    } finally {
      setGenerating(false)
    }
  }, [fetchOverview, generateForPair, monitoredPairs, removeOldScope, removeOldSignals, symbol])

  const handleAddPair = useCallback(async () => {
    const normalized = normalizePair(pairInput)
    if (!normalized || !normalized.includes('/')) {
      setError('Pair must be in BASE/QUOTE format (example: ETH/USDT)')
      return
    }
    if (monitoredPairs.includes(normalized)) {
      setPairInput('')
      return
    }

    setError(null)
    try {
      await apiClient.addSignalMonitorPairs([normalized])
      setMonitoredPairs((prev) => Array.from(new Set([...prev, normalized])))
      setSymbol(normalized)
      setPairInput('')
      await fetchOverview()
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || 'Failed to add monitored pair'
      setError(detail)
    }
  }, [fetchOverview, monitoredPairs, pairInput])

  const handleRemovePair = useCallback(
    async (pair: string) => {
      if (monitoredPairs.length <= 1) {
        setError('At least one monitored pair is required')
        return
      }

      setError(null)
      try {
        await apiClient.removeSignalMonitorPairs([pair])
        const nextPairs = monitoredPairs.filter((p) => p !== pair)
        setMonitoredPairs(nextPairs)
        if (symbol === pair) {
          setSymbol(nextPairs[0] || DEFAULT_PAIR)
        }
        await fetchOverview()
      } catch (err: any) {
        const detail = err?.response?.data?.detail || err?.message || 'Failed to remove monitored pair'
        setError(detail)
      }
    },
    [fetchOverview, monitoredPairs, symbol],
  )

  useEffect(() => {
    loadMonitorPairs()
  }, [loadMonitorPairs])

  useEffect(() => {
    fetchOverview()
  }, [fetchOverview])

  useEffect(() => {
    const interval = setInterval(() => {
      fetchOverview()
    }, 30_000)
    return () => clearInterval(interval)
  }, [fetchOverview])

  useEffect(() => {
    if (!latestEntryCandidates.length) return
    const selectedStillExists = latestEntryCandidates.some((candidate) => candidate.label === selectedEntryLabel)
    if (!selectedStillExists) {
      setSelectedEntryLabel(latestEntryCandidates[0].label)
    }
  }, [latestEntryCandidates, selectedEntryLabel])

  useEffect(() => {
    if (!selectedSignal) return
    const exists = smcSignals.some((signal) => signal.id === selectedSignal.id && signal.symbol === selectedSignal.symbol)
    if (!exists) {
      setSelectedSignal(null)
    }
  }, [selectedSignal, smcSignals])

  useEffect(() => {
    if (!selectedSignal) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelectedSignal(null)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectedSignal])

  return (
    <>
      <Head>
        <title>Smart Money Concepts | TradeBot</title>
      </Head>

      <div className="space-y-6">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-2">
              <Brain className="w-6 h-6 text-cyan-400" />
              Smart Money Concepts
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Generate SMC buy/sell signals with AI validation, sentiment insights, SL/TP levels, and sniper/rug-pull context.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={fetchOverview}
              disabled={loading}
              className="px-3 py-2 rounded-lg bg-gray-800 text-gray-200 hover:bg-gray-700 disabled:opacity-50 inline-flex items-center gap-2"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
            <button
              type="button"
              onClick={handleGenerateSignal}
              disabled={generating}
              className="px-4 py-2 rounded-lg bg-cyan-600 text-white hover:bg-cyan-500 disabled:opacity-50 inline-flex items-center gap-2"
            >
              <Zap className={`w-4 h-4 ${generating ? 'animate-pulse' : ''}`} />
              {generating ? 'Generating...' : 'Generate Active Pair'}
            </button>
            <button
              type="button"
              onClick={handleGenerateMonitoredSignals}
              disabled={generating || !monitoredPairs.length}
              className="px-4 py-2 rounded-lg bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 inline-flex items-center gap-2"
            >
              <Zap className={`w-4 h-4 ${generating ? 'animate-pulse' : ''}`} />
              {generating ? 'Generating...' : `Generate All (${monitoredPairs.length || 1})`}
            </button>
          </div>
        </div>

        {error && (
          <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-red-300 text-sm">
            {error}
          </div>
        )}

        <div className="rounded-2xl border border-gray-700/60 bg-gray-900/70 p-4 md:p-5">
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-6 gap-3">
            <label className="text-sm text-gray-300">
              Active Pair
              <select
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
              >
                {Array.from(new Set([symbol, ...monitoredPairs].filter(Boolean))).map((pair) => (
                  <option key={pair} value={pair}>
                    {pair}
                  </option>
                ))}
              </select>
            </label>

            <label className="text-sm text-gray-300">
              Add Pair
              <div className="mt-1 flex items-center gap-2">
                <input
                  value={pairInput}
                  onChange={(e) => setPairInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      handleAddPair()
                    }
                  }}
                  className="flex-1 rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
                  placeholder="ETH/USDT"
                />
                <button
                  type="button"
                  onClick={handleAddPair}
                  className="rounded-lg bg-gray-700 hover:bg-gray-600 p-2 text-white"
                  title="Add monitored pair"
                >
                  <Plus className="w-4 h-4" />
                </button>
              </div>
            </label>

            <label className="text-sm text-gray-300">
              Timeframe
              <select
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
              >
                {TIMEFRAMES.map((tf) => (
                  <option key={tf} value={tf}>
                    {tf}
                  </option>
                ))}
              </select>
            </label>

            <label className="text-sm text-gray-300">
              Exchange
              <select
                value={exchange}
                onChange={(e) => setExchange(e.target.value)}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
              >
                {EXCHANGES.map((ex) => (
                  <option key={ex} value={ex}>
                    {ex}
                  </option>
                ))}
              </select>
            </label>

            <label className="text-sm text-gray-300">
              Stop Loss %
              <input
                type="number"
                min={0.2}
                step={0.1}
                value={stopLossPct}
                onChange={(e) => setStopLossPct(Number(e.target.value) || 0)}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
              />
            </label>

            <label className="text-sm text-gray-300">
              Take Profit %
              <input
                type="number"
                min={0.2}
                step={0.1}
                value={takeProfitPct}
                onChange={(e) => setTakeProfitPct(Number(e.target.value) || 0)}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
              />
            </label>
          </div>

          <div className="mt-3 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
            <label className="text-sm text-gray-300">
              <span className="inline-flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={removeOldSignals}
                  onChange={(e) => setRemoveOldSignals(e.target.checked)}
                  className="rounded border-gray-600 bg-gray-800 text-cyan-500 focus:ring-cyan-500"
                />
                Remove old SMC signals
              </span>
            </label>

            <label className="text-sm text-gray-300">
              Remove Scope
              <select
                value={removeOldScope}
                onChange={(e) => setRemoveOldScope(e.target.value as 'symbol_timeframe' | 'symbol' | 'all')}
                disabled={!removeOldSignals}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white disabled:opacity-60"
              >
                {REMOVE_SCOPES.map((scope) => (
                  <option key={scope.value} value={scope.value}>
                    {scope.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="text-sm text-gray-300">
              Entry Strategy
              <select
                value={entryMode}
                onChange={(e) => setEntryMode(e.target.value as 'best_limit' | 'conservative' | 'balanced' | 'aggressive' | 'market' | 'custom' | 'candidate')}
                className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
              >
                {ENTRY_MODES.map((mode) => (
                  <option key={mode.value} value={mode.value}>
                    {mode.label}
                  </option>
                ))}
              </select>
            </label>

            {entryMode === 'custom' ? (
              <label className="text-sm text-gray-300">
                Custom Entry Price
                <input
                  type="number"
                  min={0}
                  step="0.000001"
                  value={customEntryPrice}
                  onChange={(e) => {
                    const raw = e.target.value
                    if (raw === '') {
                      setCustomEntryPrice('')
                      return
                    }
                    const parsed = Number(raw)
                    setCustomEntryPrice(Number.isFinite(parsed) ? parsed : '')
                  }}
                  className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white"
                  placeholder="Enter exact entry"
                />
              </label>
            ) : (
              <label className="text-sm text-gray-300">
                Candidate Entry
                <select
                  value={selectedEntryLabel}
                  onChange={(e) => setSelectedEntryLabel(e.target.value)}
                  disabled={entryMode !== 'candidate' || !latestEntryCandidates.length}
                  className="mt-1 w-full rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-white disabled:opacity-60"
                >
                  {!latestEntryCandidates.length && <option value="">Generate signal to load candidates</option>}
                  {latestEntryCandidates.map((candidate) => (
                    <option key={candidate.label} value={candidate.label}>
                      {(candidate.title || candidate.label).replace(/_/g, ' ')} @ {fmt(candidate.price)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {monitoredPairs.map((pair) => (
              <div
                key={pair}
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 ${
                  pair === symbol
                    ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-200'
                    : 'border-gray-700 bg-gray-800 text-gray-200'
                }`}
              >
                <button
                  type="button"
                  onClick={() => setSymbol(pair)}
                  className="text-xs font-medium"
                  title="Set as active pair"
                >
                  {pair}
                </button>
                <button
                  type="button"
                  onClick={() => handleRemovePair(pair)}
                  className="p-0.5 rounded hover:bg-gray-700/60"
                  title={`Remove ${pair}`}
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>

          <p className="text-xs text-gray-500 mt-3">
            Monitoring {monitoredPairs.length || 1} pair(s). Last updated: {fmtDate(lastUpdated)}
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
          <div className="rounded-xl border border-cyan-500/30 bg-cyan-500/10 p-4">
            <p className="text-xs uppercase tracking-wide text-cyan-300">SMC Signals</p>
            <p className="text-2xl font-semibold text-white mt-1">{smcSignals.length}</p>
          </div>
          <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/10 p-4">
            <p className="text-xs uppercase tracking-wide text-indigo-300">Sniper Positions</p>
            <p className="text-2xl font-semibold text-white mt-1">{sniperPositions.length}</p>
          </div>
          <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4">
            <p className="text-xs uppercase tracking-wide text-emerald-300">SMC-Linked Sniper</p>
            <p className="text-2xl font-semibold text-white mt-1">{smcLinkedSniperCount}</p>
          </div>
          <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
            <p className="text-xs uppercase tracking-wide text-amber-300">Rug Watchlist</p>
            <p className="text-2xl font-semibold text-white mt-1">{rugTokens.length}</p>
          </div>
        </div>

        {latestSignal && (
          <div className="rounded-2xl border border-gray-700/60 bg-gray-900/70 p-5">
            <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
              <Target className="w-5 h-5 text-cyan-300" />
              Latest Smart Money Concepts Signal
            </h2>

            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
              <div className="rounded-xl bg-gray-800/70 border border-gray-700 p-3">
                <p className="text-xs text-gray-400">Action</p>
                <span className={`mt-2 inline-flex px-3 py-1 rounded-full text-sm font-semibold ${actionBadge(latestSignal.action)}`}>
                  {latestSignal.action.toUpperCase()}
                </span>
              </div>
              <div className="rounded-xl bg-gray-800/70 border border-gray-700 p-3">
                <p className="text-xs text-gray-400">Confidence</p>
                <p className="mt-2 text-white font-semibold">{(latestSignal.confidence * 100).toFixed(1)}%</p>
                <p className="text-xs text-gray-500">Score: {latestSignal.score.toFixed(3)}</p>
              </div>
              <div className="rounded-xl bg-gray-800/70 border border-gray-700 p-3">
                <p className="text-xs text-gray-400">Entry / SL / TP</p>
                <p className="mt-2 text-white text-sm">Entry: {fmt(latestSignal.entry_price ?? latestSignal.selected_entry?.price ?? latestSignal.price)}</p>
                <p className="text-red-300 text-sm">SL: {fmt(latestSignal.stop_loss)}</p>
                <p className="text-green-300 text-sm">TP: {fmt(latestSignal.take_profit)}</p>
                <p className="text-xs text-gray-500 mt-1">
                  Market: {fmt(latestSignal.market_price ?? latestSignal.price)} | Mode: {latestSignal.selected_entry?.mode || latestSignal.entry_mode || 'n/a'}
                </p>
                <p className="text-xs text-gray-500">
                  Selected: {(latestSignal.selected_entry?.title || latestSignal.selected_entry?.label || 'n/a').replace(/_/g, ' ')}
                  {latestSignal.selected_entry?.is_limit ? ' (limit)' : ' (market)'}
                </p>
                <p className="text-xs text-gray-500">
                  Quality: {latestSignal.entry_quality?.label || 'n/a'} | Vol ratio:{' '}
                  {fmt((latestSignal.volume_context?.volume_ratio as number | null) ?? null, 2)}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {(() => {
                    const orderFlowBadge = getOrderFlowBadge(latestSignalOrderFlow)
                    const btcNewsBadge = getBtcNewsBadge(latestSignalBtcNews)
                    return (
                      <>
                        <span className={`inline-flex px-2 py-1 rounded-full text-[11px] font-semibold ${orderFlowBadge.className}`}>
                          {orderFlowBadge.label}
                        </span>
                        <span className={`inline-flex px-2 py-1 rounded-full text-[11px] font-semibold ${btcNewsBadge.className}`}>
                          {btcNewsBadge.label}
                        </span>
                      </>
                    )
                  })()}
                </div>
                {!!latestSignalReasons.length && (
                  <div className="mt-2">
                    <p className="text-[11px] uppercase tracking-wide text-gray-500">Decision reasons</p>
                    <ul className="mt-1 space-y-1 text-xs text-gray-300 list-disc list-inside">
                      {latestSignalReasons.slice(0, 3).map((reason, index) => (
                        <li key={`latest-reason-${index}`}>{reason}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(latestSignal.cleanup_removed || 0) > 0 && (
                  <p className="text-xs text-amber-300 mt-1">Removed old signals: {latestSignal.cleanup_removed}</p>
                )}
              </div>
              <div className="rounded-xl bg-gray-800/70 border border-gray-700 p-3">
                <p className="text-xs text-gray-400">Strategy Script</p>
                <p className="mt-2 text-white text-sm font-medium">{latestSignal.script?.name || 'Smart Money Concepts'}</p>
                <p className="text-xs text-gray-500">Source: {latestSignal.source}</p>
              </div>
            </div>

            {!!latestEntryCandidates.length && (
              <div className="rounded-xl bg-gray-800/60 border border-gray-700 p-3 mt-4">
                <p className="text-xs text-cyan-300 uppercase tracking-wide mb-2">
                  Entry candidates (pick one, then generate to add as limit)
                </p>
                <div className="flex flex-wrap gap-2">
                  {latestEntryCandidates.map((candidate) => {
                    const isSelected = entryMode === 'candidate' && selectedEntryLabel === candidate.label
                    return (
                      <button
                        key={`${candidate.label}:${candidate.price}`}
                        type="button"
                        onClick={() => {
                          setEntryMode('candidate')
                          setSelectedEntryLabel(candidate.label)
                        }}
                        className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                          isSelected
                            ? 'border-cyan-400 bg-cyan-500/20 text-cyan-100'
                            : 'border-gray-600 bg-gray-800 text-gray-200 hover:border-cyan-500/60'
                        }`}
                        title={candidate.reason || 'Entry candidate'}
                      >
                        <span className="font-medium">{(candidate.title || candidate.label).replace(/_/g, ' ')}</span>{' '}
                        @ {fmt(candidate.price)}
                        {typeof candidate.score === 'number' && (
                          <span className="text-gray-400"> ({(candidate.score * 100).toFixed(0)}%)</span>
                        )}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 mt-4">
              <div className="rounded-xl bg-gray-800/60 border border-gray-700 p-3">
                <p className="text-xs text-cyan-300 uppercase tracking-wide flex items-center gap-1">
                  <ShieldCheck className="w-4 h-4" />
                  AI Agents
                </p>
                <p className="text-sm text-gray-200 mt-2">
                  {latestSignal.ai_agents?.final_reasoning || 'No AI reasoning available for this signal.'}
                </p>
              </div>
              <div className="rounded-xl bg-gray-800/60 border border-gray-700 p-3">
                <p className="text-xs text-violet-300 uppercase tracking-wide flex items-center gap-1">
                  <TrendingUp className="w-4 h-4" />
                  Market Insights
                </p>
                <p className="text-sm text-gray-200 mt-2">
                  Classification: {(latestSignal.insights?.classification as string) || 'n/a'} | Score: {fmt(latestSignal.insights?.score as number | null, 3)}
                </p>
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
          <section className="rounded-2xl border border-gray-700/60 bg-gray-900/70 p-4 md:p-5">
            <h3 className="text-lg font-semibold text-white flex items-center gap-2 mb-4">
              <Brain className="w-5 h-5 text-cyan-300" />
              Smart Money Concepts Signals
            </h3>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700">
                    <th className="text-left py-2">Time</th>
                    <th className="text-left py-2">Symbol</th>
                    <th className="text-left py-2">Action</th>
                    <th className="text-left py-2">Confidence</th>
                    <th className="text-left py-2">Entry / SL / TP</th>
                    <th className="text-left py-2">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {smcSignals.map((signal) => {
                    const displayAction = getDisplayAction(signal)
                    const linkedLivePosition = getLinkedLivePosition(signal)
                    const orderFlowBadge = getOrderFlowBadge(resolveOrderFlowConfirmed(signal))
                    const btcNewsBadge = getBtcNewsBadge(resolveBtcNewsContext(signal))
                    const decisionReasons = resolveDecisionReasons(signal)
                    return (
                    <tr key={signal.id} className="border-b border-gray-800 text-gray-200 hover:bg-gray-800/30 transition-colors">
                      <td className="py-2 pr-3">{fmtDate(signal.created_at)}</td>
                      <td className="py-2 pr-3">{signal.symbol}</td>
                      <td className="py-2 pr-3">
                        <div className="inline-flex items-center gap-2">
                          <span className={`inline-flex px-2 py-1 rounded-full text-xs font-semibold ${actionBadge(displayAction)}`}>
                            {displayAction.toUpperCase()}
                          </span>
                          {displayAction === 'hold' && signal.status?.toLowerCase() === 'ignored' && (
                            <span className="inline-flex px-2 py-1 rounded-full text-[10px] font-semibold bg-slate-500/10 text-slate-300 border border-slate-500/30">
                              Logged only
                            </span>
                          )}
                          {linkedLivePosition && (
                            <span className="inline-flex px-2 py-1 rounded-full text-[10px] font-semibold bg-indigo-500/10 text-indigo-300 border border-indigo-500/30">
                              Live {linkedLivePosition.status}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2 pr-3">{(signal.confidence * 100).toFixed(1)}%</td>
                      <td className="py-2 pr-3 text-xs">
                        <span className="text-cyan-300">E {fmt(signal.entry_price ?? signal.price)}</span>
                        <span className="text-gray-600 mx-2">|</span>
                        <span className="text-red-300">SL {fmt(signal.stop_loss)}</span>
                        <span className="text-gray-600 mx-2">|</span>
                        <span className="text-green-300">TP {fmt(signal.take_profit)}</span>
                      </td>
                      <td className="py-2 pr-3">
                        <div className="space-y-1.5">
                          <div className="flex flex-wrap gap-1">
                            <span className={`inline-flex px-2 py-1 rounded-full text-[10px] font-semibold ${orderFlowBadge.className}`}>
                              {orderFlowBadge.label}
                            </span>
                            <span className={`inline-flex px-2 py-1 rounded-full text-[10px] font-semibold ${btcNewsBadge.className}`}>
                              {btcNewsBadge.label}
                            </span>
                          </div>
                          {!!decisionReasons.length && (
                            <p className="text-[11px] text-gray-400 max-w-[260px] truncate" title={decisionReasons.join(' | ')}>
                              {decisionReasons[0]}
                            </p>
                          )}
                          <button
                            type="button"
                            onClick={() => setSelectedSignal(signal)}
                            className="px-2.5 py-1 rounded-lg border border-cyan-500/40 bg-cyan-500/10 text-cyan-200 text-xs hover:bg-cyan-500/20"
                          >
                            View
                          </button>
                        </div>
                      </td>
                    </tr>
                    )
                  })}
                  {!smcSignals.length && !loading && (
                    <tr>
                      <td className="py-4 text-gray-500" colSpan={6}>
                        No Smart Money Concepts signals yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="rounded-2xl border border-gray-700/60 bg-gray-900/70 p-4 md:p-5">
            <h3 className="text-lg font-semibold text-white flex items-center gap-2 mb-4">
              <Crosshair className="w-5 h-5 text-indigo-300" />
              Sniper Positions
            </h3>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700">
                    <th className="text-left py-2">Time</th>
                    <th className="text-left py-2">Symbol</th>
                    <th className="text-left py-2">Side</th>
                    <th className="text-left py-2">Status</th>
                    <th className="text-left py-2">PnL %</th>
                    <th className="text-left py-2">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {sniperPositions.map((position) => (
                    <tr key={position.id} className="border-b border-gray-800 text-gray-200">
                      <td className="py-2 pr-3">{fmtDate(position.created_at)}</td>
                      <td className="py-2 pr-3">{position.symbol}</td>
                      <td className="py-2 pr-3 uppercase">{position.side}</td>
                      <td className="py-2 pr-3">{position.status}</td>
                      <td
                        className={`py-2 pr-3 ${
                          (position.pnl_percentage || 0) >= 0 ? 'text-green-300' : 'text-red-300'
                        }`}
                      >
                        {fmtPct(position.pnl_percentage)}
                      </td>
                      <td className="py-2 pr-3">
                        <span className={`inline-flex px-2 py-1 rounded-full text-xs font-semibold ${sourceBadge(position.source)}`}>
                          {position.source === 'smc_signal' ? 'Smart Money Concepts' : 'Sniper Engine'}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {!sniperPositions.length && !loading && (
                    <tr>
                      <td className="py-4 text-gray-500" colSpan={6}>
                        No sniper positions available for this selection.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        <section className="rounded-2xl border border-gray-700/60 bg-gray-900/70 p-4 md:p-5">
          <h3 className="text-lg font-semibold text-white flex items-center gap-2 mb-4">
            <Skull className="w-5 h-5 text-amber-300" />
            Rug Pull Watchlist
          </h3>
          <div className="overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-gray-700">
                  <th className="text-left py-2">Token</th>
                  <th className="text-left py-2">Status</th>
                  <th className="text-left py-2">Risk</th>
                  <th className="text-left py-2">24h Change</th>
                  <th className="text-left py-2">Entry / SL / TP</th>
                  <th className="text-left py-2">Updated</th>
                </tr>
              </thead>
              <tbody>
                {rugTokens.map((token) => (
                  <tr key={token.id} className="border-b border-gray-800 text-gray-200">
                    <td className="py-2 pr-3">
                      <div className="font-medium">{token.symbol}</div>
                      <div className="text-xs text-gray-500">{token.name}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <span className="inline-flex px-2 py-1 rounded-full text-xs font-semibold bg-amber-500/10 text-amber-300 border border-amber-500/30">
                        {token.status}
                      </span>
                    </td>
                    <td className="py-2 pr-3">{fmt(token.risk_score, 2)}</td>
                    <td className={`py-2 pr-3 ${(token.price_change_24h || 0) >= 0 ? 'text-green-300' : 'text-red-300'}`}>
                      {fmtPct(token.price_change_24h)}
                    </td>
                    <td className="py-2 pr-3 text-xs">
                      <span className="text-cyan-300">E {fmt(token.recommended_entry)}</span>
                      <span className="text-gray-600 mx-2">|</span>
                      <span className="text-red-300">SL {fmt(token.recommended_sl)}</span>
                      <span className="text-gray-600 mx-2">|</span>
                      <span className="text-green-300">TP {fmt(token.recommended_tp)}</span>
                    </td>
                    <td className="py-2 pr-3">{fmtDate(token.updated_at)}</td>
                  </tr>
                ))}
                {!rugTokens.length && !loading && (
                  <tr>
                    <td className="py-4 text-gray-500" colSpan={6}>
                      No rug pull tokens available for this selection.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="mt-4 rounded-xl border border-gray-700 bg-gray-800/60 p-3 text-xs text-gray-400 flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 text-amber-300" />
            Smart Money Concepts view fuses structural SMC signals with AI agent validation and market insights.
            Sniper and rug pull sections are displayed in the same page to support fast risk checks before entry.
          </div>
        </section>
      </div>

      {selectedSignal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={() => setSelectedSignal(null)}>
          <div
            className="w-full max-w-2xl rounded-2xl border border-gray-700 bg-gray-900 shadow-xl p-5"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-lg font-semibold text-white">{selectedSignal.symbol} Signal Details</h2>
                <p className="text-xs text-gray-400 mt-1">Created: {fmtDate(selectedSignal.created_at)}</p>
              </div>
              <button
                type="button"
                onClick={() => setSelectedSignal(null)}
                className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800"
                aria-label="Close details"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="rounded-xl border border-gray-700 bg-gray-800/60 p-3">
                <p className="text-xs text-gray-400">Signal Type</p>
                <span className={`mt-2 inline-flex px-3 py-1 rounded-full text-sm font-semibold ${actionBadge(selectedSignalAction)}`}>
                  {selectedSignalAction.toUpperCase()}
                </span>
                <p className="text-xs text-gray-500 mt-2">Confidence: {(selectedSignal.confidence * 100).toFixed(1)}%</p>
              </div>

              <div className="rounded-xl border border-gray-700 bg-gray-800/60 p-3">
                <p className="text-xs text-gray-400">Entry / Stop Loss / Take Profit</p>
                <p className="mt-2 text-cyan-300 text-sm">Entry: {fmt(selectedSignal.entry_price ?? selectedSignal.price)}</p>
                <p className="text-red-300 text-sm">SL: {fmt(selectedSignal.stop_loss)}</p>
                <p className="text-green-300 text-sm">TP: {fmt(selectedSignal.take_profit)}</p>
              </div>

              <div className="rounded-xl border border-gray-700 bg-gray-800/60 p-3 md:col-span-2">
                <p className="text-xs text-gray-400">Decision Guards</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {(() => {
                    const orderFlowBadge = getOrderFlowBadge(selectedSignalOrderFlow)
                    const btcNewsBadge = getBtcNewsBadge(selectedSignalBtcNews)
                    return (
                      <>
                        <span className={`inline-flex px-2 py-1 rounded-full text-xs font-semibold ${orderFlowBadge.className}`}>
                          {orderFlowBadge.label}
                        </span>
                        <span className={`inline-flex px-2 py-1 rounded-full text-xs font-semibold ${btcNewsBadge.className}`}>
                          {btcNewsBadge.label}
                        </span>
                      </>
                    )
                  })()}
                </div>
                {!!selectedSignalBtcNews?.summary && (
                  <p className="mt-2 text-xs text-gray-300">BTC context: {selectedSignalBtcNews.summary}</p>
                )}
                {!!selectedSignalReasons.length && (
                  <div className="mt-2">
                    <p className="text-[11px] uppercase tracking-wide text-gray-500">Reason trail</p>
                    <ul className="mt-1 space-y-1 text-xs text-gray-300 list-disc list-inside">
                      {selectedSignalReasons.map((reason, index) => (
                        <li key={`selected-reason-${index}`}>{reason}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>

              <div className="rounded-xl border border-gray-700 bg-gray-800/60 p-3 md:col-span-2">
                <p className="text-xs text-gray-400">Live Trade Link</p>
                {selectedSignalLivePosition ? (
                  <div className="mt-2 text-sm text-gray-200 space-y-1">
                    <p>
                      Status: <span className="text-indigo-300">{selectedSignalLivePosition.status}</span>
                    </p>
                    <p>
                      Side: <span className="uppercase">{selectedSignalLivePosition.side}</span>
                    </p>
                    <p>
                      PnL: <span className={(selectedSignalLivePosition.pnl_percentage || 0) >= 0 ? 'text-green-300' : 'text-red-300'}>{fmtPct(selectedSignalLivePosition.pnl_percentage)}</span>
                    </p>
                    <p className="text-xs text-amber-300">Hold is shown because this signal is currently in an active live trade.</p>
                  </div>
                ) : (
                  <p className="mt-2 text-sm text-emerald-300">Not in active live trade: showing directional signal as BUY/SELL.</p>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
