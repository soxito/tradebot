/**
 * AccountTabs — live linked accounts on the trading-room rail.
 *
 * One call to the unified monitor feeds two tabs: MT5 (per-account balance,
 * equity, floating P&L, open positions) and Crypto (per-exchange balance +
 * open positions). Polls on an interval so the desk always shows real money.
 */
import { useCallback, useEffect, useState } from 'react'
import { Bitcoin, ChevronDown, ChevronUp, Landmark, RefreshCw } from 'lucide-react'
import { apiClient } from '@/services/api'

interface Position {
  exchange?: string
  symbol: string
  side: string
  size: number
  entry_price?: number
  mark_price?: number
  pnl: number
  pnl_pct?: number
}
interface CryptoAccount { exchange: string; currency: string; total: number; free: number; used: number }
interface Mt5Position { symbol?: string; side?: string; type?: string; volume?: number; profit?: number; [k: string]: unknown }
interface Mt5Account {
  account_id: number
  name: string
  login: string
  server: string
  balance: number
  equity: number
  floating_pnl: number
  currency: string
  positions: Mt5Position[]
  position_count: number
}
interface Monitor {
  crypto_positions: Position[]
  crypto_accounts: CryptoAccount[]
  crypto_total_pnl: number
  mt5_accounts: Mt5Account[]
  mt5_total_balance: number
  mt5_total_equity: number
  mt5_total_floating_pnl: number
  total_pnl: number
  total_position_count: number
}

const money = (n: number | undefined, ccy = 'USD') =>
  `${(n ?? 0) < 0 ? '-' : ''}${ccy === 'USD' ? '$' : ''}${Math.abs(n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}${ccy !== 'USD' ? ' ' + ccy : ''}`
const pnlColor = (n: number | undefined) => ((n ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400')

export default function AccountTabs() {
  const [data, setData] = useState<Monitor | null>(null)
  const [tab, setTab] = useState<'mt5' | 'crypto'>('mt5')
  const [loading, setLoading] = useState(false)
  const [collapsed, setCollapsed] = useState(false)

  const load = useCallback(async (sync = false) => {
    setLoading(true)
    try {
      const res = await apiClient.jarvis.unifiedMonitor(sync)
      setData(res.data as Monitor)
    } catch {
      /* bridge may be offline — panel degrades to last-known / empty */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(true)
    // Fast cached refresh keeps the panel live; every 4th tick forces a real
    // sync from the MT5 bridge so balances/positions are genuinely current.
    let tick = 0
    const t = setInterval(() => {
      tick += 1
      load(tick % 4 === 0)
    }, 5000)
    return () => clearInterval(t)
  }, [load])

  const mt5 = data?.mt5_accounts ?? []
  const cryptoAccts = data?.crypto_accounts ?? []
  const cryptoPos = data?.crypto_positions ?? []

  return (
    <div className="overflow-hidden rounded-xl border border-amber-500/20 bg-gradient-to-br from-slate-900/80 to-slate-950/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="flex items-center gap-2 text-left"
        >
          <h2 className="text-xs font-semibold uppercase tracking-wide text-amber-200/80">Linked accounts</h2>
          {collapsed ? <ChevronDown className="h-3.5 w-3.5 text-amber-200/60" /> : <ChevronUp className="h-3.5 w-3.5 text-amber-200/60" />}
        </button>
        <span className={`ml-auto font-mono text-[11px] ${pnlColor(data?.total_pnl)}`}>
          {money(data?.total_pnl)} P&L
        </span>
        <button
          type="button"
          onClick={() => load(true)}
          title="Force live sync from the MT5 bridge"
          className="rounded-md border border-slate-700 p-1 text-slate-400 hover:border-slate-500 hover:text-slate-200"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
        </button>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="rounded p-1 text-slate-500 hover:bg-slate-800 hover:text-slate-300"
          title={collapsed ? 'Expand' : 'Collapse'}
          aria-label={collapsed ? 'Expand linked accounts' : 'Collapse linked accounts'}
        >
          {collapsed ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
        </button>
      </div>
      {collapsed ? null : (
        <div className="px-3 pb-3">

      <div className="mb-2 grid grid-cols-2 gap-1 rounded-lg bg-slate-800/40 p-0.5">
        <button
          type="button"
          onClick={() => setTab('mt5')}
          className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium transition ${
            tab === 'mt5' ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Landmark className="h-3 w-3" /> MT5 ({mt5.length})
        </button>
        <button
          type="button"
          onClick={() => setTab('crypto')}
          className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium transition ${
            tab === 'crypto' ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Bitcoin className="h-3 w-3" /> Crypto ({cryptoAccts.length})
        </button>
      </div>

      {tab === 'mt5' && (
        <div className="max-h-[30vh] space-y-2 overflow-y-auto pr-0.5">
          {mt5.length === 0 && (
            <p className="py-3 text-center text-[11px] text-slate-500">
              No MT5 accounts linked. Add one in the MT5 plugin.
            </p>
          )}
          {mt5.map((a) => (
            <div key={a.account_id} className="rounded-lg border border-slate-700/60 bg-slate-900/50 p-2">
              <div className="flex items-baseline gap-2">
                <span className="truncate text-[12px] font-semibold text-slate-100">{a.name}</span>
                <span className="font-mono text-[10px] text-slate-500">{a.login}</span>
                <span className={`ml-auto font-mono text-[11px] ${pnlColor(a.floating_pnl)}`}>{money(a.floating_pnl, a.currency)}</span>
              </div>
              <div className="mt-1 flex gap-3 text-[10px] text-slate-400">
                <span>Bal <span className="font-mono text-slate-200">{money(a.balance, a.currency)}</span></span>
                <span>Eq <span className="font-mono text-slate-200">{money(a.equity, a.currency)}</span></span>
                <span className="ml-auto">{a.position_count} open</span>
              </div>
              {a.positions?.slice(0, 4).map((p, i) => (
                <div key={i} className="mt-1 flex items-center gap-2 text-[10px]">
                  <span className="font-mono text-slate-300">{String(p.symbol ?? '')}</span>
                  <span className="uppercase text-slate-500">{String(p.side ?? p.type ?? '')}</span>
                  <span className={`ml-auto font-mono ${pnlColor(Number(p.profit ?? 0))}`}>{money(Number(p.profit ?? 0), a.currency)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {tab === 'crypto' && (
        <div className="max-h-[30vh] space-y-2 overflow-y-auto pr-0.5">
          {cryptoAccts.length === 0 && (
            <p className="py-3 text-center text-[11px] text-slate-500">
              No crypto exchange connected. Add API keys in Settings.
            </p>
          )}
          {cryptoAccts.map((a) => (
            <div key={a.exchange} className="rounded-lg border border-slate-700/60 bg-slate-900/50 p-2">
              <div className="flex items-baseline gap-2">
                <span className="text-[12px] font-semibold capitalize text-slate-100">{a.exchange}</span>
                <span className="ml-auto font-mono text-[11px] text-slate-200">{money(a.total, a.currency)}</span>
              </div>
              <div className="mt-1 flex gap-3 text-[10px] text-slate-400">
                <span>Free <span className="font-mono text-slate-200">{money(a.free, a.currency)}</span></span>
                <span>Used <span className="font-mono text-slate-200">{money(a.used, a.currency)}</span></span>
              </div>
            </div>
          ))}
          {cryptoPos.length > 0 && (
            <div className="rounded-lg border border-slate-700/60 bg-slate-900/50 p-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">Open positions</div>
              {cryptoPos.slice(0, 6).map((p, i) => (
                <div key={`${p.symbol}-${i}`} className="flex items-center gap-2 text-[10px]">
                  <span className="font-mono text-slate-300">{p.symbol}</span>
                  <span className={`uppercase ${p.side === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>{p.side}</span>
                  <span className="text-slate-500">×{p.size}</span>
                  <span className={`ml-auto font-mono ${pnlColor(p.pnl)}`}>
                    {money(p.pnl)} {p.pnl_pct != null && <span className="text-slate-500">({p.pnl_pct.toFixed(1)}%)</span>}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
        </div>
      )}
    </div>
  )
}
