import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  RefreshCw, Rocket, TrendingUp, BarChart3, Zap, Power,
  Search, Trash2, ChevronDown, ChevronUp, Activity,
  Volume2, Flame, Globe, ArrowUpCircle, Bitcoin, Eye,
  Shield, Mountain, Waves, Star
} from 'lucide-react'

interface PumpToken {
  id: number
  coin_id: string
  symbol: string
  name: string
  image: string | null
  price_at_detection: number
  current_price: number | null
  price_change_1h: number | null
  price_change_24h: number | null
  price_change_7d: number | null
  volume_24h: number | null
  volume_change_pct: number | null
  high_24h: number | null
  low_24h: number | null
  ath: number | null
  ath_change_pct: number | null
  market_cap: number | null
  market_cap_rank: number | null
  // Original 4 indicators
  volume_spike_score: number
  price_accel_score: number
  social_score: number
  order_flow_score: number
  // New 4 indicators
  momentum_score: number
  btc_relative_score: number
  volatility_score: number
  ath_breakout_score: number
  pump_score: number
  // BTC context
  btc_price_1h_pct: number | null
  btc_price_24h_pct: number | null
  market_sentiment: string | null
  is_watchlist: boolean
  // Tracking
  peak_price: number | null
  peak_gain_pct: number
  gain_since_detection: number
  trade_id: number | null
  signal_id: number | null
  status: string
  detected_at: string | null
  updated_at: string | null
}

interface Stats {
  status_counts: Record<string, number>
  total: number
  top_candidates: { symbol: string; pump_score: number; gain_pct: number; price_change_1h: number | null; btc_relative_score: number; momentum_score: number; is_watchlist: boolean; market_sentiment: string | null }[]
  recent_pumps: { symbol: string; peak_gain_pct: number; pump_score: number; is_watchlist: boolean }[]
  watchlist_count: number
}

interface MonitorStatus {
  running: boolean
  interval_seconds: number
  started_at: string | null
  last_run: {
    at: string
    status: string
    new?: number
    updated?: number
    total_scanned?: number
    signals_created?: number
    pumped_count?: number
    error?: string
    market_ctx?: { btc_1h: number; btc_24h: number; btc_7d: number; sentiment: string; btc_price: number }
  } | null
}

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; border: string }> = {
  detected: { label: 'Detected', color: 'text-yellow-400', bg: 'bg-yellow-500/10', border: 'border-yellow-500/30' },
  confirmed: { label: 'Confirmed', color: 'text-cyan-400', bg: 'bg-cyan-500/10', border: 'border-cyan-500/30' },
  signalled: { label: 'Signalled', color: 'text-blue-400', bg: 'bg-blue-500/10', border: 'border-blue-500/30' },
  traded: { label: 'Traded', color: 'text-purple-400', bg: 'bg-purple-500/10', border: 'border-purple-500/30' },
  pumped: { label: 'Pumped', color: 'text-green-400', bg: 'bg-green-500/10', border: 'border-green-500/30' },
  faded: { label: 'Faded', color: 'text-red-400', bg: 'bg-red-500/10', border: 'border-red-500/30' },
  expired: { label: 'Expired', color: 'text-gray-500', bg: 'bg-gray-500/10', border: 'border-gray-500/30' },
}

const SENTIMENT_CONFIG: Record<string, { label: string; color: string; icon: string }> = {
  strong_bull: { label: 'Strong Bull', color: 'text-green-400', icon: '🟢' },
  bullish: { label: 'Bullish', color: 'text-green-300', icon: '🟡' },
  neutral: { label: 'Neutral', color: 'text-gray-400', icon: '⚪' },
  bearish: { label: 'Bearish', color: 'text-red-400', icon: '🔴' },
}

export default function PumpMonitorPage() {
  const [tokens, setTokens] = useState<PumpToken[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [watchlistOnly, setWatchlistOnly] = useState(false)
  const [monitorStatus, setMonitorStatus] = useState<MonitorStatus | null>(null)
  const [monitorToggling, setMonitorToggling] = useState(false)

  const fetchTokens = useCallback(async () => {
    setLoading(true)
    try {
      const [tokensRes, statsRes] = await Promise.allSettled([
        apiClient.getPumpTokens({ status: statusFilter || undefined, limit: 200 }),
        apiClient.getPumpMonitorStats(),
      ])
      if (tokensRes.status === 'fulfilled') setTokens(tokensRes.value.data?.tokens || [])
      if (statsRes.status === 'fulfilled') setStats(statsRes.value.data || null)
    } catch {} finally { setLoading(false) }
  }, [statusFilter])

  const fetchMonitorStatus = useCallback(async () => {
    try {
      const res = await apiClient.getPumpMonitorStatus()
      setMonitorStatus(res.data)
    } catch {}
  }, [])

  useEffect(() => { fetchTokens(); fetchMonitorStatus() }, [fetchTokens, fetchMonitorStatus])

  useEffect(() => {
    const interval = setInterval(() => {
      fetchTokens()
      fetchMonitorStatus()
    }, 60_000)
    return () => clearInterval(interval)
  }, [fetchTokens, fetchMonitorStatus])

  const handleScan = async () => {
    setScanning(true)
    try {
      await apiClient.runPumpMonitorOnce()
      await fetchTokens()
      await fetchMonitorStatus()
    } catch {} finally { setScanning(false) }
  }

  const handleDelete = async (tokenId: number) => {
    try {
      await apiClient.deletePumpToken(tokenId)
      setTokens(prev => prev.filter(t => t.id !== tokenId))
    } catch {}
  }

  const handleMonitorToggle = async () => {
    setMonitorToggling(true)
    try {
      if (monitorStatus?.running) {
        await apiClient.stopPumpMonitor()
      } else {
        await apiClient.startPumpMonitor(120)
      }
      await fetchMonitorStatus()
    } catch {} finally { setMonitorToggling(false) }
  }

  const handleRunOnce = async () => {
    setMonitorToggling(true)
    try {
      await apiClient.runPumpMonitorOnce()
      await fetchTokens()
      await fetchMonitorStatus()
    } catch {} finally { setMonitorToggling(false) }
  }

  const timeAgo = (dateStr: string | null) => {
    if (!dateStr) return '—'
    const diff = Date.now() - new Date(dateStr).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h ago`
    const days = Math.floor(hrs / 24)
    return `${days}d ago`
  }

  const formatNum = (n: number | null) => {
    if (n == null) return '—'
    if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(2)}B`
    if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`
    if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`
    return `$${n.toFixed(2)}`
  }

  const formatPrice = (n: number | null) => {
    if (n == null) return '—'
    if (n >= 1) return `$${n.toFixed(2)}`
    if (n >= 0.01) return `$${n.toFixed(4)}`
    return `$${n.toFixed(6)}`
  }

  const scoreColor = (score: number) => {
    if (score >= 0.7) return 'text-green-400'
    if (score >= 0.55) return 'text-cyan-400'
    if (score >= 0.4) return 'text-yellow-400'
    return 'text-gray-500'
  }

  const scoreBar = (score: number, max: number = 1) => {
    const pct = Math.min(100, (score / max) * 100)
    const color = score >= 0.7 ? 'bg-green-500' : score >= 0.5 ? 'bg-cyan-500' : score >= 0.3 ? 'bg-yellow-500' : 'bg-gray-600'
    return (
      <div className="w-full bg-gray-800 rounded-full h-1.5">
        <div className={`${color} h-1.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
    )
  }

  const pctBadge = (val: number | null) => {
    if (val == null) return <span className="text-gray-600">—</span>
    const color = val >= 0 ? 'text-green-400' : 'text-red-400'
    return <span className={`font-mono ${color}`}>{val >= 0 ? '+' : ''}{val.toFixed(1)}%</span>
  }

  // Filter tokens
  const displayTokens = watchlistOnly ? tokens.filter(t => t.is_watchlist) : tokens

  const detectedCount = stats?.status_counts?.detected || 0
  const confirmedCount = stats?.status_counts?.confirmed || 0
  const signalledCount = stats?.status_counts?.signalled || 0
  const tradedCount = stats?.status_counts?.traded || 0
  const pumpedCount = stats?.status_counts?.pumped || 0
  const fadedCount = stats?.status_counts?.faded || 0
  const watchlistCount = stats?.watchlist_count || 0

  // Market context from last run
  const mktCtx = monitorStatus?.last_run?.market_ctx
  const sentimentKey = mktCtx?.sentiment || 'neutral'
  const sentimentCfg = SENTIMENT_CONFIG[sentimentKey] || SENTIMENT_CONFIG.neutral

  return (
    <>
      <Head><title>TradeBot - Pump Monitor v2</title></Head>
      <div className="space-y-4 max-w-7xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-2">
              <Rocket className="w-6 h-6 text-green-500" />
              Pump Monitor <span className="text-xs text-gray-500 font-normal">v2</span>
            </h1>
            <p className="text-xs text-gray-500 mt-0.5">
              Deep analysis: 8 indicators, BTC market context, multi-timeframe momentum, ATH breakout detection
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchTokens}
              disabled={loading}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-gray-800 border border-gray-700 rounded-lg text-gray-300 hover:bg-gray-700 transition"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
            <button
              onClick={handleScan}
              disabled={scanning}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-green-600/20 border border-green-500/30 rounded-lg text-green-300 hover:bg-green-600/30 transition disabled:opacity-50"
            >
              {scanning ? (
                <><RefreshCw className="w-3.5 h-3.5 animate-spin" /> Scanning...</>
              ) : (
                <><Search className="w-3.5 h-3.5" /> Scan Now</>
              )}
            </button>
          </div>
        </div>

        {/* BTC Market Context Banner */}
        {mktCtx && (
          <div className="rounded-lg p-3 bg-orange-500/5 border border-orange-500/20">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div className="flex items-center gap-3">
                <Bitcoin className="w-5 h-5 text-orange-400" />
                <div>
                  <span className="text-xs font-bold text-orange-300">BTC Market Context</span>
                  <div className="flex items-center gap-3 text-[10px] text-gray-400 mt-0.5">
                    <span>Price: <span className="text-orange-300 font-mono">{formatNum(mktCtx.btc_price)}</span></span>
                    <span>1h: {pctBadge(mktCtx.btc_1h)}</span>
                    <span>24h: {pctBadge(mktCtx.btc_24h)}</span>
                    <span>7d: {pctBadge(mktCtx.btc_7d)}</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`text-xs font-bold ${sentimentCfg.color}`}>
                  {sentimentCfg.icon} {sentimentCfg.label}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Monitor Control Panel */}
        <div className={`rounded-lg p-3 border ${monitorStatus?.running ? 'bg-green-500/5 border-green-500/30' : 'bg-gray-800/40 border-gray-700'}`}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2">
                <Activity className={`w-4 h-4 ${monitorStatus?.running ? 'text-green-400 animate-pulse' : 'text-gray-500'}`} />
                <div>
                  <span className={`text-xs font-bold ${monitorStatus?.running ? 'text-green-300' : 'text-gray-400'}`}>
                    Pump Auto-Scanner v2
                  </span>
                  <span className="text-[9px] text-gray-600 block">
                    {monitorStatus?.running
                      ? `Every ${monitorStatus.interval_seconds}s — 8 indicators | BTC-relative | multi-timeframe | watchlist: BTC ETH SOL XRP`
                      : '8 deep indicators + BTC market context + always-watch BTC/ETH/SOL/XRP'}
                  </span>
                </div>
              </div>
              {monitorStatus?.running && monitorStatus.last_run && (
                <div className="flex items-center gap-3 text-[10px] text-gray-500 hidden md:flex">
                  <span>Scanned: <span className="text-gray-300">{monitorStatus.last_run.total_scanned ?? '—'}</span></span>
                  <span>New: <span className="text-green-400">{monitorStatus.last_run.new ?? 0}</span></span>
                  <span>Signals: <span className="text-cyan-400">{monitorStatus.last_run.signals_created ?? 0}</span></span>
                  <span>Pumped: <span className="text-yellow-400">{monitorStatus.last_run.pumped_count ?? 0}</span></span>
                  {monitorStatus.last_run.error && (
                    <span className="text-red-400">Error</span>
                  )}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={handleRunOnce}
                disabled={monitorToggling}
                className="flex items-center gap-1 px-2.5 py-1.5 text-[10px] font-medium rounded bg-gray-700 border border-gray-600 text-gray-300 hover:bg-gray-600 transition disabled:opacity-50"
              >
                <Zap className="w-3 h-3" /> Run Once
              </button>
              <button
                onClick={handleMonitorToggle}
                disabled={monitorToggling}
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg transition disabled:opacity-50 ${
                  monitorStatus?.running
                    ? 'bg-red-600/20 border border-red-500/30 text-red-300 hover:bg-red-600/30'
                    : 'bg-green-600/20 border border-green-500/30 text-green-300 hover:bg-green-600/30'
                }`}
              >
                {monitorToggling ? (
                  <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Power className="w-3.5 h-3.5" />
                )}
                {monitorStatus?.running ? 'Stop Monitor' : 'Start Monitor'}
              </button>
            </div>
          </div>
        </div>

        {/* Stats Cards */}
        <div className="grid grid-cols-4 md:grid-cols-8 gap-2">
          {[
            { label: 'Total', count: stats?.total || 0, icon: BarChart3, color: 'text-white' },
            { label: 'Watchlist', count: watchlistCount, icon: Eye, color: 'text-orange-400' },
            { label: 'Detected', count: detectedCount, icon: Search, color: 'text-yellow-400' },
            { label: 'Confirmed', count: confirmedCount, icon: Flame, color: 'text-cyan-400' },
            { label: 'Signalled', count: signalledCount, icon: Zap, color: 'text-blue-400' },
            { label: 'Traded', count: tradedCount, icon: ArrowUpCircle, color: 'text-purple-400' },
            { label: 'Pumped', count: pumpedCount, icon: TrendingUp, color: 'text-green-400' },
            { label: 'Faded', count: fadedCount, icon: TrendingUp, color: 'text-red-400' },
          ].map(({ label, count, icon: Icon, color }) => (
            <div key={label} className="bg-gray-800/40 border border-gray-700/50 rounded-lg p-2.5 text-center">
              <Icon className={`w-4 h-4 mx-auto mb-1 ${color}`} />
              <div className={`text-lg font-bold ${color}`}>{count}</div>
              <div className="text-[10px] text-gray-500">{label}</div>
            </div>
          ))}
        </div>

        {/* Filters */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-1.5 flex-wrap">
            {['', 'detected', 'confirmed', 'signalled', 'traded', 'pumped', 'faded', 'expired'].map(s => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-2.5 py-1 text-[10px] font-medium rounded-full border transition ${
                  statusFilter === s
                    ? 'bg-green-600/20 border-green-500/40 text-green-300'
                    : 'bg-gray-800/40 border-gray-700 text-gray-400 hover:text-gray-300'
                }`}
              >
                {s ? STATUS_CONFIG[s]?.label || s : 'All'}
              </button>
            ))}
          </div>
          <div className="h-4 w-px bg-gray-700" />
          <button
            onClick={() => setWatchlistOnly(!watchlistOnly)}
            className={`flex items-center gap-1 px-2.5 py-1 text-[10px] font-medium rounded-full border transition ${
              watchlistOnly
                ? 'bg-orange-600/20 border-orange-500/40 text-orange-300'
                : 'bg-gray-800/40 border-gray-700 text-gray-400 hover:text-gray-300'
            }`}
          >
            <Star className="w-3 h-3" />
            Watchlist Only
          </button>
        </div>

        {/* Token List */}
        <div className="space-y-2">
          {loading && tokens.length === 0 ? (
            <div className="text-center py-12 text-gray-500">
              <RefreshCw className="w-6 h-6 mx-auto mb-2 animate-spin" />
              Loading pump tokens...
            </div>
          ) : displayTokens.length === 0 ? (
            <div className="text-center py-12 text-gray-500">
              <Rocket className="w-8 h-8 mx-auto mb-2 opacity-30" />
              <p className="text-sm">No pump tokens detected yet</p>
              <p className="text-xs mt-1">Start the monitor or click &ldquo;Scan Now&rdquo; to detect tokens</p>
            </div>
          ) : (
            displayTokens.map(token => {
              const st = STATUS_CONFIG[token.status] || STATUS_CONFIG.detected
              const isExpanded = expandedId === token.id

              return (
                <div
                  key={token.id}
                  className={`rounded-lg border ${st.border} ${st.bg} overflow-hidden transition-all ${token.is_watchlist ? 'ring-1 ring-orange-500/20' : ''}`}
                >
                  {/* Main row */}
                  <div
                    className="flex items-center gap-3 p-3 cursor-pointer hover:bg-white/[0.02] transition"
                    onClick={() => setExpandedId(isExpanded ? null : token.id)}
                  >
                    {/* Token info */}
                    <div className="flex items-center gap-2 min-w-[140px]">
                      {token.image && (
                        <img src={token.image} alt={token.symbol} className="w-6 h-6 rounded-full" />
                      )}
                      <div>
                        <div className="text-sm font-bold text-white flex items-center gap-1">
                          {token.symbol}
                          {token.is_watchlist && (
                            <Star className="w-3 h-3 text-orange-400 fill-orange-400" />
                          )}
                        </div>
                        <div className="text-[10px] text-gray-500 truncate max-w-[100px]">{token.name}</div>
                      </div>
                    </div>

                    {/* Price */}
                    <div className="text-right min-w-[80px]">
                      <div className="text-xs text-white font-mono">{formatPrice(token.current_price)}</div>
                      <div className={`text-[10px] ${(token.gain_since_detection ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {token.gain_since_detection >= 0 ? '+' : ''}{token.gain_since_detection?.toFixed(1) || '0'}%
                      </div>
                    </div>

                    {/* 1h change */}
                    <div className="text-center min-w-[55px]">
                      <div className="text-[10px] text-gray-500">1h</div>
                      <div className="text-xs">{pctBadge(token.price_change_1h)}</div>
                    </div>

                    {/* 24h change */}
                    <div className="text-center min-w-[55px]">
                      <div className="text-[10px] text-gray-500">24h</div>
                      <div className="text-xs">{pctBadge(token.price_change_24h)}</div>
                    </div>

                    {/* 7d change */}
                    <div className="text-center min-w-[55px] hidden md:block">
                      <div className="text-[10px] text-gray-500">7d</div>
                      <div className="text-xs">{pctBadge(token.price_change_7d)}</div>
                    </div>

                    {/* BTC Relative */}
                    <div className="text-center min-w-[55px] hidden lg:block">
                      <div className="text-[10px] text-gray-500">vs BTC</div>
                      <div className={`text-xs font-mono ${(token.btc_relative_score ?? 0) >= 0.4 ? 'text-green-400' : 'text-gray-500'}`}>
                        {((token.btc_relative_score ?? 0) * 100).toFixed(0)}%
                      </div>
                    </div>

                    {/* Pump Score */}
                    <div className="text-center min-w-[70px]">
                      <div className="text-[10px] text-gray-500">Score</div>
                      <div className={`text-sm font-bold ${scoreColor(token.pump_score)}`}>
                        {(token.pump_score * 100).toFixed(0)}%
                      </div>
                    </div>

                    {/* Peak Gain */}
                    <div className="text-center min-w-[60px] hidden md:block">
                      <div className="text-[10px] text-gray-500">Peak</div>
                      <div className="text-xs text-green-400 font-mono">
                        {token.peak_gain_pct > 0 ? `+${token.peak_gain_pct.toFixed(1)}%` : '—'}
                      </div>
                    </div>

                    {/* Status */}
                    <div className="min-w-[70px] text-right">
                      <span className={`inline-block px-2 py-0.5 text-[10px] font-semibold rounded-full border ${st.border} ${st.bg} ${st.color}`}>
                        {st.label}
                      </span>
                    </div>

                    {/* Time */}
                    <div className="text-right min-w-[50px]">
                      <div className="text-[10px] text-gray-500">{timeAgo(token.detected_at)}</div>
                    </div>

                    {/* Expand arrow */}
                    <div className="text-gray-600">
                      {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                    </div>
                  </div>

                  {/* Expanded details */}
                  {isExpanded && (
                    <div className="border-t border-gray-700/50 p-3 bg-gray-900/30">
                      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-3">
                        {/* Score Breakdown — 8 indicators */}
                        <div className="space-y-2 md:col-span-2">
                          <div className="text-[10px] text-gray-500 font-semibold uppercase">Score Breakdown (8 indicators)</div>
                          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><Bitcoin className="w-3 h-3" /> BTC Relative</span>
                                <span className="text-white">{((token.btc_relative_score ?? 0) * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.btc_relative_score ?? 0)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><TrendingUp className="w-3 h-3" /> Price Accel</span>
                                <span className="text-white">{(token.price_accel_score * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.price_accel_score)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><Volume2 className="w-3 h-3" /> Volume Spike</span>
                                <span className="text-white">{(token.volume_spike_score * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.volume_spike_score)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><Waves className="w-3 h-3" /> Momentum</span>
                                <span className="text-white">{((token.momentum_score ?? 0) * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.momentum_score ?? 0)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><ArrowUpCircle className="w-3 h-3" /> Order Flow</span>
                                <span className="text-white">{(token.order_flow_score * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.order_flow_score)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><Shield className="w-3 h-3" /> Volatility</span>
                                <span className="text-white">{((token.volatility_score ?? 0) * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.volatility_score ?? 0)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><Mountain className="w-3 h-3" /> ATH Breakout</span>
                                <span className="text-white">{((token.ath_breakout_score ?? 0) * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.ath_breakout_score ?? 0)}
                            </div>
                            <div>
                              <div className="flex justify-between text-[10px]">
                                <span className="text-gray-400 flex items-center gap-1"><Globe className="w-3 h-3" /> Social</span>
                                <span className="text-white">{(token.social_score * 100).toFixed(0)}%</span>
                              </div>
                              {scoreBar(token.social_score)}
                            </div>
                          </div>
                        </div>

                        {/* Market Data */}
                        <div className="space-y-2">
                          <div className="text-[10px] text-gray-500 font-semibold uppercase">Market Data</div>
                          <div className="space-y-1 text-xs">
                            <div className="flex justify-between">
                              <span className="text-gray-500">Market Cap</span>
                              <span className="text-gray-300">{formatNum(token.market_cap)}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">Rank</span>
                              <span className="text-gray-300">#{token.market_cap_rank || '—'}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">Vol/MCap</span>
                              <span className="text-gray-300">{token.volume_change_pct?.toFixed(1) || '—'}%</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">24h High</span>
                              <span className="text-gray-300">{formatPrice(token.high_24h)}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">24h Low</span>
                              <span className="text-gray-300">{formatPrice(token.low_24h)}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">ATH</span>
                              <span className="text-gray-300">{formatPrice(token.ath)}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">From ATH</span>
                              <span className="text-red-400">{token.ath_change_pct?.toFixed(1) || '—'}%</span>
                            </div>
                          </div>
                        </div>

                        {/* Performance + Context */}
                        <div className="space-y-2">
                          <div className="text-[10px] text-gray-500 font-semibold uppercase">Performance</div>
                          <div className="space-y-1 text-xs">
                            <div className="flex justify-between">
                              <span className="text-gray-500">Entry Price</span>
                              <span className="text-gray-300">{formatPrice(token.price_at_detection)}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">Gain</span>
                              <span className={token.gain_since_detection >= 0 ? 'text-green-400' : 'text-red-400'}>
                                {token.gain_since_detection >= 0 ? '+' : ''}{token.gain_since_detection?.toFixed(1) || '0'}%
                              </span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">Peak Gain</span>
                              <span className="text-green-400">+{token.peak_gain_pct?.toFixed(1) || '0'}%</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">BTC 1h%</span>
                              <span className="text-orange-300">{token.btc_price_1h_pct != null ? `${token.btc_price_1h_pct >= 0 ? '+' : ''}${token.btc_price_1h_pct.toFixed(1)}%` : '—'}</span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">Sentiment</span>
                              <span className={SENTIMENT_CONFIG[token.market_sentiment || 'neutral']?.color || 'text-gray-400'}>
                                {SENTIMENT_CONFIG[token.market_sentiment || 'neutral']?.label || token.market_sentiment || '—'}
                              </span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-gray-500">Signal</span>
                              <span className={token.signal_id ? 'text-blue-400' : 'text-gray-600'}>
                                {token.signal_id ? `#${token.signal_id}` : 'None'}
                              </span>
                            </div>
                          </div>
                        </div>

                        {/* Actions */}
                        <div className="space-y-2">
                          <div className="text-[10px] text-gray-500 font-semibold uppercase">Actions</div>
                          <div className="space-y-1.5">
                            {token.is_watchlist && (
                              <div className="flex items-center gap-1.5 px-2 py-1.5 text-[10px] rounded bg-orange-600/10 border border-orange-500/20 text-orange-300">
                                <Star className="w-3 h-3 fill-orange-400" /> Watchlist Coin
                              </div>
                            )}
                            <button
                              onClick={(e) => { e.stopPropagation(); handleDelete(token.id) }}
                              className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-[10px] rounded bg-red-600/10 border border-red-500/20 text-red-400 hover:bg-red-600/20 transition"
                            >
                              <Trash2 className="w-3 h-3" /> Remove
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )
            })
          )}
        </div>
      </div>
    </>
  )
}
