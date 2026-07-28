import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';

export interface KronosForecastCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface KronosBandPoint {
  time: number;
  value: number;
}

export interface KronosOverlaySeries {
  name: string;
  type: 'line';
  pane: string;
  color: string;
  lineWidth?: number;
  lineStyle?: number;
  data: { time: number; value: number }[];
}

export interface KronosMarker {
  time: number;
  position: 'belowBar' | 'aboveBar' | 'inBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square';
  text: string;
}

export type KronosDirection = 'up' | 'down' | 'flat' | 'no_trade';
export type KronosDecision = 'OK' | 'LOW_CONFIDENCE' | 'NO_TRADE';
export type KronosVolumeStatus = 'OK' | 'UNAVAILABLE' | 'STALE' | 'INSUFFICIENT';
export type KronosVolumeRegime = 'DEAD' | 'NORMAL' | 'ELEVATED' | 'CLIMACTIC' | 'UNKNOWN';
export type KronosVolumeDivergence =
  | 'CONFIRMED_UP' | 'EXHAUSTION_UP' | 'CONFIRMED_DOWN' | 'EXHAUSTION_DOWN'
  | 'NEUTRAL' | 'UNKNOWN';

/**
 * Resolved volume evidence. Volume is a hard precondition: when `status` is not
 * 'OK' the backend returns NO_TRADE and no direction was inferred.
 */
export interface KronosVolumeContext {
  status: KronosVolumeStatus;
  symbol: string;
  source: string;
  /** 'futures' = the matching CME contract's volume, the published proxy for
   *  spot FX/metals flow (spot FX is OTC and prints no volume of its own). */
  volume_unit: 'base' | 'tick' | 'futures' | 'unknown';
  volume_24h: number | null;
  volume_1h: number | null;
  hourly_mean_24h: number | null;
  relative_volume: number | null;
  z_score: number | null;
  regime: KronosVolumeRegime;
  divergence: KronosVolumeDivergence;
  divergence_bars: number;
  price_change_pct: number | null;
  volume_slope_norm: number | null;
  reversal_risk: boolean;
  hours_covered: number;
  last_bar_time: number | null;
  age_seconds: number | null;
  detail: string;
}

export interface KronosSignal {
  direction: KronosDirection;
  pct_change: number;
  confidence: number;
  target_price: number;
  anchor_price: number;
  summary: string;
  decision: KronosDecision;
  /** Why this direction was chosen — every signal carries it. */
  rationale: string[];
}

export interface KronosForecastData {
  exchange: string;
  symbol: string;
  timeframe: string;
  engine: 'kronos' | 'heuristic' | 'unavailable';
  model_name: string;
  lookback: number;
  pred_len: number;
  samples: number;
  anchor_time: number;
  anchor_price: number;
  forecast: KronosForecastCandle[];
  upper_band: KronosBandPoint[];
  lower_band: KronosBandPoint[];
  signal: KronosSignal | null;
  overlays: KronosOverlaySeries[];
  markers: KronosMarker[];
  candles: KronosForecastCandle[];
  volume: KronosVolumeContext | null;
  decision: KronosDecision;
  note?: string | null;
}

export interface KronosForecastParams {
  timeframe?: string;
  lookback?: number;
  pred_len?: number;
  samples?: number;
  temperature?: number;
  top_p?: number;
}

/**
 * Fetch a Kronos forecast for a symbol. Returns overlays/markers ready to feed
 * straight into <TradingViewChart overlays=... markers=... />, plus the raw
 * forecast + signal for custom UI.
 */
export function useKronosForecast(
  exchange: string,
  symbol: string,
  params: KronosForecastParams = {},
  enabled = true,
) {
  const [data, setData] = useState<KronosForecastData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Stabilise the params object across renders
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const fetchForecast = useCallback(async () => {
    if (!enabled || !symbol) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiClient.kronos.forecast(exchange, symbol, paramsRef.current);
      setData(res.data as KronosForecastData);
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Forecast failed');
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [exchange, symbol, enabled]);

  useEffect(() => {
    fetchForecast();
  }, [fetchForecast, params.timeframe, params.pred_len, params.samples]);

  return { data, loading, error, refetch: fetchForecast };
}
