import { useState, useEffect, useMemo } from 'react'
import { AlertTriangle, Clock, RefreshCw, Search, Shield, ExternalLink } from 'lucide-react'
import apiClient from '@/services/api'
import { formatDateZA, formatTimeZA } from '@/utils/datetime'

interface PairInfo {
  symbol: string
  baseCoin: string
  quoteCoin: string
  market: string
  status: string
  delisting_ts: number | null
  delisting_date: string | null
  minLever: number | null
  maxLever: number | null
  futures_adjustment?: string
  maintain_time?: string
  limit_open_time?: string
}

export default function DelistingsPage() {
  const [pairs, setPairs] = useState<PairInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [lastFetch, setLastFetch] = useState<Date | null>(null)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'delisting' | 'restricted' | 'abnormal'>('all')
  const [sortBy, setSortBy] = useState<'date' | 'symbol'>('date')

  const fetchPairs = async () => {
    setLoading(true)
    try {
      const res = await apiClient.getBitgetAvailablePairs('USDT')
      const data = res.data
      // Only keep pairs with warnings: delisting, abnormal status, restricted, maintenance
      const warningPairs = (data.pairs || []).filter((p: PairInfo) =>
        p.delisting_ts ||
        (p.status && !['online', 'normal'].includes(p.status)) ||
        p.futures_adjustment ||
        p.maintain_time ||
        p.limit_open_time
      )
      setPairs(warningPairs)
      setLastFetch(new Date())
    } catch (err) {
      console.error('Failed to fetch pairs:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchPairs() }, [])

  const filtered = useMemo(() => {
    let result = pairs

    // Text search
    if (search) {
      const q = search.toUpperCase()
      result = result.filter(p => p.symbol.toUpperCase().includes(q) || p.baseCoin.toUpperCase().includes(q))
    }

    // Category filter
    if (filter === 'delisting') {
      result = result.filter(p => p.delisting_ts)
    } else if (filter === 'restricted') {
      result = result.filter(p => p.limit_open_time || p.maintain_time)
    } else if (filter === 'abnormal') {
      result = result.filter(p => p.status && !['online', 'normal'].includes(p.status))
    }

    // Sort
    if (sortBy === 'date') {
      result = [...result].sort((a, b) => {
        const aTs = a.delisting_ts || Infinity
        const bTs = b.delisting_ts || Infinity
        return aTs - bTs
      })
    } else {
      result = [...result].sort((a, b) => a.baseCoin.localeCompare(b.baseCoin))
    }

    return result
  }, [pairs, search, filter, sortBy])

  const delistingCount = pairs.filter(p => p.delisting_ts).length
  const restrictedCount = pairs.filter(p => p.limit_open_time || p.maintain_time).length

  const getDaysUntil = (ts: number | null) => {
    if (!ts) return null
    const now = Date.now()
    const diff = ts - now
    if (diff <= 0) return 0
    return Math.ceil(diff / (1000 * 60 * 60 * 24))
  }

  const getUrgencyColor = (ts: number | null) => {
    const days = getDaysUntil(ts)
    if (days === null) return 'text-gray-400'
    if (days <= 1) return 'text-red-400'
    if (days <= 3) return 'text-orange-400'
    if (days <= 7) return 'text-yellow-400'
    return 'text-gray-300'
  }

  const getUrgencyBg = (ts: number | null) => {
    const days = getDaysUntil(ts)
    if (days === null) return 'bg-gray-800/40'
    if (days <= 1) return 'bg-red-900/20 border-red-500/30'
    if (days <= 3) return 'bg-orange-900/20 border-orange-500/30'
    if (days <= 7) return 'bg-yellow-900/20 border-yellow-500/30'
    return 'bg-gray-800/40'
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <AlertTriangle className="w-7 h-7 text-red-400" />
            Delistings & Warnings
          </h1>
          <p className="text-sm text-gray-400 mt-1">
            Pairs scheduled for delisting, under maintenance, or with trading restrictions on Bitget
          </p>
        </div>
        <button
          onClick={fetchPairs}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-600 rounded-lg text-sm text-gray-300 transition disabled:opacity-50"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-red-900/20 border border-red-500/30 rounded-lg p-4">
          <div className="flex items-center gap-2 text-red-400 mb-1">
            <AlertTriangle className="w-4 h-4" />
            <span className="text-xs font-semibold uppercase">Pending Delisting</span>
          </div>
          <p className="text-3xl font-bold text-red-300">{delistingCount}</p>
          <p className="text-xs text-red-400/70 mt-1">Pairs with confirmed removal date</p>
        </div>
        <div className="bg-orange-900/20 border border-orange-500/30 rounded-lg p-4">
          <div className="flex items-center gap-2 text-orange-400 mb-1">
            <Shield className="w-4 h-4" />
            <span className="text-xs font-semibold uppercase">Restricted</span>
          </div>
          <p className="text-3xl font-bold text-orange-300">{restrictedCount}</p>
          <p className="text-xs text-orange-400/70 mt-1">Trading restricted or under maintenance</p>
        </div>
        <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-4">
          <div className="flex items-center gap-2 text-gray-400 mb-1">
            <Clock className="w-4 h-4" />
            <span className="text-xs font-semibold uppercase">Last Updated</span>
          </div>
          <p className="text-lg font-bold text-gray-300">
            {lastFetch ? formatTimeZA(lastFetch) : '—'}
          </p>
          <p className="text-xs text-gray-500 mt-1">
            {lastFetch ? formatDateZA(lastFetch) : 'Not yet loaded'}
          </p>
        </div>
      </div>

      {/* Filters & Search */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-[360px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search pairs..."
            className="w-full bg-gray-800 border border-gray-700 rounded-lg pl-10 pr-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
          />
        </div>
        <div className="flex bg-gray-800 rounded-lg overflow-hidden border border-gray-700">
          {([
            { key: 'all', label: `All (${pairs.length})` },
            { key: 'delisting', label: `Delisting (${delistingCount})` },
            { key: 'restricted', label: `Restricted (${restrictedCount})` },
          ] as const).map(f => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`px-3 py-2 text-xs font-semibold transition ${
                filter === f.key
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <select
          value={sortBy}
          onChange={e => setSortBy(e.target.value as 'date' | 'symbol')}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-white"
        >
          <option value="date">Sort: Soonest First</option>
          <option value="symbol">Sort: A–Z</option>
        </select>
      </div>

      {/* Pairs List */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <RefreshCw className="w-6 h-6 text-gray-500 animate-spin" />
          <span className="ml-3 text-gray-400">Loading pairs from Bitget...</span>
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-16">
          <AlertTriangle className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="text-gray-400">
            {search ? 'No matching pairs found' : 'No pairs with warnings right now'}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map(pair => {
            const daysLeft = getDaysUntil(pair.delisting_ts)
            const urgencyColor = getUrgencyColor(pair.delisting_ts)
            const urgencyBg = getUrgencyBg(pair.delisting_ts)

            return (
              <div
                key={pair.symbol}
                className={`border rounded-lg p-4 transition hover:border-gray-500 ${urgencyBg}`}
              >
                <div className="flex items-start justify-between gap-4">
                  {/* Left: Pair info */}
                  <div className="flex items-center gap-3">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="text-lg font-bold text-white">{pair.symbol}</span>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${
                          pair.market === 'both' ? 'bg-purple-500/20 text-purple-300' :
                          pair.market === 'futures' ? 'bg-orange-500/20 text-orange-300' :
                          'bg-blue-500/20 text-blue-300'
                        }`}>
                          {pair.market === 'both' ? 'SPOT + FUTURES' : pair.market === 'futures' ? 'FUTURES' : 'SPOT'}
                        </span>
                        {pair.maxLever && (
                          <span className="text-[10px] text-gray-500 font-mono">{pair.maxLever}x max</span>
                        )}
                      </div>
                      <div className="flex items-center gap-2 mt-1 text-xs text-gray-500">
                        <span>Status: <span className={`font-medium ${
                          ['online', 'normal'].includes(pair.status) ? 'text-green-400' : 'text-yellow-400'
                        }`}>{pair.status}</span></span>
                      </div>
                    </div>
                  </div>

                  {/* Right: Countdown & dates */}
                  <div className="text-right shrink-0">
                    {pair.delisting_ts && (
                      <>
                        <div className={`text-2xl font-bold ${urgencyColor}`}>
                          {daysLeft === 0 ? 'TODAY' : daysLeft === 1 ? '1 day' : `${daysLeft} days`}
                        </div>
                        <div className="text-xs text-gray-400 mt-0.5">
                          {pair.delisting_date}
                        </div>
                      </>
                    )}
                  </div>
                </div>

                {/* Warning badges */}
                <div className="flex flex-wrap gap-2 mt-3">
                  {pair.delisting_ts && (
                    <span className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-red-500/15 text-red-300 border border-red-500/20">
                      <AlertTriangle className="w-3 h-3" />
                      Delisting: {pair.delisting_date}
                    </span>
                  )}
                  {pair.limit_open_time && (
                    <span className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-orange-500/15 text-orange-300 border border-orange-500/20">
                      <Shield className="w-3 h-3" />
                      No new positions after: {pair.limit_open_time}
                    </span>
                  )}
                  {pair.maintain_time && (
                    <span className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-yellow-500/15 text-yellow-300 border border-yellow-500/20">
                      <Clock className="w-3 h-3" />
                      Maintenance: {pair.maintain_time}
                    </span>
                  )}
                  {pair.futures_adjustment && (
                    <span className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-purple-500/15 text-purple-300 border border-purple-500/20">
                      Futures: {pair.futures_adjustment}
                    </span>
                  )}
                </div>

                {/* Actions row */}
                <div className="flex items-center gap-3 mt-3 pt-3 border-t border-gray-700/50">
                  <a
                    href={`https://www.bitget.com/spot/${pair.baseCoin}USDT`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 transition"
                  >
                    <ExternalLink className="w-3 h-3" /> View on Bitget
                  </a>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
