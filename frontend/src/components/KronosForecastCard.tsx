import { useEffect, useState } from 'react';
import { Telescope, TrendingUp, TrendingDown, Minus, Ban, BarChart3, AlertTriangle } from 'lucide-react';
import { apiClient } from '@/services/api';
import type {
  KronosDecision,
  KronosDirection,
  KronosVolumeContext,
  KronosVolumeRegime,
} from '@/hooks/useKronosForecast';

interface KronosSignal {
  direction: KronosDirection;
  pct_change: number;
  confidence: number;
  target_price: number;
  spoken: string;
  decision: KronosDecision;
  volume: KronosVolumeContext | null;
  rationale: string[];
}

/** Compact volume formatter — 1.23B / 45.6M / 789.0K. */
export const fmtVol = (v?: number | null): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return 'n/a';
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(2)}K`;
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
};

export const REGIME_TONE: Record<KronosVolumeRegime, string> = {
  DEAD: 'bg-gray-500/15 text-gray-400 border-gray-600/40',
  NORMAL: 'bg-blue-500/15 text-blue-300 border-blue-700/40',
  ELEVATED: 'bg-emerald-500/15 text-emerald-300 border-emerald-700/40',
  CLIMACTIC: 'bg-amber-500/15 text-amber-300 border-amber-700/40',
  UNKNOWN: 'bg-gray-500/15 text-gray-400 border-gray-600/40',
};

/**
 * Volume evidence block — 24h volume, last completed 1h, relative volume and
 * the regime. Rendered wherever a forecast or signal is shown, because volume
 * is a hard precondition for both.
 */
export function VolumeEvidence({
  volume,
  compact = false,
}: {
  volume: KronosVolumeContext | null | undefined;
  compact?: boolean;
}) {
  if (!volume) {
    return (
      <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-2 text-[11px] text-gray-500">
        No volume context returned — nothing was forecast.
      </div>
    );
  }
  const unit =
    volume.volume_unit === 'tick' ? 'tick vol'
    : volume.volume_unit === 'futures' ? 'CME vol'
    : 'vol';

  if (volume.status !== 'OK') {
    return (
      <div className="rounded-lg border border-amber-600/40 bg-amber-500/10 p-2 text-[11px] text-amber-300">
        <span className="flex items-center gap-1.5 font-semibold">
          <Ban className="w-3.5 h-3.5 shrink-0" /> Volume {volume.status.toLowerCase()} — NO_TRADE
        </span>
        <p className="mt-1 text-amber-200/80 leading-snug">{volume.detail}</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-2 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold text-gray-300">
          <BarChart3 className="w-3.5 h-3.5 text-blue-400" /> Volume
        </span>
        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${REGIME_TONE[volume.regime]}`}>
          {volume.regime}
          {volume.relative_volume != null && ` ×${volume.relative_volume.toFixed(2)}`}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-1.5 text-[11px]">
        <div>
          <div className="text-gray-500">24h {unit}</div>
          <div className="text-white tabular-nums">{fmtVol(volume.volume_24h)}</div>
        </div>
        <div>
          <div className="text-gray-500">Last 1h</div>
          <div className="text-white tabular-nums">{fmtVol(volume.volume_1h)}</div>
        </div>
        <div>
          <div className="text-gray-500">Rel. vol</div>
          <div className="text-white tabular-nums">
            {volume.relative_volume != null ? `×${volume.relative_volume.toFixed(2)}` : 'n/a'}
            {volume.z_score != null && (
              <span className="text-gray-500 ml-1">z{volume.z_score > 0 ? '+' : ''}{volume.z_score.toFixed(1)}</span>
            )}
          </div>
        </div>
      </div>
      {!compact && volume.divergence !== 'NEUTRAL' && volume.divergence !== 'UNKNOWN' && (
        <div className="text-[11px] text-gray-400">
          {volume.divergence.replace('_', ' ').toLowerCase()} over {volume.divergence_bars}h
          {volume.price_change_pct != null && (
            <span className="text-gray-500">
              {' '}(price {volume.price_change_pct >= 0 ? '+' : ''}{volume.price_change_pct.toFixed(2)}%)
            </span>
          )}
        </div>
      )}
      {volume.reversal_risk && (
        <div className="flex items-start gap-1.5 text-[11px] text-amber-300">
          <AlertTriangle className="w-3.5 h-3.5 mt-px shrink-0" />
          Climactic volume is confirming the opposite move — reversal risk.
        </div>
      )}
    </div>
  );
}

/**
 * Compact, self-contained Kronos forecast badge/card for any signal row or panel
 * (sniper signals, telegram signals, MT5 setups). Fetches on demand.
 *
 * Volume is a hard precondition on the backend: when it cannot be resolved the
 * API returns NO_TRADE and this card shows why rather than a direction.
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
  const noTrade = sig?.decision === 'NO_TRADE' || dir === 'no_trade';
  const color = noTrade ? 'text-amber-400'
    : dir === 'up' ? 'text-green-400'
    : dir === 'down' ? 'text-red-400'
    : 'text-yellow-400';
  const Icon = noTrade ? Ban : dir === 'up' ? TrendingUp : dir === 'down' ? TrendingDown : Minus;

  if (compact) {
    return (
      <span className={`inline-flex items-center gap-1 text-xs font-medium ${loading ? 'text-gray-500' : color}`}>
        <Telescope className={`w-3.5 h-3.5 ${loading ? 'animate-pulse' : ''}`} />
        {loading ? 'Kronos…'
          : error || !sig ? 'Kronos n/a'
          : noTrade ? `no trade · vol ${(sig.volume?.status ?? 'unavailable').toLowerCase()}`
          : `${sig.pct_change >= 0 ? '+' : ''}${sig.pct_change.toFixed(2)}% · ${Math.round(sig.confidence * 100)}%`
            + (sig.volume?.relative_volume != null ? ` · vol ×${sig.volume.relative_volume.toFixed(2)}` : '')}
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
          {noTrade ? (
            <div className={`flex items-center gap-2 text-base font-bold ${color}`}>
              <Icon className="w-5 h-5" /> NO TRADE
            </div>
          ) : (
            <>
              <div className={`flex items-center gap-2 text-lg font-bold ${color}`}>
                <Icon className="w-5 h-5" />
                {sig.pct_change >= 0 ? '+' : ''}{sig.pct_change.toFixed(2)}%
                <span className="text-xs font-normal text-gray-400 capitalize">{dir}</span>
              </div>
              <div className="mt-1 flex items-center justify-between text-xs">
                <span className="text-gray-500">Target {sig.target_price?.toPrecision?.(6) ?? sig.target_price}</span>
                <span className="text-gray-400">
                  {Math.round(sig.confidence * 100)}% conf.
                  {sig.decision === 'LOW_CONFIDENCE' && (
                    <span className="ml-1 text-amber-400">(low)</span>
                  )}
                </span>
              </div>
            </>
          )}

          {/* Volume evidence — always shown, because the call is gated on it. */}
          <div className="mt-2">
            <VolumeEvidence volume={sig.volume} />
          </div>

          {/* Why this direction was chosen */}
          {sig.rationale?.length > 0 && (
            <details className="mt-2 group">
              <summary className="text-[11px] text-purple-300/80 cursor-pointer hover:text-purple-200 list-none">
                Why this call? ▾
              </summary>
              <ul className="mt-1 space-y-0.5">
                {sig.rationale.map((r, i) => (
                  <li key={i} className="text-[11px] text-gray-400 leading-snug">• {r}</li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}
