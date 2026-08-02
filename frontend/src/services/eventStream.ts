/**
 * Realtime EventStream — a single shared Server-Sent Events connection.
 *
 * One browser tab (elected leader via the Web Locks API) holds the actual
 * `EventSource` to the backend and rebroadcasts every event over a
 * `BroadcastChannel` to the other tabs. This keeps the whole app to ONE backend
 * connection regardless of how many tabs are open, sidestepping the HTTP/1.1
 * 6-connections-per-origin cap.
 *
 * Consumers use the `useEventStream` hook; they never touch this class directly.
 * All polling in the app is kept as a fallback gated on `connected === false`.
 */

import { getApiBaseUrl } from './api';

export const STREAM_TOPICS = [
  'signal.new',
  'trade.update',
  'sniper.event',
  'sentiment.update',
  'monitor.status',
  'price.tick',
  'system.alert',
] as const;

export type StreamTopic = (typeof STREAM_TOPICS)[number];
export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'closed';

type Handler = (data: unknown) => void;
type StateHandler = (state: StreamState) => void;

const CHANNEL_NAME = 'tradebot-stream';
const LOCK_NAME = 'tradebot-stream-leader';
const MAX_BACKOFF_MS = 30_000;

class EventStreamManager {
  private handlers = new Map<string, Set<Handler>>();
  private stateHandlers = new Set<StateHandler>();
  private es: EventSource | null = null;
  private channel: BroadcastChannel | null = null;
  private isLeader = false;
  private started = false;
  private state: StreamState = 'closed';
  private backoff = 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private releaseLock: (() => void) | null = null;

  // ── Public API ────────────────────────────────────────────────────────────
  subscribe(topic: StreamTopic, handler: Handler): () => void {
    let set = this.handlers.get(topic);
    if (!set) {
      set = new Set();
      this.handlers.set(topic, set);
    }
    set.add(handler);
    this.ensureStarted();
    return () => {
      set?.delete(handler);
    };
  }

  onState(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    // Emit current state immediately so late subscribers are in sync.
    handler(this.state);
    return () => {
      this.stateHandlers.delete(handler);
    };
  }

  getState(): StreamState {
    return this.state;
  }

  // ── Startup / leader election ─────────────────────────────────────────────
  private ensureStarted() {
    if (this.started || typeof window === 'undefined') return;
    this.started = true;

    try {
      this.channel = new BroadcastChannel(CHANNEL_NAME);
      this.channel.onmessage = (ev) => this.onChannelMessage(ev.data);
    } catch {
      this.channel = null; // BroadcastChannel unsupported → per-tab connection
    }

    // Release leadership cleanly when the tab goes away so another tab takes over.
    window.addEventListener('pagehide', () => this.releaseLock?.());

    this.electLeader();
  }

  private electLeader() {
    const locks = (navigator as Navigator & { locks?: LockManager }).locks;
    if (!locks || !this.channel) {
      // No Web Locks or no BroadcastChannel → this tab owns its own connection.
      this.becomeLeader();
      return;
    }
    // Hold an exclusive lock for as long as this tab is the leader. When the tab
    // closes, the lock auto-releases and a waiting tab's callback fires.
    locks
      .request(LOCK_NAME, { mode: 'exclusive' }, () => {
        this.becomeLeader();
        return new Promise<void>((resolve) => {
          this.releaseLock = resolve;
        });
      })
      .catch(() => this.becomeLeader());
  }

  private becomeLeader() {
    this.isLeader = true;
    this.connect();
  }

  // ── Leader: the real EventSource ──────────────────────────────────────────
  private connect() {
    if (typeof window === 'undefined') return;
    this.setState('connecting');

    const url = `${getApiBaseUrl()}/stream/events`; // no topic filter → receive all
    let es: EventSource;
    try {
      es = new EventSource(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.es = es;

    es.addEventListener('connected', () => {
      this.backoff = 1000;
      this.setState('live');
    });

    for (const topic of STREAM_TOPICS) {
      es.addEventListener(topic, (ev: MessageEvent) => {
        let data: unknown = null;
        try {
          data = JSON.parse(ev.data);
        } catch {
          data = ev.data;
        }
        this.dispatchLocal(topic, data);
        this.channel?.postMessage({ kind: 'event', topic, data });
      });
    }

    es.onopen = () => {
      this.backoff = 1000;
      this.setState('live');
    };

    es.onerror = () => {
      es.close();
      this.es = null;
      this.setState('reconnecting');
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.isLeader) this.connect();
    }, delay);
  }

  // ── Follower: receive rebroadcast events ──────────────────────────────────
  private onChannelMessage(msg: { kind: string; topic?: string; state?: StreamState; data?: unknown }) {
    if (this.isLeader) return; // leader already handled it locally
    if (msg.kind === 'event' && msg.topic) {
      this.dispatchLocal(msg.topic, msg.data);
    } else if (msg.kind === 'state' && msg.state) {
      this.setState(msg.state, /* fromLeader */ true);
    }
  }

  // ── Dispatch + state ──────────────────────────────────────────────────────
  private dispatchLocal(topic: string, data: unknown) {
    const set = this.handlers.get(topic);
    if (!set) return;
    set.forEach((h) => {
      try {
        h(data);
      } catch {
        /* handler errors must not break the stream */
      }
    });
  }

  private setState(state: StreamState, fromLeader = false) {
    if (this.state === state) return;
    this.state = state;
    this.stateHandlers.forEach((h) => {
      try {
        h(state);
      } catch {
        /* ignore */
      }
    });
    // Leader broadcasts its connection state so followers can reflect it.
    if (this.isLeader && !fromLeader) {
      this.channel?.postMessage({ kind: 'state', state });
    }
  }
}

export const eventStream = new EventStreamManager();
