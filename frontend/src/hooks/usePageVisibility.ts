import { useEffect, useState } from 'react';

/**
 * Page Visibility API hook.
 *
 * Returns `true` while the tab is visible. Use it to pause polling fallbacks and
 * expensive work while the user is on another tab — the realtime SSE stream keeps
 * the UI fresh, so nothing needs to poll in the background.
 */
export function usePageVisibility(): boolean {
  const [visible, setVisible] = useState<boolean>(
    typeof document === 'undefined' ? true : !document.hidden,
  );

  useEffect(() => {
    if (typeof document === 'undefined') return;
    const onChange = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);

  return visible;
}
