/**
 * useDeepgramAgent — wraps @deepgram/agents AgentSession for the Binary Engine
 * and PaulChat Deepgram mode.
 *
 * Key design:
 * - ALL functions are client_side (no endpoint field) — Deepgram's cloud can't
 *   reach localhost:8000, so every FunctionCallRequest is handled in the browser
 *   and we proxy to the backend ourselves via fetch('/api/v1/...').
 * - Token security: the browser never holds the raw Deepgram API key.
 *   Short-lived JWTs come from /api/v1/voice/deepgram/token (30s TTL).
 * - Full error handling: connect() is wrapped in try/catch; state always
 *   transitions to 'disconnected' with a clear error message on any failure.
 * - Auto-reconnect is disabled so errors surface immediately rather than
 *   keeping the UI stuck in 'reconnecting' forever.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import apiClient from '@/services/api';

// ── Types ─────────────────────────────────────────────────────────────────────

export type AgentSessionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

export interface ConversationEntry {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  ts: number;
}

export interface AgentLatency {
  total: number;
  tts: number;
  ttt: number;
}

export interface DeepgramAgentConfig {
  sttModel?: string;        // e.g. 'nova-3' | 'flux-general-en'
  llmProvider?: string;     // e.g. 'open_ai' | 'anthropic' | 'google' | 'groq'
  llmModel?: string;        // e.g. 'gpt-4o-mini'
  ttsModel?: string;        // e.g. 'aura-2-thalia-en'
  systemPrompt?: string;
  greeting?: string;
  eotThreshold?: number;    // 0.5–0.9, Flux only
  eagerEotThreshold?: number;
  eotTimeoutMs?: number;
}

export interface UseDeepgramAgentReturn {
  state: AgentSessionState;
  /** Error message from the last failed connection attempt, or null */
  error: string | null;
  transcript: ConversationEntry[];
  isSpeaking: boolean;
  isUserSpeaking: boolean;
  latency: AgentLatency | null;
  connect: (config?: DeepgramAgentConfig) => Promise<void>;
  disconnect: () => void;
  injectMessage: (content: string) => void;
  updatePrompt: (prompt: string) => void;
  updateVoice: (model: string) => void;
  clearTranscript: () => void;
  /** AgentPlayer output freq data for canvas visualisation — or null when idle */
  getOutputFreqData: () => Uint8Array | null;
  /** AgentMicrophone input volume 0–1 */
  getInputVolume: () => number;
}

// ── Function definitions (ALL client_side — NO endpoint field) ────────────────
// Deepgram's cloud cannot reach localhost:8000.  Every FunctionCallRequest is
// handled here in the browser; we call the backend ourselves via fetch.

function buildFunctions() {
  return [
    {
      name: 'navigate_to_page',
      description: 'Navigate to a page in the trading dashboard',
      parameters: {
        type: 'object',
        properties: {
          route: {
            type: 'string',
            enum: ['/', '/signals', '/binary-engine', '/trading', '/futures',
                   '/trending', '/strategies', '/sentiment', '/pump-monitor',
                   '/rug-pulled', '/sniper-signals', '/telegram-signals', '/vault',
                   '/settings', '/agents', '/intelligence'],
            description: 'The Next.js route to navigate to',
          },
        },
        required: ['route'],
      },
      // No endpoint → client_side: true
    },
    {
      name: 'get_active_signals',
      description: 'Get the current active trading signals',
      parameters: { type: 'object', properties: {} },
      // No endpoint → handled client-side
    },
    {
      name: 'get_price',
      description: 'Get the current spot price for a trading symbol like BTCUSDT',
      parameters: {
        type: 'object',
        properties: {
          symbol: { type: 'string', description: 'Trading pair e.g. BTCUSDT' },
        },
        required: ['symbol'],
      },
      // No endpoint → client-side
    },
    {
      name: 'place_limit_order',
      description: 'Place a limit order via MT5 — always confirm details with user first',
      parameters: {
        type: 'object',
        properties: {
          symbol: { type: 'string', description: 'Trading symbol' },
          side: { type: 'string', enum: ['buy', 'sell'] },
          volume: { type: 'number', description: 'Lot size e.g. 0.01' },
          price: { type: 'number', description: 'Limit price' },
        },
        required: ['symbol', 'side', 'volume', 'price'],
      },
      // No endpoint → client-side
    },
    {
      name: 'get_account_balance',
      description: 'Get current exchange account balance',
      parameters: { type: 'object', properties: {} },
      // No endpoint → client-side
    },
    {
      name: 'get_position_summary',
      description: 'Get summary of open MT5 positions',
      parameters: { type: 'object', properties: {} },
      // No endpoint → client-side
    },
  ];
}

// ── JARVIS persona — default system prompt & greeting ─────────────────────────
// These mirror JARVIS's voice/speech rules used by the in-page assistant
// (PaulChat) and the backend (jarvis.py): a concise, polite British "butler"
// tone that always addresses the user as "Sir". Exported so the Deepgram tab can
// prefill them as editable defaults.

export const JARVIS_DEFAULT_PROMPT = `You are JARVIS (you also answer to "PAUL" and "Sox"), an intelligent AI trading assistant embedded in a professional crypto trading platform.

Persona & speech rules (always follow):
- You are a polite, refined British butler. Always address the user as "Sir".
- Be concise and professional — short, direct sentences. No filler, no emoji.
- Acknowledge actions briefly ("Right away, Sir.", "One moment, Sir.", "Done, Sir.").
- Stay calm and matter-of-fact even when reporting problems.

You have access to real-time trading data and can help with:
- Checking live prices and market signals
- Analysing active trading opportunities
- Placing limit orders through MT5
- Reviewing account balances and open positions
- Navigating to different sections of the dashboard

When discussing trades, always confirm key details (symbol, side, volume, price) before executing.
If asked to place a trade, summarise the order details and ask for confirmation before calling place_limit_order.

Current platform context: crypto trading dashboard with MT5 integration, SMC signals, and multi-exchange support.`;

// JARVIS's opening line, mirroring PaulChat's greeting (minus the wake-word note,
// since the live agent is always listening). Used whenever no greeting is given.
export const JARVIS_DEFAULT_GREETING =
  "Good day, Sir. I'm PAUL, your personal trading assistant. How can I help you today? I can check open positions, recent signals, live market news, or forecast any pair.";

// ── Error mapping ─────────────────────────────────────────────────────────────
// Translate raw axios/SDK errors (e.g. "Request failed with status code 503")
// into clear, actionable messages so the UI never shows a bare status code.

function mapDeepgramError(err: any): string {
  const status: number | undefined = err?.response?.status;
  const data = err?.response?.data;
  // When responseType is 'text' the body is a string; otherwise it may be JSON.
  const detail: string | undefined =
    data && typeof data === 'object' ? (data.detail || data.error) : undefined;
  const msg = (err?.message ?? String(err)) || '';
  const lower = msg.toLowerCase();
  const is = (code: number) => status === code || msg.includes(String(code));

  if (is(503) || lower.includes('service unavailable')) {
    return (typeof detail === 'string' && detail) ||
      'Deepgram key not configured on the backend — add DEEPGRAM_API_KEY to .env and restart the backend.';
  }
  if (is(401) || is(403) || lower.includes('forbidden') || lower.includes('unauthorized')) {
    return (typeof detail === 'string' && detail) ||
      'Invalid Deepgram API key — check DEEPGRAM_API_KEY in .env (the key needs Member role / usage:write scope).';
  }
  if (lower.includes('network') || lower.includes('failed to fetch') || err?.code === 'ERR_NETWORK') {
    return 'Backend not running — start the backend server first, then retry.';
  }
  if (lower.includes('token')) {
    return `Token error: ${msg}`;
  }
  return (typeof detail === 'string' && detail) || msg || 'Connection failed';
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useDeepgramAgent(): UseDeepgramAgentReturn {
  const router = useRouter();

  const sessionRef = useRef<any>(null);
  const micRef     = useRef<any>(null);
  const playerRef  = useRef<any>(null);

  const [state,          setState]          = useState<AgentSessionState>('idle');
  const [error,          setError]          = useState<string | null>(null);
  const [transcript,     setTranscript]     = useState<ConversationEntry[]>([]);
  const [isSpeaking,     setIsSpeaking]     = useState(false);
  const [isUserSpeaking, setIsUserSpeaking] = useState(false);
  const [latency,        setLatency]        = useState<AgentLatency | null>(null);

  // ── Cleanup ───────────────────────────────────────────────────────────────

  const _cleanup = useCallback(() => {
    try { sessionRef.current?.disconnect(); } catch {}
    try { micRef.current?.stop(); }           catch {}
    try { playerRef.current?.dispose(); }     catch {}
    sessionRef.current = null;
    micRef.current     = null;
    playerRef.current  = null;
  }, []);

  // ── Function dispatcher (all client-side — proxies to backend via fetch) ──

  const _dispatch = useCallback(async (session: any, fns: any[]) => {
    for (const fn of fns) {
      let result: any;
      try {
        const args = (() => { try { return JSON.parse(fn.arguments ?? fn.input ?? '{}'); } catch { return {}; } })();
        switch (fn.name) {
          case 'navigate_to_page': {
            router.push(args.route ?? '/');
            result = { navigated: args.route ?? '/' };
            break;
          }
          case 'get_active_signals': {
            const r = await fetch('/api/v1/signals').catch(() => null);
            result = r?.ok ? await r.json().catch(() => ({})) : { error: 'signals unavailable' };
            break;
          }
          case 'get_price': {
            const sym = args.symbol ?? 'BTCUSDT';
            const r = await fetch(`https://api.binance.com/api/v3/ticker/price?symbol=${sym}`).catch(() => null);
            result = r?.ok ? await r.json().catch(() => ({})) : { error: `price unavailable for ${sym}` };
            break;
          }
          case 'place_limit_order': {
            const r = await fetch('/api/v1/voice/deepgram/fn/place_limit_order', {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args),
            }).catch(() => null);
            result = r?.ok ? await r.json().catch(() => ({})) : { error: 'order failed — backend unavailable' };
            break;
          }
          case 'get_account_balance': {
            const r = await fetch('/api/v1/voice/deepgram/fn/get_account_balance', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
            }).catch(() => null);
            result = r?.ok ? await r.json().catch(() => ({})) : { error: 'balance unavailable' };
            break;
          }
          case 'get_position_summary': {
            const r = await fetch('/api/v1/voice/deepgram/fn/get_position_summary', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
            }).catch(() => null);
            result = r?.ok ? await r.json().catch(() => ({})) : { error: 'positions unavailable' };
            break;
          }
          default:
            result = { error: `Unknown function: ${fn.name}` };
        }
      } catch (e) {
        result = { error: String(e) };
      }
      try { session.sendFunctionCallResponse(fn.id, fn.name, JSON.stringify(result)); } catch {}
    }
  }, [router]);

  // ── connect ───────────────────────────────────────────────────────────────

  const connect = useCallback(async (config: DeepgramAgentConfig = {}) => {
    if (typeof window === 'undefined') return;

    _cleanup();
    setError(null);
    setState('connecting');

    try {
      // ── Pre-flight ──────────────────────────────────────────────────────
      // Validate we can obtain a token BEFORE constructing the AgentSession.
      // This fails fast (so the UI never sticks on "connecting…") and maps a
      // missing/invalid key to a clear message instead of the SDK surfacing a
      // raw axios "Request failed with status code 503".
      try {
        const preToken = await apiClient.deepgram.getToken();
        if (!preToken || typeof preToken !== 'string' || preToken.length < 10) {
          throw new Error('Invalid token — is the backend running?');
        }
      } catch (preErr: any) {
        _cleanup();
        setState('disconnected');
        setError(mapDeepgramError(preErr));
        return;
      }

      const { AgentSession, AgentMicrophone, AgentPlayer } = await import('@deepgram/agents');

      // Listen config
      const isFlux = (config.sttModel ?? 'nova-3').startsWith('flux');
      const listenProvider: any = {
        type: 'deepgram',
        model: config.sttModel ?? 'nova-3',
        ...(isFlux
          ? {
              version: 'v2',
              ...(config.eotThreshold      && { eot_threshold:       config.eotThreshold }),
              ...(config.eagerEotThreshold && { eager_eot_threshold: config.eagerEotThreshold }),
              ...(config.eotTimeoutMs      && { eot_timeout_ms:      config.eotTimeoutMs }),
            }
          : { version: 'v1' }),
      };

      // Think config
      const thinkProvider: any = {
        type:        config.llmProvider ?? 'open_ai',
        model:       config.llmModel    ?? 'gpt-4o-mini',
        temperature: 0.7,
      };

      // Agent config (all functions are client_side — no endpoint)
      const agentConfig: any = {
        listen: { provider: listenProvider },
        think: {
          provider:  thinkProvider,
          prompt:    config.systemPrompt ?? JARVIS_DEFAULT_PROMPT,
          functions: buildFunctions(),
        },
        speak: {
          provider: { type: 'deepgram', model: config.ttsModel ?? 'aura-2-thalia-en' },
        },
        // Default to JARVIS's greeting so the agent opens in-character.
        greeting: config.greeting ?? JARVIS_DEFAULT_GREETING,
      };

      const session = new AgentSession({
        auth: {
          tokenFactory: async () => {
            const token = await apiClient.deepgram.getToken();
            if (!token || typeof token !== 'string' || token.length < 10) {
              throw new Error('Invalid token — is the backend running?');
            }
            return token;
          },
        },
        agent: agentConfig,
        audio: {
          input:  { encoding: 'linear16', sampleRate: 16000 },
          output: { encoding: 'linear16', sampleRate: 24000 },
        },
        // Disable auto-reconnect so errors surface cleanly instead of looping
        reconnect: { enabled: false, maxAttempts: 0 },
        keepAliveInterval: 10000,
      });

      const player = new AgentPlayer({ sampleRate: 24000 });
      const mic    = new AgentMicrophone(
        (data: ArrayBuffer) => { try { session.sendAudio(data); } catch {} },
        { sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      );

      // Events
      session.on('connecting',   ()           => setState('connecting'));
      session.on('connected',    ()           => { setState('connected'); setError(null); });
      session.on('reconnecting', ()           => setState('reconnecting'));
      session.on('disconnected', (reason: string) => {
        setState('disconnected');
        const msg = reason || '';
        setError(msg && !msg.includes('1000') ? msg : null);
        try { mic.stop(); }       catch {}
        try { player.dispose(); } catch {}
      });
      session.on('sdk-error', (err: Error) => {
        // Map raw SDK/axios errors (e.g. "Request failed with status code 503")
        // to an actionable message and always leave the "connecting" state.
        setError(mapDeepgramError(err));
        setState('disconnected');
      });
      session.on('error', (msg: any) => {
        const raw = msg?.description ?? msg?.message ?? String(msg);
        const mapped = mapDeepgramError({ message: String(raw), response: { status: msg?.code } });
        setError(mapped);
        // If the error arrives before we ever reached "connected", make sure the
        // UI doesn't stay stuck on "connecting…".
        setState(prev => (prev === 'connecting' || prev === 'reconnecting') ? 'disconnected' : prev);
      });

      session.on('audio',                (chunk: ArrayBuffer) => { try { player.queue(chunk); } catch {} });
      session.on('user-started-speaking', ()                  => { setIsUserSpeaking(true); try { player.interrupt(); } catch {} });
      session.on('agent-started-speaking', (msg: any)         => {
        setIsSpeaking(true);
        if (msg?.total_latency != null) {
          setLatency({ total: msg.total_latency, tts: msg.tts_latency ?? 0, ttt: msg.ttt_latency ?? 0 });
        }
      });
      session.on('agent-audio-done', () => setIsSpeaking(false));
      session.on('conversation-text', (msg: any) => {
        setIsUserSpeaking(false);
        setTranscript(prev => [...prev, {
          id:      `${Date.now()}-${Math.random()}`,
          role:    msg.role as 'user' | 'assistant',
          content: msg.content ?? '',
          ts:      Date.now(),
        }]);
      });
      session.on('function-call-request', async (msg: any) => {
        await _dispatch(session, msg?.functions ?? []);
      });

      sessionRef.current = session;
      micRef.current     = mic;
      playerRef.current  = player;

      await session.connect();
      await mic.start();

    } catch (err: any) {
      _cleanup();
      setState('disconnected');
      setError(mapDeepgramError(err));
    }
  }, [_cleanup, _dispatch]);

  // ── disconnect ────────────────────────────────────────────────────────────

  const disconnect = useCallback(() => {
    _cleanup();
    setState('idle');
    setError(null);
    setIsSpeaking(false);
    setIsUserSpeaking(false);
  }, [_cleanup]);

  const injectMessage  = useCallback((c: string)   => { try { sessionRef.current?.injectUserMessage(c); }                                       catch {} }, []);
  const updatePrompt   = useCallback((p: string)   => { try { sessionRef.current?.updatePrompt(p); }                                            catch {} }, []);
  const updateVoice    = useCallback((m: string)   => { try { sessionRef.current?.updateSpeak({ provider: { type: 'deepgram', model: m } }); }  catch {} }, []);
  const clearTranscript = useCallback(() => setTranscript([]), []);
  const getOutputFreqData = useCallback((): Uint8Array | null => playerRef.current?.getOutputByteFrequencyData?.() ?? null, []);
  const getInputVolume    = useCallback((): number            => micRef.current?.getInputVolume?.() ?? 0,                                        []);

  useEffect(() => () => { _cleanup(); }, [_cleanup]);

  return {
    state,
    error,
    transcript,
    isSpeaking,
    isUserSpeaking,
    latency,
    connect,
    disconnect,
    injectMessage,
    updatePrompt,
    updateVoice,
    clearTranscript,
    getOutputFreqData,
    getInputVolume,
  };
}
