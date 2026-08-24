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

// ── Full-duplex voice turn state machine ─────────────────────────────────────
//
//   IDLE ──start()──▶ LISTENING ──beginThinking()──▶ THINKING ──beginSpeaking()──▶ SPEAKING
//                        ▲                              │                            │
//                        └──── endSpeaking() ───────────┴──── bargeIn() ◀─────────────┘
//
// One machine, one owner. Every voice surface (chat, robot, orb) reads `state`
// instead of keeping its own `isSpeaking` / `isListening` / `micGated` flags —
// the old boolean soup is exactly how JARVIS used to end up in a state where it
// was neither speaking nor listening.
//
// Two invariants the machine enforces itself:
//
//  1. The microphone stream is opened once by start() and stays open — and stays
//     *processing* — through every state, including SPEAKING. It is never
//     stopped to suppress feedback; self-hearing is solved by gating, below.
//  2. SPEAKING always terminates. endSpeaking() returns to LISTENING, and a
//     watchdog does the same if playback dies without firing its events, so the
//     assistant can never be left deaf after a reply.
//
// Self-hearing without muting (requirement: full duplex). Four independent
// gates, all active only while SPEAKING:
//   • the browser's own AEC/NS/AGC on the capture (echoCancellation & co.),
//   • a raised energy gate (`speakingGate` ≫ `listenGate`),
//   • N consecutive voiced frames instead of one (a click or an echo tail
//     transient can spike a single frame; speech sustains),
//   • the caller's reference level and echo classifier — what JARVIS is
//     emitting right now is subtracted from the bar the user has to clear.

export type VoiceTurnState = 'IDLE' | 'LISTENING' | 'THINKING' | 'SPEAKING';

export type BargeInReason = 'voice' | 'manual' | 'wake-word' | 'camera';

/** Everything cancellable that makes up one spoken turn. */
export interface SpeakingHandles {
  /** Audio element(s) carrying the reply. Paused, ducked and flushed on barge-in. */
  audio?: HTMLAudioElement | null;
  /** In-flight TTS request (fetch) — aborted on barge-in. */
  ttsAbort?: AbortController | null;
  /** In-flight streaming response (LLM/SSE) — aborted on barge-in. */
  streamAbort?: AbortController | null;
  /** Also cancel window.speechSynthesis (system-voice path). Default true. */
  cancelSynthesis?: boolean;
}

export interface VoiceTurnOptions {
  /** RMS gate for user speech while NOT speaking (0–1). */
  listenGate?: number;
  /** RMS gate for user speech while TTS is playing — deliberately higher. */
  speakingGate?: number;
  /** Consecutive voiced frames required to accept speech during TTS. */
  bargeInFrames?: number;
  /** Consecutive voiced frames required to report user speech while listening. */
  listenFrames?: number;
  /** Analysis frame interval (ms). 40ms ⇒ 25Hz ⇒ ~160ms barge-in detection. */
  frameMs?: number;
  /** Ignore the first moments of playback (speaker ramp-up transient). */
  graceMs?: number;
  /** How much of the TTS reference level is subtracted from the gate (0–1). */
  refSuppress?: number;
  /** Live level 0–1 of what JARVIS is emitting right now, when tappable. */
  getReferenceLevel?: () => number;
  /** True when the current mic frame has been classified as our own echo. */
  isSelfEcho?: () => boolean;
  /** True when the current mic frame matches the calibrated user (accept-all when voice-match is off). Gates barge-in so JARVIS's own echo can never interrupt him. */
  isUserVoice?: () => boolean;
  /** Confirmed user speech during SPEAKING/THINKING — after cancellation. */
  onBargeIn?: (reason: BargeInReason) => void;
  /** Every state transition (for visuals / logging). */
  onStateChange?: (next: VoiceTurnState, prev: VoiceTurnState) => void;
  /** Capture failed (permission denied, no device). */
  onCaptureError?: (err: unknown) => void;
  /** Give up on a stuck THINKING turn after this long. */
  thinkingTimeoutMs?: number;
}

export interface UseVoiceTurnReturn {
  /** The single source of truth for the conversation turn. */
  state: VoiceTurnState;
  /** Same value, readable from inside callbacks without re-subscribing. */
  stateRef: React.MutableRefObject<VoiceTurnState>;
  /** True while the energy monitor has confirmed the user is talking. */
  userSpeaking: boolean;
  /** True once the capture is open and processing. */
  captureLive: boolean;
  /** Smoothed mic RMS 0–1 (for meters / robot motion). */
  getLevel: () => number;
  /** Open the capture (idempotent) and enter LISTENING. */
  start: () => Promise<boolean>;
  /** Tear the capture down — for unmount / the user disabling voice only. */
  stop: () => void;
  /** A reply is being produced. Pass the streaming abort so barge-in kills it. */
  beginThinking: (handles?: SpeakingHandles) => void;
  beginSpeaking: (handles?: SpeakingHandles) => void;
  /** Add another clip to the current turn's playback queue. */
  enqueueAudio: (el: HTMLAudioElement) => void;
  /**
   * Cancel whatever is currently on the speakers WITHOUT changing state — for
   * replacing one utterance with the next ("One moment, Sir." → the answer).
   * Barge-in is `bargeIn()`, not this.
   */
  cancelSpeech: () => void;
  /** Normal end of a reply — always returns to LISTENING. */
  endSpeaking: () => void;
  /** Cancel the current turn (playback + requests) and return to LISTENING. */
  bargeIn: (reason?: BargeInReason) => void;
}

const TURN_DEFAULTS = {
  listenGate: 0.030,
  speakingGate: 0.085,
  bargeInFrames: 5,
  listenFrames: 2,
  frameMs: 40,
  graceMs: 400,
  refSuppress: 0.55,
  thinkingTimeoutMs: 45_000,
};

// Ceiling on the reference-derived part of the speaking gate (RMS). Raised so a
// loud reply lifts the gate well above post-AEC residual echo — the user talking
// over JARVIS still clears it, but his own trailing echo never does.
const REF_SUPPRESS_MAX = 0.12;

// A frame is "quiet" enough to teach the noise floor when it sits below this
// multiple of the current floor; the floor then sets a relative gate so the
// machine works in a quiet study and next to an air conditioner alike.
const NOISE_FLOOR_RATIO = 2.4;
const NOISE_FLOOR_ALPHA = 0.05;

/**
 * The turn-taking machine + full-duplex mic monitor.
 *
 * Deliberately independent of the STT engine: it measures raw energy on its own
 * always-open capture, so barge-in fires in ~150ms without waiting for a
 * recogniser to deliver a transcript (which, mid-reply, it may never do).
 */
export function useVoiceTurn(options: VoiceTurnOptions = {}): UseVoiceTurnReturn {
  const optsRef = useRef(options);
  optsRef.current = options;

  const [state, setStateRaw] = useState<VoiceTurnState>('IDLE');
  const stateRef = useRef<VoiceTurnState>('IDLE');
  const [userSpeaking, setUserSpeaking] = useState(false);
  const [captureLive, setCaptureLive] = useState(false);

  const streamRef   = useRef<MediaStream | null>(null);
  const ctxRef      = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceRef   = useRef<MediaStreamAudioSourceNode | null>(null);
  const bufRef      = useRef<Uint8Array<ArrayBuffer> | null>(null);
  const timerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const startingRef = useRef<Promise<boolean> | null>(null);

  const levelRef       = useRef(0);
  const noiseFloorRef  = useRef(0.012);
  const voicedRef      = useRef(0);
  const speakStartRef  = useRef(0);
  const thinkStartRef   = useRef(0);
  const handlesRef     = useRef<SpeakingHandles>({});
  const audioQueueRef  = useRef<HTMLAudioElement[]>([]);

  const cfg = useCallback(<K extends keyof typeof TURN_DEFAULTS>(key: K): number => {
    const v = (optsRef.current as Record<string, unknown>)[key];
    return typeof v === 'number' ? v : TURN_DEFAULTS[key];
  }, []);

  // ── Transitions ───────────────────────────────────────────────────────────
  // Every state change goes through here so the notification and the mirrored
  // ref can never drift from the rendered state.
  const transition = useCallback((next: VoiceTurnState) => {
    const prev = stateRef.current;
    if (prev === next) return;
    stateRef.current = next;
    setStateRaw(next);
    if (next !== 'LISTENING') { voicedRef.current = 0; setUserSpeaking(false); }
    try { optsRef.current.onStateChange?.(next, prev); } catch { /* consumer's problem */ }
  }, []);

  /** Stop and discard every clip queued for the current turn. */
  const flushPlayback = useCallback(() => {
    const queue = audioQueueRef.current;
    audioQueueRef.current = [];
    for (const el of queue) {
      try { el.volume = 0; } catch { /* detached */ }
      try { el.pause(); } catch { /* detached */ }
      try { el.currentTime = 0; } catch { /* not seekable */ }
    }
  }, []);

  /** Cancel everything that belongs to the in-flight turn. */
  const cancelTurn = useCallback(() => {
    const h = handlesRef.current;
    handlesRef.current = {};
    if (h.audio) {
      try { h.audio.volume = 0; } catch { /* detached */ }
      try { h.audio.pause(); } catch { /* detached */ }
    }
    flushPlayback();
    try { h.ttsAbort?.abort(); }    catch { /* already settled */ }
    try { h.streamAbort?.abort(); } catch { /* already settled */ }
    if (h.cancelSynthesis !== false && typeof window !== 'undefined' && 'speechSynthesis' in window) {
      try { window.speechSynthesis.cancel(); } catch { /* unsupported */ }
    }
  }, [flushPlayback]);

  const bargeIn = useCallback((reason: BargeInReason = 'manual') => {
    const cur = stateRef.current;
    if (cur !== 'SPEAKING' && cur !== 'THINKING') return;
    cancelTurn();
    // Straight back to LISTENING — the user is mid-sentence and the next words
    // must land somewhere. No blackout, no intermediate "gated" limbo.
    transition('LISTENING');
    voicedRef.current = 0;
    try { optsRef.current.onBargeIn?.(reason); } catch { /* consumer's problem */ }
  }, [cancelTurn, transition]);

  // ── The monitor frame ─────────────────────────────────────────────────────
  const frame = useCallback(() => {
    const analyser = analyserRef.current;
    const buf = bufRef.current;
    if (!analyser || !buf) return;

    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    levelRef.current += (rms - levelRef.current) * 0.4;

    const cur = stateRef.current;
    const speaking = cur === 'SPEAKING';

    // Learn the ambient floor from quiet frames only, and never while our own
    // audio is in the room (that would train the floor on the echo).
    if (!speaking && rms < noiseFloorRef.current * NOISE_FLOOR_RATIO) {
      noiseFloorRef.current += (rms - noiseFloorRef.current) * NOISE_FLOOR_ALPHA;
    }

    const floorGate = noiseFloorRef.current * NOISE_FLOOR_RATIO;
    let gate = Math.max(speaking ? cfg('speakingGate') : cfg('listenGate'), floorGate);
    if (speaking) {
      // Add a margin for what we are emitting: the louder JARVIS is, the more
      // residual echo AEC leaves behind. Capped, because past a point this stops
      // rejecting echo and starts rejecting the user — and being interruptible
      // matters more than a perfectly clean gate.
      let ref = 0;
      try { ref = optsRef.current.getReferenceLevel?.() ?? 0; } catch { ref = 0; }
      if (ref > 0) gate += Math.min(REF_SUPPRESS_MAX, Math.min(1, ref) * cfg('refSuppress'));
    }

    let voiced = rms >= gate;
    if (voiced && speaking) {
      let echo = false;
      try { echo = optsRef.current.isSelfEcho?.() ?? false; } catch { echo = false; }
      if (echo) voiced = false;
    }

    voicedRef.current = voiced ? voicedRef.current + 1 : 0;

    if (speaking) {
      const needed = Math.max(1, Math.round(cfg('bargeInFrames')));
      const grace = Date.now() - speakStartRef.current < cfg('graceMs');
      // A barge-in must be the calibrated user, never JARVIS's own residual
      // echo — otherwise a loud passage of his own voice cancels the turn and he
      // stops talking mid-sentence. When voice-match is off this returns true,
      // preserving the echo-gate-only behaviour.
      let isUser = true;
      try { isUser = optsRef.current.isUserVoice?.() ?? true; } catch { isUser = true; }
      if (!grace && isUser && voicedRef.current >= needed) {
        bargeIn('voice');
        return;
      }
    } else if (cur === 'LISTENING') {
      const needed = Math.max(1, Math.round(cfg('listenFrames')));
      setUserSpeaking(prev => {
        const now = voicedRef.current >= needed;
        return prev === now ? prev : now;
      });
    }

    // ── Watchdogs: the machine must never park itself out of LISTENING ──────
    if (speaking) {
      const h = handlesRef.current;
      const synth = typeof window !== 'undefined' && 'speechSynthesis' in window
        ? (window.speechSynthesis.speaking || window.speechSynthesis.pending)
        : false;
      const clipLive = !!h.audio && !h.audio.paused && !h.audio.ended;
      const queueLive = audioQueueRef.current.some(el => !el.paused && !el.ended);
      const pendingReq = !!h.ttsAbort && !h.ttsAbort.signal.aborted;
      const settled = Date.now() - speakStartRef.current > 700;
      if (settled && !synth && !clipLive && !queueLive && !pendingReq) {
        // Playback ended without telling us (error, detached element, autoplay
        // block). Recover instead of staying silent and deaf.
        handlesRef.current = {};
        audioQueueRef.current = [];
        transition('LISTENING');
      }
    } else if (cur === 'THINKING') {
      if (thinkStartRef.current && Date.now() - thinkStartRef.current > cfg('thinkingTimeoutMs')) {
        thinkStartRef.current = 0;
        transition('LISTENING');
      }
    }
  }, [bargeIn, cfg, transition]);

  // ── Capture lifecycle ─────────────────────────────────────────────────────
  const start = useCallback(async (): Promise<boolean> => {
    if (typeof window === 'undefined' || typeof navigator === 'undefined') return false;
    // Already live — never re-request the device.
    const live = streamRef.current?.getAudioTracks().some(t => t.readyState === 'live');
    if (live) {
      if (stateRef.current === 'IDLE') transition('LISTENING');
      return true;
    }
    if (startingRef.current) return startingRef.current;

    startingRef.current = (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            // Full duplex without muting starts here: the browser's own echo
            // canceller removes most of our TTS from the capture, so the mic can
            // stay open while JARVIS talks.
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: 1,
          },
        });
        streamRef.current = stream;
        const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
        const ctx = new Ctx();
        ctxRef.current = ctx;
        if (ctx.state === 'suspended') { try { await ctx.resume(); } catch { /* gesture pending */ } }
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.2;
        analyserRef.current = analyser;
        bufRef.current = new Uint8Array(new ArrayBuffer(analyser.fftSize));
        const source = ctx.createMediaStreamSource(stream);
        source.connect(analyser);
        sourceRef.current = source;

        if (timerRef.current) clearInterval(timerRef.current);
        // A timer, not requestAnimationFrame: rAF is throttled to a crawl in a
        // background tab, which would silently disable barge-in there.
        timerRef.current = setInterval(() => frame(), Math.max(10, cfg('frameMs')));
        setCaptureLive(true);
        if (stateRef.current === 'IDLE') transition('LISTENING');
        return true;
      } catch (err) {
        setCaptureLive(false);
        try { optsRef.current.onCaptureError?.(err); } catch { /* consumer's problem */ }
        return false;
      } finally {
        startingRef.current = null;
      }
    })();
    return startingRef.current;
  }, [cfg, frame, transition]);

  const stop = useCallback(() => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    try { sourceRef.current?.disconnect(); } catch { /* already gone */ }
    try { streamRef.current?.getTracks().forEach(t => t.stop()); } catch { /* already gone */ }
    try { ctxRef.current?.close(); } catch { /* already closed */ }
    sourceRef.current = null;
    streamRef.current = null;
    analyserRef.current = null;
    ctxRef.current = null;
    bufRef.current = null;
    levelRef.current = 0;
    voicedRef.current = 0;
    setCaptureLive(false);
    setUserSpeaking(false);
    cancelTurn();
    transition('IDLE');
  }, [cancelTurn, transition]);

  const beginThinking = useCallback((handles: SpeakingHandles = {}) => {
    handlesRef.current = { ...handlesRef.current, ...handles };
    thinkStartRef.current = Date.now();
    transition('THINKING');
  }, [transition]);

  const beginSpeaking = useCallback((handles: SpeakingHandles = {}) => {
    handlesRef.current = { ...handlesRef.current, ...handles };
    if (handles.audio) {
      const q = audioQueueRef.current;
      if (!q.includes(handles.audio)) q.push(handles.audio);
    }
    speakStartRef.current = Date.now();
    thinkStartRef.current = 0;
    voicedRef.current = 0;
    transition('SPEAKING');
  }, [transition]);

  const enqueueAudio = useCallback((el: HTMLAudioElement) => {
    if (!audioQueueRef.current.includes(el)) audioQueueRef.current.push(el);
  }, []);

  const cancelSpeech = useCallback(() => {
    if (stateRef.current !== 'SPEAKING' && stateRef.current !== 'THINKING') return;
    cancelTurn();
    speakStartRef.current = Date.now();  // don't let the watchdog fire mid-handover
  }, [cancelTurn]);

  const endSpeaking = useCallback(() => {
    handlesRef.current = {};
    audioQueueRef.current = [];
    thinkStartRef.current = 0;
    // Requirement: after a reply JARVIS is listening again, with nothing to
    // re-enable. IDLE is only for "capture not running at all".
    transition(streamRef.current ? 'LISTENING' : 'IDLE');
  }, [transition]);

  const getLevel = useCallback(() => levelRef.current, []);

  useEffect(() => () => { stop(); }, [stop]);

  return {
    state,
    stateRef,
    userSpeaking,
    captureLive,
    getLevel,
    start,
    stop,
    beginThinking,
    beginSpeaking,
    enqueueAudio,
    cancelSpeech,
    endSpeaking,
    bargeIn,
  };
}

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
  /** Turn-taking state of the live agent, in the shared vocabulary the robot /
   *  orb visuals react to. Derived from the session — never set by a component. */
  turnState: VoiceTurnState;
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
  /** Called by parent (PaulChat) when JARVIS is speaking externally (TTS) */
  setExternalSpeaking: (speaking: boolean) => void;
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

  // External speaking state (from PaulChat TTS) - pauses Deepgram mic during TTS
  const [externalSpeaking, setExternalSpeaking] = useState(false);
  const externalSpeakingRef = useRef(false)
  useEffect(() => { externalSpeakingRef.current = externalSpeaking }, [externalSpeaking])

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
      // The capture runs continuously for the whole session — it is never stopped
      // or re-requested to suppress feedback. While the *page's* JARVIS (PaulChat
      // TTS) is talking we simply stop forwarding frames to Deepgram, so the
      // agent cannot transcribe another JARVIS's voice, while the microphone
      // itself keeps running and the barge-in monitor keeps seeing every frame.
      const mic    = new AgentMicrophone(
        (data: ArrayBuffer) => {
          if (externalSpeakingRef.current) return;
          try { session.sendAudio(data); } catch {}
        },
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

  // The microphone deliberately has NO reaction to `externalSpeaking` here: it
  // stays open and processing for the whole session. Feeding of frames is gated
  // in the AgentMicrophone callback above instead (see comment there).

  useEffect(() => () => { _cleanup(); }, [_cleanup]);

  // Single derived turn state for the visuals — the agent's own duplex handling
  // (server-side VAD → 'user-started-speaking' → player.interrupt()) is what
  // performs barge-in on this path, so the state only has to report it.
  const turnState: VoiceTurnState =
    state !== 'connected' ? 'IDLE'
    : isSpeaking         ? 'SPEAKING'
    : 'LISTENING';

  return {
    state,
    turnState,
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
    setExternalSpeaking,
  };
}
