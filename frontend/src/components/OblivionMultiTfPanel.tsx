/**
 * OblivionMultiTfPanel — OBLIVION AI engine signals on every timeframe,
 * each confirmed by its Kronos forecast (GET /plugins/kronos/oblivion/...).
 *
 * Oblivion leads: each row is the engine's BUY / SELL / WAIT with its Entry /
 * SL / TP1-3; Kronos lifts confidence when aligned and vetoes when it opposes
 * decisively. The news window vetoes every row. "Execute" routes one row to
 * every configured venue via /plugins/kronos/oblivion/execute (two-click).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, ShieldAlert, Zap } from 'lucide-react';
import { apiClient } from '@/services/api';

export interface OblivionTfSignal {
  timeframe: string;
  action: 'buy' | 'sell' | 'wait';
  confirmed: boolean;
  new: boolean;
  score: number;
  required: number;
  bull: number;
  bear: number;
  market_bias: number;
  bias_label: string;
  entry: number | null;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  tp3: number | null;
  risk_reward: number | null;
  confidence: number;
  kronos_direction: string | null;
  kronos_pct_change: number | null;
  kronos_confidence: number | null;
  kronos_aligned: boolean | null;
  vetoed_by: string | null;
  news_blocked: boolean;
  session: string;
  structure: string;
  summary: string;
  error: string | null;
}

export interface OblivionMultiTfResponse {
  exchange: string;
  symbol: string;
  generated_at: number;
  with_kronos: boolean;
  consensus: { action: 'buy' | 'sell' | 'wait'; score: number; agreeing_tfs: string[]; opposing_tfs: string[]; vetoed_tfs: string[]; note: string };
  per_tf: OblivionTfSignal[];
  news_blocked: boolean;
  note: string | null;
}

const fmt = (v: number | null | undefined, d = 2) => (v == null ? '—' : v >= 1000 ? v.toFixed(d) : v.toFixed(Math.max(d, 4)));

export default function OblivionMultiTfPanel({
  exchange, symbol, isExecutable = true, onExecuted,
}: { exchange: string; symbol: string; isExecutable?: boolean; onExecuted?: () => void }) {
  const [data, setData] = useState<OblivionMultiTfResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirmTf, setConfirmTf] = useState<string | null>(null);
  const [execTf, setExecTf] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, { ok: boolean; msg: string }>>({});

  // Identifies the instrument a response belongs to. The engine run can take up
  // to three minutes, so without this a slow answer for the previous pair could
  // land after the new one and repopulate the table with the wrong instrument.
  const wantKey = `${exchange}|${symbol}`;
  const wantKeyRef = useRef(wantKey);
  wantKeyRef.current = wantKey;

  const load = useCallback(async () => {
    if (!symbol) return;
    const key = `${exchange}|${symbol}`;
    // Drop everything tied to the previous pair up front — rows, errors and the
    // half-finished execute/confirm state — so nothing on screen outlives it.
    setData(null);
    setResults({});
    setConfirmTf(null);
    setExecTf(null);
    setLoading(true);
    setErr(null);
    try {
      const _fn = (apiClient as any)?.kronos?.oblivion
        ? (apiClient as any).kronos.oblivion(exchange, symbol)
        : (await import('@/services/api')).api.get(`/plugins/kronos/oblivion/${exchange}/${symbol.replace('/', '')}`, { timeout: 180000 });
      const r = await _fn;
      if (wantKeyRef.current !== key) return;
      setData((r as any).data as OblivionMultiTfResponse);
    } catch (e: any) {
      if (wantKeyRef.current !== key) return;
      setErr(e?.response?.data?.detail || e?.message || 'Failed to run the Oblivion engine');
    } finally {
      if (wantKeyRef.current === key) setLoading(false);
    }
  }, [exchange, symbol]);

  useEffect(() => { load(); }, [load]);

  const execute = async (row: OblivionTfSignal) => {
    if (row.action === 'wait' || row.entry == null || row.stop_loss == null || row.tp2 == null && row.tp1 == null) return;
    if (confirmTf !== row.timeframe) { setConfirmTf(row.timeframe); return; }
    setConfirmTf(null);
    setExecTf(row.timeframe);
    try {
      const _execFn = (apiClient as any)?.kronos?.executeOblivion
        ? (apiClient as any).kronos.executeOblivion({
            symbol, side: row.action, entry: row.entry, stop_loss: row.stop_loss,
            take_profit: (row.tp2 ?? row.tp1) as number, timeframe: row.timeframe,
          })
        : (await import('@/services/api')).api.post('/plugins/kronos/oblivion/execute', null, { params: { symbol, side: row.action, entry: row.entry, stop_loss: row.stop_loss, take_profit: (row.tp2 ?? row.tp1) as number, timeframe: row.timeframe } });
      const r = await _execFn;
      const ok = r.data?.ok ?? false;
      const venues = (r.data?.placed ?? []).map((o: any) => `${o.venue}/${o.role ?? ''}`.replace(/\/$/, '')).join(', ');
      setResults((p) => ({ ...p, [row.timeframe]: ok
        ? { ok: true, msg: `${row.action.toUpperCase()} sent → ${venues || 'room engine'}` }
        : { ok: false, msg: r.data?.errors?.[0]?.reason ?? r.data?.reason ?? 'Execution failed' } }));
      if (ok) onExecuted?.();
    } catch (e: any) {
      setResults((p) => ({ ...p, [row.timeframe]: { ok: false, msg: e?.response?.data?.detail || e?.message || 'Execution failed' } }));
    } finally {
      setExecTf(null);
    }
  };

  const c = data?.consensus;
  const cColor = c?.action === 'buy' ? 'text-lime-400 border-lime-500/40 bg-lime-500/10'
    : c?.action === 'sell' ? 'text-red-400 border-red-500/40 bg-red-500/10'
    : 'text-gray-300 border-gray-600 bg-gray-800/40';

  return (
    <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4" data-testid="oblivion-multi-tf">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div>
          <h2 className="text-sm font-semibold text-gray-200">Oblivion AI — all timeframes</h2>
          <p className="text-[11px] text-gray-500">OBLIVION MASTER v5 leads (scalp + sniper scores, structure, FVG/OB, sweeps, OTE, HTF, session); Kronos confirms or vetoes per timeframe.</p>
        </div>
        <button onClick={load} disabled={loading} className="flex items-center gap-1 text-xs text-gray-300 hover:text-white disabled:opacity-50">
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> Refresh
        </button>
      </div>

      {err && <div className="text-xs text-red-300 mb-2">{err}</div>}
      {data?.news_blocked && (
        <div className="flex items-center gap-2 text-xs text-orange-300 bg-orange-500/10 border border-orange-500/30 rounded px-3 py-2 mb-2">
          <ShieldAlert className="w-4 h-4" /> {data.note ?? 'News window active — no entries.'}
        </div>
      )}
      {c && (
        <div className={`flex items-center justify-between rounded-lg border px-3 py-2 mb-3 ${cColor}`}>
          <div className="text-sm font-bold">Consensus: {c.action.toUpperCase()} <span className="font-mono text-xs opacity-80">({c.score >= 0 ? '+' : ''}{c.score.toFixed(2)})</span></div>
          <div className="text-[11px] opacity-90">{c.note}</div>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono">
          <thead className="text-gray-500">
            <tr className="text-left">
              <th className="py-1 pr-2">TF</th>
              <th className="py-1 pr-2">Signal</th>
              <th className="py-1 pr-2">Score</th>
              <th className="py-1 pr-2">Bias</th>
              <th className="py-1 pr-2">Entry</th>
              <th className="py-1 pr-2">SL</th>
              <th className="py-1 pr-2">TP1 / TP2 / TP3</th>
              <th className="py-1 pr-2">Kronos</th>
              <th className="py-1 pr-2">Conf</th>
              <th className="py-1 pr-2">Session</th>
              <th className="py-1 pr-2">News</th>
              <th className="py-1"></th>
            </tr>
          </thead>
          <tbody>
            {(data?.per_tf ?? []).map((row) => {
              const sigColor = row.action === 'buy' ? 'text-lime-400' : row.action === 'sell' ? 'text-red-400' : 'text-gray-400';
              const kColor = row.kronos_direction === 'up' ? 'text-green-400' : row.kronos_direction === 'down' ? 'text-red-400' : 'text-gray-400';
              const vetoed = !!row.vetoed_by;
              const res = results[row.timeframe];
              return (
                <tr key={row.timeframe} className={`border-t border-gray-800 ${vetoed ? 'opacity-60' : ''}`} title={row.error ?? row.summary}>
                  <td className="py-1.5 pr-2 text-gray-200 font-semibold">{row.timeframe}</td>
                  <td className={`py-1.5 pr-2 font-bold ${sigColor}`}>
                    {row.error ? <span className="text-gray-600">n/a</span> : row.action.toUpperCase()}
                    {row.action !== 'wait' && !row.confirmed && <span className="text-gray-500 font-normal"> forming</span>}
                    {vetoed && <span className="text-orange-400 font-normal"> · veto:{row.vetoed_by}</span>}
                  </td>
                  <td className="py-1.5 pr-2"><span className="text-lime-400">{row.bull}</span>/<span className="text-red-400">{row.bear}</span> <span className="text-gray-600">≥{row.required}</span></td>
                  <td className={`py-1.5 pr-2 ${row.market_bias === 1 ? 'text-lime-400' : row.market_bias === -1 ? 'text-red-400' : 'text-gray-400'}`}>{row.bias_label} <span className="text-gray-500">{row.structure}</span></td>
                  <td className="py-1.5 pr-2 text-gray-200">{fmt(row.entry)}</td>
                  <td className="py-1.5 pr-2 text-red-300">{fmt(row.stop_loss)}</td>
                  <td className="py-1.5 pr-2 text-green-300">{fmt(row.tp1)} / {fmt(row.tp2)} / {fmt(row.tp3)}{row.risk_reward != null && <span className="text-gray-500"> R{row.risk_reward}</span>}</td>
                  <td className={`py-1.5 pr-2 ${kColor}`}>
                    {row.kronos_direction ? `${row.kronos_direction.toUpperCase()} ${row.kronos_pct_change != null ? `${row.kronos_pct_change >= 0 ? '+' : ''}${row.kronos_pct_change.toFixed(2)}%` : ''}` : '—'}
                    {row.kronos_aligned === true && <span className="text-lime-400"> ✓</span>}
                    {row.kronos_aligned === false && <span className="text-orange-400"> ✗</span>}
                  </td>
                  <td className="py-1.5 pr-2 text-gray-200">{Math.round((row.confidence ?? 0) * 100)}%</td>
                  <td className="py-1.5 pr-2 text-gray-300">{row.session}</td>
                  <td className={`py-1.5 pr-2 ${row.news_blocked ? 'text-orange-400' : 'text-lime-400'}`}>{row.news_blocked ? 'BLOCKED' : 'CLEAR'}</td>
                  <td className="py-1.5 text-right">
                    {row.action !== 'wait' && !vetoed && row.entry != null && (
                      <button
                        onClick={() => execute(row)}
                        disabled={!isExecutable || execTf === row.timeframe}
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-semibold disabled:opacity-40 ${
                          confirmTf === row.timeframe
                            ? 'bg-amber-600/30 border-amber-500/50 text-amber-200'
                            : row.action === 'buy' ? 'bg-lime-700/30 border-lime-600/40 text-lime-200 hover:bg-lime-700/50' : 'bg-red-700/30 border-red-600/40 text-red-200 hover:bg-red-700/50'
                        }`}
                        title="Route this timeframe's plan to every configured venue"
                      >
                        <Zap className="w-3 h-3" />
                        {execTf === row.timeframe ? 'Sending…' : confirmTf === row.timeframe ? 'Confirm' : 'Execute'}
                      </button>
                    )}
                    {res && <div className={`text-[10px] mt-0.5 ${res.ok ? 'text-lime-300' : 'text-red-300'}`}>{res.msg}</div>}
                  </td>
                </tr>
              );
            })}
            {!loading && !data?.per_tf?.length && !err && (
              <tr><td colSpan={12} className="py-3 text-center text-gray-500">No timeframes analysed yet.</td></tr>
            )}
            {loading && !data && (
              <tr><td colSpan={12} className="py-3 text-center text-gray-500">Running the engine on every timeframe…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
