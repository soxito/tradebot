import { useCallback, useEffect, useRef, useState } from 'react';

type WakeLockSentinelLike = { release: () => Promise<void>; released?: boolean };

/**
 * Screen Wake Lock hook — keeps the display awake while `active` is true
 * (e.g. during live trading monitoring). Re-acquires automatically when the tab
 * regains visibility, since the OS releases the lock when a tab is hidden.
 *
 * Degrades to a no-op on browsers without the Wake Lock API.
 */
export function useWakeLock(active: boolean): { supported: boolean; held: boolean } {
  const supported =
    typeof navigator !== 'undefined' && 'wakeLock' in navigator;
  const [held, setHeld] = useState(false);
  const sentinelRef = useRef<WakeLockSentinelLike | null>(null);

  const acquire = useCallback(async () => {
    if (!supported || document.visibilityState !== 'visible') return;
    try {
      const wl = (navigator as Navigator & {
        wakeLock: { request: (t: 'screen') => Promise<WakeLockSentinelLike> };
      }).wakeLock;
      const sentinel = await wl.request('screen');
      sentinelRef.current = sentinel;
      setHeld(true);
    } catch {
      setHeld(false);
    }
  }, [supported]);

  const release = useCallback(async () => {
    try {
      await sentinelRef.current?.release();
    } catch {
      /* ignore */
    }
    sentinelRef.current = null;
    setHeld(false);
  }, []);

  useEffect(() => {
    if (!supported) return;
    if (active) {
      acquire();
      const onVisible = () => {
        if (document.visibilityState === 'visible' && active) acquire();
      };
      document.addEventListener('visibilitychange', onVisible);
      return () => {
        document.removeEventListener('visibilitychange', onVisible);
        release();
      };
    }
    release();
  }, [active, supported, acquire, release]);

  return { supported, held };
}
