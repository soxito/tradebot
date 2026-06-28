import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import { useZarRate } from '@/hooks/useZarRate'
import { formatPrice } from '@/utils/price'
import {
  RefreshCw, Skull, TrendingUp, TrendingDown, AlertTriangle,
  Eye, Target, Trash2, ChevronDown, ChevronUp, Search,
  BarChart3, Shield, Crosshair, Clock, Zap, Power, Activity
} from 'lucide-react'

interface RugPullToken {
  id: number
  coin_id: string
  symbol: string
  name: string
  image: string | null
  price_at_detection: number
  price_change_24h: number
  market_cap: number | null
  volume_24h: number | null
  market_cap_rank: number | null
  current_price: number | null
  price_change_since_detection: number
  peak_price: number | null
  peak_change_pct: number
  ai_analysis: string | null
  risk_score: number | null
  recommended_entry: number | null
  recommended_sl: number | null
  recommended_tp: number | null
  status: string
  trade_id: number | null
  detected_at: string | null
  updated_at: string | null
}

interface Stats {
  status_counts: Record<string, number>
  total: number
  top_watching: { symbol: string; pump_pct: number; risk_score: number | null }[]
  recent_dumps: { symbol: string; pump_pct: number; drop_pct: number }[]
}

interface SniperStatus {
  running: boolean
  interval_seconds: number
  started_at: string | null
  last_run: {
    at: string
    status: string
    scanned?: number
    declining?: number
    signals_created?: number
    trades_executed?: number
    positions_monitored?: number
    profits_taken?: number
    re_entries?: number
    error?: string
  } | null
}

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; border: string }> = {
  watching: { label: 'Watching', color: 'text-yellow-400', bg: 'bg-yellow-500/10', border: 'border-yellow-500/30' },
  entry_ready: { label: 'Entry Ready', color: 'text-cyan-400', bg: 'bg-cyan-500/10', border: 'border-cyan-500/30' },
  cooling: { label: 'Cooling', color: 'text-orange-400', bg: 'bg-orange-500/10', border: 'border-orange-500/30' },
  shorted: { label: 'Shorted', color: 'text-purple-400', bg: 'bg-purple-500/10', border: 'border-purple-500/30' },
  dumped: { label: 'Dumped', color: 'text-red-400', bg: 'bg-red-500/10', border: 'border-red-500/30' },
  survived: { label: 'Survived', color: 'text-green-400', bg: 'bg-green-500/10', border: 'border-green-500/30' },
  expired: { label: 'Expired', color: 'text-gray-500', bg: 'bg-gray-500/10', border: 'border-gray-500/30' },
}

export default function RugPulledPage() {
  const { toZar } = useZarRate()
  const [tokens, setTokens] = useState<RugPullToken[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [analyzingId, setAnalyzingId] = useState<number | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [sniperStatus, setSniperStatus] = useState<SniperStatus | null>(null)
  const [sniperToggling, setSniperToggling] = useState(false)
  const [pumpThreshold, setPumpThreshold] = useState<number>(30)

  const fetchTokens = useCallback(async () => {
    setLoading(true)
    try {
      const [tokensRes, statsRes, settingsRes] = await Promise.allSettled([
        apiClient.getRugPullTokens({ status: statusFilter || undefined, limit: 200 }),
        apiClient.getRugPullStats(),
        apiClient.getLiveTradeSettings(),
      ])
      if (tokensRes.status === 'fulfilled') setTokens(tokensRes.value.data?.tokens || [])
      if (statsRes.status === 'fulfilled') setStats(statsRes.value.data || null)
      if (settingsRes.status === 'fulfilled') {
        const configured = Number(settingsRes.value.data?.min_pump_pct)
        if (Number.isFinite(configured) && configured > 0) setPumpThreshold(configured)
      }
    } catch {} finally { setLoading(false) }
  }, [statusFilter])

  const fetchSniperStatus = useCallback(async () => {
    try {
      const res = await apiClient.getSniperStatus()
      setSniperStatus(res.data)
    } catch {}
  }, [])

  useEffect(() => { fetchTokens(); fetchSniperStatus() }, [fetchTokens, fetchSniperStatus])

  // Auto-refresh every 60 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      fetchTokens()
      fetchSniperStatus()
    }, 60_000)
    return () => clearInterval(interval)
  }, [fetchTokens, fetchSniperStatus])

  const handleScan = async () => {
    setScanning(true)
    try {
      await apiClient.triggerRugPullScan()
      await fetchTokens()
    } catch {} finally { setScanning(false) }
  }

  const handleAnalyze = async (tokenId: number) => {
    setAnalyzingId(tokenId)
    try {
      await apiClient.analyzeRugPullToken(tokenId)
      await fetchTokens()
    } catch {} finally { setAnalyzingId(null) }
  }

  const handleStatusChange = async (tokenId: number, newStatus: string) => {
    try {
      await apiClient.updateRugPullStatus(tokenId, newStatus)
      await fetchTokens()
    } catch {}
  }

  const handleDelete = async (tokenId: number) => {
    try {
      await apiClient.deleteRugPullToken(tokenId)
      setTokens(prev => prev.filter(t => t.id !== tokenId))
    } catch {}
  }

  const handleSniperToggle = async () => {
    setSniperToggling(true)
    try {
      if (sniperStatus?.running) {
        await apiClient.stopSniperLoop()
      } else {
        await apiClient.startSniperLoop(60)
      }
      await fetchSniperStatus()
    } catch {} finally { setSniperToggling(false) }
  }

  const handleSniperRunOnce = async () => {
    setSniperToggling(true)
    try {
      await apiClient.runSniperOnce()
      await fetchTokens()
      await fetchSniperStatus()
    } catch {} finally { setSniperToggling(false) }
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

  const riskColor = (score: number | null) => {
    if (score == null) return 'text-gray-500'
    if (score >= 0.7) return 'text-red-400'
    if (score >= 0.5) return 'text-orange-400'
    if (score >= 0.3) return 'text-yellow-400'
    return 'text-green-400'
  }

  const riskLabel = (score: number | null) => {
    if (score == null) return 'Unknown'
    if (score >= 0.7) return 'HIGH RISK'
    if (score >= 0.5) return 'Medium Risk'
    if (score >= 0.3) return 'Low Risk'
    return 'Minimal'
  }

  const watchingCount = stats?.status_counts?.watching || 0
  const entryReadyCount = stats?.status_counts?.entry_ready || 0
  const coolingCount = stats?.status_counts?.cooling || 0
  const dumpedCount = stats?.status_counts?.dumped || 0
  const shortedCount = stats?.status_counts?.shorted || 0

  return (
    <>
      <Head><title>TradeBot - Rug Pulled</title></Head>
      <div className="space-y-4 max-w-7xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-2">
              <Skull className="w-6 h-6 text-red-500" />
              Rug Pulled
            </h1>
            <p className="text-xs text-gray-500 mt-0.5">
              Tokens pumped {pumpThreshold}%+ — auto-scan for buying power decline &amp; sniper short entries (adjust in Settings)
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
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-red-600/20 border border-red-500/30 rounded-lg text-red-300 hover:bg-red-600/30 transition disabled:opacity-50"
            >
              {scanning ? (
                <><RefreshCw className="w-3.5 h-3.5 animate-spin" /> Scanning...</>
              ) : (
                <><Search className="w-3.5 h-3.5" /> Scan for Pumps</>
              )}
            </button>
          </div>
        </div>

        {/* Sniper Auto-Scan Control Panel */}
        <div className={`rounded-lg p-3 border ${sniperStatus?.running ? 'bg-orange-500/5 border-orange-500/30' : 'bg-gray-800/40 border-gray-700'}`}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2">
                <Crosshair className={`w-4 h-4 ${sniperStatus?.running ? 'text-orange-400 animate-pulse' : 'text-gray-500'}`} />
                <div>
                  <span className={`text-xs font-bold ${sniperStatus?.running ? 'text-orange-300' : 'text-gray-400'}`}>
                    Sniper Auto-Scan
                  </span>
                  <span className="text-[9px] text-gray-600 block">
                    {sniperStatus?.running
                      ? `Every ${sniperStatus.interval_seconds}s — scanning, shorting & monitoring positions`
                      : 'Scans every 60s: detect dump → short (SL only) → ride down → TP on hard pullback → re-entry'}
                  </span>
                </div>
              </div>
              {sniperStatus?.running && sniperStatus.last_run && (
                <div className="flex items-center gap-3 text-[10px] text-gray-500 hidden md:flex">
                  <span>Scanned: <span className="text-gray-300">{sniperStatus.last_run.scanned ?? '—'}</span></span>
                  <span>Declining: <span className="text-orange-400">{sniperStatus.last_run.declining ?? 0}</span></span>
                  <span>Trades: <span className="text-cyan-400">{sniperStatus.last_run.trades_executed ?? 0}</span></span>
                  <span>Monitoring: <span className="text-purple-400">{sniperStatus.last_run.positions_monitored ?? 0}</span></span>
                  <span>Profits: <span className="text-green-400">{sniperStatus.last_run.profits_taken ?? 0}</span></span>
                  <span>Re-entries: <span className="text-yellow-400">{sniperStatus.last_run.re_entries ?? 0}</span></span>
                  {sniperStatus.last_run.error && (
                    <span className="text-red-400">Error</span>
                  )}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={handleSniperRunOnce}
                disabled={sniperToggling}
                className="flex items-center gap-1 px-2.5 py-1.5 text-[10px] font-medium rounded bg-gray-700 border border-gray-600 text-gray-300 hover:bg-gray-600 transition disabled:opacity-50"
              >
                <Zap className="w-3 h-3" /> Run Once
              </button>
              <button
                onClick={handleSniperToggle}
                disabled={sniperToggling}
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg transition disabled:opacity-50 ${
                  sniperStatus?.running
                    ? 'bg-red-600/20 border border-red-500/30 text-red-300 hover:bg-red-600/30'
                    : 'bg-orange-600/20 border border-orange-500/30 text-orange-300 hover:bg-orange-600/30'
                }`}
              >
                {sniperToggling ? (
                  <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Power className="w-3.5 h-3.5" />
                )}
                {sniperStatus?.running ? 'Stop Sniper' : 'Start Sniper'}
              </button>
            </div>
          </div>
        </div>

        {/* Stats Cards */}
        <div className="grid grid-cols-4 md:grid-cols-7 gap-3">
          <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3">
            <div className="text-[10px] text-gray-500 mb-0.5">Total Tracked</div>
            <div className="text-xl font-bold text-white">{stats?.total ?? '—'}</div>
          </div>
          <div className="bg-yellow-500/5 border border-yellow-500/20 rounded-lg p-3">
            <div className="text-[10px] text-yellow-500 mb-0.5 flex items-center gap-1">
              <Eye className="w-3 h-3" /> Watching
            </div>
            <div className="text-xl font-bold text-yellow-400">{watchingCount}</div>
          </div>
          <div className="bg-cyan-500/5 border border-cyan-500/20 rounded-lg p-3">
            <div className="text-[10px] text-cyan-500 mb-0.5 flex items-center gap-1">
              <Target className="w-3 h-3" /> Entry Ready
            </div>
            <div className="text-xl font-bold text-cyan-400">{entryReadyCount}</div>
          </div>
          <div className="bg-orange-500/5 border border-orange-500/20 rounded-lg p-3">
            <div className="text-[10px] text-orange-500 mb-0.5 flex items-center gap-1">
              <Clock className="w-3 h-3" /> Cooling
            </div>
            <div className="text-xl font-bold text-orange-400">{coolingCount}</div>
          </div>
          <div className="bg-red-500/5 border border-red-500/20 rounded-lg p-3">
            <div className="text-[10px] text-red-500 mb-0.5 flex items-center gap-1">
              <Skull className="w-3 h-3" /> Dumped
            </div>
            <div className="text-xl font-bold text-red-400">{dumpedCount}</div>
          </div>
          <div className="bg-purple-500/5 border border-purple-500/20 rounded-lg p-3">
            <div className="text-[10px] text-purple-500 mb-0.5 flex items-center gap-1">
              <Activity className="w-3 h-3" /> Shorted
            </div>
            <div className="text-xl font-bold text-purple-400">{shortedCount}</div>
          </div>
          <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3">
            <div className="text-[10px] text-gray-500 mb-0.5 flex items-center gap-1">
              <Shield className="w-3 h-3" /> Survived
            </div>
            <div className="text-xl font-bold text-green-400">{stats?.status_counts?.survived || 0}</div>
          </div>
        </div>

        {/* Filters */}
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-500">Filter:</span>
          {['', 'watching', 'entry_ready', 'cooling', 'shorted', 'dumped', 'survived', 'expired'].map(s => {
            const cfg = s ? STATUS_CONFIG[s] : null
            return (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-2.5 py-1 rounded text-[11px] font-medium border transition ${
                  statusFilter === s
                    ? 'bg-white/10 border-white/20 text-white'
                    : 'bg-gray-800/30 border-gray-800 text-gray-500 hover:text-gray-300'
                }`}
              >
                {s ? cfg?.label || s : 'All'}
              </button>
            )
          })}
          <span className="ml-auto text-[10px] text-gray-600">{tokens.length} tokens</span>
        </div>

        {/* Token List */}
        {loading && tokens.length === 0 ? (
          <div className="text-center py-12 text-gray-500 text-sm">Loading...</div>
        ) : tokens.length === 0 ? (
          <div className="text-center py-12">
            <Skull className="w-10 h-10 text-gray-700 mx-auto mb-2" />
            <p className="text-gray-500 text-sm">No tokens detected yet</p>
            <p className="text-gray-600 text-xs mt-1">Click &quot;Scan for Pumps&quot; or start the Sniper to auto-detect</p>
          </div>
        ) : (
          <div className="space-y-1.5">
            {tokens.map(token => {
              const isExpanded = expandedId === token.id
              const sc = STATUS_CONFIG[token.status] || STATUS_CONFIG.watching
              const analysis = token.ai_analysis ? (() => { try { return JSON.parse(token.ai_analysis) } catch { return null } })() : null
              const dropFromPeak = token.peak_price && token.current_price && token.peak_price > 0
                ? ((token.peak_price - token.current_price) / token.peak_price * 100)
                : 0

              return (
                <div key={token.id} className="bg-gray-800/30 border border-gray-800 rounded-lg overflow-hidden">
                  {/* Main Row */}
                  <div
                    className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-gray-800/50 transition"
                    onClick={() => setExpandedId(isExpanded ? null : token.id)}
                  >
                    {/* Token icon + name */}
                    <div className="flex items-center gap-2 w-40 shrink-0">
                      {token.image && (
                        <img src={token.image} alt={token.symbol} className="w-5 h-5 rounded-full" />
                      )}
                      <div>
                        <span className="font-mono font-bold text-sm text-white">{token.symbol}</span>
                        <span className="text-[10px] text-gray-600 block truncate w-28">{token.name}</span>
                      </div>
                    </div>

                    {/* 24h Pump */}
                    <div className="w-20 text-right shrink-0">
                      <span className="text-green-400 font-mono font-bold text-sm">
                        +{token.price_change_24h.toFixed(0)}%
                      </span>
                      <span className="text-[9px] text-gray-600 block">24h pump</span>
                    </div>

                    {/* Current Price */}
                    <div className="w-24 text-right shrink-0">
                      <span className="text-gray-300 font-mono text-xs">{formatPrice(token.current_price)}</span>
                      {token.price_change_since_detection !== 0 && (
                        <span className={`text-[9px] block font-mono ${token.price_change_since_detection >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                          {token.price_change_since_detection >= 0 ? '+' : ''}{token.price_change_since_detection.toFixed(1)}%
                        </span>
                      )}
                    </div>

                    {/* Drop from peak */}
                    <div className="w-20 text-right shrink-0">
                      {dropFromPeak > 0 ? (
                        <span className="text-red-400 font-mono text-xs flex items-center justify-end gap-0.5">
                          <TrendingDown className="w-3 h-3" />
                          -{dropFromPeak.toFixed(1)}%
                        </span>
                      ) : (
                        <span className="text-gray-600 text-xs">—</span>
                      )}
                      <span className="text-[9px] text-gray-600 block">from peak</span>
                    </div>

                    {/* Market Cap */}
                    <div className="w-20 text-right shrink-0 hidden md:block">
                      <span className="text-gray-400 text-xs">{formatNum(token.market_cap)}</span>
                      <span className="text-[9px] text-gray-600 block">mcap</span>
                    </div>

                    {/* Risk Score */}
                    <div className="w-20 text-center shrink-0">
                      {token.risk_score != null ? (
                        <div>
                          <span className={`font-bold text-xs ${riskColor(token.risk_score)}`}>
                            {(token.risk_score * 100).toFixed(0)}%
                          </span>
                          <span className={`text-[9px] block ${riskColor(token.risk_score)}`}>
                            {riskLabel(token.risk_score)}
                          </span>
                        </div>
                      ) : (
                        <span className="text-gray-600 text-[10px]">Not analyzed</span>
                      )}
                    </div>

                    {/* Status */}
                    <div className="w-24 shrink-0">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${sc.color} ${sc.bg} ${sc.border}`}>
                        {sc.label}
                      </span>
                    </div>

                    {/* Time */}
                    <div className="w-16 text-right shrink-0 hidden lg:block">
                      <span className="text-[10px] text-gray-600 flex items-center justify-end gap-0.5">
                        <Clock className="w-2.5 h-2.5" />
                        {timeAgo(token.detected_at)}
                      </span>
                    </div>

                    {/* Expand */}
                    <div className="shrink-0">
                      {isExpanded ? <ChevronUp className="w-3.5 h-3.5 text-gray-500" /> : <ChevronDown className="w-3.5 h-3.5 text-gray-500" />}
                    </div>
                  </div>

                  {/* Expanded Details */}
                  {isExpanded && (
                    <div className="px-3 pb-3 pt-1 border-t border-gray-800/50 space-y-3">
                      {/* Price Info Grid */}
                      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
                        <div className="bg-gray-900/50 rounded p-2">
                          <div className="text-[9px] text-gray-600">Detection Price</div>
                          <div className="text-xs font-mono text-gray-300">{formatPrice(token.price_at_detection)}</div>
                        </div>
                        <div className="bg-gray-900/50 rounded p-2">
                          <div className="text-[9px] text-gray-600">Current Price</div>
                          <div className="text-xs font-mono text-gray-300">{formatPrice(token.current_price)}</div>
                          {token.current_price && toZar(token.current_price) && (
                            <div className="text-[9px] text-gray-600 font-mono">{toZar(token.current_price)}</div>
                          )}
                        </div>
                        <div className="bg-gray-900/50 rounded p-2">
                          <div className="text-[9px] text-gray-600">Peak Price</div>
                          <div className="text-xs font-mono text-gray-300">{formatPrice(token.peak_price)}</div>
                        </div>
                        <div className="bg-gray-900/50 rounded p-2">
                          <div className="text-[9px] text-gray-600">Volume 24h</div>
                          <div className="text-xs font-mono text-gray-300">{formatNum(token.volume_24h)}</div>
                        </div>
                        <div className="bg-gray-900/50 rounded p-2">
                          <div className="text-[9px] text-gray-600">Rank</div>
                          <div className="text-xs font-mono text-gray-300">
                            {token.market_cap_rank ? `#${token.market_cap_rank}` : 'Unranked'}
                          </div>
                        </div>
                      </div>

                      {/* AI Entry Recommendations */}
                      {(token.recommended_entry || token.recommended_sl || token.recommended_tp) && (
                        <div className="bg-cyan-500/5 border border-cyan-500/20 rounded-lg p-3">
                          <div className="text-[10px] text-cyan-500 font-semibold mb-2 flex items-center gap-1">
                            <Crosshair className="w-3 h-3" /> AI Short Entry Recommendation
                          </div>
                          <div className="grid grid-cols-3 gap-3">
                            {token.recommended_entry != null && (
                              <div>
                                <div className="text-[9px] text-gray-500">Sniper Entry (Short)</div>
                                <div className="text-sm font-mono font-bold text-cyan-400">{formatPrice(token.recommended_entry)}</div>
                                {analysis?.sniper_entry?.method && (
                                  <div className="text-[8px] text-cyan-600 mt-0.5">{analysis.sniper_entry.method.replace('_', ' ')}</div>
                                )}
                              </div>
                            )}
                            {token.recommended_sl != null && (
                              <div>
                                <div className="text-[9px] text-gray-500">Stop Loss</div>
                                <div className="text-sm font-mono font-bold text-red-400">{formatPrice(token.recommended_sl)}</div>
                              </div>
                            )}
                            {token.recommended_tp != null && (
                              <div>
                                <div className="text-[9px] text-gray-500">Take Profit</div>
                                <div className="text-sm font-mono font-bold text-green-400">{formatPrice(token.recommended_tp)}</div>
                                {analysis?.sniper_entry?.risk_reward != null && (
                                  <div className="text-[8px] text-green-600 mt-0.5">R:R 1:{analysis.sniper_entry.risk_reward}</div>
                                )}
                              </div>
                            )}
                          </div>

                          {/* Fibonacci Levels */}
                          {analysis?.sniper_entry?.fib_levels && (
                            <div className="mt-2 pt-2 border-t border-gray-700/50">
                              <div className="text-[9px] text-gray-500 mb-1">Fibonacci Levels</div>
                              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                                {Object.entries(analysis.sniper_entry.fib_levels as Record<string, number>).map(([level, price]) => (
                                  <span key={level} className="text-[10px] font-mono text-gray-400">
                                    <span className="text-purple-400">{level}</span>: {formatPrice(price)}
                                  </span>
                                ))}
                              </div>
                              {analysis.sniper_entry.rejection_candles > 0 && (
                                <div className="text-[9px] text-orange-400 mt-1">
                                  ⚠ {analysis.sniper_entry.rejection_candles} rejection candle{analysis.sniper_entry.rejection_candles > 1 ? 's' : ''} detected
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}

                      {/* Buying Power Indicator */}
                      {analysis?.buying_power && (
                        <div className={`rounded-lg p-3 border ${
                          analysis.buying_power.declining
                            ? 'bg-orange-500/5 border-orange-500/20'
                            : 'bg-gray-900/50 border-gray-700'
                        }`}>
                          <div className="text-[10px] font-semibold mb-1.5 flex items-center gap-1">
                            <TrendingDown className={`w-3 h-3 ${analysis.buying_power.declining ? 'text-orange-400' : 'text-gray-500'}`} />
                            <span className={analysis.buying_power.declining ? 'text-orange-400' : 'text-gray-500'}>
                              Buying Power: {analysis.buying_power.declining ? 'DECLINING' : 'Stable'}
                            </span>
                            <span className={`ml-1 text-[9px] font-mono ${
                              analysis.buying_power.score >= 0.5 ? 'text-red-400' :
                              analysis.buying_power.score >= 0.35 ? 'text-orange-400' : 'text-gray-500'
                            }`}>
                              ({(analysis.buying_power.score * 100).toFixed(0)}%)
                            </span>
                          </div>
                          {analysis.buying_power.signals?.length > 0 && (
                            <div className="flex flex-wrap gap-1">
                              {(analysis.buying_power.signals as string[]).map((sig: string, i: number) => (
                                <span key={i} className={`px-1.5 py-0.5 rounded text-[9px] font-mono border ${
                                  analysis.buying_power.declining
                                    ? 'text-orange-400 bg-orange-500/10 border-orange-500/20'
                                    : 'text-gray-500 bg-gray-500/10 border-gray-500/20'
                                }`}>
                                  {sig}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      )}

                      {/* AI Analysis */}
                      {analysis && (
                        <div className="bg-gray-900/50 rounded-lg p-3">
                          <div className="text-[10px] text-gray-500 font-semibold mb-2 flex items-center gap-1">
                            <BarChart3 className="w-3 h-3" /> Analysis
                          </div>
                          {analysis.risk_indicators && (
                            <div className="flex flex-wrap gap-1.5 mb-2">
                              {Object.entries(analysis.risk_indicators as Record<string, boolean>).map(([key, val]) => (
                                <span
                                  key={key}
                                  className={`px-1.5 py-0.5 rounded text-[9px] font-medium border ${
                                    val
                                      ? 'text-red-400 bg-red-500/10 border-red-500/20'
                                      : 'text-gray-500 bg-gray-500/10 border-gray-500/20'
                                  }`}
                                >
                                  {val ? '⚠️' : '✓'} {key.replace(/_/g, ' ')}
                                </span>
                              ))}
                            </div>
                          )}
                          <div className="grid grid-cols-2 gap-2 text-[10px]">
                            <div>
                              <span className="text-gray-600">Pump 24h:</span>{' '}
                              <span className="text-green-400 font-mono">+{analysis.pump_24h}%</span>
                            </div>
                            <div>
                              <span className="text-gray-600">Drop from peak:</span>{' '}
                              <span className="text-red-400 font-mono">-{analysis.drop_from_peak_pct}%</span>
                            </div>
                            <div>
                              <span className="text-gray-600">Change since detected:</span>{' '}
                              <span className={`font-mono ${analysis.change_since_detection_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                {analysis.change_since_detection_pct >= 0 ? '+' : ''}{analysis.change_since_detection_pct}%
                              </span>
                            </div>
                            <div>
                              <span className="text-gray-600">Market Cap:</span>{' '}
                              <span className="text-gray-300">{formatNum(analysis.market_cap)}</span>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Actions */}
                      <div className="flex items-center gap-2 flex-wrap">
                        <button
                          onClick={() => handleAnalyze(token.id)}
                          disabled={analyzingId === token.id}
                          className="flex items-center gap-1 px-2.5 py-1.5 text-[11px] font-medium rounded bg-cyan-600/20 border border-cyan-500/30 text-cyan-300 hover:bg-cyan-600/30 transition disabled:opacity-50"
                        >
                          {analyzingId === token.id ? (
                            <><RefreshCw className="w-3 h-3 animate-spin" /> Analyzing...</>
                          ) : (
                            <><Zap className="w-3 h-3" /> Analyze Entry</>
                          )}
                        </button>

                        {token.status === 'watching' && (
                          <button
                            onClick={() => handleStatusChange(token.id, 'survived')}
                            className="flex items-center gap-1 px-2.5 py-1.5 text-[11px] font-medium rounded bg-green-600/20 border border-green-500/30 text-green-300 hover:bg-green-600/30 transition"
                          >
                            <Shield className="w-3 h-3" /> Mark Survived
                          </button>
                        )}

                        {token.status === 'watching' && (
                          <button
                            onClick={() => handleStatusChange(token.id, 'dumped')}
                            className="flex items-center gap-1 px-2.5 py-1.5 text-[11px] font-medium rounded bg-red-600/20 border border-red-500/30 text-red-300 hover:bg-red-600/30 transition"
                          >
                            <Skull className="w-3 h-3" /> Mark Dumped
                          </button>
                        )}

                        <button
                          onClick={() => handleDelete(token.id)}
                          className="flex items-center gap-1 px-2.5 py-1.5 text-[11px] font-medium rounded bg-gray-600/20 border border-gray-500/30 text-gray-400 hover:bg-gray-600/30 transition ml-auto"
                        >
                          <Trash2 className="w-3 h-3" /> Remove
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </>
  )
}
