import { useEffect, useState } from 'react';
import { Telescope } from 'lucide-react';
import TradingViewChart, {
  IndicatorOverlaySeries, IndicatorMarker,
} from '@/components/TradingViewChart';
import { apiClient } from '@/services/api';

/**
 * Wraps TradingViewChart and adds a one-click "AI Forecast" toggle that overlays
 * the Kronos K-line forecast (mean line + p10/p90 band + direction marker) on top
 * of whatever overlays/markers the page already passes. Fully self-contained — the
 * host page needs no extra state. Drop-in replacement for <TradingViewChart/>.
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
      </div>
      <TradingViewChart
        symbol={symbol}
        exchange={exchange}
        timeframe={timeframe}
        overlays={[...overlays, ...kOverlays]}
        markers={[...markers, ...kMarkers]}
        {...rest}
      />
    </div>
  );
}
