import { useEffect, useMemo, useState } from 'react';
import { Telescope, GitBranch } from 'lucide-react';
import TradingViewChart, {
  IndicatorOverlaySeries, IndicatorMarker,
} from '@/components/TradingViewChart';
import { apiClient } from '@/services/api';
import { useBtcCycleWindows } from '@/hooks/useBtcCycle';
import { toCycleBoxes } from '@/utils/cycleOverlay';

/** Bases that ride the Bitcoin cycle — mirrors backend cycle_applies. */
const CYCLE_BASES = /^(BTC|ETH|SOL|XRP|DOGE|ADA|AVAX|LINK|BNB|LTC)(USD|USDT|USDC)?$/i;

/**
 * Wraps TradingViewChart and adds two independent one-click overlay toggles —
 * the Kronos K-line forecast (mean line + p10/p90 band + direction marker) and
 * auto fib retracement (levels off the latest ZigZag swing) — on top of whatever
 * overlays/markers the page already passes. Fully self-contained — the host page
 * needs no extra state. Drop-in replacement for <TradingViewChart/>.
 */
export default function KronosOverlayChart(props: {
  symbol: string;
  exchange?: string;
  timeframe?: string;
  overlays?: IndicatorOverlaySeries[];
  markers?: IndicatorMarker[];
  predLen?: number;
  [key: string]: any;
}) {
  const {
    symbol, exchange = 'bitget', timeframe = '1h',
    overlays = [], markers = [], predLen = 24, ...rest
  } = props;

  // The Bitcoin calendar rides along on cycle-driven symbols unless the host
  // page already supplied its own cycleWindows.
  const hostCycle = (rest as { cycleWindows?: unknown }).cycleWindows;
  const cycleWins = useBtcCycleWindows();
  const autoCycle = useMemo(
    () => (hostCycle ? undefined : CYCLE_BASES.test(symbol.replace(/[/\-:]/g, '')) ? toCycleBoxes(cycleWins) : undefined),
    [cycleWins, symbol, hostCycle],
  );

  const [show, setShow] = useState(false);
  const [kOverlays, setKOverlays] = useState<IndicatorOverlaySeries[]>([]);
  const [kMarkers, setKMarkers] = useState<IndicatorMarker[]>([]);
  const [loading, setLoading] = useState(false);
  const [signal, setSignal] = useState<{ direction: string; pct_change: number; confidence: number } | null>(null);

  useEffect(() => {
    if (!show || !symbol) { setKOverlays([]); setKMarkers([]); setSignal(null); return; }
    let cancelled = false;
    setLoading(true);
    apiClient.kronos.overlay(exchange, symbol, { timeframe, pred_len: predLen })
      .then((r) => {
        if (cancelled) return;
        setKOverlays(r.data?.overlays ?? []);
        setKMarkers(r.data?.markers ?? []);
        setSignal(r.data?.signal ?? null);
      })
      .catch(() => { if (!cancelled) { setKOverlays([]); setKMarkers([]); setSignal(null); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [show, symbol, exchange, timeframe, predLen]);

  const [showFib, setShowFib] = useState(false);
  const [fibOverlays, setFibOverlays] = useState<IndicatorOverlaySeries[]>([]);
  const [fibMarkers, setFibMarkers] = useState<IndicatorMarker[]>([]);
  const [fibLoading, setFibLoading] = useState(false);
  const [fibSignal, setFibSignal] = useState<{ direction: string; confidence: number | null; in_golden_zone: boolean } | null>(null);

  useEffect(() => {
    if (!showFib || !symbol) { setFibOverlays([]); setFibMarkers([]); setFibSignal(null); return; }
    let cancelled = false;
    setFibLoading(true);
    apiClient.technical.fibOverlay(exchange, symbol, { timeframe })
      .then((r) => {
        if (cancelled) return;
        setFibOverlays(r.data?.overlays ?? []);
        setFibMarkers(r.data?.markers ?? []);
        setFibSignal(r.data?.signal ?? null);
      })
      .catch(() => { if (!cancelled) { setFibOverlays([]); setFibMarkers([]); setFibSignal(null); } })
      .finally(() => { if (!cancelled) setFibLoading(false); });
    return () => { cancelled = true; };
  }, [showFib, symbol, exchange, timeframe]);

  const dirColor = signal?.direction === 'up' ? 'text-green-400'
    : signal?.direction === 'down' ? 'text-red-400' : 'text-yellow-400';

  return (
    <div className="relative">
      <div className="absolute top-2 right-2 z-20 flex items-center gap-2">
        {show && signal && (
          <span className={`text-xs font-semibold bg-gray-900/80 rounded px-2 py-1 ${dirColor}`}>
            Kronos {signal.pct_change >= 0 ? '+' : ''}{signal.pct_change.toFixed(2)}% · {Math.round(signal.confidence * 100)}%
          </span>
        )}
        <button
          type="button"
          onClick={() => setShow((s) => !s)}
          title="Toggle Kronos AI forecast overlay"
          className={`flex items-center gap-1 text-xs font-medium rounded px-2 py-1 transition-colors ${
            show ? 'bg-purple-600 text-white' : 'bg-gray-800/80 text-gray-300 hover:text-white'
          }`}
        >
          <Telescope className={`w-4 h-4 ${loading ? 'animate-pulse' : ''}`} />
          Forecast
        </button>
        {showFib && fibSignal?.in_golden_zone && (
          <span className="text-xs font-semibold bg-gray-900/80 rounded px-2 py-1 text-amber-400">
            Golden zone{fibSignal.confidence != null ? ` · ${Math.round(fibSignal.confidence * 100)}%` : ''}
          </span>
        )}
        <button
          type="button"
          onClick={() => setShowFib((s) => !s)}
          title="Toggle auto fib retracement overlay"
          className={`flex items-center gap-1 text-xs font-medium rounded px-2 py-1 transition-colors ${
            showFib ? 'bg-amber-600 text-white' : 'bg-gray-800/80 text-gray-300 hover:text-white'
          }`}
        >
          <GitBranch className={`w-4 h-4 ${fibLoading ? 'animate-pulse' : ''}`} />
          Fib
        </button>
      </div>
      <TradingViewChart
        symbol={symbol}
        exchange={exchange}
        timeframe={timeframe}
        overlays={[...overlays, ...kOverlays, ...fibOverlays]}
        markers={[...markers, ...kMarkers, ...fibMarkers]}
        cycleWindows={autoCycle}
        {...rest}
      />
    </div>
  );
}
