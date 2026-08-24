import { useEffect, useRef } from 'react';
import { pollMultiplier } from '@/utils/devicePerformance';

interface SmartPollOptions {
  /** Base interval in ms (before device multiplier). */
  intervalMs: number;
  /** Pause polling after this many ms hidden (refetch on show). Default 60s. */
  stopAfterHiddenMs?: number;
  /** Multiply the interval by the device performance multiplier. Default true. */
  scaleWithDevice?: boolean;
  enabled?: boolean;
}

/**
 * Interval polling that is kind to weak devices and background tabs:
 * - scales the interval by the device performance multiplier,
 * - pauses while document.hidden and stops entirely after a grace period,
 * - refetches immediately on becoming visible again,
 * - backs off (doubling) on consecutive errors.
 */
export function useSmartPoll(fn: () => void | Promise<void>, opts: SmartPollOptions): void {
  const {
    intervalMs,
    stopAfterHiddenMs = 60_000,
    scaleWithDevice = true,
    enabled = true,
  } = opts;

  const fnRef = useRef(fn);
  fnRef.current = fn;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hiddenSince = useRef<number | null>(null);
  const errorBackoff = useRef(1);

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;

    const base = () =>
      Math.max(500, intervalMs * (scaleWithDevice ? pollMultiplier() : 1));

    const schedule = (delay: number) => {
      if (cancelled) return;
      timer.current = setTimeout(tick, delay);
    };

    const tick = async () => {
      if (cancelled) return;
      // Stop entirely if hidden past the grace period.
      if (typeof document !== 'undefined' && document.hidden) {
        if (hiddenSince.current == null) hiddenSince.current = Date.now();
        if (Date.now() - hiddenSince.current > stopAfterHiddenMs) {
          return; // visibility handler restarts us
        }
      } else {
        hiddenSince.current = null;
      }
      try {
        await fnRef.current();
        errorBackoff.current = 1;
      } catch {
        errorBackoff.current = Math.min(errorBackoff.current * 2, 8);
      }
      schedule(base() * errorBackoff.current);
    };

    const onVisible = () => {
      if (typeof document !== 'undefined' && !document.hidden) {
        hiddenSince.current = null;
        if (timer.current) clearTimeout(timer.current);
        void tick(); // refetch immediately on show
      }
    };

    document.addEventListener('visibilitychange', onVisible);
    void tick();

    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, intervalMs, stopAfterHiddenMs, scaleWithDevice]);
}
