import Head from 'next/head'
import TradeHistory from '@/components/TradeHistory'

export default function HistoryPage() {
  return (
    <>
      <Head><title>TradeBot - Trade History</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        <div>
          <h1 className="text-3xl font-bold text-white">Trade History</h1>
          <p className="text-sm text-gray-400 mt-1">
            All executed, pending, and failed trades
          </p>
        </div>

        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <TradeHistory />
        </div>
      </div>
    </>
  )
}
