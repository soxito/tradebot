import Head from 'next/head';
import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Bot, Brain, RefreshCw, Settings2, Sparkles, Users } from 'lucide-react';
import { apiClient } from '@/services/api';
import OblivionDashboard, { type OblivionRead } from '@/components/OblivionDashboard';
import OblivionSettingsModal from '@/components/OblivionSettingsModal';

const TradingViewChart = dynamic(() => import('@/components/TradingViewChart'), { ssr: false });
const OblivionMultiTfPanel = dynamic(() => import('@/components/OblivionMultiTfPanel'), { ssr: false });

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w'];
const EXCHANGES = ['bitget', 'binance', 'bybit', 'okx', 'kucoin'];
const QUICK_PAIRS = ['XAUUSD', 'BTC/USDT', 'ETH/USDT', 'EURUSD', 'GBPJPY', 'US30', 'SOL/USDT', 'XAGUSD'];

interface RoomDecision {
  agent_name?: string;
  agent_role?: string;
  action?: string;
  confidence?: number;
  reasoning?: string;
  source?: string;
}
interface RoomResult {
  session_id?: string;
  symbol?: string;
  timeframe?: string;
  final_action?: string;
  final_confidence?: number;
  final_reasoning?: string;
  decisions?: RoomDecision[];
  errors?: string[];
  oblivion?: OblivionRead | null;
  news_blocked?: boolean;
  error?: string;
  ai_enabled?: boolean;
}
interface AiAnalysis {
  analysis: string;
  provider?: string | null;
  note?: string | null;
  decision?: string;
  position_advice?: string | null;
}

/** Same rule the Kronos page uses: crypto gets `BASE/QUOTE`, everything else stays as typed. */
function normalizePair(raw: string): string {
  const s = raw.trim().toUpperCase().replace(/\s+/g, '');
  if (!s) return s;
  if (s.includes('/')) return s;
  for (const q of ['USDT', 'USDC', 'BTC', 'ETH']) {
    if (s.endsWith(q) && s.length > q.length) return `${s.slice(0, -q.length)}/${q}`;
  }
  return s;
}

const actionTone = (a?: string) => {
  const v = (a || '').toLowerCase();
  if (v === 'buy' || v === 'bullish' || v === 'execute' || v === 'approve') return 'text-lime-400 border-lime-500/40 bg-lime-500/10';
  if (v === 'sell' || v === 'bearish' || v === 'reject' || v === 'cancel') return 'text-red-400 border-red-500/40 bg-red-500/10';
  return 'text-gray-300 border-gray-600 bg-gray-800/60';
};

/** Where the last-used selection is kept. Separate from the Kronos page's keys:
 *  the two are different workflows, and picking gold here should not drag the
 *  Kronos page off whatever it was left on. */
const PREFS_KEY = 'oblivion:prefs';

export default function OblivionPage() {
  // These MUST start on deterministic defaults. Reading localStorage during the
  // first render makes the server HTML (no window, so the default) disagree with
  // the client's (the stored pair), which is a hydration mismatch. The stored
  // selection is applied in an effect after mount instead — see below.
  const [pairInput, setPairInput] = useState('XAUUSD');
  const [symbol, setSymbol] = useState('XAUUSD');
  const [exchange, setExchange] = useState('bitget');
  const [timeframe, setTimeframe] = useState('1h');
  // False until the stored selection has been applied. The chart and the
  // multi-timeframe panel wait for it so a refresh does not fire a throwaway
  // run against the default pair before switching to the remembered one.
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  // The read is stored with the instrument it was computed for. Clearing it in
  // an effect would be one render too late: React paints the new symbol with the
  // old read still in state, flashing the previous pair's levels under the new
  // pair's name. Matching on the key makes the switch atomic instead.
  const [readState, setReadState] = useState<{ key: string; data: OblivionRead } | null>(null);
  const [chartKey, setChartKey] = useState(0);
  const [showSettings, setShowSettings] = useState(false);

  const [ai, setAi] = useState<AiAnalysis | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiErr, setAiErr] = useState<string | null>(null);

  const [room, setRoom] = useState<RoomResult | null>(null);
  const [roomLoading, setRoomLoading] = useState(false);
  const [roomErr, setRoomErr] = useState<string | null>(null);

  const applyPair = useCallback(() => {
    const norm = normalizePair(pairInput);
    if (!norm) return;
    setPairInput(norm);
    setSymbol(norm);
  }, [pairInput]);

  // Identifies what is on screen. Everything derived — the engine read, the AI
  // narrative, the room verdict — belongs to one instrument and timeframe, and
  // a response that arrives after the user moved on must be dropped rather than
  // shown under the new pair's name.
  const viewKey = `${exchange}|${symbol}|${timeframe}`;
  const viewKeyRef = useRef(viewKey);
  viewKeyRef.current = viewKey;

  // Only ever render a read that belongs to what is currently selected.
  const read = readState?.key === viewKey ? readState.data : null;

  // Apply the last-used selection after mount, so the first client render still
  // matches the server HTML.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(PREFS_KEY);
      if (raw) {
        const p = JSON.parse(raw) as { symbol?: unknown; exchange?: unknown; timeframe?: unknown };
        // Validate before applying: a hand-edited or outdated entry must not be
        // able to pin the page to an exchange or timeframe the UI cannot show.
        if (typeof p.symbol === 'string' && p.symbol.trim()) {
          const norm = normalizePair(p.symbol);
          setSymbol(norm);
          setPairInput(norm);
        }
        if (typeof p.exchange === 'string' && EXCHANGES.includes(p.exchange)) setExchange(p.exchange);
        if (typeof p.timeframe === 'string' && TIMEFRAMES.includes(p.timeframe)) setTimeframe(p.timeframe);
      }
    } catch { /* ignore private-mode, quota and malformed-JSON errors */ }
    setPrefsLoaded(true);
  }, []);

  // Remember the selection. Guarded on prefsLoaded so the mount-time defaults
  // can never overwrite the stored pair before it has been read back.
  useEffect(() => {
    if (!prefsLoaded) return;
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify({ symbol, exchange, timeframe }));
    } catch { /* ignore quota / private-mode errors */ }
  }, [prefsLoaded, symbol, exchange, timeframe]);

  // A new pair / timeframe invalidates every read built on the old one.
  useEffect(() => {
    setAi(null);
    setAiErr(null);
    setAiLoading(false);
    setRoom(null);
    setRoomErr(null);
    setRoomLoading(false);
  }, [symbol, timeframe, exchange]);

  const runAi = useCallback(async () => {
    const key = viewKeyRef.current;
    setAiLoading(true);
    setAiErr(null);
    try {
      // Hand the endpoint the exact read the chart computed on its own bars —
      // a server-side refetch could land on a different feed (futures vs
      // Swissquote spot) and quote levels the picture does not show.
      const r = read
        ? await apiClient.kronos.oblivionAnalysisFor({ symbol, timeframe, exchange, oblivion: read })
        : await apiClient.kronos.oblivionAnalysis(exchange, symbol, { timeframe });
      if (viewKeyRef.current !== key) return;
      setAi(r.data as AiAnalysis);
    } catch (e: any) {
      if (viewKeyRef.current !== key) return;
      setAiErr(e?.response?.data?.detail || e?.message || 'AI analysis failed');
    } finally {
      if (viewKeyRef.current === key) setAiLoading(false);
    }
  }, [exchange, symbol, timeframe, read]);

  const runRoom = useCallback(async () => {
    const key = viewKeyRef.current;
    setRoomLoading(true);
    setRoomErr(null);
    try {
      const r = await apiClient.analyzeSymbol({ symbol, timeframe });
      if (viewKeyRef.current !== key) return;
      const data = r.data as RoomResult;
      if (data?.error) setRoomErr(data.error);
      setRoom(data);
    } catch (e: any) {
      if (viewKeyRef.current !== key) return;
      setRoomErr(e?.response?.data?.detail || e?.message || 'The room could not be convened');
    } finally {
      if (viewKeyRef.current === key) setRoomLoading(false);
    }
  }, [symbol, timeframe]);

  const sig = read?.signal;
  const action = (sig?.action || 'wait').toUpperCase();
  const headline = useMemo(() => {
    const sc: any = read?.score;
    if (!sc) return null;
    const kind = sig?.kind ? ` ${sig.kind}` : '';
    const state = sig && sig.action !== 'wait' && !sig.confirmed ? ' (forming)' : '';
    const buy = sc.buy ?? sc.bull, sell = sc.sell ?? sc.bear;
    return `${read?.mode ?? 'HYBRID'} · ${action}${kind}${state} · buy ${buy} / sell ${sell} `
      + `(scalp ≥${sc.scalp_min ?? sc.required}, sniper ≥${sc.sniper_min ?? 9}) · ${read?.bias_label ?? 'NEUTRAL'} · ${read?.session?.name ?? 'OFF'}`;
  }, [read, action, sig]);

  return (
    <>
      <Head><title>Oblivion AI — TradeBot</title></Head>
      <div className="space-y-4">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-lime-500/15 border border-lime-500/30"><Sparkles className="w-6 h-6 text-lime-400" /></div>
            <div>
              <h1 className="text-2xl font-bold"><span className="text-lime-400">OBLIVION</span> <span className="text-gray-400 text-base font-semibold">MASTER v5 — Scalper + Sniper</span></h1>
              <p className="text-sm text-gray-500">Two scorers on one chart: a scalp score (≥6) and a sniper score (≥9 + HTF, sweep, zone and EMA 200) — with the trade plan, gates and its own TP/SL track record.</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setShowSettings(true)} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 text-xs font-semibold"><Settings2 className="w-3.5 h-3.5" /> Oblivion settings</button>
            {headline && (
              <div className={`text-sm font-semibold px-3 py-1.5 rounded-lg border ${actionTone(sig?.action)}`} data-testid="oblivion-headline">
                {headline}{read?.news?.blocked ? ' · NEWS BLOCKED' : ''}
              </div>
            )}
          </div>
        </div>

        {/* Controls */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 flex flex-wrap items-end gap-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Pair</label>
            <div className="flex gap-2">
              <input
                value={pairInput}
                onChange={(e) => setPairInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') applyPair(); }}
                className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm w-44 focus:border-lime-500 focus:outline-none"
                placeholder="XAUUSD, BTC/USDT…"
              />
              <button onClick={applyPair} className="px-3 py-2 rounded-lg bg-lime-600 hover:bg-lime-500 text-black text-sm font-semibold">Go</button>
            </div>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Exchange</label>
            <select value={exchange} onChange={(e) => setExchange(e.target.value)} className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm">
              {EXCHANGES.map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Timeframe</label>
            <div className="flex gap-1">
              {TIMEFRAMES.map((tf) => (
                <button key={tf} onClick={() => setTimeframe(tf)}
                  className={`px-2.5 py-1.5 rounded-md text-xs font-semibold ${tf === timeframe ? 'bg-lime-600 text-black' : 'bg-gray-800 text-gray-300 hover:bg-gray-700'}`}>
                  {tf}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5 ml-auto">
            {QUICK_PAIRS.map((p) => (
              <button key={p} onClick={() => { setPairInput(p); setSymbol(p); }}
                className={`px-2 py-1 rounded text-[11px] border ${p === symbol ? 'border-lime-500/60 text-lime-300 bg-lime-500/10' : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}>
                {p}
              </button>
            ))}
            <button onClick={() => setChartKey((k) => k + 1)} title="Reload candles" className="px-2 py-1 rounded text-[11px] border border-gray-700 text-gray-400 hover:text-gray-200">
              <RefreshCw className="w-3 h-3 inline" />
            </button>
          </div>
        </div>

        {/* Chart with every overlay, computed on the bars it draws */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-2 min-h-[460px]">
          {prefsLoaded && (
            <TradingViewChart
              key={chartKey}
              symbol={symbol}
              exchange={exchange}
              timeframe={timeframe}
              showOblivion
              onOblivion={(r) => setReadState(r ? { key: viewKeyRef.current, data: r as OblivionRead } : null)}
              strategyName="Oblivion MASTER v5"
              strategyAction={sig?.action}
              strategyScore={read?.score ? (read.score.bull - read.score.bear) / Math.max(1, read.score.required) : undefined}
              fitMode="recent"
            />
          )}
        </div>

        {/* Dashboard */}
        <OblivionDashboard read={read} symbol={symbol} timeframe={timeframe} />

        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {/* AI analysis */}
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4" data-testid="oblivion-ai">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-gray-200 flex items-center gap-2"><Brain className="w-4 h-4 text-purple-400" /> AI analysis</h2>
              <button onClick={runAi} disabled={aiLoading || !read}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-40 text-white text-xs font-semibold">
                <RefreshCw className={`w-3.5 h-3.5 ${aiLoading ? 'animate-spin' : ''}`} /> {ai ? 'Re-run' : 'Analyse this read'}
              </button>
            </div>
            <p className="text-[11px] text-gray-500 mb-3">The model reads the same engine result as the chart. Its answer is only shown when it quotes the score and the plan; otherwise the engine's own summary is shown and the note says why.</p>
            {aiErr && <div className="text-xs text-red-300 flex items-center gap-2"><AlertTriangle className="w-4 h-4" /> {aiErr}</div>}
            {aiLoading && <div className="text-xs text-gray-500">Reading the chart… this can take up to a minute.</div>}
            {ai && !aiLoading && (
              <div>
                <div className="flex items-center gap-2 text-[11px] text-gray-500 mb-2">
                  <span className={`px-1.5 py-0.5 rounded border ${ai.provider ? 'border-purple-500/40 text-purple-300' : 'border-gray-600 text-gray-400'}`}>{ai.provider ?? 'engine summary'}</span>
                  {ai.note && <span className="text-orange-300">{ai.note}</span>}
                </div>
                <div className="text-sm text-gray-200 whitespace-pre-wrap leading-relaxed max-h-[420px] overflow-y-auto pr-1">{ai.analysis}</div>
                {ai.position_advice && <div className="mt-3 text-xs text-amber-300 border-t border-gray-800 pt-2">💡 {ai.position_advice}</div>}
              </div>
            )}
            {!ai && !aiLoading && !aiErr && <div className="text-xs text-gray-600">Not run yet.</div>}
          </div>

          {/* Trading room */}
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4" data-testid="oblivion-room">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-gray-200 flex items-center gap-2"><Users className="w-4 h-4 text-cyan-400" /> Trading room</h2>
              <button onClick={runRoom} disabled={roomLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white text-xs font-semibold">
                <Bot className={`w-3.5 h-3.5 ${roomLoading ? 'animate-pulse' : ''}`} /> {room ? 'Convene again' : 'Convene the room'}
              </button>
            </div>
            <p className="text-[11px] text-gray-500 mb-3">Every seat is handed this Oblivion read (score, bias, structure, levels, session, news) and is required to argue from it. Runs the full seven-seat pipeline on {symbol} {timeframe}.</p>
            {roomErr && <div className="text-xs text-red-300 flex items-center gap-2 mb-2"><AlertTriangle className="w-4 h-4" /> {roomErr}</div>}
            {roomLoading && <div className="text-xs text-gray-500">The agents are in session — usually one to three minutes.</div>}
            {room && !roomLoading && !room.error && (
              <div className="space-y-3">
                <div className={`rounded-lg border px-3 py-2 ${actionTone(room.final_action)}`}>
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-base">Verdict: {(room.final_action || 'hold').toUpperCase()}</span>
                    <span className="font-mono text-xs">{Math.round((room.final_confidence ?? 0) * 100)}%{room.news_blocked ? ' · news window' : ''}</span>
                  </div>
                  {room.final_reasoning && <p className="text-xs mt-1 opacity-90">{room.final_reasoning}</p>}
                </div>
                {room.oblivion?.summary && (
                  <p className="text-[11px] text-gray-500"><span className="text-lime-400 font-semibold">Engine read the seats argued from:</span> {room.oblivion.summary}</p>
                )}
                <div className="space-y-2 max-h-[460px] overflow-y-auto pr-1">
                  {(room.decisions || []).map((d, i) => (
                    <div key={i} className="rounded-lg border border-gray-800 bg-gray-950/40 p-2.5">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-semibold text-gray-200">{d.agent_name || d.agent_role}<span className="text-gray-600 font-normal"> · {d.agent_role}</span></span>
                        <span className={`text-[11px] font-mono px-1.5 py-0.5 rounded border ${actionTone(d.action)}`}>{(d.action || '—').toUpperCase()} {d.confidence != null ? `${Math.round(d.confidence * 100)}%` : ''}</span>
                      </div>
                      <p className="text-[12px] text-gray-400 leading-relaxed">{d.reasoning}</p>
                    </div>
                  ))}
                </div>
                {room.errors && room.errors.length > 0 && (
                  <div className="text-[11px] text-orange-300">{room.errors.join(' · ')}</div>
                )}
              </div>
            )}
            {!room && !roomLoading && !roomErr && <div className="text-xs text-gray-600">Not convened yet.</div>}
          </div>
        </div>

        {/* All timeframes, Kronos-confirmed */}
        {prefsLoaded && <OblivionMultiTfPanel exchange={exchange} symbol={symbol} />}
        <OblivionSettingsModal open={showSettings} onClose={() => setShowSettings(false)} onSaved={() => setChartKey(k => k + 1)} />
      </div>
    </>
  );
}
