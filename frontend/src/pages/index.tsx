import Head from 'next/head'
import { useEffect, useState } from 'react'
import dynamic from 'next/dynamic'
import WalletBalance from '@/components/WalletBalance'
import SignalFeed from '@/components/SignalFeed'
import TradeHistory from '@/components/TradeHistory'
import CycleBadge from '@/components/CycleBadge'
import { useTradeStore } from '@/store/useTradeStore'
import { apiClient } from '@/services/api'
import { SMART_MONEY_CONCEPTS_STUDY_ID } from '@/utils/tradingviewStudies'

const TradingViewChart = dynamic(() => import('@/components/TradingViewChart'), {
  ssr: false,
})
const TradingViewWidget = dynamic(() => import('@/components/TradingViewWidget'), {
  ssr: false,
})

export default function Dashboard() {
  const [apiStatus, setApiStatus] = useState<{
    modules?: {
      trading?: string
      signals?: string
    }
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [exchangeStatus, setExchangeStatus] = useState<{
    initialized_count?: number
  } | null>(null)
  const { selectedSymbol, selectedExchange, selectedTimeframe } = useTradeStore()
  const [dashChartMode, setDashChartMode] = useState<'tradingview' | 'custom'>('tradingview')

  useEffect(() => {
    fetchStatus()
  }, [])

  const fetchStatus = async () => {
    try {
      const [apiRes, exchangeRes] = await Promise.all([
        apiClient.status(),
        apiClient.getExchangesStatus(),
      ])
      setApiStatus(apiRes.data)
      setExchangeStatus(exchangeRes.data)
    } catch (err) {
      console.error('Failed to fetch status:', err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <Head>
        <title>TradeBot - Dashboard</title>
        <meta name="description" content="AI-powered crypto trading bot" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        {/* Header */}
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h1 className="text-3xl font-bold text-white">Dashboard</h1>
            <p className="text-sm text-gray-400 mt-1">
              Overview of your trading activity
            </p>
          </div>
          <div className="ml-auto">
            <CycleBadge />
          </div>
        </div>

        {/* Status Cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatusCard
            title="API Status"
            value={loading ? 'Connecting...' : apiStatus ? 'Online' : 'Offline'}
            status={loading ? 'warning' : apiStatus ? 'success' : 'error'}
          />
          <StatusCard
            title="Exchanges"
            value={
              exchangeStatus
                ? `${exchangeStatus.initialized_count ?? 0} Active`
                : 'Unknown'
            }
            status={
              exchangeStatus && (exchangeStatus.initialized_count ?? 0) > 0
                ? 'success'
                : 'warning'
            }
          />
          <StatusCard
            title="Auto-Trading"
            value={
              apiStatus?.modules?.trading === 'ready' ? 'Ready' : 'Disabled'
            }
            status="warning"
            subtitle="DRY RUN"
          />
          <StatusCard
            title="Signals"
            value={
              apiStatus?.modules?.signals === 'ready' ? 'Active' : 'Unknown'
            }
            status={
              apiStatus?.modules?.signals === 'ready' ? 'success' : 'warning'
            }
          />
        </div>

        {/* Balances */}
        <WalletBalance />

        {/* Chart + Signals */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold">{selectedSymbol}</h2>
              <div className="flex items-center gap-2">
                <div className="flex bg-gray-900/60 rounded-md p-0.5">
                  <button
                    onClick={() => setDashChartMode('tradingview')}
                    className={`px-2 py-0.5 rounded text-[10px] font-medium transition ${
                      dashChartMode === 'tradingview'
                        ? 'bg-blue-600 text-white'
                        : 'text-gray-400 hover:text-white'
                    }`}
                  >
                    TV
                  </button>
                  <button
                    onClick={() => setDashChartMode('custom')}
                    className={`px-2 py-0.5 rounded text-[10px] font-medium transition ${
                      dashChartMode === 'custom'
                        ? 'bg-blue-600 text-white'
                        : 'text-gray-400 hover:text-white'
                    }`}
                  >
                    Custom
                  </button>
                </div>
                <span className="text-xs text-gray-500">{selectedTimeframe}</span>
              </div>
            </div>
            {dashChartMode === 'tradingview' ? (
              <TradingViewWidget
                symbol={selectedSymbol}
                exchange={selectedExchange}
                timeframe={selectedTimeframe}
                studies={[{ id: SMART_MONEY_CONCEPTS_STUDY_ID }]}
              />
            ) : (
              <TradingViewChart
                symbol={selectedSymbol}
                exchange={selectedExchange}
                timeframe={selectedTimeframe}
              />
            )}
          </div>
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <SignalFeed />
          </div>
        </div>

        {/* Recent Trades */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <TradeHistory />
        </div>
      </div>
    </>
  )
}

function StatusCard({
  title,
  value,
  status,
  subtitle,
}: {
  title: string
  value: string
  status: 'success' | 'warning' | 'error'
  subtitle?: string
}) {
  const colors = {
    success: 'border-green-500 bg-green-500/10',
    warning: 'border-yellow-500 bg-yellow-500/10',
    error: 'border-red-500 bg-red-500/10',
  }

  return (
    <div className={`p-4 rounded-lg border ${colors[status]}`}>
      <h3 className="text-xs text-gray-400 mb-1">{title}</h3>
      <div className="text-lg font-semibold capitalize">{value}</div>
      {subtitle && (
        <div className="text-xs text-gray-500 mt-1">{subtitle}</div>
      )}
    </div>
  )
}
