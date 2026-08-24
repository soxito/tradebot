import Head from 'next/head';
import dynamic from 'next/dynamic';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Telescope, RefreshCw, TrendingUp, TrendingDown, Minus,
  Volume2, Settings2, Cpu, AlertTriangle,
  BrainCircuit, Sparkles, CheckCircle2, Loader2, Wallet,
  Search, ChevronDown, Database, Download,
  Crosshair, Zap, Check, X, Rocket, ShieldAlert, ChevronRight, Ban,
} from 'lucide-react';
import { apiClient } from '@/services/api';
import { useKronosForecast } from '@/hooks/useKronosForecast';
import type {
  KronosDecision, KronosDirection, KronosVolumeContext, KronosVolumeRegime,
} from '@/hooks/useKronosForecast';
import { VolumeEvidence, fmtVol } from '@/components/KronosForecastCard';
import { useJarvisSpeak } from '@/hooks/useJarvisSpeak';

// Chart is client-only (lightweight-charts touches window)
const TradingViewChart = dynamic(() => import('@/components/TradingViewChart'), { ssr: false });

const TIMEFRAMES = ['5m', '15m', '1h', '4h', '1d'];
const EXCHANGES = ['bitget', 'binance', 'bybit', 'okx', 'kucoin'];

// Supported forex / metals / macro symbols (static list — these don't appear in
// the crypto catalog but are valid inputs for the Kronos forecast endpoint).
const FOREX_METALS: { symbol: string; name: string; category: string }[] = [
  { symbol: 'XAU/USD',   name: 'Gold (Spot)',        category: 'Metals'   },
  { symbol: 'XAG/USD',   name: 'Silver (Spot)',       category: 'Metals'   },
  { symbol: 'XAU/USDT',  name: 'Gold / Tether',       category: 'Metals'   },
  { symbol: 'EUR/USD',   name: 'Euro / US Dollar',    category: 'Forex'    },
  { symbol: 'GBP/USD',   name: 'British Pound / USD', category: 'Forex'    },
  { symbol: 'USD/JPY',   name: 'US Dollar / Yen',     category: 'Forex'    },
  { symbol: 'USD/CHF',   name: 'US Dollar / Franc',   category: 'Forex'    },
  { symbol: 'AUD/USD',   name: 'Australian Dollar',   category: 'Forex'    },
  { symbol: 'NZD/USD',   name: 'New Zealand Dollar',  category: 'Forex'    },
  { symbol: 'USD/CAD',   name: 'Canadian Dollar',     category: 'Forex'    },
];

interface PairOption {
  symbol: string;
  name?: string;
  category?: string;
  price?: number | null;
  price_change_24h?: number | null;
  market_cap_rank?: number | null;
}

const normalizePair = (raw: string): string => {
  const v = (raw || '').trim().toUpperCase();
  if (!v) return '';
  if (v.includes('/')) return v;
  if (v.endsWith('USDT') && v.length > 4) return `${v.slice(0, -4)}/USDT`;
  if (v.endsWith('USD') && v.length > 3) return `${v.slice(0, -3)}/USD`;
  return v;
};

interface KronosStatus {
  available: boolean;
  engine: 'kronos' | 'heuristic' | 'unavailable';
  model_name: string;
  device: string;
  detail: string;
}

interface MarketCapInfo {
  symbol: string;
  name?: string | null;
  market_cap?: number | null;
  market_cap_rank?: number | null;
  volume_24h?: number | null;
  price?: number | null;
  price_change_24h?: number | null;
  is_crypto: boolean;
}

interface JarvisAnalysis {
  exchange: string;
  symbol: string;
  timeframe: string;
  engine: string;
  analysis: string;
  spoken: string;
  market?: MarketCapInfo | null;
  position?: PositionInfo | null;
  position_advice?: string | null;
  volume?: KronosVolumeContext | null;
  decision?: KronosDecision;
  learned: boolean;
  provider?: string | null;
  note?: string | null;
}

interface PositionInfo {
  exchange: string;
  symbol: string;
  side: string;
  size: number;
  entry_price: number;
  mark_price: number;
  pnl: number;
  pnl_pct: number;
  leverage?: number | null;
  liquidation_price?: number | null;
}

interface SniperSignal {
  id: string;
  side: 'long' | 'short';
  order_kind: 'market' | 'limit';
  label: string;
  entry: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2?: number | null;
  risk_reward: number;
  confidence: number;
  leverage: number;
  reasons: string[];
  // Volume evidence carried on every emitted entry.
  volume_24h?: number | null;
  volume_1h?: number | null;
  relative_volume?: number | null;
  volume_regime?: KronosVolumeRegime;
  volume_divergence?: string;
}

interface SniperSignalsResponse {
  exchange: string;
  symbol: string;
  timeframe: string;
  engine: string;
  anchor_price: number;
  direction: KronosDirection;
  pct_change: number;
  confidence: number;
  signals: SniperSignal[];
  volume?: KronosVolumeContext | null;
  decision?: KronosDecision;
  rationale?: string[];
  note?: string | null;
}

interface Tradability {
  exchange: string;
  symbol: string;
  tradable: boolean;
  margin_coin: string;
  available_margin: number;
  max_leverage: number | null;
  min_trade_size: number | null;
  size_precision: number | null;
  current_position: { side: string; size: number; margin: number | null; notional: number | null } | null;
  max_open_margin: number;
  max_open_notional: number;
  note?: string | null;
}

const fmtUsd = (v?: number | null): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return 'n/a';
  const abs = Math.abs(v);
  if (abs >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(v / 1e3).toFixed(2)}K`;
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
};

// Price formatter that keeps precision for small-cap coins.
const fmtPx = (v?: number | null): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (abs >= 1) return v.toFixed(4).replace(/\.?0+$/, '');
  return v.toPrecision(4);
};

export default function KronosForecastPage() {
  // Last-used pair/exchange/timeframe are persisted to localStorage, but MUST be
  // initialised with deterministic SSR-safe defaults. Reading localStorage during
  // the initial render causes a hydration mismatch (the server has no window and
  // renders the fallback, while the client renders the stored value). We hydrate
  // from localStorage in an effect AFTER mount instead — see below.
  const [pairInput, setPairInput] = useState('BTC/USDT');
  const [symbol, setSymbol] = useState('BTC/USDT');
  const [exchange, setExchange] = useState('bitget');
  const [timeframe, setTimeframe] = useState('1h');
  const [marginMode, setMarginMode] = useState<'crossed' | 'isolated'>('isolated');
  const [predLen, setPredLen] = useState(24);
  const [samples, setSamples] = useState(10);
  const [temperature, setTemperature] = useState(1.0);
  const [status, setStatus] = useState<KronosStatus | null>(null);

  // ── Pair search combobox ─────────────────────────────────────────────────
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [searchResults, setSearchResults] = useState<PairOption[]>([]);
  const [searching, setSearching] = useState(false);
  const [resolving, setResolving] = useState(false);
  const comboRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Close dropdown when clicking outside.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (comboRef.current && !comboRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  // Hydrate persisted preferences from localStorage AFTER mount so the first
  // client render matches the server HTML (prevents the hydration mismatch).
  const prefsHydrated = useRef(false);
  useEffect(() => {
    try {
      const s = window.localStorage.getItem('kronos:symbol');
      const ex = window.localStorage.getItem('kronos:exchange');
      const tf = window.localStorage.getItem('kronos:timeframe');
      const mm = window.localStorage.getItem('kronos:marginMode');
      if (s) { setSymbol(s); setPairInput(s); }
      if (ex) setExchange(ex);
      if (tf) setTimeframe(tf);
      if (mm === 'crossed' || mm === 'isolated') setMarginMode(mm);
    } catch { /* ignore private-mode / quota errors */ }
  }, []);

  // Persist the active pair/exchange/timeframe so the page reopens on the last one.
  // Skip the first run so mount-time defaults never clobber the stored values
  // before the hydrate effect above has read them.
  useEffect(() => {
    if (!prefsHydrated.current) { prefsHydrated.current = true; return; }
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem('kronos:symbol', symbol);
      window.localStorage.setItem('kronos:exchange', exchange);
      window.localStorage.setItem('kronos:timeframe', timeframe);
      window.localStorage.setItem('kronos:marginMode', marginMode);
    } catch { /* ignore quota/private-mode errors */ }
  }, [symbol, exchange, timeframe, marginMode]);

  // Debounced pair search: queries the catalog + prepends static forex/metals.
  const handlePairInputChange = (val: string) => {
    setPairInput(val);
    setDropdownOpen(true);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!val.trim()) {
      setSearchResults([]);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      setSearching(true);
      try {
        // Forex / metals — match the static list first.
        const q = val.toLowerCase();
        const fxMatches = FOREX_METALS.filter(
          (f) =>
            f.symbol.toLowerCase().includes(q) ||
            f.name.toLowerCase().includes(q) ||
            f.category.toLowerCase().includes(q),
        ).map<PairOption>((f) => ({ symbol: f.symbol, name: f.name, category: f.category }));

        // Crypto catalog — live search.
        const res = await apiClient.jarvis.pairs(val.trim(), 28);
        const crypto: PairOption[] = (res.data?.pairs || []).map((p: any) => ({
          symbol: p.symbol,
          name: p.name,
          category: 'Crypto',
          price: p.price,
          price_change_24h: p.price_change_24h,
          market_cap_rank: p.market_cap_rank,
        }));

        setSearchResults([...fxMatches, ...crypto]);
      } catch {
        setSearchResults([]);
      } finally {
        setSearching(false);
      }
    }, 280);
  };

  // Select a pair from the dropdown; resolve canonical form first.
  const selectPair = async (rawSymbol: string) => {
    setDropdownOpen(false);
    setResolving(true);
    try {
      const res = await apiClient.jarvis.resolvePair(rawSymbol.replace('/', ''));
      const resolved = res.data?.ok ? (res.data.symbol as string) : rawSymbol;
      setPairInput(resolved);
      setSymbol(resolved);
    } catch {
      const norm = normalizePair(rawSymbol);
      setPairInput(norm);
      setSymbol(norm);
    } finally {
      setResolving(false);
    }
  };

  // Apply free-text entry (Enter key or "Go" button) — resolves via API.
  const applyPair = async () => {
    if (!pairInput.trim()) return;
    setResolving(true);
    setDropdownOpen(false);
    try {
      const res = await apiClient.jarvis.resolvePair(pairInput.trim().replace('/', ''));
      if (res.data?.ok) {
        const resolved = res.data.symbol as string;
        setPairInput(resolved);
        setSymbol(resolved);
      } else {
        const norm = normalizePair(pairInput);
        setPairInput(norm);
        setSymbol(norm);
      }
    } catch {
      const norm = normalizePair(pairInput);
      setPairInput(norm);
      setSymbol(norm);
    } finally {
      setResolving(false);
    }
  };

  const params = useMemo(
    () => ({ timeframe, pred_len: predLen, samples, temperature }),
    [timeframe, predLen, samples, temperature],
  );

  const { data, loading, error, refetch } = useKronosForecast(exchange, symbol, params);

  // ── Model selector ───────────────────────────────────────────────────────
  interface KronosModelEntry { id: string; label: string; params_m: number; max_context: number; installed?: boolean; }
  const [modelCatalog, setModelCatalog] = useState<KronosModelEntry[]>([]);
  const [activeModel, setActiveModel] = useState<string>('');
  const [switching, setSwitching] = useState(false);
  const [installingAll, setInstallingAll] = useState(false);

  const refreshModels = async () => {
    try {
      const r = await apiClient.kronos.models();
      setModelCatalog(r.data?.models ?? []);
      // Always sync the highlighted model to the server's real current model so
      // the selector reflects what's actually loaded (reflects hot-swaps).
      if (r.data?.current) setActiveModel(r.data.current);
      return r.data?.models ?? [];
    } catch {
      return [];
    }
  };

  const installAllModels = async () => {
    if (installingAll) return;
    setInstallingAll(true);
    try {
      await apiClient.kronos.installAllModels();
      // Poll the catalogue until every model reports installed (or we give up).
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const models = await refreshModels();
        if (models.length > 0 && models.every((m: KronosModelEntry) => m.installed)) break;
      }
    } catch (e: any) {
      console.error('[Kronos] install-all failed', e?.response?.data?.detail || e?.message);
    } finally {
      setInstallingAll(false);
      refreshModels();
    }
  };

  const switchKronosModel = async (modelId: string) => {
    if (modelId === activeModel || switching) return;
    setSwitching(true);
    try {
      await apiClient.kronos.switchModel(modelId);
      // Poll status until the model name changes (background load on server).
      // Larger variants (Kronos-base) may download weights on first use, so
      // allow a generous window before giving up.
      let confirmed = false;
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const s = await apiClient.kronos.status().catch(() => null);
        if (s?.data?.model_name === modelId && s?.data?.available) {
          setActiveModel(modelId);
          setStatus(s.data);
          confirmed = true;
          break;
        }
      }
      // Reconcile the selector with the server's real current model either way
      // (covers slow loads that finish just after the poll window).
      await refreshModels();
      if (!confirmed) {
        const s = await apiClient.kronos.status().catch(() => null);
        if (s?.data) setStatus(s.data);
      }
      refetch();
    } catch (e: any) {
      console.error('[Kronos] model switch failed', e?.response?.data?.detail || e?.message);
    } finally {
      setSwitching(false);
    }
  };

  // Global JARVIS voice — routes through PaulChat so the voice chosen in the
  // JARVIS chat (AI voice or selected system voice) is used app-wide.
  const speakAsJarvis = useJarvisSpeak();

  const [analysis, setAnalysis] = useState<JarvisAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisErr, setAnalysisErr] = useState<string | null>(null);

  // ── Sniper signals (executable entries) ───────────────────────────────────
  const [sniper, setSniper] = useState<SniperSignalsResponse | null>(null);
  const [sniperLoading, setSniperLoading] = useState(false);
  const [sniperErr, setSniperErr] = useState<string | null>(null);
  const [marginUsd, setMarginUsd] = useState(10);   // margin (USD) committed per entry
  const [paperMode, setPaperMode] = useState(true); // default to paper for safety
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [execId, setExecId] = useState<string | null>(null);
  const [execResults, setExecResults] = useState<Record<string, { ok: boolean; msg: string }>>({});

  // Live exchange context for order sizing (available margin, max open, etc.)
  const [tradability, setTradability] = useState<Tradability | null>(null);
  const [tradabilityLoading, setTradabilityLoading] = useState(false);

  useEffect(() => {
    apiClient.kronos.status()
      .then((r) => setStatus(r.data))
      .catch(() => setStatus(null));
    refreshModels();
  }, []);

  const runAnalysis = async () => {
    setAnalyzing(true);
    setAnalysisErr(null);
    try {
      const r = await apiClient.kronos.analyze(exchange, symbol, {
        timeframe, pred_len: predLen, samples, temperature, learn: true,
      });
      setAnalysis(r.data as JarvisAnalysis);
    } catch (e: any) {
      setAnalysisErr(e?.response?.data?.detail || e?.message || 'JARVIS analysis failed');
    } finally {
      setAnalyzing(false);
    }
  };

  // Crypto USDT pairs are the only ones we can route to Bitget futures execution.
  const isExecutable = /USDT$/.test(symbol.replace('/', '').toUpperCase());

  const fetchSniper = async () => {
    setSniperLoading(true);
    setSniperErr(null);
    setConfirmId(null);
    try {
      const r = await apiClient.kronos.sniper(exchange, symbol, {
        timeframe, pred_len: predLen, samples,
      });
      setSniper(r.data as SniperSignalsResponse);
      setExecResults({});
    } catch (e: any) {
      setSniperErr(e?.response?.data?.detail || e?.message || 'Failed to build sniper signals');
      setSniper(null);
    } finally {
      setSniperLoading(false);
    }
  };

  // Live exchange context (available margin, max leverage, min size, position).
  const fetchTradability = async () => {
    if (!isExecutable) { setTradability(null); return; }
    setTradabilityLoading(true);
    try {
      const r = await apiClient.kronos.tradability(exchange, symbol);
      setTradability(r.data as Tradability);
    } catch {
      setTradability(null);
    } finally {
      setTradabilityLoading(false);
    }
  };

  useEffect(() => {
    fetchTradability();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, exchange]);

  // Execute a sniper entry — paper (simulation) by default, or live Bitget futures.
  const executeSniper = async (s: SniperSignal) => {
    // First click arms the confirm state; second click fires.
    if (confirmId !== s.id) {
      setConfirmId(s.id);
      return;
    }
    setConfirmId(null);
    setExecId(s.id);
    setExecResults((prev) => { const n = { ...prev }; delete n[s.id]; return n; });
    try {
      const entry = s.entry > 0 ? s.entry : (sniper?.anchor_price || 0);
      if (entry <= 0) throw new Error('No valid entry price');

      // The $ amount IS the margin. Notional = margin × leverage; base size =
      // notional ÷ entry. Cap the margin to what's openable (live), then round
      // the size to the contract's precision and enforce its minimum.
      let effMargin = marginUsd;
      let capped = false;
      if (!paperMode && tradability?.tradable && tradability.available_margin > 0
          && marginUsd > tradability.available_margin) {
        effMargin = tradability.available_margin;
        capped = true;
      }

      const prec = tradability?.size_precision ?? (entry >= 1 ? 3 : 4);
      const minSize = tradability?.min_trade_size ?? 0;
      const factor = Math.pow(10, prec);
      const rawSize = (effMargin * s.leverage) / entry;
      const size = Math.floor(rawSize * factor) / factor;   // floor so we never exceed margin

      if (size <= 0 || (minSize && size < minSize)) {
        const needMargin = ((minSize || 1 / factor) * entry) / s.leverage;
        throw new Error(`Below Bitget min size (${minSize || 1 / factor}). Need ≥ $${needMargin.toFixed(2)} margin at ${s.leverage}x.`);
      }
      const sizeStr = String(size);
      const slPct = Math.abs(entry - s.stop_loss) / entry * 100;
      const tpPct = Math.abs(s.take_profit_1 - entry) / entry * 100;

      if (paperMode) {
        await apiClient.placeSimOrder({
          symbol,
          side: s.side === 'long' ? 'buy' : 'sell',
          amount: Number(sizeStr),
          price: s.order_kind === 'limit' ? entry : undefined,
          order_type: s.order_kind,
          trade_type: 'futures',
          leverage: s.leverage,
          margin_mode: marginMode,
          auto_sl: true,
        });
        setExecResults((p) => ({
          ...p,
          [s.id]: { ok: true, msg: `Paper ${s.side.toUpperCase()} · $${effMargin.toFixed(2)} margin (${sizeStr} @ ${s.leverage}x)` },
        }));
      } else {
        await apiClient.createBitgetFuturesOrder({
          symbol: symbol.replace('/', ''),
          margin_coin: 'USDT',
          side: s.side === 'long' ? 'buy' : 'sell',
          order_type: s.order_kind,
          size: sizeStr,
          price: s.order_kind === 'limit' ? String(entry) : undefined,
          margin_mode: marginMode,
          leverage: s.leverage,
          trade_side: 'open',
          product_type: 'USDT-FUTURES',
          stop_loss_pct: Number(slPct.toFixed(2)),
          take_profit_pct: Number(tpPct.toFixed(2)),
        });
        setExecResults((p) => ({
          ...p,
          [s.id]: { ok: true, msg: `LIVE ${s.side.toUpperCase()} · $${effMargin.toFixed(2)} margin${capped ? ' (capped to available)' : ''} · ${sizeStr} @ ${s.leverage}x · SL ${slPct.toFixed(1)}% / TP ${tpPct.toFixed(1)}%` },
        }));
        // Refresh available margin / position after a live order.
        fetchTradability();
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      const msg = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ')
        : (detail?.msg || e?.message || 'Execution failed');
      setExecResults((p) => ({ ...p, [s.id]: { ok: false, msg } }));
    } finally {
      setExecId(null);
    }
  };

  // Auto-run JARVIS analysis (and learn) whenever a fresh forecast arrives.
  const forecastSig = data?.signal
    ? `${symbol}|${timeframe}|${data.signal.target_price}|${data.signal.anchor_price}`
    : '';
  useEffect(() => {
    if (!forecastSig) return;
    runAnalysis();
    fetchSniper();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forecastSig]);

  const speak = () => {
    if (data?.signal?.summary) speakAsJarvis(data.signal.summary);
  };

  const speakAnalysis = () => {
    if (analysis?.analysis) speakAsJarvis(analysis.analysis);
  };

  const sig = data?.signal;
  const dirColor = sig?.direction === 'up' ? 'text-green-400'
    : sig?.direction === 'down' ? 'text-red-400'
    : sig?.direction === 'no_trade' ? 'text-amber-400' : 'text-yellow-400';
  const DirIcon = sig?.direction === 'up' ? TrendingUp
    : sig?.direction === 'down' ? TrendingDown
    : sig?.direction === 'no_trade' ? Ban : Minus;

  return (
    <>
      <Head><title>Kronos Forecast — TradeBot</title></Head>

      <div className="space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-purple-500/15">
              <Telescope className="w-6 h-6 text-purple-400" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-white">Kronos Forecast</h1>
              <p className="text-sm text-gray-400">
                K-line foundation-model price prediction with confidence bands
              </p>
            </div>
          </div>

          {/* Engine badge */}
          {status && (
            <div
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium ${
                status.engine === 'kronos'
                  ? 'bg-purple-500/15 text-purple-300'
                  : 'bg-yellow-500/15 text-yellow-300'
              }`}
              title={status.detail}
            >
              <Cpu className="w-4 h-4" />
              {status.engine === 'kronos'
                ? `${status.model_name} · ${status.device}`
                : 'Heuristic fallback — run setup_kronos.sh for the real model'}
            </div>
          )}

          {/* Model selector */}
          {modelCatalog.length > 0 && (
            <div className="flex items-center gap-2">
              <Database className="w-4 h-4 text-gray-500" />
              <div className="flex gap-1">
                {modelCatalog.map((m) => (
                  <button
                    key={m.id}
                    disabled={switching || installingAll}
                    onClick={() => switchKronosModel(m.id)}
                    title={`${m.label}${m.installed ? ' — installed' : ' — downloads on first use'}`}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all inline-flex items-center gap-1 ${
                      activeModel === m.id
                        ? 'bg-purple-600 text-white'
                        : 'bg-gray-800/70 text-gray-400 hover:text-white hover:bg-gray-700/80'
                    } disabled:opacity-50`}
                  >
                    {switching && activeModel !== m.id ? (
                      <Loader2 className="w-3 h-3 animate-spin inline" />
                    ) : (
                      <span
                        className={`w-1.5 h-1.5 rounded-full ${m.installed ? 'bg-green-400' : 'bg-gray-500'}`}
                        aria-hidden
                      />
                    )}
                    {m.id.split('/')[1]}
                  </button>
                ))}
              </div>
              {!modelCatalog.every((m) => m.installed) && (
                <button
                  onClick={installAllModels}
                  disabled={installingAll || switching}
                  title="Download every Kronos model + tokenizer locally so any variant is instant (no heuristic fallback)"
                  className="px-2.5 py-1 rounded-lg text-xs font-medium bg-purple-700/70 text-purple-100 hover:bg-purple-600 transition-all inline-flex items-center gap-1 disabled:opacity-50"
                >
                  {installingAll ? <Loader2 className="w-3 h-3 animate-spin" /> : <Download className="w-3 h-3" />}
                  {installingAll ? 'Installing…' : 'Install all'}
                </button>
              )}
              {switching && <Loader2 className="w-3.5 h-3.5 animate-spin text-purple-400" />}
            </div>
          )}
        </div>

        {/* Controls */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 flex flex-wrap items-end gap-4">

          {/* Pair combobox — searches all crypto + forex/metals */}
          <div className="relative" ref={comboRef}>
            <label className="block text-xs text-gray-400 mb-1">Pair</label>
            <div className="flex gap-2">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500 pointer-events-none" />
                <input
                  value={pairInput}
                  onChange={(e) => handlePairInputChange(e.target.value)}
                  onFocus={() => pairInput && setDropdownOpen(true)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') { e.preventDefault(); applyPair(); }
                    if (e.key === 'Escape') setDropdownOpen(false);
                  }}
                  className="w-48 bg-gray-800 border border-gray-700 rounded-lg pl-7 pr-3 py-2 text-sm text-white focus:outline-none focus:border-purple-500 placeholder-gray-600"
                  placeholder="BTC/USDT, gold, EUR/USD…"
                  autoComplete="off"
                  spellCheck={false}
                />
                {(searching || resolving) && (
                  <Loader2 className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-purple-400 animate-spin pointer-events-none" />
                )}
              </div>
              <button
                onClick={applyPair}
                disabled={resolving}
                className="px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium disabled:opacity-50 flex items-center gap-1"
              >
                {resolving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ChevronDown className="w-3.5 h-3.5" />}
                Go
              </button>
            </div>

            {/* Dropdown results */}
            {dropdownOpen && searchResults.length > 0 && (
              <div className="absolute z-50 top-full mt-1 left-0 w-80 bg-gray-900 border border-gray-700 rounded-xl shadow-2xl overflow-hidden">
                <div className="max-h-72 overflow-y-auto">
                  {searchResults.map((p) => (
                    <button
                      key={p.symbol}
                      onMouseDown={(e) => { e.preventDefault(); selectPair(p.symbol); }}
                      className="w-full flex items-center justify-between px-3 py-2.5 text-sm hover:bg-gray-800/80 transition-colors border-b border-gray-800/60 last:border-0 text-left"
                    >
                      <div className="flex items-center gap-2.5">
                        <span
                          className={`text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase ${
                            p.category === 'Crypto' ? 'bg-purple-500/20 text-purple-300'
                            : p.category === 'Metals' ? 'bg-yellow-500/20 text-yellow-300'
                            : 'bg-blue-500/20 text-blue-300'
                          }`}
                        >
                          {p.category === 'Crypto' ? 'C' : p.category === 'Metals' ? 'M' : 'FX'}
                        </span>
                        <div>
                          <span className="font-semibold text-white">{p.symbol}</span>
                          {p.name && <span className="text-gray-400 ml-1.5 text-xs">{p.name}</span>}
                          {p.market_cap_rank != null && (
                            <span className="text-gray-600 ml-1 text-[10px]">#{p.market_cap_rank}</span>
                          )}
                        </div>
                      </div>
                      {p.price != null && (
                        <div className="text-right shrink-0">
                          <div className="text-white text-xs tabular-nums">
                            ${p.price < 0.01
                              ? p.price.toExponential(2)
                              : p.price < 1
                              ? p.price.toPrecision(4)
                              : p.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                          </div>
                          {p.price_change_24h != null && (
                            <div className={`text-[10px] ${p.price_change_24h >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                              {p.price_change_24h >= 0 ? '+' : ''}{p.price_change_24h.toFixed(2)}%
                            </div>
                          )}
                        </div>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {dropdownOpen && searchResults.length === 0 && pairInput && !searching && (
              <div className="absolute z-50 top-full mt-1 left-0 w-72 bg-gray-900 border border-gray-700 rounded-xl px-3 py-2.5 text-xs text-gray-500 shadow-2xl">
                No results — press Go to try resolving "{pairInput}" directly
              </div>
            )}
          </div>

          <div>
            <label className="block text-xs text-gray-400 mb-1">Exchange</label>
            <select
              value={exchange}
              onChange={(e) => setExchange(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-purple-500"
            >
              {EXCHANGES.map((ex) => <option key={ex} value={ex}>{ex}</option>)}
            </select>
          </div>

          <div>
            <label className="block text-xs text-gray-400 mb-1">Timeframe</label>
            <div className="flex gap-1">
              {TIMEFRAMES.map((tf) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  className={`px-3 py-2 rounded-lg text-sm font-medium ${
                    timeframe === tf ? 'bg-purple-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'
                  }`}
                >
                  {tf}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-xs text-gray-400 mb-1">Horizon ({predLen} candles)</label>
            <input
              type="range" min={4} max={120} step={4} value={predLen}
              onChange={(e) => setPredLen(Number(e.target.value))}
              className="w-40 accent-purple-500"
            />
          </div>

          <div>
            <label className="block text-xs text-gray-400 mb-1">Samples ({samples})</label>
            <input
              type="range" min={1} max={30} value={samples}
              onChange={(e) => setSamples(Number(e.target.value))}
              className="w-32 accent-purple-500"
            />
          </div>

          <div>
            <label className="block text-xs text-gray-400 mb-1">Temp ({temperature.toFixed(1)})</label>
            <input
              type="range" min={0.1} max={2} step={0.1} value={temperature}
              onChange={(e) => setTemperature(Number(e.target.value))}
              className="w-28 accent-purple-500"
            />
          </div>

          <button
            onClick={refetch}
            disabled={loading}
            className="ml-auto flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-white text-sm font-medium disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            Forecast
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 text-sm text-red-300">
            <AlertTriangle className="w-4 h-4" /> {error}
          </div>
        )}

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          {/* Chart + sniper entries (left column) */}
          <div className="xl:col-span-2 space-y-4">
            {/* Chart with forecast overlay */}
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-2 min-h-[420px]">
              <TradingViewChart
                symbol={symbol}
                exchange={exchange}
                timeframe={timeframe}
                overlays={data?.overlays ?? []}
                markers={data?.markers ?? []}
                strategyName="Kronos"
                strategyAction={sig?.direction}
                strategyScore={sig ? sig.confidence : undefined}
                initialCandles={data?.candles?.length ? data.candles : undefined}
                // Backend only returns `candles` for FX / metals, and those bars
                // are anchored to Swissquote — naming the crypto exchange there
                // would credit the wrong feed.
                sourceLabel={data?.candles?.length ? 'Swissquote' : undefined}
              />
            </div>

            {/* Sniper entries derived from Kronos direction + JARVIS analytics */}
            <SniperPanel
              sniper={sniper}
              loading={sniperLoading}
              error={sniperErr}
              hasForecast={!!data?.signal}
              symbol={symbol}
              timeframe={timeframe}
              predLen={predLen}
              isExecutable={isExecutable}
              marginUsd={marginUsd}
              setMarginUsd={setMarginUsd}
              paperMode={paperMode}
              setPaperMode={setPaperMode}
              marginMode={marginMode}
              setMarginMode={setMarginMode}
              confirmId={confirmId}
              execId={execId}
              execResults={execResults}
              onExecute={executeSniper}
              onRefresh={fetchSniper}
              tradability={tradability}
              tradabilityLoading={tradabilityLoading}
              onRefreshTradability={fetchTradability}
            />
          </div>

          {/* Forecast metrics */}
          <div className="space-y-4">
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-gray-300">Forecast Signal</h2>
                <button
                  onClick={speak}
                  disabled={!sig}
                  className="flex items-center gap-1 text-xs text-purple-300 hover:text-purple-200 disabled:opacity-40"
                >
                  <Volume2 className="w-4 h-4" /> Speak
                </button>
              </div>

              {sig ? (
                <>
                  {data?.decision === 'NO_TRADE' ? (
                    <div className="flex items-center gap-2 text-xl font-bold text-amber-400">
                      <Ban className="w-6 h-6" /> NO TRADE
                    </div>
                  ) : (
                    <>
                      <div className={`flex items-center gap-2 text-2xl font-bold ${dirColor}`}>
                        <DirIcon className="w-6 h-6" />
                        {sig.pct_change >= 0 ? '+' : ''}{sig.pct_change.toFixed(2)}%
                      </div>
                      <p className="text-xs text-gray-400 mt-1 capitalize">{sig.direction} over next {predLen}×{timeframe}</p>
                    </>
                  )}

                  <div className="mt-4 space-y-2 text-sm">
                    {data?.decision !== 'NO_TRADE' && (
                      <>
                        <Row label="Target price" value={sig.target_price.toPrecision(6)} />
                        <Row label="Confidence" value={`${Math.round(sig.confidence * 100)}%`} />
                      </>
                    )}
                    <Row label="Current price" value={sig.anchor_price.toPrecision(6)} />
                    <Row label="Engine" value={data?.engine ?? '—'} />
                    <Row label="Decision" value={data?.decision ?? 'OK'} />
                  </div>

                  {/* Confidence bar */}
                  {data?.decision !== 'NO_TRADE' && (
                    <div className="mt-3 h-2 rounded-full bg-gray-800 overflow-hidden">
                      <div
                        className={`h-full ${data?.decision === 'LOW_CONFIDENCE' ? 'bg-amber-500' : 'bg-purple-500'}`}
                        style={{ width: `${Math.round(sig.confidence * 100)}%` }}
                      />
                    </div>
                  )}

                  {/* Volume evidence — the precondition this call was gated on */}
                  <div className="mt-3">
                    <VolumeEvidence volume={data?.volume} />
                  </div>

                  {/* Why the direction was chosen */}
                  {sig.rationale?.length > 0 && (
                    <div className="mt-3">
                      <h3 className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1">
                        Why this call
                      </h3>
                      <ul className="space-y-1">
                        {sig.rationale.map((r, i) => (
                          <li key={i} className="text-[11px] text-gray-400 leading-snug flex gap-1.5">
                            <span className="text-purple-400 shrink-0">•</span>{r}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              ) : (
                <p className="text-sm text-gray-500">{loading ? 'Forecasting…' : 'No forecast yet.'}</p>
              )}
            </div>

            {data?.note && (
              <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-xl p-3 text-xs text-yellow-300 flex items-start gap-2">
                <Settings2 className="w-4 h-4 mt-0.5 shrink-0" />
                {data.note}
              </div>
            )}

            {/* JARVIS Analysis + brain learning */}
            <div className="bg-gray-900/60 border border-purple-700/40 rounded-xl p-4">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-purple-200 flex items-center gap-2">
                  <BrainCircuit className="w-4 h-4 text-purple-400" />
                  JARVIS Analysis
                </h2>
                <div className="flex items-center gap-2">
                  {analysis?.learned && (
                    <span
                      className="flex items-center gap-1 text-[10px] font-medium text-green-300 bg-green-500/10 px-2 py-0.5 rounded-full"
                      title="Stored to the JARVIS knowledge brain"
                    >
                      <CheckCircle2 className="w-3 h-3" /> Learned
                    </span>
                  )}
                  <button
                    onClick={speakAnalysis}
                    disabled={!analysis?.analysis}
                    className="flex items-center gap-1 text-xs text-purple-300 hover:text-purple-200 disabled:opacity-40"
                  >
                    <Volume2 className="w-4 h-4" /> Speak
                  </button>
                  <button
                    onClick={runAnalysis}
                    disabled={analyzing || !data?.signal}
                    className="flex items-center gap-1 text-xs text-purple-300 hover:text-purple-200 disabled:opacity-40"
                  >
                    {analyzing
                      ? <Loader2 className="w-4 h-4 animate-spin" />
                      : <Sparkles className="w-4 h-4" />}
                    {analyzing ? 'Thinking…' : 'Re-analyze'}
                  </button>
                </div>
              </div>

              {/* Market cap chips (crypto) */}
              {analysis?.market && (
                <div className="flex flex-wrap gap-2 mb-3">
                  <Chip label="Market cap" value={fmtUsd(analysis.market.market_cap)}
                    extra={analysis.market.market_cap_rank ? `#${analysis.market.market_cap_rank}` : undefined} />
                  <Chip label="24h vol" value={fmtUsd(analysis.market.volume_24h)} />
                  {analysis.market.price_change_24h !== null && analysis.market.price_change_24h !== undefined && (
                    <Chip
                      label="24h"
                      value={`${analysis.market.price_change_24h >= 0 ? '+' : ''}${analysis.market.price_change_24h.toFixed(2)}%`}
                      tone={analysis.market.price_change_24h >= 0 ? 'up' : 'down'}
                    />
                  )}
                </div>
              )}

              {analysisErr && (
                <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2 text-xs text-red-300 mb-2">
                  <AlertTriangle className="w-3.5 h-3.5" /> {analysisErr}
                </div>
              )}

              {analyzing && !analysis ? (
                <p className="text-sm text-gray-500 flex items-center gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" /> JARVIS is analysing the forecast…
                </p>
              ) : analysis?.analysis ? (
                <p className="text-sm text-gray-200 leading-relaxed whitespace-pre-line">
                  {analysis.analysis}
                </p>
              ) : (
                <p className="text-sm text-gray-500">Run a forecast to get a JARVIS analysis.</p>
              )}

              {(analysis?.provider || analysis?.note) && (
                <p className="mt-3 text-[11px] text-gray-500">
                  {analysis?.provider && <span>via {analysis.provider}</span>}
                  {analysis?.provider && analysis?.note && <span> · </span>}
                  {analysis?.note && <span className="text-yellow-400/80">{analysis.note}</span>}
                </p>
              )}
            </div>

            {/* Your open position on this symbol */}
            {analysis?.position && (
              <div className="bg-gray-900/60 border border-blue-700/40 rounded-xl p-4">
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-sm font-semibold text-blue-200 flex items-center gap-2">
                    <Wallet className="w-4 h-4 text-blue-400" />
                    Your Position
                  </h2>
                  <span
                    className={`text-[11px] font-semibold px-2 py-0.5 rounded-full uppercase ${
                      analysis.position.side === 'long'
                        ? 'bg-green-500/15 text-green-300'
                        : 'bg-red-500/15 text-red-300'
                    }`}
                  >
                    {analysis.position.side} · {analysis.position.leverage ? `${analysis.position.leverage}x` : '—'}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  <Row label="Size" value={`${analysis.position.size}`} />
                  <Row
                    label="PnL"
                    value={`${analysis.position.pnl >= 0 ? '+' : ''}${analysis.position.pnl.toFixed(2)} (${analysis.position.pnl_pct >= 0 ? '+' : ''}${analysis.position.pnl_pct.toFixed(2)}%)`}
                  />
                  <Row label="Entry" value={analysis.position.entry_price.toPrecision(6)} />
                  <Row label="Mark" value={analysis.position.mark_price.toPrecision(6)} />
                  {analysis.position.liquidation_price ? (
                    <Row label="Liq." value={analysis.position.liquidation_price.toPrecision(6)} />
                  ) : null}
                </div>

                {analysis.position_advice && (
                  <div className="mt-3 bg-blue-500/10 border border-blue-500/30 rounded-lg px-3 py-2 text-sm text-blue-100 leading-relaxed">
                    <span className="font-semibold text-blue-300">JARVIS suggestion: </span>
                    {analysis.position_advice}
                  </div>
                )}
              </div>
            )}

            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 text-xs text-gray-400 leading-relaxed">
              <p className="text-gray-300 font-medium mb-1">About Kronos</p>
              Kronos is an open-source decoder-only foundation model trained on K-lines
              from 45+ exchanges. The purple line is the mean predicted close; the dashed
              band is the p10–p90 range across {samples} sampled paths. Wider bands = more
              uncertainty.
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-gray-500">{label}</span>
      <span className="text-white font-medium tabular-nums">{value}</span>
    </div>
  );
}

function Chip({ label, value, extra, tone }: {
  label: string; value: string; extra?: string; tone?: 'up' | 'down';
}) {
  const valColor = tone === 'up' ? 'text-green-300' : tone === 'down' ? 'text-red-300' : 'text-white';
  return (
    <div className="flex items-center gap-1.5 bg-gray-800/70 border border-gray-700/50 rounded-lg px-2.5 py-1 text-xs">
      <span className="text-gray-500">{label}</span>
      <span className={`font-medium tabular-nums ${valColor}`}>{value}</span>
      {extra && <span className="text-purple-300/80">{extra}</span>}
    </div>
  );
}

interface SniperPanelProps {
  sniper: SniperSignalsResponse | null;
  loading: boolean;
  error: string | null;
  hasForecast: boolean;
  symbol: string;
  timeframe: string;
  predLen: number;
  isExecutable: boolean;
  marginUsd: number;
  setMarginUsd: (v: number) => void;
  paperMode: boolean;
  setPaperMode: (v: boolean) => void;
  marginMode: 'crossed' | 'isolated';
  setMarginMode: (v: 'crossed' | 'isolated') => void;
  confirmId: string | null;
  execId: string | null;
  execResults: Record<string, { ok: boolean; msg: string }>;
  onExecute: (s: SniperSignal) => void;
  onRefresh: () => void;
  tradability: Tradability | null;
  tradabilityLoading: boolean;
  onRefreshTradability: () => void;
}

function SniperPanel({
  sniper, loading, error, hasForecast, symbol, timeframe, predLen,
  isExecutable, marginUsd, setMarginUsd, paperMode, setPaperMode,
  marginMode, setMarginMode,
  confirmId, execId, execResults, onExecute, onRefresh,
  tradability, tradabilityLoading, onRefreshTradability,
}: SniperPanelProps) {
  const signals = sniper?.signals ?? [];
  const overMargin = !paperMode && !!tradability?.tradable
    && tradability.available_margin > 0 && marginUsd > tradability.available_margin;

  return (
    <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <div className="flex items-center gap-2">
          <div className="p-1.5 rounded-lg bg-emerald-500/15">
            <Crosshair className="w-5 h-5 text-emerald-400" />
          </div>
          <div>
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              Sniper Entries
              {sniper && (
                <span
                  className={`text-[10px] font-semibold px-2 py-0.5 rounded-full uppercase ${
                    sniper.direction === 'up' ? 'bg-green-500/15 text-green-300'
                    : sniper.direction === 'down' ? 'bg-red-500/15 text-red-300'
                    : 'bg-yellow-500/15 text-yellow-300'
                  }`}
                >
                  {sniper.direction === 'up' ? 'Long bias' : sniper.direction === 'down' ? 'Short bias' : 'No edge'}
                  {sniper.direction !== 'flat' && ` · ${sniper.pct_change >= 0 ? '+' : ''}${sniper.pct_change.toFixed(2)}%`}
                </span>
              )}
            </h2>
            <p className="text-xs text-gray-500">
              Executable entries built from Kronos direction + JARVIS confidence bands
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Paper / Live toggle */}
          <div className="flex rounded-lg overflow-hidden border border-gray-700 text-xs font-medium">
            <button
              onClick={() => setPaperMode(true)}
              className={`px-2.5 py-1.5 transition-colors ${paperMode ? 'bg-emerald-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              Paper
            </button>
            <button
              onClick={() => setPaperMode(false)}
              className={`px-2.5 py-1.5 transition-colors ${!paperMode ? 'bg-red-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              Live
            </button>
          </div>

          {/* Margin mode: Cross / Isolated */}
          <div className="flex rounded-lg overflow-hidden border border-gray-700 text-xs font-medium" title="Margin mode for the order. Bitget keeps an open position's existing mode.">
            <button
              onClick={() => setMarginMode('crossed')}
              className={`px-2.5 py-1.5 transition-colors ${marginMode === 'crossed' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              Cross
            </button>
            <button
              onClick={() => setMarginMode('isolated')}
              className={`px-2.5 py-1.5 transition-colors ${marginMode === 'isolated' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              Isolated
            </button>
          </div>

          {/* Margin size (USD) */}
          <div className="flex items-center gap-1 bg-gray-800 border border-gray-700 rounded-lg px-2 py-1">
            <span className="text-[11px] text-gray-500">Margin $</span>
            <input
              type="number"
              min={1}
              value={marginUsd}
              onChange={(e) => setMarginUsd(Math.max(1, Number(e.target.value) || 0))}
              className={`w-14 bg-transparent text-xs tabular-nums focus:outline-none ${overMargin ? 'text-red-300' : 'text-white'}`}
            />
            {tradability?.tradable && tradability.available_margin > 0 && (
              <button
                onClick={() => setMarginUsd(Math.max(1, Math.floor(tradability.available_margin)))}
                title={`Use all openable margin ($${tradability.available_margin.toFixed(2)})`}
                className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-emerald-600/80 hover:bg-emerald-500 text-white"
              >
                Max
              </button>
            )}
          </div>

          <button
            onClick={onRefresh}
            disabled={loading}
            title="Rebuild sniper entries from the latest forecast"
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs font-medium disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Live-mode warning */}
      {!paperMode && (
        <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2 text-xs text-red-300 mb-3">
          <ShieldAlert className="w-4 h-4 shrink-0" />
          Live mode places real Bitget futures orders with SL/TP attached. Click once to arm, again to confirm.
        </div>
      )}

      {/* Exchange tradability — openable margin, max leverage, max open, position */}
      {isExecutable && (
        <div className="flex flex-wrap items-center gap-2 mb-3 text-[11px]">
          {tradabilityLoading && !tradability ? (
            <span className="text-gray-500 flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> Reading account…</span>
          ) : tradability ? (
            <>
              <Chip label="Free margin" value={`$${(tradability.available_margin || 0).toFixed(2)}`} tone={tradability.available_margin > 0 ? 'up' : 'down'} />
              {tradability.max_leverage && <Chip label="Max lev" value={`${tradability.max_leverage}x`} />}
              <Chip label="Max open" value={`$${(tradability.max_open_notional || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                extra={`margin $${(tradability.max_open_margin || 0).toFixed(0)}`} />
              {tradability.current_position && (
                <span className={`inline-flex items-center gap-1 rounded-lg px-2 py-1 border ${
                  tradability.current_position.side === 'long'
                    ? 'bg-green-500/10 border-green-700/40 text-green-300'
                    : 'bg-red-500/10 border-red-700/40 text-red-300'}`}>
                  <Wallet className="w-3 h-3" />
                  Open {tradability.current_position.side} {tradability.current_position.size}
                  {tradability.current_position.notional != null && ` · $${tradability.current_position.notional.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                </span>
              )}
              <button
                onClick={onRefreshTradability}
                title="Refresh account balance / position"
                className="ml-auto text-gray-500 hover:text-gray-300 p-1"
              >
                <RefreshCw className={`w-3 h-3 ${tradabilityLoading ? 'animate-spin' : ''}`} />
              </button>
            </>
          ) : (
            <span className="text-gray-500">Account context unavailable — Bitget keys may be missing.</span>
          )}
        </div>
      )}

      {/* Margin exceeds openable balance (live) */}
      {overMargin && tradability && (
        <div className="flex items-center gap-2 bg-amber-500/10 border border-amber-500/30 rounded-lg px-3 py-2 text-xs text-amber-300 mb-3">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          Margin ${marginUsd} exceeds openable ${tradability.available_margin.toFixed(2)} — orders will be capped to the available margin.
        </div>
      )}

      {/* States */}
      {error ? (
        <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2.5 text-sm text-red-300">
          <AlertTriangle className="w-4 h-4" /> {error}
        </div>
      ) : loading && !sniper ? (
        <p className="text-sm text-gray-500 flex items-center gap-2 py-4">
          <Loader2 className="w-4 h-4 animate-spin" /> Building sniper entries from the forecast…
        </p>
      ) : !hasForecast ? (
        <p className="text-sm text-gray-500 py-4">Run a forecast to generate sniper entries.</p>
      ) : signals.length === 0 ? (
        <div className="space-y-2">
          <div
            className={`flex items-start gap-2 border rounded-lg px-3 py-2.5 text-xs ${
              sniper?.decision === 'NO_TRADE'
                ? 'bg-amber-500/10 border-amber-500/30 text-amber-300'
                : 'bg-yellow-500/10 border-yellow-500/30 text-yellow-300'
            }`}
          >
            {sniper?.decision === 'NO_TRADE'
              ? <Ban className="w-4 h-4 mt-0.5 shrink-0" />
              : <Minus className="w-4 h-4 mt-0.5 shrink-0" />}
            {sniper?.note || 'Kronos sees no directional edge right now — no high-conviction entry. Try another timeframe.'}
          </div>
          {/* Show the volume evidence even when nothing is tradeable — it is the
              reason there is no entry. */}
          <VolumeEvidence volume={sniper?.volume} />
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {signals.map((s) => {
            const long = s.side === 'long';
            const armed = confirmId === s.id;
            const busy = execId === s.id;
            const result = execResults[s.id];
            return (
              <div
                key={s.id}
                className={`rounded-xl border p-3 ${
                  long ? 'border-green-700/40 bg-green-500/[0.03]' : 'border-red-700/40 bg-red-500/[0.03]'
                }`}
              >
                {/* Card header */}
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-flex items-center gap-1 text-[11px] font-bold px-2 py-0.5 rounded-full uppercase ${
                        long ? 'bg-green-500/15 text-green-300' : 'bg-red-500/15 text-red-300'
                      }`}
                    >
                      {long ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                      {s.side}
                    </span>
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full bg-gray-800 text-gray-300">
                      {s.order_kind === 'market' ? <Zap className="w-3 h-3 text-yellow-400" /> : <Rocket className="w-3 h-3 text-purple-400" />}
                      {s.label}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 text-[11px] text-gray-400">
                    <span title="Model path agreement">{Math.round(s.confidence * 100)}%</span>
                    <span className="text-gray-600">·</span>
                    <span title="Suggested leverage">{s.leverage}x</span>
                  </div>
                </div>

                {/* Price levels */}
                <div className="grid grid-cols-4 gap-2 mb-2.5 text-center">
                  <Level label="Entry" value={fmtPx(s.entry)} tone="neutral" />
                  <Level label="Stop" value={fmtPx(s.stop_loss)} tone="down" />
                  <Level label="TP1" value={fmtPx(s.take_profit_1)} tone="up" />
                  <Level label="R:R" value={`${s.risk_reward}`} tone="accent" />
                </div>
                {s.take_profit_2 != null && (
                  <div className="text-[11px] text-gray-500 mb-2.5">
                    Stretch target (TP2): <span className="text-emerald-300 tabular-nums">{fmtPx(s.take_profit_2)}</span>
                  </div>
                )}

                {/* Volume evidence — required on every emitted entry */}
                <div className="flex flex-wrap items-center gap-1.5 mb-2.5 text-[10px]">
                  <span className="rounded px-1.5 py-0.5 bg-gray-800 text-gray-300 tabular-nums">
                    24h vol <span className="text-white">{fmtVol(s.volume_24h)}</span>
                  </span>
                  <span className="rounded px-1.5 py-0.5 bg-gray-800 text-gray-300 tabular-nums">
                    1h vol <span className="text-white">{fmtVol(s.volume_1h)}</span>
                  </span>
                  <span className="rounded px-1.5 py-0.5 bg-gray-800 text-gray-300 tabular-nums">
                    rel <span className="text-white">
                      {s.relative_volume != null ? `×${s.relative_volume.toFixed(2)}` : 'n/a'}
                    </span>
                  </span>
                  <span className={`rounded px-1.5 py-0.5 border font-semibold ${
                    s.volume_regime === 'CLIMACTIC' ? 'bg-amber-500/15 text-amber-300 border-amber-700/40'
                    : s.volume_regime === 'ELEVATED' ? 'bg-emerald-500/15 text-emerald-300 border-emerald-700/40'
                    : s.volume_regime === 'DEAD' ? 'bg-gray-500/15 text-gray-400 border-gray-600/40'
                    : 'bg-blue-500/15 text-blue-300 border-blue-700/40'
                  }`}>
                    {s.volume_regime ?? 'UNKNOWN'}
                  </span>
                  {s.volume_divergence && s.volume_divergence !== 'NEUTRAL' && s.volume_divergence !== 'UNKNOWN' && (
                    <span className="rounded px-1.5 py-0.5 bg-gray-800 text-gray-300">
                      {s.volume_divergence.replace('_', ' ').toLowerCase()}
                    </span>
                  )}
                </div>

                {/* Reasons */}
                <ul className="space-y-1 mb-3">
                  {s.reasons.map((r, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-[11px] text-gray-400 leading-snug">
                      <ChevronRight className={`w-3 h-3 mt-0.5 shrink-0 ${long ? 'text-green-500/70' : 'text-red-500/70'}`} />
                      <span>{r}</span>
                    </li>
                  ))}
                </ul>

                {/* Execution result */}
                {result && (
                  <div
                    className={`flex items-start gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] mb-2 ${
                      result.ok ? 'bg-green-500/10 text-green-300 border border-green-500/30'
                      : 'bg-red-500/10 text-red-300 border border-red-500/30'
                    }`}
                  >
                    {result.ok ? <Check className="w-3.5 h-3.5 mt-px shrink-0" /> : <X className="w-3.5 h-3.5 mt-px shrink-0" />}
                    <span>{result.msg}</span>
                  </div>
                )}

                {/* Execute button */}
                {isExecutable ? (
                  <button
                    onClick={() => onExecute(s)}
                    disabled={busy}
                    className={`w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-semibold transition-all disabled:opacity-60 ${
                      armed
                        ? (long ? 'bg-green-600 hover:bg-green-500 text-white ring-2 ring-green-400/50'
                                : 'bg-red-600 hover:bg-red-500 text-white ring-2 ring-red-400/50')
                        : (long ? 'bg-green-600/80 hover:bg-green-600 text-white'
                                : 'bg-red-600/80 hover:bg-red-600 text-white')
                    }`}
                  >
                    {busy ? (
                      <><Loader2 className="w-4 h-4 animate-spin" /> Placing…</>
                    ) : armed ? (
                      <><Check className="w-4 h-4" /> Confirm {paperMode ? 'paper' : 'LIVE'} {s.side} · ${marginUsd} margin</>
                    ) : (
                      <><Crosshair className="w-4 h-4" /> {paperMode ? 'Paper' : 'Live'} {long ? 'buy' : 'sell'} · ${marginUsd}</>
                    )}
                  </button>
                ) : (
                  <div className="w-full text-center text-[11px] text-gray-500 bg-gray-800/60 border border-gray-700/50 rounded-lg px-3 py-2">
                    Execution available for USDT crypto pairs — chart-only for {symbol}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {signals.length > 0 && (
        <p className="mt-3 text-[10px] text-gray-600 leading-relaxed">
          Entries are model-derived suggestions over the next {predLen}×{timeframe} horizon, not financial advice.
          The $ shown is your margin (what you commit); leverage is set per pair. Always verify levels before executing.
        </p>
      )}
    </div>
  );
}

function Level({ label, value, tone }: { label: string; value: string; tone: 'up' | 'down' | 'neutral' | 'accent' }) {
  const color = tone === 'up' ? 'text-green-300' : tone === 'down' ? 'text-red-300'
    : tone === 'accent' ? 'text-purple-300' : 'text-white';
  return (
    <div className="bg-gray-800/60 rounded-lg py-1.5">
      <div className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-xs font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  );
}
