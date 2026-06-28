import Head from 'next/head'
import BitgetFutures from '@/components/BitgetFutures'

export default function FuturesPage() {
  return (
    <>
      <Head><title>TradeBot - Futures</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        <div>
          <h1 className="text-3xl font-bold text-white">Futures Trading</h1>
          <p className="text-sm text-gray-400 mt-1">
            Manage futures positions, leverage, and margin settings
          </p>
        </div>

        <BitgetFutures />
      </div>
    </>
  )
}
