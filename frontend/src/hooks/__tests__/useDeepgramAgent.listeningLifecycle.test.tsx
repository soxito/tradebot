/**
 * The listening lifecycle — regression lock for "the mic is stuck on Listening…".
 *
 * This bug has been fixed and has come back repeatedly, because every previous
 * fix treated a symptom (a silence timeout, a liveness watchdog, a tighter audio
 * gate) while the state machine underneath kept two writers for one flag and no
 * disposal guard on any recogniser. These tests pin the two invariants that make
 * that impossible, and they are written to FAIL on the old code:
 *
 *   INVARIANT 1 — `listening` means "a user utterance is being captured RIGHT
 *   NOW", and nothing else. An armed recogniser waiting in silence is the
 *   resting state, not listening. The extension's continuously-armed recogniser
 *   (`status { listening: true }`) must never put this page into listening; only
 *   its separate, honest `capturing` flag may.
 *
 *   INVARIANT 2 — tearing voice down makes it stay down. `rec.stop()` FIRES
 *   `onend`, and `onend` is where every auto-restart lives, so a teardown that
 *   merely stops a recogniser *creates* its replacement. Handlers are detached
 *   before stopping, and every restart path checks a disposed flag.
 *
 * The observable contract is the `jarvis-activity` broadcast — the same message
 * the JARVIS Room orb renders from — plus the recognisers and mic tracks that
 * are (or are not) still alive.
 */
import { render, waitFor, act } from '@testing-library/react'
import PaulChat from '@/components/PaulChat'

// The 3D avatar is stubbed: the shared test setup mocks `three` with an
// incomplete module (no Vector3), so pulling in the real scene graph explodes on
// import. What is under test here is the *state PaulChat hands the robot*, not
// the scene — so the stub simply reflects that state back into the DOM.
vi.mock('@/components/JarvisRobot', () => ({
  default: ({ state }: { state: string }) =>
    <div data-testid="robot-3d" data-robot-state={state} />,
}))

// ── Doubles ──────────────────────────────────────────────────────────────────

let trackStop: ReturnType<typeof vi.fn>
let getUserMedia: ReturnType<typeof vi.fn>
let messages: Array<Record<string, unknown>>

/** Silent mic — a DC-centred buffer is 0 RMS, so nothing ever barges in. */
const analyser = {
  fftSize: 2048,
  smoothingTimeConstant: 0,
  frequencyBinCount: 1024,
  getByteTimeDomainData: (buf: Uint8Array) => buf.fill(128),
  getByteFrequencyData: (buf: Uint8Array) => buf.fill(0),
  connect: vi.fn(),
  disconnect: vi.fn(),
}

class MockAudioContext {
  state = 'running'
  sampleRate = 48000
  currentTime = 0
  destination = { connect: vi.fn(), disconnect: vi.fn() }
  createAnalyser = () => analyser
  createGain = () => ({ gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() })
  createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
  createMediaElementSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) }
  resume = vi.fn().mockResolvedValue(undefined)
  suspend = vi.fn().mockResolvedValue(undefined)
  close = vi.fn().mockResolvedValue(undefined)
}

class MockUtterance {
  text: string
  lang = ''
  rate = 1
  pitch = 1
  volume = 1
  voice: SpeechSynthesisVoice | null = null
  onend: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(text: string) { this.text = text }
}

let utterances: MockUtterance[]
const speechSynthesis = {
  speaking: false,
  pending: false,
  paused: false,
  getVoices: () => [] as SpeechSynthesisVoice[],
  speak: vi.fn((u: MockUtterance) => { speechSynthesis.speaking = true; utterances.push(u) }),
  cancel: vi.fn(() => { speechSynthesis.speaking = false }),
  pause: vi.fn(),
  resume: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
}

/**
 * A Web Speech recogniser that behaves like the real one in the way that
 * matters: `stop()` fires `onend`. That single behaviour is what turned every
 * previous teardown into a restart, so the double has to reproduce it or the
 * tests prove nothing.
 */
class MockRecognition {
  static instances: MockRecognition[] = []
  lang = ''
  continuous = false
  interimResults = false
  maxAlternatives = 1
  running = false
  aborted = false
  onresult: ((e: unknown) => void) | null = null
  onend: (() => void) | null = null
  onerror: ((e: unknown) => void) | null = null
  onstart: (() => void) | null = null

  constructor() { MockRecognition.instances.push(this) }

  start() {
    if (this.running) throw new Error('InvalidStateError')
    this.running = true
    this.onstart?.()
  }
  stop() {
    if (!this.running) return
    this.running = false
    this.onend?.()          // the real API does exactly this
  }
  abort() {
    this.aborted = true
    if (!this.running) return
    this.running = false
    this.onend?.()
  }
  /** Deliver a final transcript, as Chrome would. */
  emit(transcript: string, confidence = 0.9) {
    this.onresult?.({
      resultIndex: 0,
      results: [Object.assign([{ transcript, confidence }], { isFinal: true, length: 1 })],
    })
  }
}

/** Recognisers that are still running — i.e. still holding the microphone. */
const liveRecognizers = () => MockRecognition.instances.filter(r => r.running)

/** The last `listening` value broadcast on `jarvis-activity`. */
const lastBroadcastListening = (): boolean | undefined => {
  const activity = messages.filter(m => m.type === 'jarvis-activity')
  return activity.length ? (activity[activity.length - 1].listening as boolean) : undefined
}

/**
 * Pretend to be the extension's content script talking to the page. Dispatched
 * rather than posted because PaulChat requires `event.source === window` and
 * jsdom's postMessage leaves `source` null.
 */
function fromExtension(payload: Record<string, unknown>) {
  window.dispatchEvent(new MessageEvent('message', {
    data: { __jarvisExt: true, ...payload },
    source: window as unknown as MessageEventSource,
    origin: window.location.origin,
  }))
}

let collect: (e: MessageEvent) => void

beforeEach(() => {
  messages = []
  utterances = []
  MockRecognition.instances = []
  speechSynthesis.speaking = false
  speechSynthesis.pending = false

  trackStop = vi.fn()
  const track = { kind: 'audio', readyState: 'live', stop: trackStop }
  getUserMedia = vi.fn().mockResolvedValue({
    getAudioTracks: () => [track],
    getTracks: () => [track],
  })
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true, writable: true, value: { getUserMedia },
  })

  vi.stubGlobal('AudioContext', MockAudioContext)
  vi.stubGlobal('SpeechSynthesisUtterance', MockUtterance)
  // The shared setup file installs window.SpeechRecognition as a non-configurable
  // vi.fn(), so it can be neither stubbed nor redefined — only re-implemented.
  // `new SR()` returns the object the implementation returns, which is our
  // instrumented recogniser.
  ;(window.SpeechRecognition as unknown as ReturnType<typeof vi.fn>)
    .mockImplementation(() => new MockRecognition())
  Object.defineProperty(window, 'speechSynthesis', {
    configurable: true, writable: true, value: speechSynthesis,
  })

  // Component defaults: wake word on, AI voice off (deterministic system voice).
  vi.mocked(window.localStorage.getItem).mockReturnValue(null)

  collect = (e: MessageEvent) => {
    const d = e.data as Record<string, unknown> | null
    if (d && d.__jarvisPage === true) messages.push(d)
  }
  window.addEventListener('message', collect)
})

afterEach(() => {
  window.removeEventListener('message', collect)
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('listening lifecycle — the stuck-microphone regression lock', () => {
  // ── INVARIANT 1: one flag, one meaning, one writer ────────────────────────

  it('does not enter listening because the extension is merely ARMED', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())
    messages = []

    // This is the extension's resting state: its continuous recogniser is armed
    // and has been for as long as voice was enabled. Nobody is speaking.
    act(() => {
      fromExtension({ type: 'status', listening: true, voiceReady: true, enabled: true })
    })

    await waitFor(() => expect(messages.length).toBeGreaterThan(0))
    // The old code did `setListening(!!d.listening)` here and latched the page on
    // "Listening…" forever — which also disabled every in-page recogniser re-arm,
    // because `listeningRef` guards all of them.
    expect(lastBroadcastListening()).not.toBe(true)
  })

  it('enters listening only for the extension\'s explicit capturing flag, and leaves on silence', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())

    act(() => {
      fromExtension({ type: 'status', listening: true, capturing: true, voiceReady: true, enabled: true })
    })
    await waitFor(() => expect(lastBroadcastListening()).toBe(true))

    // The user said nothing; the extension's capture window closed. Silence must
    // put the state back to idle rather than leaving it pinned.
    act(() => {
      fromExtension({ type: 'status', listening: true, capturing: false, voiceReady: true, enabled: true })
    })
    await waitFor(() => expect(lastBroadcastListening()).toBe(false))
  })

  it('leaves listening when a dictation utterance ends in silence', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(MockRecognition.instances.length).toBeGreaterThan(0))

    // Wake JARVIS, which opens dictation.
    const wake = MockRecognition.instances[0]
    act(() => { wake.emit('jarvis') })
    await waitFor(() => expect(MockRecognition.instances.length).toBeGreaterThan(1))

    const dictation = MockRecognition.instances[MockRecognition.instances.length - 1]
    await waitFor(() => expect(lastBroadcastListening()).toBe(true))

    // Nothing was said. The recogniser gives up on its own.
    act(() => { dictation.stop() })

    // Must be released synchronously in onend — not after a dispatch that may
    // never resolve, and not only when some later watchdog happens to run.
    await waitFor(() => expect(lastBroadcastListening()).toBe(false))
  })

  // ── TTS ───────────────────────────────────────────────────────────────────

  it('does not re-trigger listening while JARVIS is speaking', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())

    // Put the page into a genuine capture first, so this proves TTS *clears* it.
    const wake = MockRecognition.instances[0]
    act(() => { wake.emit('jarvis') })
    await waitFor(() => expect(lastBroadcastListening()).toBe(true))

    act(() => {
      window.postMessage(
        { __jarvisPage: true, type: 'jarvis-speak', text: 'The EURUSD setup is invalidated, Sir.' },
        '*',
      )
    })
    await waitFor(() => expect(speechSynthesis.speak).toHaveBeenCalled())

    // JARVIS holding the floor is not the user being captured. The mic itself
    // stays open (full duplex) — but the *listening* state must not claim a user
    // utterance that isn't happening.
    await waitFor(() => expect(lastBroadcastListening()).toBe(false))
    expect(trackStop).not.toHaveBeenCalled()
  })

  // ── INVARIANT 2: teardown stays torn down ─────────────────────────────────

  it('stops every microphone track on unmount and never re-opens one', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { unmount } = render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())
    await waitFor(() => expect(MockRecognition.instances.length).toBeGreaterThan(0))

    const before = MockRecognition.instances.length
    unmount()

    expect(trackStop).toHaveBeenCalled()
    expect(liveRecognizers()).toHaveLength(0)

    // The teardown's own stop() fires onend, and onend used to schedule a fresh
    // recogniser 400–600ms later — one nothing held a reference to and nothing
    // could ever stop. Walk past every one of those timers.
    await act(async () => { vi.advanceTimersByTime(5000) })

    expect(MockRecognition.instances.length).toBe(before)
    expect(liveRecognizers()).toHaveLength(0)
  })

  it('returns to idle when the tab is hidden, and re-arms when it comes back', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(MockRecognition.instances.length).toBeGreaterThan(0))
    messages = []

    const hide = (state: 'hidden' | 'visible') => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
      document.dispatchEvent(new Event('visibilitychange'))
    }

    act(() => { hide('hidden') })

    await waitFor(() => expect(lastBroadcastListening()).toBe(false))
    expect(liveRecognizers()).toHaveLength(0)
    expect(trackStop).toHaveBeenCalled()

    // A pause, not a death sentence: coming back must arm the mic again.
    await act(async () => { hide('visible') })
    await waitFor(() => expect(liveRecognizers().length).toBeGreaterThan(0))
  })

  it('keeps exactly one recogniser across a double mount (StrictMode)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    // Two mounts of the same component, the way StrictMode double-invokes them.
    const first = render(<PaulChat />)
    await waitFor(() => expect(MockRecognition.instances.length).toBeGreaterThan(0))
    first.unmount()

    const second = render(<PaulChat />)
    await waitFor(() => expect(liveRecognizers().length).toBeGreaterThan(0))

    // Let every deferred restart the first mount could have queued land.
    await act(async () => { vi.advanceTimersByTime(5000) })

    // One microphone, one recogniser. Two would fight for the device, and only
    // the newest is reachable through the ref — the older one keeps the mic hot
    // forever and reports "listening" that nothing can clear.
    expect(liveRecognizers()).toHaveLength(1)

    second.unmount()
    await act(async () => { vi.advanceTimersByTime(5000) })
    expect(liveRecognizers()).toHaveLength(0)
  })
})

/**
 * The robot avatar is a second consumer of the same armed-vs-capturing
 * distinction, and it regressed independently of the mic indicator.
 *
 * `turn.state === 'LISTENING'` is the machine's ARMED state — true for as long
 * as the capture is open, which with the wake word on is permanently. Mapping
 * that to the robot's `listening` animation pinned the robot: 'listening' is one
 * of the avatar's STOP_STATES, so the robot stopped walking, never returned to
 * idle, and wore a "Listening…" label in a silent room.
 */
describe('robot avatar state — armed is not listening', () => {
  /** Render with the robot force-enabled (it is otherwise gated by device tier). */
  const renderWithRobot = () => {
    vi.mocked(window.localStorage.getItem).mockImplementation(
      (k: string) => (k === 'paul.forceRobot' ? '1' : null),
    )
    return render(<PaulChat />)
  }

  const lastActivity = () => {
    const a = messages.filter(m => m.type === 'jarvis-activity')
    return a.length ? a[a.length - 1] : null
  }

  it('stays idle while the mic is armed but nobody is speaking', async () => {
    const { queryByTestId, queryByText } = renderWithRobot()

    // The capture opens on mount (wake word is on by default), which is exactly
    // the condition that used to pin the robot.
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())
    await waitFor(() => expect(queryByTestId('robot-3d')).not.toBeNull())
    await waitFor(() => expect(lastActivity()).not.toBeNull())

    // Precondition — without this the test could pass simply because the
    // capture never opened, which would prove nothing.
    await waitFor(() => expect(lastActivity()?.armed).toBe(true))

    // The machine is armed, and nothing is being captured. Both consumers must
    // report idle: the broadcast the JARVIS Room renders, and the robot itself.
    expect(lastActivity()?.listening).toBe(false)
    expect(queryByText('Listening…')).toBeNull()

    // 'walking' is what the avatar maps idle onto; 'listening' is the frozen
    // state the bug produced.
    expect(queryByTestId('robot-3d')?.getAttribute('data-robot-state')).not.toBe('listening')
    expect(queryByTestId('robot-3d')?.getAttribute('data-robot-state')).toBe('walking')
  })

  it('shows listening only while an utterance is actually being captured', async () => {
    const { queryByTestId, queryByText } = renderWithRobot()
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())
    await waitFor(() => expect(queryByTestId('robot-3d')).not.toBeNull())

    // The extension reports a real capture in progress.
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { __jarvisExt: true, type: 'status', listening: true, capturing: true, voiceReady: true, enabled: true },
        source: window as unknown as MessageEventSource,
        origin: window.location.origin,
      }))
    })

    await waitFor(() => expect(queryByTestId('robot-3d')?.getAttribute('data-robot-state')).toBe('listening'))
    expect(queryByText('Listening…')).not.toBeNull()
    expect(lastActivity()?.listening).toBe(true)
  })
})
