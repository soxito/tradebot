/**
 * useZonesData — fetches the desk's zone read (supply/demand, fib bands,
 * channels, S/R levels) for a symbol and keeps it fresh. Returns null while
 * loading or when the endpoint has nothing, so charts simply draw without
 * overlays instead of erroring.
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '@/services/api';
import type { ZonesData } from '@/utils/zonesOverlay';

const REFRESH_MS = 45_000;

export function useZonesData(
  symbol: string | null | undefined,
  timeframe: string,
  enabled: boolean,
): ZonesData | null {
  const [data, setData] = useState<ZonesData | null>(null);
  const symbolRef = useRef(symbol);
  symbolRef.current = symbol;

  useEffect(() => {
    if (!enabled || !symbol) {
      setData(null);
      return;
    }

    let cancelled = false;

    async function load() {
      const sym = symbolRef.current;
      if (!sym) return;
      try {
        const urlSymbol = encodeURIComponent(sym.includes('/') ? sym : sym);
        const res = await api.get(`/signals/zones/${urlSymbol}`, {
          params: { timeframe },
          timeout: 15000,
        });
        if (!cancelled && res?.data) setData(res.data as ZonesData);
      } catch {
        // A missing/failed zone read is silence on the chart, not an error banner.
        if (!cancelled) setData(null);
      }
    }

    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [symbol, timeframe, enabled]);

  return data;
}
