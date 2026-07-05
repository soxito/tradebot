import { useEffect, useState } from 'react';
import { eventStream, type StreamState, type StreamTopic } from '@/services/eventStream';

/**
 * Subscribe to a realtime SSE topic.
 *
 * @param topic   One of the STREAM_TOPICS.
 * @param handler Called with the parsed event payload on every matching event.
 *                Keep it stable (useCallback) or rely on the ref-capture below.
 * @returns       `{ connected }` — true while the shared stream is live. Use it
 *                to gate polling fallbacks (poll only when `!connected`).
 */
export function useEventStream(topic: StreamTopic, handler: (data: unknown) => void) {
  const [connected, setConnected] = useState<boolean>(eventStream.getState() === 'live');

  useEffect(() => {
    const unsubState = eventStream.onState((s: StreamState) => setConnected(s === 'live'));
    const unsub = eventStream.subscribe(topic, handler);
    return () => {
      unsub();
      unsubState();
    };
    // handler is intentionally re-subscribed when it changes so callers can pass
    // an inline closure; pass a memoized handler to avoid churn if needed.
  }, [topic, handler]);

  return { connected };
}

/** Track only the shared stream connection state (no topic subscription). */
export function useStreamState(): StreamState {
  const [state, setState] = useState<StreamState>(eventStream.getState());
  useEffect(() => eventStream.onState(setState), []);
  return state;
}
