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

export interface KronosSignal {
  direction: 'up' | 'down' | 'flat';
  pct_change: number;
  confidence: number;
  target_price: number;
  anchor_price: number;
  summary: string;
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
