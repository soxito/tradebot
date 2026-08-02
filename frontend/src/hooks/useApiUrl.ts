import { useEffect, useState } from 'react';
import { getApiBaseUrl, getApiRootUrl } from '@/services/api';

/**
 * The API base URL, resolved on the client.
 *
 * `getApiBaseUrl()` needs `window` to discover the backend — from the current
 * host in development, or from the port the desktop app injects at launch.
 * During prerendering there is no window, so calling it directly inside JSX
 * bakes the wrong URL into the HTML and then trips a hydration mismatch when
 * the client computes the real one.
 *
 * This hook renders empty on the server and the first client pass, then fills
 * in after mount. Use it only for URLs that are *displayed* or put in an
 * `href`; for fetches, call `getApiBaseUrl()` directly at request time.
 */
export function useApiBaseUrl(): string {
  const [url, setUrl] = useState('');
  useEffect(() => setUrl(getApiBaseUrl()), []);
  return url;
}

/** As {@link useApiBaseUrl}, without the `/api/v1` suffix — for /docs, /health. */
export function useApiRootUrl(): string {
  const [url, setUrl] = useState('');
  useEffect(() => setUrl(getApiRootUrl()), []);
  return url;
}
