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
  Bot,
  Users,
  Activity,
  RefreshCw,
} from 'lucide-react'

interface MT5Account {
  id: number
  name: string
  login: number | string
  server?: string
  balance?: number
}

interface CopyProfile {
  id: number
  name: string
  mode: 'sim' | 'live' | string
  source_account_id: number | null
  allocation_mode: string
  allocation_value: number
  max_open_positions: number
  symbol_whitelist: string[] | null
  enabled: boolean
  paper_balance: number
  paper_equity: number
  created_at: string
}

interface CopySimTrade {
  id: number
  symbol: string
  side: string
  qty_sim: number
  entry_time: string | null
  entry_price: number | null
  exit_time: string | null
  exit_price: number | null
  pnl_sim: number
  status: string
}

interface CopyPerformance {
  total_trades: number
  total_pnl: number
  win_rate: number
  avg_pnl: number
  drawdown_pct?: number
}

interface CopyFollower {
  id: number
  copy_profile_id: number
  account_id: number
  enabled: boolean
  allocation_mode: string
  allocation_value: number
  max_open_positions: number
  copied_tickets: Record<string, number> | null
  last_error: string | null
  created_at: string
}

interface SupervisorDecision {
  id: number
  symbol: string
  action: string
  reasoning: string
  confidence: number
  created_at: string | null
}

const EMPTY_FORM = {
  name: '',
  source_account_id: '',
  mode: 'sim',
  allocation_mode: 'fixed_lot',
  allocation_value: '0.01',
  max_open_positions: '10',
}

export default function MT5CopySimPage() {
  const [accounts, setAccounts] = useState<MT5Account[]>([])
  const [profiles, setProfiles] = useState<CopyProfile[]>([])
  const [selectedProfile, setSelectedProfile] = useState<number | null>(null)
  const [trades, setTrades] = useState<CopySimTrade[]>([])
  const [performance, setPerformance] = useState<CopyPerformance | null>(null)
  const [followers, setFollowers] = useState<CopyFollower[]>([])
  const [decisions, setDecisions] = useState<SupervisorDecision[]>([])
  const [roomManaging, setRoomManaging] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)
  const [followerForm, setFollowerForm] = useState({ account_id: '', allocation_mode: 'multiplier', allocation_value: '1.0', max_open_positions: '10' })

  const fetchAccounts = useCallback(async () => {
    try {
      const res = await apiClient.mt5.getAccounts()
      setAccounts(res.data ?? [])
    } catch (e: any) {
      setError(e.message)
    }
  }, [])

  const fetchProfiles = useCallback(async () => {
    try {
      const res = await apiClient.mt5.getCopyProfiles()
      setProfiles(res.data ?? [])
    } catch (e: any) {
      setError(e.message)
    }
  }, [])

  const fetchDetail = useCallback(async () => {
    if (!selectedProfile) return
    try {
      const [t, p, f] = await Promise.all([
        apiClient.mt5.getCopySimTrades(selectedProfile),
        apiClient.mt5.getCopyPerformance(selectedProfile),
        apiClient.mt5.getCopyFollowers(selectedProfile),
      ])
      setTrades(t.data ?? [])
      setPerformance(p.data ?? null)
      setFollowers(f.data ?? [])
    } catch (e: any) {
      setError(e.message)
    }
  }, [selectedProfile])

  const fetchRoomOverview = useCallback(async () => {
    try {
      const res = await apiClient.mt5.getRoomCopyOverview()
      setDecisions(res.data?.decisions ?? [])
      setRoomManaging(!!res.data?.manage_copy_profiles)
    } catch {
      // room overview is optional — never block the page on it
    }
  }, [])

  useEffect(() => {
    fetchAccounts(); fetchProfiles(); fetchRoomOverview()
  }, [fetchAccounts, fetchProfiles, fetchRoomOverview])
  useEffect(() => { fetchDetail() }, [fetchDetail])

  // Light polling keeps balances/trades/agent actions fresh.
  useEffect(() => {
    const t = setInterval(() => { fetchProfiles(); fetchDetail(); fetchRoomOverview() }, 15000)
    return () => clearInterval(t)
  }, [fetchProfiles, fetchDetail, fetchRoomOverview])

  const handleCreate = async () => {
    try {
      await apiClient.mt5.createCopyProfile({
        name: form.name || `Copy of account ${form.source_account_id}`,
        source_account_id: parseInt(form.source_account_id),
        mode: form.mode,
        allocation_mode: form.allocation_mode,
        allocation_value: parseFloat(form.allocation_value),
        max_open_positions: parseInt(form.max_open_positions),
      })
      setShowAdd(false)
      setForm(EMPTY_FORM)
      await fetchProfiles()
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    }
  }

  const handleToggle = async (id: number) => {
    try {
      await apiClient.mt5.toggleCopyProfile(id)
      await fetchProfiles()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this copy profile and all its sim trades/followers?')) return
    try {
      await apiClient.mt5.deleteCopyProfile(id)
      if (selectedProfile === id) setSelectedProfile(null)
      await fetchProfiles()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const handleAddFollower = async () => {
    if (!selectedProfile) return
    try {
      await apiClient.mt5.addCopyFollower(selectedProfile, {
        account_id: parseInt(followerForm.account_id),
        allocation_mode: followerForm.allocation_mode,
        allocation_value: parseFloat(followerForm.allocation_value),
        max_open_positions: parseInt(followerForm.max_open_positions),
      })
      setFollowerForm({ account_id: '', allocation_mode: 'multiplier', allocation_value: '1.0', max_open_positions: '10' })
      await fetchDetail()
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    }
  }

  const handleToggleFollower = async (f: CopyFollower) => {
    try {
      await apiClient.mt5.updateCopyFollower(f.id, { enabled: !f.enabled })
      await fetchDetail()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const handleDeleteFollower = async (id: number) => {
    try {
      await apiClient.mt5.deleteCopyFollower(id)
      await fetchDetail()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const accountLabel = (id: number | null) => {
    if (!id) return '—'
    const a = accounts.find(x => x.id === id)
    return a ? `${a.name} (${a.login})` : `#${id}`
  }

  const selected = profiles.find(p => p.id === selectedProfile) ?? null

  return (
    <>
      <Head><title>MT5 Copy Sim | TradeBot</title></Head>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Copy className="w-6 h-6 text-tradebot-accent" />
            <h1 className="text-2xl font-bold text-white">Copy Trading</h1>
            {roomManaging && (
              <span className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-purple-900/40 text-purple-300 border border-purple-700/50">
                <Bot className="w-3 h-3" /> Managed by Trading Room
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => { fetchProfiles(); fetchDetail(); fetchRoomOverview() }}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors">
              <RefreshCw className="w-4 h-4" />
            </button>
            <button onClick={() => setShowAdd(true)}
              className="flex items-center gap-2 px-4 py-2 bg-green-600/20 text-green-400 rounded-lg hover:bg-green-600/30 transition-colors">
              <Plus className="w-4 h-4" /> New Profile
            </button>
          </div>
        </div>

        {error && (
          <div className="flex items-center gap-2 p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm">
            <AlertTriangle className="w-4 h-4 shrink-0" /> <span>{error}</span>
            <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Add Profile */}
        {showAdd && (
          <div className="p-4 bg-gray-800 border border-gray-700 rounded-lg space-y-3">
            <h3 className="text-white font-medium">New Copy Profile</h3>
            <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
              <input placeholder="Profile name" value={form.name}
                onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm col-span-2" />
              <select value={form.source_account_id}
                onChange={e => setForm(p => ({ ...p, source_account_id: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm col-span-2">
                <option value="">Select source account…</option>
                {accounts.map(a => (
                  <option key={a.id} value={a.id}>{a.name} — {a.login}{a.server ? ` @ ${a.server}` : ''}</option>
                ))}
              </select>
              <select value={form.mode}
                onChange={e => setForm(p => ({ ...p, mode: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm">
                <option value="sim">Sim (paper)</option>
                <option value="live">Live (real orders)</option>
              </select>
              <select value={form.allocation_mode}
                onChange={e => setForm(p => ({ ...p, allocation_mode: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm">
                <option value="fixed_lot">Fixed Lot</option>
                <option value="risk_percent">Risk %</option>
                <option value="multiplier">Multiplier</option>
              </select>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
              <input placeholder="Allocation value" type="number" step="0.01" value={form.allocation_value}
                onChange={e => setForm(p => ({ ...p, allocation_value: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
                <input placeholder="Max positions" type="number" value={form.max_open_positions}
                onChange={e => setForm(p => ({ ...p, max_positions: e.target.value }))}
                className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            </div>
            <div className="flex gap-2">
              <button onClick={handleCreate} disabled={!form.source_account_id}
                className="px-4 py-2 bg-green-600 text-white rounded text-sm hover:bg-green-700 disabled:opacity-40 disabled:cursor-not-allowed">Create</button>
              <button onClick={() => setShowAdd(false)} className="px-4 py-2 bg-gray-700 text-gray-300 rounded text-sm hover:bg-gray-600">Cancel</button>
            </div>
          </div>
        )}

        {/* Profiles */}
        {profiles.length === 0 && !loading && (
          <div className="p-8 text-center bg-gray-800/30 border border-gray-700/40 rounded-xl text-gray-400 text-sm">
            No copy profiles yet. Create one to start mirroring an existing MT5 account.
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {profiles.map(p => (
            <div key={p.id}
              onClick={() => setSelectedProfile(p.id)}
              className={`bg-gray-800/50 border rounded-xl p-4 cursor-pointer transition-colors ${
                selectedProfile === p.id ? 'border-tradebot-accent/50' : 'border-gray-700/50 hover:border-gray-600'
              }`}>
              <div className="flex items-center justify-between mb-2">
                <span className="text-white font-medium">{p.name}</span>
                <div className="flex items-center gap-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${
                    p.mode === 'live' ? 'bg-orange-900/40 text-orange-400' : 'bg-blue-900/30 text-blue-400'
                  }`}>{p.mode === 'live' ? 'LIVE' : 'SIM'}</span>
                  <span className={`px-2 py-0.5 rounded text-xs ${
                    p.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400'
                  }`}>{p.enabled ? 'active' : 'paused'}</span>
                  <button title={p.enabled ? 'Pause' : 'Activate'} onClick={(e) => { e.stopPropagation(); handleToggle(p.id) }}
                    className={`${p.enabled ? 'text-green-400 hover:text-green-300' : 'text-gray-500 hover:text-gray-300'}`}>
                    <Power className="w-3.5 h-3.5" />
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); handleDelete(p.id) }}
                    className="text-red-400 hover:text-red-300"><Trash2 className="w-3 h-3" /></button>
                </div>
              </div>
              <div className="text-gray-400 text-xs space-x-3 mb-2">
                <span>Source: {accountLabel(p.source_account_id)}</span>
              </div>
              <div className="text-gray-500 text-xs space-x-3">
                <span>{p.allocation_mode}: {p.allocation_value}</span>
                <span>Max: {p.max_open_positions}</span>
                {p.mode === 'sim' && (
                  <>
                    <span>Bal: ${p.paper_balance.toFixed(2)}</span>
                    <span>Eq: ${p.paper_equity.toFixed(2)}</span>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Selected profile detail */}
        {selected && (
          <>
            {/* Performance */}
            <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-700/50 flex items-center gap-2 text-white font-medium">
                <Activity className="w-4 h-4 text-tradebot-accent" />
                Performance — {selected.name}
              </div>
              {performance ? (
                <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-gray-700/30">
                  <div className="p-4 text-center">
                    <div className="text-xs text-gray-500 mb-1">Closed Trades</div>
                    <div className="text-xl text-white font-semibold">{performance.total_trades}</div>
                  </div>
                  <div className="p-4 text-center">
                    <div className="text-xs text-gray-500 mb-1">Total P&L</div>
                    <div className={`text-xl font-semibold ${(performance.total_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      ${performance.total_pnl?.toFixed(2)}
                    </div>
                  </div>
                  <div className="p-4 text-center">
                    <div className="text-xs text-gray-500 mb-1">Win Rate</div>
                    <div className="text-xl text-white font-semibold">{performance.win_rate}%</div>
                  </div>
                  <div className="p-4 text-center">
                    <div className="text-xs text-gray-500 mb-1">Avg P&L / Trade</div>
                    <div className={`text-xl font-semibold ${(performance.avg_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      ${performance.avg_pnl?.toFixed(2)}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="p-4 text-gray-500 text-sm">Loading performance…</div>
              )}
            </div>

            {/* Followers (live mode) */}
            {selected.mode === 'live' && (
              <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-700/50 flex items-center gap-2 text-white font-medium">
                  <Users className="w-4 h-4 text-tradebot-accent" /> Follower Accounts ({followers.length})
                  <span className="text-xs text-orange-400 ml-auto">Real execution — trades open on these accounts</span>
                </div>
                <div className="divide-y divide-gray-700/30">
                  {followers.map(f => (
                    <div key={f.id} className="px-4 py-2 flex items-center gap-3 text-sm">
                      <span className={`px-2 py-0.5 rounded text-xs ${f.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400'}`}>
                        {f.enabled ? 'on' : 'off'}
                      </span>
                      <span className="text-white">{accountLabel(f.account_id)}</span>
                      <span className="text-gray-500 text-xs">{f.allocation_mode} × {f.allocation_value}</span>
                      <span className="text-gray-500 text-xs">max {f.max_open_positions}</span>
                      <span className="text-gray-500 text-xs">{Object.keys(f.copied_tickets ?? {}).length} open copies</span>
                      {f.last_error && (
                        <span className="text-red-400 text-xs truncate max-w-xs" title={f.last_error}>⚠ {f.last_error}</span>
                      )}
                      <div className="ml-auto flex items-center gap-2">
                        <button onClick={() => handleToggleFollower(f)}
                          className="text-gray-400 hover:text-white"><Power className="w-3.5 h-3.5" /></button>
                        <button onClick={() => handleDeleteFollower(f.id)}
                          className="text-red-400 hover:text-red-300"><Trash2 className="w-3 h-3" /></button>
                      </div>
                    </div>
                  ))}
                  {followers.length === 0 && (
                    <div className="px-4 py-3 text-gray-500 text-sm">No follower accounts attached yet.</div>
                  )}
                </div>
                <div className="p-3 border-t border-gray-700/50 flex flex-wrap items-center gap-2">
                  <select value={followerForm.account_id}
                    onChange={e => setFollowerForm(p => ({ ...p, account_id: e.target.value }))}
                    className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-white text-sm min-w-[180px]">
                    <option value="">Add follower account…</option>
                    {accounts.filter(a => a.id !== selected.source_account_id).map(a => (
                      <option key={a.id} value={a.id}>{a.name} — {a.login}</option>
                    ))}
                  </select>
                  <select value={followerForm.allocation_mode}
                    onChange={e => setFollowerForm(p => ({ ...p, allocation_mode: e.target.value }))}
                    className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-white text-sm">
                    <option value="multiplier">Multiplier</option>
                    <option value="fixed_lot">Fixed Lot</option>
                    <option value="risk_percent">Risk %</option>
                  </select>
                  <input placeholder="Value" type="number" step="0.1" value={followerForm.allocation_value}
                    onChange={e => setFollowerForm(p => ({ ...p, allocation_value: e.target.value }))}
                    className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-white text-sm w-24" />
                  <input placeholder="Max pos" type="number" value={followerForm.max_open_positions}
                    onChange={e => setFollowerForm(p => ({ ...p, max_open_positions: e.target.value }))}
                    className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-white text-sm w-24" />
                  <button onClick={handleAddFollower} disabled={!followerForm.account_id}
                    className="px-3 py-1.5 bg-green-600/20 text-green-400 rounded text-sm hover:bg-green-600/30 disabled:opacity-40">
                    <Plus className="w-4 h-4" />
                  </button>
                </div>
              </div>
            )}

            {/* Simulated Trades */}
            <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-700/50 text-white font-medium">
                {selected.mode === 'live' ? 'Copied Trade Log (paper mirror)' : 'Simulated Trades'} ({trades.length})
              </div>
              {trades.length === 0 ? (
                <div className="p-4 text-gray-500 text-sm">
                  No trades yet. Make sure the profile is active and the source account has open positions.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-gray-400 text-xs border-b border-gray-700/30">
                      <tr>
                        <th className="text-left px-4 py-2">Symbol</th>
                        <th className="text-left px-4 py-2">Direction</th>
                        <th className="text-right px-4 py-2">Volume</th>
                        <th className="text-left px-4 py-2">Opened</th>
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
                          <td className={`px-4 py-2 ${t.side === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                            {t.side.toUpperCase()}
                          </td>
                          <td className="px-4 py-2 text-right text-gray-300">{t.qty_sim}</td>
                          <td className="px-4 py-2 text-gray-400 text-xs">
                            {t.entry_time ? new Date(t.entry_time).toLocaleString() : '-'}
                          </td>
                          <td className="px-4 py-2 text-right text-gray-300">{t.entry_price ?? '-'}</td>
                          <td className="px-4 py-2 text-right text-gray-300">{t.exit_price ?? '-'}</td>
                          <td className={`px-4 py-2 text-right font-medium ${(t.pnl_sim ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {t.status === 'closed' ? `$${t.pnl_sim.toFixed(2)}` : '-'}
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
              )}
            </div>
          </>
        )}

        {/* Room supervisor decisions */}
        {decisions.length > 0 && (
          <div className="bg-gray-800/50 border border-purple-800/40 rounded-xl overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-700/50 flex items-center gap-2 text-white font-medium">
              <Bot className="w-4 h-4 text-purple-400" /> Trading Room — Copy Supervision Log
              <CheckCircle className="w-4 h-4 text-purple-400 ml-auto opacity-60" />
            </div>
            <div className="divide-y divide-gray-700/30 max-h-64 overflow-y-auto">
              {decisions.map(d => (
                <div key={d.id} className="px-4 py-2 text-sm">
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded text-xs ${
                      d.action === 'disable_profile' || d.action === 'disable_follower'
                        ? 'bg-red-900/30 text-red-400'
                        : 'bg-purple-900/30 text-purple-300'
                    }`}>{d.action.replace('_', ' ')}</span>
                    <span className="text-gray-300 text-xs">{d.symbol}</span>
                    {d.created_at && (
                      <span className="text-gray-500 text-xs ml-auto">{new Date(d.created_at).toLocaleString()}</span>
                    )}
                  </div>
                  <div className="text-gray-400 text-xs mt-1">{d.reasoning}</div>
                </div>
              ))}
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
