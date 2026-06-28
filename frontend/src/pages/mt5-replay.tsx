import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import { formatDateZA } from '@/utils/datetime'
import {
  Rewind,
  Play,
  Loader2,
  AlertTriangle,
  TrendingUp,
  TrendingDown,
  BarChart2,
} from 'lucide-react'

interface ReplayRun {
  id: number
  account_id: number
  name: string
  symbol_filter: string | null
  from_date: string
  to_date: string
  status: string
  total_trades: number | null
  net_pnl: number | null
  max_drawdown_pct: number | null
  win_rate: number | null
  sharpe_ratio: number | null
  equity_curve_json: number[] | null
  created_at: string
}

export default function MT5ReplayPage() {
  const [runs, setRuns] = useState<ReplayRun[]>([])
  const [selectedRun, setSelectedRun] = useState<ReplayRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({
    account_id: '',
    name: '',
    from_date: '',
    to_date: '',
    symbol_filter: '',
  })

  const fetchRuns = useCallback(async () => {
    try {
      setLoading(true)
      const res = await apiClient.mt5.getReplayRuns()
      setRuns(res.data)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchRuns() }, [fetchRuns])

  const handleCreate = async () => {
    setCreating(true)
    try {
      await apiClient.mt5.createReplayRun({
        account_id: parseInt(form.account_id),
        name: form.name || `Replay ${formatDateZA(new Date())}`,
        from_date: form.from_date,
        to_date: form.to_date,
        symbol_filter: form.symbol_filter || undefined,
      })
      await fetchRuns()
      setForm({ account_id: '', name: '', from_date: '', to_date: '', symbol_filter: '' })
    } catch (e: any) {
      setError(e.message)
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <Head><title>MT5 Trade Replay | TradeBot</title></Head>
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <Rewind className="w-6 h-6 text-tradebot-accent" />
          <h1 className="text-2xl font-bold text-white">Trade Replay</h1>
        </div>

        {error && (
          <div className="flex items-center gap-2 p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm">
            <AlertTriangle className="w-4 h-4" /> {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Create Replay */}
        <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl p-4 space-y-3">
          <h3 className="text-white font-medium">New Replay Run</h3>
          <div className="grid grid-cols-5 gap-3">
            <input placeholder="Account ID" type="number" value={form.account_id}
              onChange={e => setForm(p => ({ ...p, account_id: e.target.value }))}
              className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            <input placeholder="Name" value={form.name}
              onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
              className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            <input type="date" value={form.from_date}
              onChange={e => setForm(p => ({ ...p, from_date: e.target.value }))}
              className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            <input type="date" value={form.to_date}
              onChange={e => setForm(p => ({ ...p, to_date: e.target.value }))}
              className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            <input placeholder="Symbol filter" value={form.symbol_filter}
              onChange={e => setForm(p => ({ ...p, symbol_filter: e.target.value }))}
              className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
          </div>
          <button onClick={handleCreate} disabled={creating || !form.account_id || !form.from_date || !form.to_date}
            className="flex items-center gap-2 px-4 py-2 bg-tradebot-accent text-white rounded text-sm hover:bg-tradebot-accent/80 disabled:opacity-50">
            {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            Run Replay
          </button>
        </div>

        {/* Replay Runs */}
        <div className="grid gap-4">
          {runs.map(run => (
            <div key={run.id}
              onClick={() => setSelectedRun(run)}
              className={`bg-gray-800/50 border rounded-xl p-4 cursor-pointer transition-colors ${
                selectedRun?.id === run.id ? 'border-tradebot-accent/50' : 'border-gray-700/50 hover:border-gray-600'
              }`}
            >
              <div className="flex items-center justify-between mb-3">
                <div>
                  <span className="text-white font-medium">{run.name}</span>
                  <span className="text-gray-500 text-xs ml-2">{run.from_date} → {run.to_date}</span>
                  {run.symbol_filter && <span className="text-yellow-400 text-xs ml-2">{run.symbol_filter}</span>}
                </div>
                <span className={`px-2 py-0.5 rounded text-xs ${
                  run.status === 'completed' ? 'bg-green-900/30 text-green-400' :
                  run.status === 'running' ? 'bg-blue-900/30 text-blue-400' :
                  'bg-gray-700 text-gray-400'
                }`}>{run.status}</span>
              </div>

              {run.status === 'completed' && (
                <div className="grid grid-cols-5 gap-4 text-sm">
                  <div>
                    <span className="text-gray-500 text-xs">Trades</span>
                    <div className="text-white font-medium">{run.total_trades ?? '-'}</div>
                  </div>
                  <div>
                    <span className="text-gray-500 text-xs">Net P&L</span>
                    <div className={`font-medium ${(run.net_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {run.net_pnl != null ? `$${run.net_pnl.toFixed(2)}` : '-'}
                    </div>
                  </div>
                  <div>
                    <span className="text-gray-500 text-xs">Win Rate</span>
                    <div className="text-white font-medium">{run.win_rate != null ? `${(run.win_rate * 100).toFixed(1)}%` : '-'}</div>
                  </div>
                  <div>
                    <span className="text-gray-500 text-xs">Max DD</span>
                    <div className="text-red-400 font-medium">{run.max_drawdown_pct != null ? `${run.max_drawdown_pct.toFixed(1)}%` : '-'}</div>
                  </div>
                  <div>
                    <span className="text-gray-500 text-xs">Sharpe</span>
                    <div className="text-white font-medium">{run.sharpe_ratio != null ? run.sharpe_ratio.toFixed(2) : '-'}</div>
                  </div>
                </div>
              )}
            </div>
          ))}

          {runs.length === 0 && !loading && (
            <div className="text-center text-gray-500 py-12">No replay runs yet. Create one above.</div>
          )}
        </div>

        {loading && (
          <div className="flex justify-center py-8">
            <Loader2 className="w-6 h-6 animate-spin text-tradebot-accent" />
          </div>
        )}
      </div>
    </>
  )
}
