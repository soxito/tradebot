import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import type { ResearchPlan } from '@/hooks/useResearchJobs';

export type { ResearchPlan };

/** Plans change on the research cadence, not the price cadence. */
const POLL_MS = 60_000;

/**
 * Research plans for a set of instruments, keyed by symbol.
 *
 * Every page that lists pairs — signals, trending, sniper signals, rug pulls —
 * reads plans through this one hook, so a pair's verdict and entries are
 * identical wherever they appear. One request covers the whole page rather than
 * one per row.
 *
 * Symbols are compared by value, not identity: callers derive them with
 * `.map()` inside render, so an array-identity dependency would refetch on
 * every keystroke.
 */
export function useResearchPlans(symbols: string[], enabled = true) {
  const [plans, setPlans] = useState<Record<string, ResearchPlan>>({});
  const [loading, setLoading] = useState(false);

  const key = Array.from(new Set(symbols.filter(Boolean))).sort().join(',');
  const keyRef = useRef(key);
  keyRef.current = key;

  const fetchPlans = useCallback(async () => {
    if (!enabled || !keyRef.current) {
      setPlans({});
      return;
    }
    setLoading(true);
    try {
      const res = await apiClient.research.plans(keyRef.current);
      setPlans(res.data?.plans || {});
    } catch {
      // A missing research subsystem must not break the page it decorates.
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    fetchPlans();
    const id = setInterval(fetchPlans, POLL_MS);
    return () => clearInterval(id);
  }, [fetchPlans, key]);

  /**
   * Look a plan up by the same symbol string the page already holds.
   *
   * The server echoes the requested spelling back as the key, so an exact hit
   * is the normal path; the fallbacks only cover a row whose symbol differs in
   * case or separator from the one that was fetched.
   */
  const planFor = useCallback(
    (symbol?: string | null): ResearchPlan | undefined => {
      if (!symbol) return undefined;
      return (
        plans[symbol] ??
        plans[symbol.toUpperCase()] ??
        plans[symbol.toUpperCase().replace(/[/\-_]/g, '')]
      );
    },
    [plans],
  );

  return { plans, planFor, loading, refetch: fetchPlans };
}
