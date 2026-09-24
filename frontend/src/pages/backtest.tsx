/**
 * Backtest OBLIVION signals and the trading room's agents on history.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import Head from 'next/head'
import dynamic from 'next/dynamic'
import { FileText, Loader2, Play, Send } from 'lucide-react'
import { api } from '@/services/api'
import type { BacktestTrade } from '@/components/BacktestChart'
import PortfolioBacktestResult, { type PortfolioDoc } from '@/components/PortfolioBacktestResult'

const BacktestChart = dynamic(() => import('@/components/BacktestChart'), { ssr: false })

interface BookSummary {
  trades: number; wins: number; losses: number; breakeven: number
  win_rate: number | null; net_profit: number; return_pct?: number; end_balance?: number
  profit_factor: number | null; avg_r: number | null; max_drawdown_pct?: number
  gross_win?: number; gross_loss?: number; pairs?: number
  targets?: { weekly_target: number; weeks: number; weeks_hit: number; avg_week: number; best_week: number; worst_week: number;
    losing_weeks: number; daily_target: number; trading_days: number; days_hit: number; avg_day: number
    days_decided?: number; days_wr90?: number; losing_days?: number }
}
interface RunRow {
  id: number; status: string; symbols: string[]; timeframe: string
  start_balance: number; risk_pct: number; has_pdf: boolean; telegram_sent: boolean
  error: string | null; created_at: string
  params: { agents_rule?: string; use_room_profile?: boolean }
  summary: { totals?: Record<string, BookSummary> & { wf_paced?: { return_pct?: number } }; traded?: { totals?: Record<string, BookSummary> } | null }
}
interface PairResult {
  bars: number; from: number; to: number; signals: number; error?: string
  summary: Record<string, BookSummary>
  trades: Record<string, BacktestTrade[]>
}
interface TradedBook {
  bar: number; approved: Record<string, string>; rejected: Record<string, string>; totals: Record<string, BookSummary>
}
interface RunDetail extends RunRow {
  results?: Record<string, PairResult>; totals?: Record<string, BookSummary>; traded?: TradedBook | null
  portfolio?: PortfolioDoc
}

const BOOKS: Record<string, string> = { oblivion: 'OBLIVION', agents: 'OBLIVION + agents' }
const pct = (x: number | null | undefined) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`)
const money = (x: number | null | undefined) =>
  x == null ? '—' : x.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const when = (t?: number) => (t ? new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—')

export default function BacktestPage() {
  const [options, setOptions] = useState<{ timeframes: string[]; focus_symbols: string[]; profiles: Record<string, { full_win_rate: number; out_of_sample_win_rate: number }> } | null>(null)
  const [symbols, setSymbols] = useState('')
  const [timeframe, setTimeframe] = useState('2h')
  const [balance, setBalance] = useState(10000)
  const [risk, setRisk] = useState(1)
  const [agentsRule, setAgentsRule] = useState<'opposite' | 'strict' | 'none'>('opposite')
  const [useProfile, setUseProfile] = useState(true)
  const [telegram, setTelegram] = useState(true)
  const [runs, setRuns] = useState<RunRow[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [pair, setPair] = useState<string | null>(null)
  const [book, setBook] = useState<'oblivion' | 'agents'>('oblivion')
  const [candles, setCandles] = useState<[number, number, number, number, number][]>([])
  const [focusTime, setFocusTime] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [pfDays, setPfDays] = useState(60)

  const loadRuns = useCallback(async () => {
    const { data } = await api.get<RunRow[]>('/backtest/runs')
    setRuns(data)
    return data
  }, [])

  useEffect(() => {
    void (async () => {
      const { data } = await api.get('/backtest/options')
      setOptions(data)
      setSymbols((data.focus_symbols as string[]).join(', '))
      const list = await loadRuns()
      if (list.length) setSelected(list[0].id)
    })()
  }, [loadRuns])

  // Poll while any run is still working.
  useEffect(() => {
    if (!runs.some((r) => r.status === 'running')) return
    const t = setInterval(async () => {
      const list = await loadRuns()
      if (selected && list.find((r) => r.id === selected)?.status !== 'running') {
        setDetail(null)
      }
    }, 4000)
    return () => clearInterval(t)
  }, [runs, selected, loadRuns])

  useEffect(() => {
    if (!selected) return
    const row = runs.find((r) => r.id === selected)
    if (!row || row.status === 'running') return
    if (detail?.id === selected) return
    void (async () => {
      const { data } = await api.get<RunDetail>(`/backtest/runs/${selected}`)
      setDetail(data)
      const first = Object.keys(data.results ?? {})[0] ?? null
      setPair(first)
    })()
  }, [selected, runs, detail?.id])

  useEffect(() => {
    if (!selected || !pair) return
    setFocusTime(null)
    void api.get(`/backtest/runs/${selected}/candles/${pair}`).then(({ data }) => setCandles(data.candles)).catch(() => setCandles([]))
  }, [selected, pair])

  const run = async () => {
    setBusy(true)
    setMsg(null)
    try {
      const { data } = await api.post('/backtest/run', {
        symbols: symbols.split(/[,\s]+/).filter(Boolean),
        timeframe, start_balance: balance, risk_pct: risk, agents_rule: agentsRule,
        use_room_profile: useProfile, send_telegram: telegram,
      })
      await loadRuns()
      setSelected(data.id)
      setDetail(null)
    } catch (e) {
      setMsg((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Could not start the backtest.')
    } finally {
      setBusy(false)
    }
  }

  const runPortfolio = async () => {
    setBusy(true)
    setMsg(null)
    try {
      const { data } = await api.post('/backtest/portfolio', { days: pfDays, send_telegram: telegram })
      await loadRuns()
      setSelected(data.id)
      setDetail(null)
    } catch (e) {
      setMsg((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Could not start the portfolio backtest.')
    } finally {
      setBusy(false)
    }
  }

  const resend = async () => {
    if (!selected) return
    try {
      await api.post(`/backtest/runs/${selected}/telegram`)
      setMsg('Report sent to Telegram.')
      void loadRuns()
    } catch (e) {
      setMsg((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Telegram did not accept the report.')
    }
  }

  const res = pair ? detail?.results?.[pair] : undefined
  const trades = useMemo(() => (res?.trades?.[book] ?? []).filter((t) => t.status === 'closed'), [res, book])
  const current = runs.find((r) => r.id === selected)
  const profile = options?.profiles?.[timeframe]
  const pdfUrl = selected ? `${api.defaults.baseURL}/backtest/runs/${selected}/pdf` : '#'

  return (
    <>
      <Head><title>Backtest — TradeBot</title></Head>
      <div className="mx-auto max-w-7xl space-y-5 p-1">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Backtest — OBLIVION &amp; agents</h1>
          <p className="text-[12px] text-slate-400">
            Replays OBLIVION&apos;s signals bar by bar with the room&apos;s trade management, and the agents&apos; confirm/veto on top.
            Stops are assumed hit before targets inside a bar, and a typical spread is charged on every fill.
          </p>
        </div>

        <section className="grid gap-3 rounded-xl border border-slate-700/70 bg-slate-900/30 p-4 md:grid-cols-4">
          <label className="md:col-span-2">
            <span className="text-[11px] uppercase tracking-wide text-slate-400">Pairs</span>
            <input value={symbols} onChange={(e) => setSymbols(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
            <span className="text-[10px] text-slate-500">Defaults to the pairs pinned in the trading room.</span>
          </label>
          <label>
            <span className="text-[11px] uppercase tracking-wide text-slate-400">Timeframe</span>
            <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100">
              {(options?.timeframes ?? ['15m', '2h']).map((tf) => <option key={tf} value={tf}>{tf}</option>)}
            </select>
            <span className="text-[10px] text-slate-500">
              {profile ? `Room profile: ${pct(profile.full_win_rate)} win rate in validation` : 'No tuned room profile — global settings'}
            </span>
          </label>
          <label>
            <span className="text-[11px] uppercase tracking-wide text-slate-400">Starting amount (per pair)</span>
            <input type="number" min={1} value={balance} onChange={(e) => setBalance(Number(e.target.value))}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          </label>
          <label>
            <span className="text-[11px] uppercase tracking-wide text-slate-400">Risk per trade %</span>
            <input type="number" min={0.1} max={10} step={0.1} value={risk} onChange={(e) => setRisk(Number(e.target.value))}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          </label>
          <label>
            <span className="text-[11px] uppercase tracking-wide text-slate-400">Agents rule</span>
            <select value={agentsRule} onChange={(e) => setAgentsRule(e.target.value as 'opposite' | 'strict' | 'none')}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100">
              <option value="opposite">Veto only when the agents read the other side</option>
              <option value="strict">Trade only when the agents agree</option>
              <option value="none">Agents never veto</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" checked={useProfile} onChange={(e) => setUseProfile(e.target.checked)} />
            Use the room&apos;s tuned profile
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" checked={telegram} onChange={(e) => setTelegram(e.target.checked)} />
            Send the PDF report to Telegram
          </label>
          <button type="button" onClick={run} disabled={busy}
            className="flex items-center justify-center gap-2 rounded-lg bg-cyan-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />} Run backtest
          </button>
          {msg && <p className="text-[12px] text-amber-300 md:col-span-4">{msg}</p>}
        </section>

        <section className="flex flex-wrap items-end gap-3 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4">
          <div className="min-w-[260px] flex-1">
            <p className="text-sm font-semibold text-slate-100">Backtest my accounts</p>
            <p className="text-[12px] text-slate-400">
              Replays the pairs the room trades on every MT5 and crypto account you have set up, on each one&apos;s real equity,
              with the live risk band, the portfolio day target and cap, and the daily win-rate floor.
            </p>
          </div>
          <label>
            <span className="text-[11px] uppercase tracking-wide text-slate-400">Days</span>
            <input type="number" min={7} max={365} value={pfDays} onChange={(e) => setPfDays(Number(e.target.value) || 60)}
              className="mt-1 block w-24 rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100" />
          </label>
          <button type="button" onClick={runPortfolio} disabled={busy}
            className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />} Run on my accounts
          </button>
        </section>

        <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
          <aside className="space-y-1.5">
            <h2 className="text-[11px] uppercase tracking-wide text-slate-400">Runs</h2>
            {runs.map((r) => {
              // The traded book (approved pairs) when the run has one — that is what the room trades.
              const traded = r.summary?.traded?.totals?.oblivion
              const wr = traded?.win_rate ?? r.summary?.totals?.oblivion?.win_rate
              const pfReturn = r.timeframe === 'portfolio' ? r.summary?.totals?.wf_paced?.return_pct : undefined
              return (
                <button key={r.id} type="button" onClick={() => { setSelected(r.id); setDetail(null) }}
                  className={`w-full rounded-lg border p-2 text-left text-[12px] ${selected === r.id ? 'border-cyan-500 bg-cyan-500/10' : 'border-slate-700 bg-slate-900/50 hover:border-slate-500'}`}>
                  <span className="flex justify-between text-slate-200">
                    <span>#{r.id} · {r.timeframe}</span>
                    <span className={r.status === 'done' ? 'text-emerald-300' : r.status === 'error' ? 'text-red-300' : 'text-amber-300'}>
                      {r.status !== 'done' ? r.status
                        : r.timeframe === 'portfolio' ? `${(pfReturn ?? 0) >= 0 ? '+' : ''}${(pfReturn ?? 0).toFixed(1)}%`
                          : `${pct(wr)}${traded ? ' traded' : ''}`}
                    </span>
                  </span>
                  <span className="block truncate text-slate-500">{r.timeframe === 'portfolio' ? 'your MT5 / crypto accounts' : r.symbols.join(', ')}</span>
                </button>
              )
            })}
            {!runs.length && <p className="text-[12px] text-slate-500">No backtests yet.</p>}
          </aside>

          <main className="min-w-0 space-y-4">
            {current?.status === 'running' && (
              <div className="flex items-center gap-2 rounded-lg border border-slate-700 p-4 text-sm text-slate-300">
                <Loader2 className="h-4 w-4 animate-spin" /> Backtesting {current.symbols.join(', ')} on {current.timeframe}…
              </div>
            )}
            {detail?.portfolio && (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold text-slate-200">#{detail.id} · portfolio · last {detail.portfolio.params.days} days</h2>
                  <a href={pdfUrl} target="_blank" rel="noreferrer"
                    className="ml-auto flex items-center gap-1 rounded-lg border border-slate-700 px-2 py-1 text-[12px] text-slate-300 hover:border-cyan-500">
                    <FileText className="h-3.5 w-3.5" /> PDF report
                  </a>
                </div>
                <PortfolioBacktestResult doc={detail.portfolio} />
              </>
            )}
            {current?.status === 'error' && (
              <div className="rounded-lg border border-red-500/50 bg-red-500/10 p-3 text-sm text-red-200">{current.error}</div>
            )}
            {detail?.totals && (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold text-slate-200">
                    #{detail.id} · {detail.timeframe} · {money(detail.start_balance)} per pair · {detail.risk_pct}% risk
                  </h2>
                  <a href={pdfUrl} target="_blank" rel="noreferrer"
                    className="ml-auto flex items-center gap-1 rounded-lg border border-slate-700 px-2 py-1 text-[12px] text-slate-300 hover:border-cyan-500">
                    <FileText className="h-3.5 w-3.5" /> PDF report
                  </a>
                  <button type="button" onClick={resend}
                    className="flex items-center gap-1 rounded-lg border border-slate-700 px-2 py-1 text-[12px] text-slate-300 hover:border-cyan-500">
                    <Send className="h-3.5 w-3.5" /> {current?.telegram_sent ? 'Send to Telegram again' : 'Send to Telegram'}
                  </button>
                </div>
                {(() => {
                  const all = detail.totals?.oblivion
                  const tb = detail.traded?.totals?.oblivion
                  const cell = (label: string, t?: BookSummary, pairs?: number) => t && (
                    <div className="rounded-xl border border-slate-700 bg-slate-900/40 p-3">
                      <p className="text-[11px] uppercase tracking-wide text-slate-400">{label} · {pairs ?? t.pairs} pairs</p>
                      <div className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
                        <span className="text-emerald-300">Profit +{money(t.gross_win ?? 0)}</span>
                        <span className="text-red-300">Losses −{money(t.gross_loss ?? 0)}</span>
                        <span className={`text-lg font-semibold ${t.net_profit >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                          Net {t.net_profit >= 0 ? '+' : ''}{money(t.net_profit)}
                        </span>
                      </div>
                      <p className="mt-1 text-[11px] text-slate-400">
                        {t.wins}W / {t.losses}L · {pct(t.win_rate)}
                        {t.targets?.days_decided ? ` · days at 90%+ win: ${t.targets.days_wr90}/${t.targets.days_decided}` : ''}
                      </p>
                    </div>
                  )
                  return (
                    <div className="grid gap-3 md:grid-cols-2">
                      {detail.traded && (tb ? cell(`Traded book (pairs ≥ ${Math.round(detail.traded.bar * 100)}%)`, tb,
                        Object.keys(detail.traded.approved).length) : (
                        <div className="rounded-xl border border-slate-700 bg-slate-900/40 p-3 text-sm text-slate-400">
                          No pair cleared the {Math.round(detail.traded.bar * 100)}% bar — nothing on this timeframe is traded.
                        </div>
                      ))}
                      {cell('All pairs tested', all)}
                    </div>
                  )
                })()}
                <div className="grid gap-3 md:grid-cols-2">
                  {Object.entries(detail.totals).map(([b, t]) => (
                    <div key={b} className="rounded-xl border border-slate-700 bg-slate-900/40 p-3">
                      <p className="text-[11px] uppercase tracking-wide text-slate-400">{BOOKS[b] ?? b} · all pairs</p>
                      <div className="mt-1 flex items-baseline gap-3">
                        <span className={`text-3xl font-semibold ${(t.win_rate ?? 0) >= 0.9 ? 'text-emerald-300' : 'text-amber-300'}`}>{pct(t.win_rate)}</span>
                        <span className="text-sm text-slate-400">{t.wins} wins · {t.losses} losses · {t.breakeven} B/E</span>
                      </div>
                      <p className={`mt-1 text-sm ${t.net_profit >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                        Net {t.net_profit >= 0 ? '+' : ''}{money(t.net_profit)} ({t.return_pct?.toFixed(2)}%)
                        <span className="ml-2 text-slate-500">PF {t.profit_factor?.toFixed(2) ?? '—'} · avg {t.avg_r?.toFixed(3) ?? '—'}R · {t.trades} trades</span>
                      </p>
                      {t.targets && (
                        <p className="mt-1 text-[11px] text-slate-400">
                          Weeks ≥ {money(t.targets.weekly_target)}: <b className="text-slate-200">{t.targets.weeks_hit}/{t.targets.weeks}</b>
                          {' '}· avg week {money(t.targets.avg_week)} · losing weeks {t.targets.losing_weeks}
                          {' '}· days ≥ {money(t.targets.daily_target)}: <b className="text-slate-200">{t.targets.days_hit}/{t.targets.trading_days}</b>
                        </p>
                      )}
                    </div>
                  ))}
                </div>

                <div className="overflow-x-auto rounded-lg border border-slate-700">
                  <table className="w-full min-w-[640px] text-left text-[12px]">
                    <thead className="bg-slate-900 text-[10px] uppercase text-slate-500">
                      <tr><th className="px-2 py-1.5">Pair</th><th>Traded</th><th>Book</th><th>History</th><th>Trades</th><th>Win rate</th><th>Net</th><th>Max DD</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(detail.results ?? {}).flatMap(([sym, r]) =>
                        Object.entries(r.summary ?? {}).map(([b, s]) => (
                          <tr key={`${sym}${b}`} onClick={() => { setPair(sym); setBook(b as 'oblivion' | 'agents') }}
                            className={`cursor-pointer border-t border-slate-800 hover:bg-slate-800/40 ${pair === sym && book === b ? 'bg-cyan-500/10' : ''}`}>
                            <td className="px-2 py-1.5 font-medium text-slate-200">{sym}</td>
                            <td className={detail.traded?.approved?.[sym] ? 'text-emerald-300' : 'text-slate-500'}
                              title={detail.traded?.approved?.[sym] ?? detail.traded?.rejected?.[sym] ?? ''}>
                              {detail.traded ? (detail.traded.approved?.[sym] ? 'yes' : 'no') : '—'}
                            </td>
                            <td className="text-slate-400">{BOOKS[b] ?? b}</td>
                            <td className="text-slate-500">{when(r.from).slice(0, 10)} → {when(r.to).slice(0, 10)}</td>
                            <td className="text-slate-300">{s.trades}</td>
                            <td className={(s.win_rate ?? 0) >= 0.9 ? 'text-emerald-300' : 'text-amber-300'}>{pct(s.win_rate)}</td>
                            <td className={s.net_profit >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(s.net_profit)}</td>
                            <td className="text-slate-400">{s.max_drawdown_pct?.toFixed(2)}%</td>
                          </tr>
                        )))}
                    </tbody>
                  </table>
                </div>

                {pair && (
                  <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-semibold text-slate-200">{pair} — {BOOKS[book]}</h3>
                      {(['oblivion', 'agents'] as const).map((b) => (
                        <button key={b} type="button" onClick={() => setBook(b)}
                          className={`rounded-lg border px-2 py-0.5 text-[11px] ${book === b ? 'border-cyan-500 text-cyan-200' : 'border-slate-700 text-slate-400'}`}>
                          {BOOKS[b]}
                        </button>
                      ))}
                    </div>
                    {candles.length > 0 && <BacktestChart candles={candles} trades={trades} focusTime={focusTime} />}
                    <div className="max-h-96 overflow-auto rounded-lg border border-slate-700">
                      <table className="w-full min-w-[820px] text-left text-[12px]">
                        <thead className="sticky top-0 bg-slate-900 text-[10px] uppercase text-slate-500">
                          <tr><th className="px-2 py-1.5">#</th><th>Side</th><th>Entry</th><th>Fill</th><th>Stop</th><th>TP1</th><th>Exit</th><th>Why</th><th>R</th><th>P&amp;L</th><th>Balance</th></tr>
                        </thead>
                        <tbody>
                          {trades.map((t, i) => (
                            <tr key={`${t.entry_time}${i}`} onClick={() => setFocusTime(t.entry_time)}
                              className="cursor-pointer border-t border-slate-800 hover:bg-slate-800/40">
                              <td className="px-2 py-1 text-slate-500">{i + 1}</td>
                              <td className={t.action === 'buy' ? 'text-sky-300' : 'text-pink-300'}>{t.action.toUpperCase()} {t.kind}</td>
                              <td className="text-slate-400">{when(t.entry_time)}</td>
                              <td className="text-slate-300">{t.fill?.toPrecision(6)}</td>
                              <td className="text-slate-400">{t.initial_stop?.toPrecision(6)}</td>
                              <td className="text-slate-400">{t.tp1?.toPrecision(6)}</td>
                              <td className="text-slate-400">{when(t.exit_time)}</td>
                              <td className="text-slate-500">{t.exit_reason}</td>
                              <td className={t.outcome === 'win' ? 'text-emerald-300' : t.outcome === 'loss' ? 'text-red-300' : 'text-slate-400'}>
                                {t.r >= 0 ? '+' : ''}{t.r.toFixed(3)}
                              </td>
                              <td className={t.pnl >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(t.pnl)}</td>
                              <td className="text-slate-400">{money(t.equity_after)}</td>
                            </tr>
                          ))}
                          {!trades.length && <tr><td colSpan={11} className="px-2 py-3 text-center text-slate-500">No trades for this pair and book.</td></tr>}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </>
            )}
          </main>
        </div>
      </div>
    </>
  )
}
