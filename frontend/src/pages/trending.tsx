import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  TrendingUp,
  TrendingDown,
  RefreshCw,
  Star,
  ArrowUpRight,
  ArrowDownRight,
  ExternalLink,
  Radio,
  Flame,
} from 'lucide-react'
import { formatTimeZA } from '@/utils/datetime'
import ResearchEntries, { ResearchVerdictBadge } from '@/components/research/ResearchEntries'
import { useResearchPlans } from '@/hooks/useResearchPlans'

interface TrendingCoin {
  id: string
  name: string
  symbol: string
  market_cap_rank: number | null
  thumb: string
  small: string
  price_btc: number
  price_usd: string | number
  price_change_24h: number
  market_cap: string
  total_volume: string
  sparkline: string
  score: number
}

interface MonitorPairDetail {
  symbol: string
  source: string
}

export default function TrendingPage() {
  const [trending, setTrending] = useState<TrendingCoin[]>([])
  const [gainers, setGainers] = useState<TrendingCoin[]>([])
  const [losers, setLosers] = useState<TrendingCoin[]>([])
  const [monitoredPairs, setMonitoredPairs] = useState<MonitorPairDetail[]>([])
  const [trendingCount, setTrendingCount] = useState(0)
  const [userCount, setUserCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)

  // Research is per pair, not per signal — one plan covers whatever signals the
  // app currently holds on this coin.
  const { planFor } = useResearchPlans(trending.map((c) => c.symbol))

  const fetchData = useCallback(async (showRefresh = false) => {
    if (showRefresh) setRefreshing(true)
    else setLoading(true)
    try {
      const [trendRes, pairsRes] = await Promise.all([
        apiClient.getTrendingCoins(),
        apiClient.getSignalMonitorPairs(),
      ])
      setTrending(trendRes.data.trending || [])
      setGainers(trendRes.data.top_gainers || [])
      setLosers(trendRes.data.top_losers || [])
      setMonitoredPairs(pairsRes.data.pair_details || [])
      setTrendingCount(pairsRes.data.trending_count || 0)
      setUserCount(pairsRes.data.user_count || 0)
      setLastUpdated(formatTimeZA(new Date()))
    } catch (err) {
      console.error('Failed to fetch trending data:', err)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(() => fetchData(true), 60_000)
    return () => clearInterval(interval)
  }, [fetchData])

  const monitoredTrendingSymbols = new Set(
    monitoredPairs.filter((p) => p.source === 'trending').map((p) => p.symbol)
  )
  const monitoredUserSymbols = new Set(
    monitoredPairs.filter((p) => p.source === 'user').map((p) => p.symbol)
  )

  const formatPrice = (price: string | number): string => {
    const num = typeof price === 'string' ? parseFloat(price.replace(/[^0-9.-]/g, '')) : price
    if (isNaN(num)) return '$0.00'
    if (num >= 1) return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    if (num >= 0.01) return `$${num.toFixed(4)}`
    if (num >= 0.0001) return `$${num.toFixed(6)}`
    return `$${num.toFixed(8)}`
  }

  const formatVolume = (volume: string | number): string => {
    const num = typeof volume === 'string' ? parseFloat(volume.replace(/[^0-9.-]/g, '')) : volume
    if (isNaN(num)) return '-'
    return `$${Math.round(num).toLocaleString()}`
  }

  const getStatusBadge = (symbol: string) => {
    const pair = `${symbol}/USDT`
    if (monitoredTrendingSymbols.has(pair)) {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
          <Radio className="w-3 h-3" /> Auto-Monitoring
        </span>
      )
    }
    if (monitoredUserSymbols.has(pair)) {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-blue-500/20 text-blue-400 border border-blue-500/30">
          <Star className="w-3 h-3" /> User Pair
        </span>
      )
    }
    return null
  }

  if (loading) {
    return (
      <>
        <Head><title>TradeBot - Trending</title></Head>
        <div className="flex items-center justify-center h-64">
          <RefreshCw className="w-8 h-8 text-gray-400 animate-spin" />
        </div>
      </>
    )
  }

  return (
    <>
      <Head><title>TradeBot - Trending</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white flex items-center gap-3">
              <Flame className="w-8 h-8 text-orange-400" />
              Trending Tokens
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Top trending coins on CoinGecko — auto-synced into your signal pipeline every 3 minutes
            </p>
          </div>
          <div className="flex items-center gap-3">
            {lastUpdated && (
              <span className="text-xs text-gray-500">Updated {lastUpdated}</span>
            )}
            <button
              onClick={() => fetchData(true)}
              disabled={refreshing}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-600 rounded-lg text-sm text-gray-300 transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
              Refresh
            </button>
          </div>
        </div>

        {/* Stats Bar */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-3 text-center">
            <p className="text-2xl font-bold text-orange-400">{trending.length}</p>
            <p className="text-xs text-gray-400">Trending Coins</p>
          </div>
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-3 text-center">
            <p className="text-2xl font-bold text-emerald-400">{trendingCount}</p>
            <p className="text-xs text-gray-400">Auto-Monitored</p>
          </div>
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-3 text-center">
            <p className="text-2xl font-bold text-blue-400">{userCount}</p>
            <p className="text-xs text-gray-400">User Pairs</p>
          </div>
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-3 text-center">
            <p className="text-2xl font-bold text-white">{trendingCount + userCount}</p>
            <p className="text-xs text-gray-400">Total Monitored</p>
          </div>
        </div>

        {/* Trending Coins Grid */}
        <div>
          <h2 className="text-lg font-semibold text-white mb-3 flex items-center gap-2">
            <TrendingUp className="w-5 h-5 text-orange-400" />
            Trending on CoinGecko
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {trending.map((coin, idx) => {
              const changeNum = typeof coin.price_change_24h === 'number' ? coin.price_change_24h : 0
              const isPositive = changeNum >= 0
              return (
                <div
                  key={coin.id}
                  className="bg-gray-800/40 border border-gray-700/50 rounded-lg p-4 hover:border-gray-600 transition-colors"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-3">
                      <div className="relative">
                        <img
                          src={coin.small || coin.thumb}
                          alt={coin.name}
                          className="w-10 h-10 rounded-full bg-gray-700"
                          onError={(e) => {
                            (e.target as HTMLImageElement).style.display = 'none'
                          }}
                        />
                        <span className="absolute -top-1 -left-1 w-5 h-5 bg-gray-900 border border-gray-600 rounded-full flex items-center justify-center text-[10px] font-bold text-orange-400">
                          {idx + 1}
                        </span>
                      </div>
                      <div>
                        <p className="font-semibold text-white text-sm">{coin.name}</p>
                        <p className="text-xs text-gray-400">{coin.symbol}</p>
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      {coin.market_cap_rank && (
                        <span className="text-[10px] text-gray-500">#{coin.market_cap_rank}</span>
                      )}
                      {getStatusBadge(coin.symbol)}
                      <ResearchVerdictBadge plan={planFor(coin.symbol)} />
                    </div>
                  </div>

                  <div className="mt-3 flex items-end justify-between">
                    <div>
                      <p className="text-lg font-bold text-white">{formatPrice(coin.price_usd)}</p>
                      <div className={`flex items-center gap-1 text-xs font-medium ${isPositive ? 'text-emerald-400' : 'text-red-400'}`}>
                        {isPositive ? (
                          <ArrowUpRight className="w-3.5 h-3.5" />
                        ) : (
                          <ArrowDownRight className="w-3.5 h-3.5" />
                        )}
                        {Math.abs(changeNum).toFixed(2)}%
                      </div>
                    </div>
                    <div className="text-right">
                      {coin.market_cap && (
                        <p className="text-[10px] text-gray-500">MCap {coin.market_cap}</p>
                      )}
                      {coin.total_volume && (
                        <p className="text-[10px] text-gray-500">Vol {coin.total_volume}</p>
                      )}
                    </div>
                  </div>

                  {coin.sparkline && (
                    <div className="mt-2 -mx-1">
                      <img
                        src={coin.sparkline}
                        alt={`${coin.symbol} sparkline`}
                        className="w-full h-8 opacity-50"
                        style={{ filter: isPositive ? 'hue-rotate(100deg) brightness(1.5)' : 'hue-rotate(0deg) brightness(1.2)' }}
                      />
                    </div>
                  )}

                  {/* Every live signal on this pair, reconciled into two
                      costed entries. Absent until the pair has been researched. */}
                  {planFor(coin.symbol) && (
                    <div className="mt-2">
                      <ResearchEntries plan={planFor(coin.symbol)} compact />
                    </div>
                  )}

                  <div className="mt-2 flex items-center justify-between">
                    <a
                      href={`https://www.coingecko.com/en/coins/${coin.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[10px] text-gray-500 hover:text-gray-300 flex items-center gap-1 transition-colors"
                    >
                      CoinGecko <ExternalLink className="w-3 h-3" />
                    </a>
                    <p className="text-[10px] text-gray-600">
                      {coin.price_btc.toFixed(10)} BTC
                    </p>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Top Gainers */}
        {gainers.length > 0 && (
          <div>
            <h2 className="text-lg font-semibold text-white mb-3 flex items-center gap-2">
              <TrendingUp className="w-5 h-5 text-emerald-400" />
              Top Gainers (24h)
            </h2>
            <div className="bg-gray-800/30 border border-gray-700/50 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-800/60">
                  <tr>
                    <th className="text-left py-2 px-4 text-gray-400 font-medium text-xs">#</th>
                    <th className="text-left py-2 px-4 text-gray-400 font-medium text-xs">Coin</th>
                    <th className="text-right py-2 px-4 text-gray-400 font-medium text-xs">Price</th>
                    <th className="text-right py-2 px-4 text-gray-400 font-medium text-xs">24h Change</th>
                    <th className="text-right py-2 px-4 text-gray-400 font-medium text-xs">Volume</th>
                  </tr>
                </thead>
                <tbody>
                  {gainers.map((coin, idx) => (
                    <tr key={coin.id} className="border-t border-gray-700/30 hover:bg-gray-800/30">
                      <td className="py-2 px-4 text-gray-500 text-xs">{idx + 1}</td>
                      <td className="py-2 px-4">
                        <div className="flex items-center gap-2">
                          <img src={coin.thumb} alt={coin.name} className="w-6 h-6 rounded-full" />
                          <span className="text-white font-medium">{coin.symbol}</span>
                          <span className="text-gray-500 text-xs">{coin.name}</span>
                          {getStatusBadge(coin.symbol)}
                      <ResearchVerdictBadge plan={planFor(coin.symbol)} />
                        </div>
                      </td>
                      <td className="py-2 px-4 text-right text-white">{formatPrice(coin.price_usd)}</td>
                      <td className="py-2 px-4 text-right text-emerald-400 font-medium">
                        +{typeof coin.price_change_24h === 'number' ? coin.price_change_24h.toFixed(2) : '0.00'}%
                      </td>
                      <td className="py-2 px-4 text-right text-gray-400">
                        {formatVolume(coin.total_volume)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Top Losers */}
        {losers.length > 0 && (
          <div>
            <h2 className="text-lg font-semibold text-white mb-3 flex items-center gap-2">
              <TrendingDown className="w-5 h-5 text-red-400" />
              Top Losers (24h)
            </h2>
            <div className="bg-gray-800/30 border border-gray-700/50 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-800/60">
                  <tr>
                    <th className="text-left py-2 px-4 text-gray-400 font-medium text-xs">#</th>
                    <th className="text-left py-2 px-4 text-gray-400 font-medium text-xs">Coin</th>
                    <th className="text-right py-2 px-4 text-gray-400 font-medium text-xs">Price</th>
                    <th className="text-right py-2 px-4 text-gray-400 font-medium text-xs">24h Change</th>
                    <th className="text-right py-2 px-4 text-gray-400 font-medium text-xs">Volume</th>
                  </tr>
                </thead>
                <tbody>
                  {losers.map((coin, idx) => (
                    <tr key={coin.id} className="border-t border-gray-700/30 hover:bg-gray-800/30">
                      <td className="py-2 px-4 text-gray-500 text-xs">{idx + 1}</td>
                      <td className="py-2 px-4">
                        <div className="flex items-center gap-2">
                          <img src={coin.thumb} alt={coin.name} className="w-6 h-6 rounded-full" />
                          <span className="text-white font-medium">{coin.symbol}</span>
                          <span className="text-gray-500 text-xs">{coin.name}</span>
                          {getStatusBadge(coin.symbol)}
                      <ResearchVerdictBadge plan={planFor(coin.symbol)} />
                        </div>
                      </td>
                      <td className="py-2 px-4 text-right text-white">{formatPrice(coin.price_usd)}</td>
                      <td className="py-2 px-4 text-right text-red-400 font-medium">
                        {typeof coin.price_change_24h === 'number' ? coin.price_change_24h.toFixed(2) : '0.00'}%
                      </td>
                      <td className="py-2 px-4 text-right text-gray-400">
                        {formatVolume(coin.total_volume)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Auto-Monitored Pairs List */}
        <div className="bg-gray-800/30 border border-gray-700/50 rounded-lg p-4">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Radio className="w-4 h-4 text-emerald-400" />
            Auto-Monitored Trending Pairs
            <span className="text-xs text-gray-500 font-normal">
              — added to signals automatically, removed when no longer trending
            </span>
          </h2>
          <div className="flex flex-wrap gap-2">
            {monitoredPairs
              .filter((p) => p.source === 'trending')
              .map((p) => (
                <span
                  key={p.symbol}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                >
                  <Flame className="w-3 h-3" />
                  {p.symbol}
                </span>
              ))}
            {monitoredPairs.filter((p) => p.source === 'trending').length === 0 && (
              <p className="text-xs text-gray-500">No trending pairs currently monitored. Next sync in up to 3 minutes.</p>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
