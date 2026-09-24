import { useState } from 'react'

export interface PfSummary {
  start_equity: number; end_equity: number; net_profit: number; return_pct: number
  gross_win: number; gross_loss: number; trades: number; wins: number; losses: number; win_rate: number | null
  max_drawdown_pct: number; days_with_closes: number; days_min_hit: number; days_capped: number
  losing_days: number; avg_day_pct: number; best_day_pct: number; worst_day_pct: number; avg_risk_pct: number
}
interface PfDay { date: string; start: number; pnl: number; pct: number; wins: number; losses: number; opened: number; min_hit: boolean; capped: boolean }
interface PfTrade { symbol: string; timeframe: string; entry_time: number; exit_time: number; outcome: string; r: number; risk_pct: number; risk_usd: number; pnl: number }
interface PfBook { summary: PfSummary; days: PfDay[]; trades: PfTrade[] }
interface PfAccount {
  venue: string; id: number; name: string; login?: string; type: string; currency?: string; start_equity: number
  room_trades_it: boolean; skipped?: string; not_listed?: string[]
  paced?: PfBook; wf_paced?: PfBook; flat?: { summary: PfSummary }; wf_flat?: { summary: PfSummary }
}
export interface PortfolioDoc {
  id: number
  params: { days: number; min_risk_pct: number; max_risk_pct: number; daily_min_pct: number; daily_max_pct: number }
  window: { from: number; to: number; days: number }
  accounts: PfAccount[]
  totals: Record<'paced' | 'flat' | 'wf_paced' | 'wf_flat', Partial<PfSummary> & { accounts?: number }>
}

const BOOKS: [keyof PortfolioDoc['totals'], string, string][] = [
  ['wf_paced', 'Honest · paced to the day target', 'Pairs chosen only on data before the window — what to expect.'],
  ['wf_flat', 'Honest · band floor only', 'Same trades, every one at the minimum risk.'],
  ['paced', "Today's approved pairs · paced", 'Chosen on this same history — reads high.'],
]
const money = (x?: number) => (x ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })
const when = (t: number) => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')

export default function PortfolioBacktestResult({ doc }: { doc: PortfolioDoc }) {
  const p = doc.params
  const [acct, setAcct] = useState(() => doc.accounts.find((a) => a.wf_paced)?.id ?? null)
  const current = doc.accounts.find((a) => a.id === acct && a.wf_paced)
  return (
    <div className="space-y-3">
      <p className="text-[12px] text-slate-400">
        {when(doc.window.from).slice(0, 10)} → {when(doc.window.to).slice(0, 10)} · risk {p.min_risk_pct}–{p.max_risk_pct}% per trade ·
        day minimum {p.daily_min_pct}% of the portfolio, cap {p.daily_max_pct}% (no new entries after it)
      </p>
      <div className="grid gap-3 md:grid-cols-3">
        {BOOKS.map(([key, label, note]) => {
          const t = doc.totals[key]
          if (!t || t.start_equity == null) return null
          return (
            <div key={key} className="rounded-xl border border-slate-700 bg-slate-900/40 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-400">{label}</p>
              <p className={`mt-1 text-2xl font-semibold ${(t.net_profit ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                {(t.return_pct ?? 0) >= 0 ? '+' : ''}{(t.return_pct ?? 0).toFixed(1)}%
              </p>
              <p className="text-[12px] text-slate-300">{money(t.start_equity)} → {money(t.end_equity)}</p>
              <p className="mt-1 text-[12px]">
                <span className="text-emerald-300">+{money(t.gross_win)}</span>{' '}
                <span className="text-red-300">−{money(t.gross_loss)}</span>{' '}
                <span className="text-slate-400">· {t.wins}W / {t.losses}L</span>
              </p>
              <p className="mt-1 text-[10px] text-slate-500">{note}</p>
            </div>
          )
        })}
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-700">
        <table className="w-full min-w-[760px] text-left text-[12px]">
          <thead className="bg-slate-900 text-[10px] uppercase text-slate-500">
            <tr><th className="px-2 py-1.5">Account</th><th>Room trades it</th><th>Start</th><th>Honest paced</th><th>Avg day</th>
              <th>Days ≥{p.daily_min_pct}%</th><th>Capped</th><th>Losing days</th><th>Worst day</th><th>Max DD</th><th>Avg risk</th></tr>
          </thead>
          <tbody>
            {doc.accounts.map((a) => {
              const s = a.wf_paced?.summary
              return (
                <tr key={`${a.venue}${a.id}`} onClick={() => s && setAcct(a.id)}
                  className={`border-t border-slate-800 ${s ? 'cursor-pointer hover:bg-slate-800/40' : ''} ${acct === a.id ? 'bg-cyan-500/10' : ''}`}>
                  <td className="px-2 py-1.5 text-slate-200">{a.name} {a.login} <span className="text-slate-500">({a.type})</span></td>
                  <td className={a.room_trades_it ? 'text-emerald-300' : 'text-slate-500'}>{a.room_trades_it ? 'yes' : 'no'}</td>
                  <td className="text-slate-300">{money(a.start_equity)} {a.currency}</td>
                  {s ? (
                    <>
                      <td className={s.net_profit >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(s.net_profit)} ({s.return_pct.toFixed(1)}%)</td>
                      <td className="text-slate-300">{s.avg_day_pct.toFixed(2)}%</td>
                      <td className="text-slate-300">{s.days_min_hit}/{s.days_with_closes}</td>
                      <td className="text-slate-300">{s.days_capped}</td>
                      <td className="text-slate-300">{s.losing_days}</td>
                      <td className="text-red-300">{s.worst_day_pct.toFixed(2)}%</td>
                      <td className="text-slate-300">{s.max_drawdown_pct.toFixed(2)}%</td>
                      <td className="text-slate-400">{s.avg_risk_pct.toFixed(2)}%</td>
                    </>
                  ) : <td colSpan={8} className="text-slate-500">{a.skipped}</td>}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {current?.wf_paced && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-slate-200">{current.name} — every day (honest, paced)</h3>
          <div className="flex h-28 items-end gap-px rounded-lg border border-slate-700 bg-slate-900/40 p-2">
            {(() => {
              const days = current.wf_paced.days.filter((d) => d.wins + d.losses)
              const top = Math.max(p.daily_min_pct, ...days.map((d) => Math.abs(d.pct)), 1)
              return days.map((d) => (
                <div key={d.date} title={`${d.date}: ${d.pct.toFixed(2)}% (${d.wins}W/${d.losses}L)`}
                  className={`flex-1 ${d.pct >= 0 ? (d.min_hit ? 'bg-sky-400' : 'bg-emerald-500') : 'bg-red-500'}`}
                  style={{ height: `${Math.max(2, (Math.abs(d.pct) / top) * 100)}%` }} />
              ))
            })()}
          </div>
          <p className="text-[10px] text-slate-500">Bar height = size of the day&apos;s move; red = losing day, blue = the {p.daily_min_pct}% minimum reached.</p>
          <div className="max-h-80 overflow-auto rounded-lg border border-slate-700">
            <table className="w-full min-w-[680px] text-left text-[12px]">
              <thead className="sticky top-0 bg-slate-900 text-[10px] uppercase text-slate-500">
                <tr><th className="px-2 py-1.5">Pair</th><th>TF</th><th>Entry</th><th>Exit</th><th>Outcome</th><th>R</th><th>Risk</th><th>P&amp;L</th></tr>
              </thead>
              <tbody>
                {current.wf_paced.trades.slice().reverse().map((t, i) => (
                  <tr key={`${t.symbol}${t.entry_time}${i}`} className="border-t border-slate-800">
                    <td className="px-2 py-1 text-slate-200">{t.symbol}</td>
                    <td className="text-slate-400">{t.timeframe}</td>
                    <td className="text-slate-400">{when(t.entry_time)}</td>
                    <td className="text-slate-400">{when(t.exit_time)}</td>
                    <td className={t.outcome === 'win' ? 'text-emerald-300' : t.outcome === 'loss' ? 'text-red-300' : 'text-slate-400'}>{t.outcome}</td>
                    <td className="text-slate-300">{t.r.toFixed(3)}</td>
                    <td className="text-slate-400">{t.risk_pct.toFixed(2)}%</td>
                    <td className={t.pnl >= 0 ? 'text-emerald-300' : 'text-red-300'}>{money(t.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
