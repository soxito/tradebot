/**
 * Every account the trading room can trade on, with its switch.
 *
 * Demo accounts are always on — they take every OBLIVION-confirmed trade so
 * there is always a record to judge the desk by. Each live account has its own
 * switch, and the room's "Live trading" master switch decides whether any live
 * account trades at all.
 */
import { useCallback, useEffect, useState } from 'react'
import { Lock, Plus, RefreshCw, Trash2, Loader2, Radar } from 'lucide-react'
import { api } from '@/services/api'

export interface RoomAccount {
  venue: 'mt5' | 'crypto'
  id: number
  name: string
  login?: string
  server?: string
  exchange?: string
  account_type: 'demo' | 'live'
  always_on: boolean
  live_enabled: boolean
  status: string
  status_message?: string | null
  balance: number
  equity: number
  currency: string
  margin_block?: string | null
}

interface AccountsResponse {
  live_trading: boolean
  execution_enabled: boolean
  accounts: RoomAccount[]
}

function detail(e: unknown, fallback: string): string {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback
}

function fmt(n: number | undefined, currency: string) {
  const v = Number(n ?? 0)
  return `${v.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`
}

export function TradingAccountsPanel({ liveTrading }: { liveTrading: boolean }) {
  const [data, setData] = useState<AccountsResponse | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ name: '', account_type: 'demo' as 'demo' | 'live', api_key: '', api_secret: '', passphrase: '' })

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<AccountsResponse>('/agents/room/accounts')
      setData(data)
      setErr(null)
    } catch (e) {
      setErr(detail(e, 'Could not load the trading accounts.'))
    }
  }, [])

  useEffect(() => { void load() }, [load, liveTrading])

  const toggle = async (a: RoomAccount) => {
    setBusy(`${a.venue}:${a.id}`)
    try {
      await api.post(`/agents/room/accounts/${a.venue}/${a.id}/live`, { enabled: !a.live_enabled })
      await load()
    } catch (e) {
      setErr(detail(e, 'Could not change that switch.'))
    } finally {
      setBusy(null)
    }
  }

  const testAccount = async (a: RoomAccount) => {
    setBusy(`test:${a.id}`)
    try {
      const { data } = await api.post(`/agents/room/crypto-accounts/${a.id}/test`)
      if (!data.ok) setErr(`${a.name}: ${data.error}`)
      await load()
    } finally {
      setBusy(null)
    }
  }

  const remove = async (a: RoomAccount) => {
    setBusy(`del:${a.id}`)
    try {
      await api.delete(`/agents/room/crypto-accounts/${a.id}`)
      await load()
    } finally {
      setBusy(null)
    }
  }

  const add = async () => {
    setBusy('add')
    setErr(null)
    try {
      const { data } = await api.post('/agents/room/crypto-accounts', {
        ...form, exchange: 'bitget', passphrase: form.passphrase || null,
      })
      if (data.test && !data.test.ok) setErr(`Saved, but the exchange refused the keys: ${data.test.error}`)
      setForm({ name: '', account_type: 'demo', api_key: '', api_secret: '', passphrase: '' })
      setAdding(false)
      await load()
    } catch (e) {
      setErr(detail(e, 'Could not add that account.'))
    } finally {
      setBusy(null)
    }
  }

  const accounts = data?.accounts ?? []
  const demos = accounts.filter((a) => a.always_on)
  const lives = accounts.filter((a) => !a.always_on)

  const row = (a: RoomAccount) => {
    const key = `${a.venue}:${a.id}`
    const trading = a.always_on || (liveTrading && a.live_enabled)
    return (
      <div key={key} className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-700 bg-slate-900/50 p-3">
        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-300">
          {a.venue === 'mt5' ? 'MT5' : a.exchange ?? 'crypto'}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-slate-100">
            {a.name}{a.login ? ` · ${a.login}` : ''}
            {a.server && <span className="ml-1 text-[11px] font-normal text-slate-500">{a.server}</span>}
          </p>
          <p className="text-[11px] text-slate-400">
            Equity {fmt(a.equity, a.currency)}
            {a.status === 'error' && <span className="ml-2 text-red-400">connection error{a.status_message ? ` — ${a.status_message}` : ''}</span>}
            {a.margin_block && <span className="ml-2 text-amber-300">{a.margin_block}</span>}
          </p>
        </div>
        <span className={`text-[11px] font-medium ${trading ? 'text-emerald-300' : 'text-slate-500'}`}>
          {trading ? 'Trading' : a.live_enabled ? 'On — waits for live trading' : 'Off'}
        </span>
        {a.always_on ? (
          <span className="flex items-center gap-1 rounded-full bg-emerald-500/15 px-2.5 py-1 text-[11px] font-medium text-emerald-300"
            title="Demo accounts always trade and cannot be switched off">
            <Lock className="h-3 w-3" /> Always on
          </span>
        ) : (
          <button
            type="button"
            role="switch"
            aria-checked={a.live_enabled}
            aria-label={`Live trading on ${a.name}`}
            disabled={busy === key}
            onClick={() => toggle(a)}
            className={`h-6 w-11 shrink-0 rounded-full p-0.5 transition disabled:opacity-50 ${a.live_enabled ? 'bg-red-500' : 'bg-slate-600'}`}
          >
            <span className={`block h-5 w-5 rounded-full bg-white transition ${a.live_enabled ? 'translate-x-5' : ''}`} />
          </button>
        )}
        {a.venue === 'crypto' && (
          <span className="flex gap-1">
            <button type="button" onClick={() => testAccount(a)} disabled={busy === `test:${a.id}`}
              className="rounded border border-slate-700 p-1.5 text-slate-400 hover:border-slate-500" title="Test the keys">
              {busy === `test:${a.id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            </button>
            <button type="button" onClick={() => remove(a)} disabled={busy === `del:${a.id}`}
              className="rounded border border-slate-700 p-1.5 text-slate-400 hover:border-red-500 hover:text-red-300" title="Remove this account">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Trading accounts</h3>
        <button type="button" onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1 rounded-lg border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:border-cyan-500">
          <Plus className="h-3 w-3" /> Add exchange account
        </button>
      </div>
      {err && <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-2 text-[12px] text-amber-200">{err}</div>}

      {adding && (
        <div className="grid gap-2 rounded-lg border border-cyan-500/40 bg-cyan-500/5 p-3 md:grid-cols-2">
          <input placeholder="Name (e.g. Bitget demo)" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            className="rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          <select value={form.account_type} onChange={(e) => setForm({ ...form, account_type: e.target.value as 'demo' | 'live' })}
            className="rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100">
            <option value="demo">Bitget demo trading (always on)</option>
            <option value="live">Bitget live (starts switched off)</option>
          </select>
          <input placeholder="API key" value={form.api_key} autoComplete="off"
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            className="rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          <input placeholder="API secret" type="password" value={form.api_secret} autoComplete="new-password"
            onChange={(e) => setForm({ ...form, api_secret: e.target.value })}
            className="rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          <input placeholder="Passphrase" type="password" value={form.passphrase} autoComplete="new-password"
            onChange={(e) => setForm({ ...form, passphrase: e.target.value })}
            className="rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          <button type="button" onClick={add}
            disabled={busy === 'add' || !form.name || form.api_key.length < 4 || form.api_secret.length < 4}
            className="rounded-lg bg-cyan-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-40">
            {busy === 'add' ? 'Checking the keys…' : 'Save and test'}
          </button>
          <p className="text-[10px] text-slate-500 md:col-span-2">
            Keys are encrypted before they are stored and are never shown again. A demo account needs
            Bitget <em>demo trading</em> API keys.
          </p>
        </div>
      )}

      {!data && !err && <p className="text-[12px] text-slate-500">Loading accounts…</p>}
      {demos.length > 0 && (
        <div className="space-y-2">
          <p className="text-[11px] text-slate-500">Demo — take every OBLIVION-confirmed trade, always</p>
          {demos.map(row)}
        </div>
      )}
      {lives.length > 0 && (
        <div className="space-y-2">
          <p className="text-[11px] text-slate-500">
            Live — {liveTrading ? 'the switched-on ones trade now' : 'nothing trades until Live trading is on'}
          </p>
          {lives.map(row)}
        </div>
      )}
      <p className="text-[10px] text-slate-500">
        Each account is sized on its own equity. One account failing never stops the others.
      </p>
    </div>
  )
}

interface OblivionReadRow {
  id: number
  symbol: string
  timeframe: string
  source: string
  analyzed_at: string
  action: string | null
  kind: string | null
  confirmed: boolean
  tradeable: boolean
  score_buy: number | null
  score_sell: number | null
  reason: string | null
}

export function OblivionCoveragePanel({ timeframe }: { timeframe: string }) {
  const [reads, setReads] = useState<OblivionReadRow[] | null>(null)
  const [scan, setScan] = useState<{ running?: boolean; interval_s?: number } | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const { data } = await api.get('/agents/room/oblivion')
        if (!alive) return
        setScan(data.scan)
        setReads((data.reads as OblivionReadRow[]).filter((r) => r.timeframe === timeframe)
          .sort((a, b) => Number(b.tradeable) - Number(a.tradeable)))
      } catch {
        if (alive) setReads([])
      }
    }
    void load()
    const t = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(t) }
  }, [timeframe])

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Radar className="h-4 w-4 text-fuchsia-400" />
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
          OBLIVION — main engine · {timeframe}
        </h3>
        <span className={`ml-auto rounded-full px-2 py-0.5 text-[10px] ${scan?.running ? 'bg-emerald-500/15 text-emerald-300' : 'bg-slate-700 text-slate-400'}`}>
          {scan?.running ? `scanning every ${Math.round((scan.interval_s ?? 300) / 60)} min` : 'scan idle'}
        </span>
      </div>
      <p className="text-[11px] text-slate-500">
        Every monitored pair is read by OBLIVION and stored. The board only convenes to trade a pair when OBLIVION
        has confirmed a signal on it, and the order is refused if a fresh re-read no longer agrees.
      </p>
      <div className="max-h-80 overflow-auto rounded-lg border border-slate-700">
        <table className="w-full min-w-[520px] text-left text-[12px]">
          <thead className="sticky top-0 bg-slate-900 text-[10px] uppercase text-slate-500">
            <tr><th className="px-2 py-1.5">Pair</th><th className="px-2">Signal</th><th className="px-2">Buy / sell</th><th className="px-2">Seen</th><th className="px-2">Why</th></tr>
          </thead>
          <tbody>
            {(reads ?? []).map((r) => (
              <tr key={r.id} className="border-t border-slate-800">
                <td className="px-2 py-1.5 font-medium text-slate-200">{r.symbol}</td>
                <td className={`px-2 font-semibold ${r.tradeable ? (r.action === 'buy' ? 'text-emerald-300' : 'text-red-300') : 'text-slate-500'}`}>
                  {r.tradeable ? `${(r.action ?? '').toUpperCase()} ${r.kind ?? ''}` : r.action && r.action !== 'wait' ? `${r.action} forming` : 'wait'}
                </td>
                <td className="px-2 text-slate-400">{r.score_buy ?? '–'} / {r.score_sell ?? '–'}</td>
                <td className="px-2 text-slate-500">{r.analyzed_at?.slice(11, 16)}</td>
                <td className="max-w-[320px] truncate px-2 text-slate-500" title={r.reason ?? ''}>{r.reason}</td>
              </tr>
            ))}
            {reads && reads.length === 0 && (
              <tr><td colSpan={5} className="px-2 py-3 text-center text-slate-500">No reads yet on this timeframe.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

interface PnL { realised: number; floating: number; total: number }
interface TargetState {
  target_usd: number
  progress: PnL
  remaining_usd: number
  progress_pct: number | null
  hit: boolean
  week_left: number
  daily: { target_usd: number; margin_pct: number; margin_in_use: number; progress: PnL; remaining_usd: number; hit: boolean; day_left: number; multiplier: number
    portfolio_value?: number; day_start_value?: number; min_pct?: number; max_pct?: number; cap_usd?: number | null
    progress_pct?: number; min_hit?: boolean; capped?: boolean }
  expected: { r_per_week: number; pairs: { symbol: string; timeframe: string; r_per_week: number; trades_per_week: number }[] }
  base_risk_usd: number
  expected_week_usd: number
  pacing: boolean
  multiplier: number
  max_multiplier: number
  per_pair?: boolean
  book_target_usd?: number
  pairs?: { symbol: string; weekly_target_usd: number; week: PnL; today: PnL; hit: boolean; r_per_week: number;
    expected_week_usd: number; risk_pct: number; daily_target_usd: number }[]
}

function Progress({ label, banked, open, target, hit, left }: { label: string; banked: number; open: number; target: number; hit: boolean; left: string }) {
  const money = (x?: number) => (x ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })
  const pct = target > 0 ? Math.max(0, Math.min(1, banked / target)) : 0
  return (
    <div className="min-w-[220px] flex-1">
      <div className="flex justify-between text-[12px] text-slate-300">
        <span>{label}: <b className={banked >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(banked)}</b> banked · {money(open)} open</span>
        <span>{target <= 0 ? 'no target' : hit ? `minimum reached — no cap, still trading` : `${money(target - banked)} to the minimum`}</span>
      </div>
      <div className="mt-1 h-2.5 overflow-hidden rounded-full bg-slate-800">
        <div className="h-full rounded-full bg-emerald-500 transition-all" style={{ width: `${pct * 100}%` }} />
      </div>
      <p className="mt-0.5 text-[10px] text-slate-500">{left}</p>
    </div>
  )
}

export function WeeklyTargetPanel({
  target, pacing, maxMult, dayMin, dayMax, perPair, minRisk, maxRisk, newsPre, newsCool, onSave,
}: {
  target: number
  pacing: boolean
  maxMult: number
  dayMin: number
  dayMax: number
  perPair: boolean
  minRisk: number
  maxRisk: number
  newsPre: number
  newsCool: number
  onSave: (patch: Record<string, number | boolean>) => void
}) {
  const [state, setState] = useState<TargetState | null>(null)
  const [draft, setDraft] = useState(String(target))
  const [dayMinDraft, setDayMinDraft] = useState(String(dayMin))
  const [dayMaxDraft, setDayMaxDraft] = useState(String(dayMax))

  useEffect(() => { setDraft(String(target)) }, [target])
  useEffect(() => { setDayMinDraft(String(dayMin)) }, [dayMin])
  useEffect(() => { setDayMaxDraft(String(dayMax)) }, [dayMax])
  useEffect(() => {
    let alive = true
    const load = () => api.get<TargetState>('/agents/room/target').then(({ data }) => alive && setState(data)).catch(() => {})
    void load()
    const t = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(t) }
  }, [target, pacing, maxMult, dayMin, dayMax])

  const money = (x?: number) => (x ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })
  return (
    <div className="space-y-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Profit targets — weekly minimum (no cap) · portfolio day band (minimum + cap)</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-[11px] text-slate-400">Weekly minimum (USD{perPair ? ' per pair' : ''})</span>
          <span className="mt-1 flex gap-2">
            <input type="number" min={0} step={100} value={draft} onChange={(e) => setDraft(e.target.value)}
              className="w-32 rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
            <button type="button" onClick={() => onSave({ weekly_target_usd: Number(draft) || 0 })}
              className="rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-white">Save</button>
          </span>
        </label>
        <Progress label="This week" banked={state?.progress.realised ?? 0} open={state?.progress.floating ?? 0}
          target={state?.target_usd ?? 0} hit={!!state?.hit}
          left={`${Math.round((state?.week_left ?? 0) * 100)}% of the trading week left`} />
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-[11px] text-slate-400">Portfolio day: minimum – cap (% of its value at 00:00 UTC)</span>
          <span className="mt-1 flex items-center gap-2">
            <input type="number" min={0} max={100} step={1} value={dayMinDraft} onChange={(e) => setDayMinDraft(e.target.value)}
              className="w-20 rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
            <span className="text-slate-500">–</span>
            <input type="number" min={0} max={100} step={1} value={dayMaxDraft} onChange={(e) => setDayMaxDraft(e.target.value)}
              className="w-20 rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
            <button type="button"
              onClick={() => onSave({ daily_target_min_pct: Number(dayMinDraft) || 0, daily_target_max_pct: Number(dayMaxDraft) || 0 })}
              className="rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-white">Save</button>
          </span>
        </label>
        <div className="min-w-[260px] flex-1">
          {(() => {
            const d = state?.daily
            const today = d?.progress.total ?? 0
            const cap = d?.cap_usd ?? 0
            const width = cap > 0 ? Math.max(0, Math.min(1, today / cap)) : 0
            const minMark = cap > 0 && d ? Math.min(1, d.target_usd / cap) : 0
            return (
              <>
                <div className="flex flex-wrap justify-between gap-2 text-[12px] text-slate-300">
                  <span>
                    Today on {money(d?.day_start_value)}:{' '}
                    <b className={today >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(today)} ({(d?.progress_pct ?? 0).toFixed(2)}%)</b>
                  </span>
                  <span className={d?.capped ? 'text-violet-300' : d?.min_hit ? 'text-emerald-300' : 'text-slate-400'}>
                    {d?.capped ? 'cap reached — no new entries until 00:00 UTC'
                      : d?.min_hit ? 'minimum banked — trading at the risk floor'
                        : `${money(d?.target_usd)} minimum · ${money(cap)} cap`}
                  </span>
                </div>
                <div className="relative mt-1 h-2.5 overflow-hidden rounded-full bg-slate-800">
                  <div className={`h-full rounded-full transition-all ${d?.capped ? 'bg-violet-500' : 'bg-emerald-500'}`} style={{ width: `${width * 100}%` }} />
                  {minMark > 0 && <div className="absolute top-0 h-full w-0.5 bg-sky-400" style={{ left: `${minMark * 100}%` }} />}
                </div>
                <p className="mt-0.5 text-[10px] text-slate-500">
                  {Math.round((d?.day_left ?? 0) * 100)}% of today left · below the minimum each trade risks up to the band top;
                  past it, the band floor; at the cap the day is locked in so it is not traded back.
                  Backtested on your accounts, 10% days are rare — run a portfolio backtest to see how often.
                </p>
              </>
            )
          })()}
        </div>
      </div>
      {state?.pairs && state.pairs.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-700">
          <table className="w-full min-w-[620px] text-left text-[12px]">
            <thead className="bg-slate-900 text-[10px] uppercase text-slate-500">
              <tr><th className="px-2 py-1.5">Pair</th><th>Week (banked / open)</th><th>Minimum</th><th>Risk / trade</th><th>Expected week</th><th>Today (banked / open)</th></tr>
            </thead>
            <tbody>
              {state.pairs.map((p) => (
                <tr key={p.symbol} className="border-t border-slate-800">
                  <td className="px-2 py-1.5 font-medium text-slate-200">{p.symbol}</td>
                  <td className={p.week.realised >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(p.week.realised)} / {money(p.week.floating)}</td>
                  <td className={p.hit ? 'text-emerald-300' : 'text-slate-300'}>{money(p.weekly_target_usd)}{p.hit ? ' ✓' : ''}</td>
                  <td className="text-slate-300">{p.risk_pct.toFixed(2)}%</td>
                  <td className="text-slate-400">{money(p.expected_week_usd)}</td>
                  <td className="text-slate-400">{money(p.today.realised)} / {money(p.today.floating)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="grid gap-2 md:grid-cols-4">
        <label className="flex items-center gap-2 text-[12px] text-slate-300">
          <input type="checkbox" checked={perPair} onChange={(e) => onSave({ target_per_pair: e.target.checked })} />
          Minimum applies to each pair
        </label>
        <label className="flex items-center gap-1 text-[12px] text-slate-300">
          Risk per trade
          <input type="number" min={0.1} max={10} step={0.5} value={minRisk}
            onChange={(e) => onSave({ min_risk_pct: Number(e.target.value) })}
            className="w-14 rounded border border-slate-700 bg-slate-900/70 px-1 py-1 text-slate-100" />
          –
          <input type="number" min={0.1} max={10} step={0.5} value={maxRisk}
            onChange={(e) => onSave({ max_risk_pct: Number(e.target.value) })}
            className="w-14 rounded border border-slate-700 bg-slate-900/70 px-1 py-1 text-slate-100" />
          %
        </label>
        <label className="flex items-center gap-1 text-[12px] text-slate-300">
          News: stop
          <input type="number" min={0} max={240} step={5} value={newsPre}
            onChange={(e) => onSave({ news_pre_min: Number(e.target.value) })}
            className="w-14 rounded border border-slate-700 bg-slate-900/70 px-1 py-1 text-slate-100" />
          min before
        </label>
        <label className="flex items-center gap-1 text-[12px] text-slate-300">
          cool-off
          <input type="number" min={0} max={480} step={15} value={newsCool}
            onChange={(e) => onSave({ news_cooloff_min: Number(e.target.value) })}
            className="w-14 rounded border border-slate-700 bg-slate-900/70 px-1 py-1 text-slate-100" />
          min after
        </label>
      </div>
      <p className="text-[11px] text-slate-500">
        Each pair is sized inside the risk band at the % its backtested earnings need to reach its weekly minimum,
        more when the week is behind. High-impact news on a pair&apos;s currencies blocks new entries (resting ones are
        cancelled) until the cool-off ends; then fresh OBLIVION signals are taken. Approved pairs earn about {state ? `${state.expected.r_per_week.toFixed(2)}R ≈ ${money(state.expected_week_usd)}` : '—'} a
        week at normal size{state && state.multiplier > 1 ? ` · sizing ×${state.multiplier} to reach the minimums` : ''}.
      </p>
      <div className="grid gap-2 md:grid-cols-2">
        <label className="flex items-center gap-2 text-[12px] text-slate-300">
          <input type="checkbox" checked={pacing} onChange={(e) => onSave({ target_pacing: e.target.checked })} />
          Size up when behind a minimum target
        </label>
        <label className="flex items-center gap-2 text-[12px] text-slate-300">
          At most ×
          <input type="number" min={1} max={5} step={0.25} value={maxMult}
            onChange={(e) => onSave({ target_max_multiplier: Number(e.target.value) })}
            className="w-16 rounded border border-slate-700 bg-slate-900/70 px-2 py-1 text-slate-100" />
          normal size
        </label>
      </div>
      <p className="text-[10px] text-slate-500">
        The room never stops at a target — they are minimums, not caps. Size-up is limited to the multiplier above and
        free margin; a week can still bring one OBLIVION signal or none, so a minimum is an aim, not a guarantee.
      </p>
    </div>
  )
}

interface PairRow {
  symbol: string; timeframe: string; supported: boolean; venue: string; approved: boolean; tested: boolean
  trades: number | null; win_rate: number | null; expectancy_r: number | null; reason: string; tradeable: boolean
}

export function PairApprovalPanel() {
  const [rows, setRows] = useState<PairRow[] | null>(null)
  useEffect(() => {
    void api.get<PairRow[]>('/agents/room/pairs').then(({ data }) => setRows(data)).catch(() => setRows([]))
  }, [])
  return (
    <div className="space-y-2">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Pinned pairs the room may trade</h3>
      <p className="text-[11px] text-slate-500">
        A pair trades on a timeframe only when an account lists it and its own OBLIVION backtest cleared 90% with
        positive expectancy. Untested pinned pairs are backtested automatically; the report goes to Telegram.
      </p>
      <div className="max-h-72 overflow-auto rounded-lg border border-slate-700">
        <table className="w-full min-w-[560px] text-left text-[12px]">
          <thead className="sticky top-0 bg-slate-900 text-[10px] uppercase text-slate-500">
            <tr><th className="px-2 py-1.5">Pair</th><th>TF</th><th>Account</th><th>Backtest</th><th>Status</th></tr>
          </thead>
          <tbody>
            {(rows ?? []).map((r) => (
              <tr key={`${r.symbol}${r.timeframe}`} className="border-t border-slate-800">
                <td className="px-2 py-1.5 font-medium text-slate-200">{r.symbol}</td>
                <td className="text-slate-400">{r.timeframe}</td>
                <td className={r.supported ? 'text-slate-400' : 'text-red-300'}>{r.supported ? r.venue : 'not listed'}</td>
                <td className="text-slate-400" title={r.reason}>
                  {r.tested ? `${r.win_rate != null ? (r.win_rate * 100).toFixed(1) : '—'}% · ${r.trades} trades` : 'pending'}
                </td>
                <td className={r.tradeable ? 'font-semibold text-emerald-300' : 'text-slate-500'} title={r.reason}>
                  {r.tradeable ? 'Trading' : !r.supported ? 'No account' : !r.tested ? 'Backtesting' : 'Not approved'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function UniversePanel({
  enabled, symbols, minWinRate, dailyFloor, recoveryBar, onSave,
}: {
  enabled: boolean
  symbols: string
  minWinRate: number
  dailyFloor: number
  recoveryBar: number
  onSave: (patch: Record<string, string | number | boolean>) => void
}) {
  const [draft, setDraft] = useState(symbols)
  const [bar, setBar] = useState(String(Math.round(minWinRate * 100)))
  useEffect(() => { setDraft(symbols) }, [symbols])
  useEffect(() => { setBar(String(Math.round(minWinRate * 100))) }, [minWinRate])
  const [floor, setFloor] = useState(String(Math.round(dailyFloor * 100)))
  const [elite, setElite] = useState(String(Math.round(recoveryBar * 100)))
  useEffect(() => { setFloor(String(Math.round(dailyFloor * 100))) }, [dailyFloor])
  useEffect(() => { setElite(String(Math.round(recoveryBar * 100))) }, [recoveryBar])
  const count = draft.split(',').map((s) => s.trim()).filter(Boolean).length
  return (
    <div className="space-y-2 rounded-lg border border-fuchsia-500/30 bg-fuchsia-500/5 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">OBLIVION trading universe</h3>
        <label className="ml-auto flex items-center gap-2 text-[12px] text-slate-300">
          <input type="checkbox" checked={enabled} onChange={(e) => onSave({ universe_enabled: e.target.checked })} />
          Scan and trade these as well as the pinned pairs
        </label>
      </div>
      <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={3}
        className="w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 font-mono text-[12px] text-slate-100" />
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => onSave({ universe_symbols: draft })}
          className="rounded-lg bg-fuchsia-600 px-3 py-1.5 text-sm font-medium text-white">Save {count} instruments</button>
        <label className="flex items-center gap-2 text-[12px] text-slate-300">
          Trade a pair/timeframe when its backtest wins at least
          <input type="number" min={50} max={100} step={1} value={bar} onChange={(e) => setBar(e.target.value)}
            onBlur={() => onSave({ pair_min_win_rate: Math.max(50, Math.min(100, Number(bar) || 95)) / 100 })}
            className="w-16 rounded border border-slate-700 bg-slate-900/70 px-2 py-1 text-slate-100" />
          %
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-[12px] text-slate-300">
        Daily floor: when today&apos;s closed room trades win less than
        <input type="number" min={0} max={100} step={1} value={floor} onChange={(e) => setFloor(e.target.value)}
          onBlur={() => onSave({ daily_min_win_rate: Math.max(0, Math.min(100, Number(floor) || 0)) / 100 })}
          className="w-16 rounded border border-slate-700 bg-slate-900/70 px-2 py-1 text-slate-100" />
        %, only pairs whose backtest won at least
        <input type="number" min={0} max={100} step={1} value={elite} onChange={(e) => setElite(e.target.value)}
          onBlur={() => onSave({ daily_recovery_min_wr: Math.max(0, Math.min(100, Number(elite) || 0)) / 100 })}
          className="w-16 rounded border border-slate-700 bg-slate-900/70 px-2 py-1 text-slate-100" />
        % may open for the rest of the day (0 switches the floor off).
      </div>
      <p className="text-[10px] text-slate-500">
        Universe instruments are read by OBLIVION on 4h, 2h, 1h, 30m, 15m and 5m and convene the board only on a
        confirmed signal — they never join the pinned-pair focus rotation. Each untested pair/timeframe is backtested
        automatically (report to Telegram) before it can trade. Scored on history the approvals never saw, a 95% bar
        won 92.3% across 60 instruments (80% of days at 90%+, ~1.5 trades a weekday); 92% won 91.5% at ~2.7 a day
        with about the same profit. No bar guarantees every single day: one loss needs nine wins beside it.
      </p>
    </div>
  )
}
