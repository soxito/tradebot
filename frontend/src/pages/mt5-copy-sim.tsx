import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  Copy,
  Plus,
  Trash2,
  Power,
  Loader2,
  AlertTriangle,
  CheckCircle,
} from 'lucide-react'

interface CopyProfile {
  id: number
  name: string
  source_account_id: number
  status: string
  allocation_mode: string
  allocation_value: number
  max_positions: number
  symbol_whitelist_json: string[] | null
  created_at: string
}

interface CopySimTrade {
  id: number
  profile_id: number
  source_ticket: string
  symbol: string
  direction: string
  volume: number
  entry_price: number
  exit_price: number | null
  pnl: number | null
  status: string
  opened_at: string
  closed_at: string | null
}

export default function MT5CopySimPage() {
  const [profiles, setProfiles] = useState<CopyProfile[]>([])
  const [selectedProfile, setSelectedProfile] = useState<number | null>(null)
  const [trades, setTrades] = useState<CopySimTrade[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [form, setForm] = useState({
    name: '',
    source_account_id: '',
    allocation_mode: 'fixed_lot',
    allocation_value: '0.01',
    max_positions: '10',
  })

  const fetchProfiles = useCallback(async () => {
    try {
      setLoading(true)
      const res = await apiClient.mt5.getCopyProfiles()
      setProfiles(res.data)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchTrades = useCallback(async () => {
    if (!selectedProfile) return
    try {
      const res = await apiClient.mt5.getCopySimTrades(selectedProfile)
      setTrades(res.data)
    } catch (e: any) {
      setError(e.message)
    }
  }, [selectedProfile])

  useEffect(() => { fetchProfiles() }, [fetchProfiles])
  useEffect(() => { fetchTrades() }, [fetchTrades])

  const handleCreate = async () => {
    try {
      await apiClient.mt5.createCopyProfile({
        name: form.name,
        source_account_id: parseInt(form.source_account_id),
        allocation_mode: form.allocation_mode,
        allocation_value: parseFloat(form.allocation_value),
        max_positions: parseInt(form.max_positions),
      })
      setShowAdd(false)
      setForm({ name: '', source_account_id: '', allocation_mode: 'fixed_lot', allocation_value: '0.01', max_positions: '10' })
      await fetchProfiles()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this copy profile?')) return
    try {
      await apiClient.mt5.deleteCopyProfile(id)
      if (selectedProfile === id) setSelectedProfile(null)
      await fetchProfiles()
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <>
      <Head><title>MT5 Copy Sim | TradeBot</title></Head>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Copy className="w-6 h-6 text-tradebot-accent" />
            <h1 className="text-2xl font-bold text-white">Copy Trading Simulator</h1>
          </div>
          <button onClick={() => setShowAdd(true)}
            className="flex items-center gap-2 px-4 py-2 bg-green-600/20 text-green-400 rounded-lg hover:bg-green-600/30 transition-colors">
            <Plus className="w-4 h-4" /> New Profile
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm">
            <AlertTriangle className="w-4 h-4" /> {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Add Profile */}
        {showAdd && (
          <div className="p-4 bg-gray-800 border border-gray-700 rounded-lg space-y-3">
            <h3 className="text-white font-medium">New Copy Profile</h3>
            <div className="grid grid-cols-5 gap-3">
              <input placeholder="Name" value={form.name}
                onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              <input placeholder="Source Account ID" type="number" value={form.source_account_id}
                onChange={e => setForm(p => ({ ...p, source_account_id: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              <select value={form.allocation_mode}
                onChange={e => setForm(p => ({ ...p, allocation_mode: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm">
                <option value="fixed_lot">Fixed Lot</option>
                <option value="risk_percent">Risk %</option>
                <option value="multiplier">Multiplier</option>
              </select>
              <input placeholder="Allocation Value" type="number" step="0.01" value={form.allocation_value}
                onChange={e => setForm(p => ({ ...p, allocation_value: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              <input placeholder="Max Positions" type="number" value={form.max_positions}
                onChange={e => setForm(p => ({ ...p, max_positions: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            </div>
            <div className="flex gap-2">
              <button onClick={handleCreate} className="px-4 py-2 bg-green-600 text-white rounded text-sm hover:bg-green-700">Create</button>
              <button onClick={() => setShowAdd(false)} className="px-4 py-2 bg-gray-700 text-gray-300 rounded text-sm hover:bg-gray-600">Cancel</button>
            </div>
          </div>
        )}

        {/* Profiles */}
        <div className="grid grid-cols-2 gap-4">
          {profiles.map(p => (
            <div key={p.id}
              onClick={() => setSelectedProfile(p.id)}
              className={`bg-gray-800/50 border rounded-xl p-4 cursor-pointer transition-colors ${
                selectedProfile === p.id ? 'border-tradebot-accent/50' : 'border-gray-700/50 hover:border-gray-600'
              }`}
            >
              <div className="flex items-center justify-between mb-2">
                <span className="text-white font-medium">{p.name}</span>
                <div className="flex items-center gap-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${
                    p.status === 'active' ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400'
                  }`}>{p.status}</span>
                  <button onClick={(e) => { e.stopPropagation(); handleDelete(p.id) }}
                    className="text-red-400 hover:text-red-300"><Trash2 className="w-3 h-3" /></button>
                </div>
              </div>
              <div className="text-gray-400 text-xs space-x-4">
                <span>Mode: {p.allocation_mode}</span>
                <span>Value: {p.allocation_value}</span>
                <span>Max: {p.max_positions}</span>
              </div>
            </div>
          ))}
        </div>

        {/* Simulated Trades */}
        {selectedProfile && trades.length > 0 && (
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-700/50 text-white font-medium">
              Simulated Trades ({trades.length})
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-gray-400 text-xs border-b border-gray-700/30">
                  <tr>
                    <th className="text-left px-4 py-2">Symbol</th>
                    <th className="text-left px-4 py-2">Direction</th>
                    <th className="text-right px-4 py-2">Volume</th>
                    <th className="text-right px-4 py-2">Entry</th>
                    <th className="text-right px-4 py-2">Exit</th>
                    <th className="text-right px-4 py-2">P&L</th>
                    <th className="text-center px-4 py-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map(t => (
                    <tr key={t.id} className="border-b border-gray-700/20 hover:bg-gray-700/20">
                      <td className="px-4 py-2 text-white font-medium">{t.symbol}</td>
                      <td className={`px-4 py-2 ${t.direction === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                        {t.direction.toUpperCase()}
                      </td>
                      <td className="px-4 py-2 text-right text-gray-300">{t.volume}</td>
                      <td className="px-4 py-2 text-right text-gray-300">{t.entry_price}</td>
                      <td className="px-4 py-2 text-right text-gray-300">{t.exit_price ?? '-'}</td>
                      <td className={`px-4 py-2 text-right font-medium ${(t.pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {t.pnl != null ? `$${t.pnl.toFixed(2)}` : '-'}
                      </td>
                      <td className="px-4 py-2 text-center">
                        <span className={`px-2 py-0.5 rounded text-xs ${
                          t.status === 'open' ? 'bg-blue-900/30 text-blue-400' : 'bg-gray-700 text-gray-400'
                        }`}>{t.status}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {loading && (
          <div className="flex justify-center py-8">
            <Loader2 className="w-6 h-6 animate-spin text-tradebot-accent" />
          </div>
        )}
      </div>
    </>
  )
}
