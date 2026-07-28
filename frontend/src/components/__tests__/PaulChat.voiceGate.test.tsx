/**
 * PaulChat voice gating — the full-duplex contract.
 *
 * JARVIS used to mute the microphone for the duration of a reply and then hold
 * it shut for a configurable blackout (`paul.postSpeechGateMs`) so it would not
 * hear itself. That made it deaf exactly when the user wanted to interrupt, and
 * it is gone. What replaced it:
 *
 *   • the capture opens once and stays open through SPEAKING — the user can cut
 *     in at any moment,
 *   • self-hearing is rejected by a *raised RMS gate* while JARVIS talks
 *     (`paul.bargeInGate`, the `speakingGate` handed to useVoiceTurn), plus the
 *     browser's AEC and the echo classifier,
 *   • the reply ends straight back into LISTENING, with no blackout in between.
 *
 * These tests drive the component through the same bridge any other surface
 * uses — a `jarvis-speak` postMessage — and assert on what it broadcasts and on
 * whether it ever touches the microphone track.
 */
import { render, waitFor } from '@testing-library/react'
import PaulChat from '@/components/PaulChat'

// ── Doubles ──────────────────────────────────────────────────────────────────

let trackStop: ReturnType<typeof vi.fn>
let getUserMedia: ReturnType<typeof vi.fn>
let utterances: MockUtterance[]
let messages: Array<Record<string, unknown>>

/** Silent mic: a DC-centred buffer is 0 RMS, so nothing ever barges in here. */
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

/** Enough of window.speechSynthesis to be honest about `speaking`: the turn
 *  machine's watchdog reads it to decide whether a reply is still in progress. */
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

/** Ask JARVIS to say something through the universal speak bridge. */
function requestSpeech(text: string) {
  window.postMessage({ __jarvisPage: true, type: 'jarvis-speak', text }, '*')
}

/** The utterance currently on the speakers finishes normally. */
function finishSpeech(u: MockUtterance) {
  speechSynthesis.speaking = false
  u.onend?.()
}

const stateMessages = () =>
  messages.filter(m => m.type === 'jarvis-voice-state').map(m => m.state)

const speakStatuses = () => messages.filter(m => m.type === 'speak-status')

let collect: (e: MessageEvent) => void

beforeEach(() => {
  utterances = []
  messages = []
  speechSynthesis.speaking = false
  speechSynthesis.pending = false

  trackStop = vi.fn()
  const track = { kind: 'audio', readyState: 'live', stop: trackStop }
  getUserMedia = vi.fn().mockResolvedValue({
    getAudioTracks: () => [track],
    getTracks: () => [track],
  })
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: { getUserMedia },
  })

  vi.stubGlobal('AudioContext', MockAudioContext)
  vi.stubGlobal('SpeechSynthesisUtterance', MockUtterance)
  Object.defineProperty(window, 'speechSynthesis', {
    configurable: true,
    writable: true,
    value: speechSynthesis,
  })

  // The setup file's localStorage is a bare spy; default every key to "unset"
  // so the component takes its documented defaults (wake word on, AI voice off,
  // which routes speak() down the deterministic system-voice path).
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
  delete (window as { __JARVIS_MIC_GATED__?: unknown }).__JARVIS_MIC_GATED__
})

describe('PaulChat — full-duplex voice gating', () => {
  it('reads the barge-in RMS threshold, not the removed blackout duration', () => {
    render(<PaulChat />)

    const keys = vi.mocked(window.localStorage.getItem).mock.calls.map(c => c[0])
    expect(keys).toContain('paul.bargeInGate')
    // The post-speech mic blackout was removed with full-duplex barge-in.
    expect(keys).not.toContain('paul.postSpeechGateMs')
  })

  it('opens the always-open capture on mount', async () => {
    render(<PaulChat />)

    // Wake word is on by default, so the barge-in capture is wanted immediately.
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())
    expect(getUserMedia.mock.calls[0][0].audio).toMatchObject({ echoCancellation: true })
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('keeps the microphone open while speaking and advertises barge-in', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())

    requestSpeech('All systems nominal, Sir.')

    await waitFor(() => expect(speechSynthesis.speak).toHaveBeenCalled())
    expect(stateMessages()).toContain('SPEAKING')

    // The extension and every other surface are told to keep listening.
    const started = speakStatuses().filter(m => m.speaking === true)
    expect(started.length).toBeGreaterThan(0)
    expect(started.every(m => m.allowBargeIn === true)).toBe(true)

    // The heart of it: nothing muted or released the mic to avoid self-hearing.
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('returns to LISTENING the moment the reply ends — no blackout window', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())

    requestSpeech('Position closed at a profit.')
    await waitFor(() => expect(utterances).toHaveLength(1))
    await waitFor(() => expect(stateMessages()).toContain('SPEAKING'))

    messages = []
    finishSpeech(utterances[0])

    // No timers advanced, nothing re-enabled: LISTENING is immediate.
    await waitFor(() => expect(stateMessages()).toContain('LISTENING'))
    expect(stateMessages()).not.toContain('IDLE')
    expect(speakStatuses().some(m => m.speaking === false)).toBe(true)
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('never gates the microphone through a global flag', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())

    requestSpeech('Rendering the chart now.')
    await waitFor(() => expect(utterances).toHaveLength(1))

    // `window.__JARVIS_MIC_GATED__` was the old blackout's signal. Turn state is
    // broadcast as `jarvis-voice-state` instead, and the mic is never gated.
    expect((window as { __JARVIS_MIC_GATED__?: unknown }).__JARVIS_MIC_GATED__).toBeUndefined()

    finishSpeech(utterances[0])
    await waitFor(() => expect(stateMessages()).toContain('LISTENING'))
    expect((window as { __JARVIS_MIC_GATED__?: unknown }).__JARVIS_MIC_GATED__).toBeUndefined()
  })

  it('supersedes an in-flight utterance instead of overlapping it', async () => {
    render(<PaulChat />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled())

    requestSpeech('One moment, Sir.')
    await waitFor(() => expect(utterances).toHaveLength(1))
    await waitFor(() => expect(stateMessages()).toContain('SPEAKING'))

    messages = []
    requestSpeech('The EURUSD setup is invalidated.')
    await waitFor(() => expect(utterances).toHaveLength(2))

    // The first utterance was cancelled, and the handover kept the floor: the
    // machine stays in SPEAKING rather than bouncing through LISTENING, so it
    // has no state change to broadcast at all.
    expect(speechSynthesis.cancel).toHaveBeenCalled()
    expect(stateMessages()).toEqual([])

    // A superseded utterance's late onend must not end the turn that replaced it.
    utterances[0].onend?.()
    expect(stateMessages()).not.toContain('LISTENING')

    finishSpeech(utterances[1])
    await waitFor(() => expect(stateMessages()).toContain('LISTENING'))
  })
})
