import { useEffect, useState } from 'react';
import { Telescope, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { apiClient } from '@/services/api';

interface KronosSignal {
  direction: 'up' | 'down' | 'flat';
  pct_change: number;
  confidence: number;
  target_price: number;
  spoken: string;
}

/**
 * Compact, self-contained Kronos forecast badge/card for any signal row or panel
 * (sniper signals, telegram signals, MT5 setups). Fetches on demand; degrades to
 * a heuristic forecast if the model isn't installed. Zero host-page wiring.
 */
export default function KronosForecastCard({
  symbol,
  exchange = 'bitget',
  timeframe = '1h',
  predLen = 12,
  compact = false,
}: {
  symbol: string;
  exchange?: string;
  timeframe?: string;
  predLen?: number;
  compact?: boolean;
}) {
  const [sig, setSig] = useState<KronosSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    apiClient.kronos.jarvis(symbol, { exchange, timeframe, pred_len: predLen })
      .then((r) => { if (!cancelled) setSig(r.data as KronosSignal); })
      .catch(() => { if (!cancelled) setError(true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol, exchange, timeframe, predLen]);

  const dir = sig?.direction ?? 'flat';
  const color = dir === 'up' ? 'text-green-400' : dir === 'down' ? 'text-red-400' : 'text-yellow-400';
  const Icon = dir === 'up' ? TrendingUp : dir === 'down' ? TrendingDown : Minus;

  if (compact) {
    return (
      <span className={`inline-flex items-center gap-1 text-xs font-medium ${loading ? 'text-gray-500' : color}`}>
        <Telescope className={`w-3.5 h-3.5 ${loading ? 'animate-pulse' : ''}`} />
        {loading ? 'Kronos…' : error || !sig ? 'Kronos n/a'
          : `${sig.pct_change >= 0 ? '+' : ''}${sig.pct_change.toFixed(2)}% · ${Math.round(sig.confidence * 100)}%`}
      </span>
    );
  }

  return (
    <div className="rounded-lg border border-purple-500/20 bg-purple-500/5 p-3">
      <div className="flex items-center justify-between mb-1.5">
        <span className="flex items-center gap-1.5 text-xs font-semibold text-purple-300">
          <Telescope className={`w-4 h-4 ${loading ? 'animate-pulse' : ''}`} /> Kronos Forecast
        </span>
        <span className="text-[10px] text-gray-500">{predLen}×{timeframe}</span>
      </div>
      {loading ? (
        <p className="text-xs text-gray-500">Forecasting {symbol}…</p>
      ) : error || !sig ? (
        <p className="text-xs text-gray-500">Forecast unavailable.</p>
      ) : (
        <>
          <div className={`flex items-center gap-2 text-lg font-bold ${color}`}>
            <Icon className="w-5 h-5" />
            {sig.pct_change >= 0 ? '+' : ''}{sig.pct_change.toFixed(2)}%
            <span className="text-xs font-normal text-gray-400 capitalize">{dir}</span>
          </div>
          <div className="mt-1 flex items-center justify-between text-xs">
            <span className="text-gray-500">Target {sig.target_price?.toPrecision?.(6) ?? sig.target_price}</span>
            <span className="text-gray-400">{Math.round(sig.confidence * 100)}% conf.</span>
          </div>
        </>
      )}
    </div>
  );
}
