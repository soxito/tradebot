/**
 * Desktop Notifications + Vibration for realtime trading events.
 *
 * Opt-in and OFF by default: nothing is requested or shown until the user
 * enables notifications in Settings (which triggers the permission prompt).
 * Wired to the shared SSE stream via `useRealtimeNotifications`.
 */
import { useEffect } from 'react';
import { eventStream } from '@/services/eventStream';

const ENABLED_KEY = 'tradebot.notify.enabled';
const VIBRATE_KEY = 'tradebot.notify.vibrate';

export function notificationsSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

export function notificationsEnabled(): boolean {
  if (!notificationsSupported()) return false;
  try {
    return localStorage.getItem(ENABLED_KEY) === '1';
  } catch {
    return false;
  }
}

export function vibrationEnabled(): boolean {
  try {
    return localStorage.getItem(VIBRATE_KEY) !== '0'; // default on when notifications on
  } catch {
    return true;
  }
}

export function setVibrationEnabled(on: boolean): void {
  try {
    localStorage.setItem(VIBRATE_KEY, on ? '1' : '0');
  } catch {
    /* ignore */
  }
}

/** Request Notification permission. Returns true if granted. */
export async function ensurePermission(): Promise<boolean> {
  if (!notificationsSupported()) return false;
  if (Notification.permission === 'granted') return true;
  if (Notification.permission === 'denied') return false;
  try {
    const res = await Notification.requestPermission();
    return res === 'granted';
  } catch {
    return false;
  }
}

/** Enable/disable notifications; enabling requests permission first. */
export async function setNotificationsEnabled(on: boolean): Promise<boolean> {
  if (on) {
    const granted = await ensurePermission();
    if (!granted) return false;
  }
  try {
    localStorage.setItem(ENABLED_KEY, on ? '1' : '0');
  } catch {
    /* ignore */
  }
  return on;
}

export function notify(
  title: string,
  body?: string,
  opts?: { tag?: string; vibrate?: boolean },
): void {
  if (!notificationsEnabled() || Notification.permission !== 'granted') return;
  try {
    // eslint-disable-next-line no-new
    new Notification(title, { body, tag: opts?.tag, icon: '/favicon.ico' });
  } catch {
    /* ignore */
  }
  if (opts?.vibrate && vibrationEnabled() && typeof navigator !== 'undefined' && navigator.vibrate) {
    try {
      navigator.vibrate([120, 60, 120]);
    } catch {
      /* ignore */
    }
  }
}

type TradeEvent = {
  event?: string;
  symbol?: string;
  side?: string;
  reason?: string;
  pnl?: number;
};
type SignalEvent = { symbol?: string; action?: string; source?: string; count?: number };

/**
 * Mount once (e.g. in Layout) to surface realtime events as desktop
 * notifications. No-op unless the user opted in via Settings.
 */
export function useRealtimeNotifications(): void {
  useEffect(() => {
    const unsubs = [
      eventStream.subscribe('signal.new', (raw) => {
        if (!notificationsEnabled()) return;
        const d = (raw ?? {}) as SignalEvent;
        const label =
          d.symbol ? `${(d.action ?? '').toUpperCase()} ${d.symbol}`.trim()
          : d.count ? `${d.count} new signal${d.count > 1 ? 's' : ''}`
          : 'A new trading signal arrived';
        notify('📊 New signal', label, { tag: 'signal', vibrate: true });
      }),
      eventStream.subscribe('trade.update', (raw) => {
        if (!notificationsEnabled()) return;
        const d = (raw ?? {}) as TradeEvent;
        if (d.event === 'sl_tp_hit') {
          const tp = d.reason === 'take_profit';
          notify(tp ? '🎯 Take-profit hit' : '🛑 Stop-loss hit', `${d.symbol} · PnL ${d.pnl}`, {
            tag: 'trade',
            vibrate: true,
          });
        } else if (d.event === 'closed_position') {
          notify('Position closed', `${d.symbol} · PnL ${d.pnl}`, { tag: 'trade' });
        }
      }),
    ];
    return () => unsubs.forEach((u) => u());
  }, []);
}
