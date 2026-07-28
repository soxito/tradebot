/**
 * useVoiceTurn — coordination between the turn machine, the microphone capture
 * and whatever is currently on the speakers.
 *
 * The contract these tests pin down is full duplex:
 *
 *   IDLE ──start()──▶ LISTENING ──beginThinking()──▶ THINKING ──beginSpeaking()──▶ SPEAKING
 *                        ▲                              │                            │
 *                        └──── endSpeaking() ───────────┴──── bargeIn() ◀─────────────┘
 *
 * Two invariants matter more than any individual transition, and both replace
 * behaviour that was deliberately removed:
 *
 *  1. The capture opens once and stays open. Nothing stops the microphone to
 *     suppress feedback — not beginSpeaking(), not external TTS. Self-hearing is
 *     handled by a *raised RMS gate* while SPEAKING (`speakingGate`, surfaced in
 *     the UI as `paul.bargeInGate`), never by muting and never by a post-speech
 *     blackout window. `trackStop` below is the tripwire for that.
 *  2. SPEAKING always terminates: via endSpeaking(), via bargeIn(), or — when
 *     playback dies without firing any event — via the watchdog.
 */
import { renderHook, act } from '@testing-library/react'
import { useVoiceTurn } from '@/hooks/useDeepgramAgent'

// ── Capture doubles ──────────────────────────────────────────────────────────

/** Raw mic level 0–1 the fake analyser reports on the next frame. */
let micRms = 0
let trackStop: ReturnType<typeof vi.fn>
let getUserMedia: ReturnType<typeof vi.fn>

/** An AnalyserNode whose time-domain data is a DC offset of exactly `micRms`. */
const analyser = {
  fftSize: 2048,
  smoothingTimeConstant: 0,
  getByteTimeDomainData(buf: Uint8Array) {
    buf.fill(Math.round(128 + Math.min(1, Math.max(0, micRms)) * 127))
  },
  connect: vi.fn(),
  disconnect: vi.fn(),
}

class MockAudioContext {
  state = 'running'
  destination = {}
  sampleRate = 48000
  createAnalyser = () => analyser
  createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
  resume = vi.fn().mockResolvedValue(undefined)
  close = vi.fn().mockResolvedValue(undefined)
}

type FakeAudio = HTMLAudioElement & { pause: ReturnType<typeof vi.fn> }

/** The parts of an <audio> element the machine touches. `playing: false` models
 *  a clip that never started or died silently — what the watchdog exists for. */
function fakeAudio(playing = true): FakeAudio {
  const el = { paused: !playing, ended: false, volume: 1, currentTime: 0 } as unknown as FakeAudio
  el.pause = vi.fn(() => { (el as unknown as { paused: boolean }).paused = true })
  return el
}

beforeEach(() => {
  micRms = 0
  analyser.fftSize = 2048
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
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

// ─────────────────────────────────────────────────────────────────────────────
// Transitions that need no capture at all: the machine accepts beginSpeaking()
// and bargeIn() whether or not a microphone was ever opened.
// ─────────────────────────────────────────────────────────────────────────────
describe('useVoiceTurn — turn transitions', () => {
  it('walks IDLE → THINKING → SPEAKING and reports every change', () => {
    const onStateChange = vi.fn()
    const { result } = renderHook(() => useVoiceTurn({ onStateChange }))

    expect(result.current.state).toBe('IDLE')

    act(() => { result.current.beginThinking() })
    expect(result.current.state).toBe('THINKING')

    act(() => { result.current.beginSpeaking({ audio: fakeAudio() }) })
    expect(result.current.state).toBe('SPEAKING')

    expect(onStateChange.mock.calls).toEqual([
      ['THINKING', 'IDLE'],
      ['SPEAKING', 'THINKING'],
    ])
  })

  it('bargeIn() during SPEAKING aborts the turn, silences playback and listens', () => {
    const onBargeIn = vi.fn()
    const { result } = renderHook(() => useVoiceTurn({ onBargeIn }))

    const audio = fakeAudio()
    const ttsAbort = new AbortController()
    const streamAbort = new AbortController()

    act(() => { result.current.beginSpeaking({ audio, ttsAbort, streamAbort }) })
    act(() => { result.current.bargeIn('voice') })

    // Everything cancellable that made up the turn is cancelled …
    expect(ttsAbort.signal.aborted).toBe(true)
    expect(streamAbort.signal.aborted).toBe(true)
    expect(audio.pause).toHaveBeenCalled()
    expect(audio.volume).toBe(0)
    // … and the user's next words land somewhere. No blackout, no gated limbo.
    expect(result.current.state).toBe('LISTENING')
    expect(onBargeIn).toHaveBeenCalledWith('voice')
  })

  it('bargeIn() during THINKING kills the in-flight reply before it is spoken', () => {
    const { result } = renderHook(() => useVoiceTurn())
    const streamAbort = new AbortController()

    act(() => { result.current.beginThinking({ streamAbort }) })
    act(() => { result.current.bargeIn('wake-word') })

    expect(streamAbort.signal.aborted).toBe(true)
    expect(result.current.state).toBe('LISTENING')
  })

  it('bargeIn() is a no-op when JARVIS is not holding the floor', () => {
    const onBargeIn = vi.fn()
    const { result } = renderHook(() => useVoiceTurn({ onBargeIn }))

    act(() => { result.current.bargeIn('manual') })

    expect(result.current.state).toBe('IDLE')
    expect(onBargeIn).not.toHaveBeenCalled()
  })

  it('endSpeaking() always leaves SPEAKING, and is safe to call twice', () => {
    const { result } = renderHook(() => useVoiceTurn())

    act(() => { result.current.beginSpeaking({ audio: fakeAudio() }) })
    act(() => { result.current.endSpeaking() })

    // No capture was ever opened here, so LISTENING would be a lie — but the
    // machine must not be parked in SPEAKING either.
    expect(result.current.state).not.toBe('SPEAKING')
    expect(result.current.state).toBe('IDLE')

    act(() => { result.current.endSpeaking() })
    expect(result.current.state).toBe('IDLE')
  })

  it('cancelSpeech() hands one utterance over to the next without leaving SPEAKING', () => {
    const onStateChange = vi.fn()
    const { result } = renderHook(() => useVoiceTurn({ onStateChange }))

    const filler = fakeAudio()          // "One moment, Sir."
    act(() => { result.current.beginSpeaking({ audio: filler }) })
    onStateChange.mockClear()

    const answer = fakeAudio()          // the actual reply
    act(() => { result.current.cancelSpeech() })
    expect(filler.pause).toHaveBeenCalled()
    expect(filler.volume).toBe(0)
    // A handover is not a barge-in: the floor never changes hands.
    expect(result.current.state).toBe('SPEAKING')
    expect(onStateChange).not.toHaveBeenCalled()

    act(() => { result.current.beginSpeaking({ audio: answer }) })
    expect(answer.pause).not.toHaveBeenCalled()
    expect(result.current.state).toBe('SPEAKING')
  })

  it("a superseded utterance's late end must not end the next turn", () => {
    // endSpeaking() is unconditional by design — the machine cannot tell which
    // clip is calling it. Ownership is therefore the caller's job (PaulChat
    // keeps an `activeSpeechRef` token per utterance), and this test pins the
    // pattern the machine requires: only the clip that currently owns the floor
    // may end the turn. Without the guard the filler's late `onended` fires
    // while the real answer is still playing and drops JARVIS out of SPEAKING.
    const { result } = renderHook(() => useVoiceTurn())

    let owner: object | null = null
    const speak = (audio: FakeAudio) => {
      const token = {}
      owner = token
      act(() => {
        result.current.cancelSpeech()
        result.current.beginSpeaking({ audio })
      })
      /** The element's onended handler, ownership guard and all. */
      return () => act(() => { if (owner === token) result.current.endSpeaking() })
    }

    const filler = fakeAudio()
    const answer = fakeAudio()
    const fillerEnded = speak(filler)
    const answerEnded = speak(answer)

    fillerEnded()                       // arrives late, after the handover
    expect(result.current.state).toBe('SPEAKING')
    expect(answer.pause).not.toHaveBeenCalled()

    answerEnded()
    expect(result.current.state).not.toBe('SPEAKING')
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// The capture path: one always-open microphone, energy barge-in, watchdogs.
// ─────────────────────────────────────────────────────────────────────────────
describe('useVoiceTurn — capture and full duplex', () => {
  /** Open the capture and settle the promise start() returns. */
  const startCapture = async (result: { current: ReturnType<typeof useVoiceTurn> }) => {
    await act(async () => { await result.current.start() })
  }

  it('start() opens one echo-cancelled capture and enters LISTENING', async () => {
    const { result } = renderHook(() => useVoiceTurn())

    await startCapture(result)

    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia.mock.calls[0][0].audio).toMatchObject({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    })
    expect(result.current.captureLive).toBe(true)
    expect(result.current.state).toBe('LISTENING')
  })

  it('start() never re-requests a device that is already live', async () => {
    const { result } = renderHook(() => useVoiceTurn())

    await startCapture(result)
    await startCapture(result)

    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('reports a capture failure instead of silently pretending to listen', async () => {
    const onCaptureError = vi.fn()
    const denied = new Error('Permission denied')
    getUserMedia.mockRejectedValue(denied)
    const { result } = renderHook(() => useVoiceTurn({ onCaptureError }))

    let ok: boolean | undefined
    await act(async () => { ok = await result.current.start() })

    expect(ok).toBe(false)
    expect(onCaptureError).toHaveBeenCalledWith(denied)
    expect(result.current.captureLive).toBe(false)
    expect(result.current.state).toBe('IDLE')
  })

  it('keeps the microphone open for the whole turn, including SPEAKING', async () => {
    const { result } = renderHook(() => useVoiceTurn())
    await startCapture(result)

    act(() => { result.current.beginThinking() })
    act(() => { result.current.beginSpeaking({ audio: fakeAudio() }) })
    act(() => { vi.advanceTimersByTime(1000) })

    // The removed behaviour stopped the mic for the duration of TTS and blacked
    // it out afterwards. Neither may happen: the track is never stopped, and the
    // machine is listening again the instant the reply ends.
    expect(trackStop).not.toHaveBeenCalled()
    expect(result.current.captureLive).toBe(true)

    act(() => { result.current.endSpeaking() })
    expect(result.current.state).toBe('LISTENING')
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('barges in on sustained speech during SPEAKING', async () => {
    const onBargeIn = vi.fn()
    const { result } = renderHook(() => useVoiceTurn({
      frameMs: 20,
      graceMs: 0,
      bargeInFrames: 3,
      speakingGate: 0.085,
      onBargeIn,
    }))
    await startCapture(result)

    const audio = fakeAudio()
    const ttsAbort = new AbortController()
    act(() => { result.current.beginSpeaking({ audio, ttsAbort }) })

    micRms = 0.4                                    // the user talks over JARVIS
    act(() => { vi.advanceTimersByTime(20 * 3) })

    expect(onBargeIn).toHaveBeenCalledWith('voice')
    expect(ttsAbort.signal.aborted).toBe(true)
    expect(audio.pause).toHaveBeenCalled()
    expect(result.current.state).toBe('LISTENING')
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('the raised speaking gate — not a muted mic — is what rejects self-hearing', async () => {
    const { result } = renderHook(() => useVoiceTurn({
      frameMs: 20,
      graceMs: 0,
      listenGate: 0.030,
      speakingGate: 0.085,   // `paul.bargeInGate`
      bargeInFrames: 3,
      listenFrames: 2,
    }))
    await startCapture(result)

    // A level of 0.05 RMS clears the listening gate …
    micRms = 0.05
    act(() => { vi.advanceTimersByTime(20 * 4) })
    expect(result.current.state).toBe('LISTENING')
    expect(result.current.userSpeaking).toBe(true)

    // … and the very same level is rejected while JARVIS is talking, because the
    // gate went up rather than the microphone going away.
    act(() => { result.current.beginSpeaking({ audio: fakeAudio() }) })
    act(() => { vi.advanceTimersByTime(20 * 10) })
    expect(result.current.state).toBe('SPEAKING')
    expect(result.current.captureLive).toBe(true)
  })

  it('does not barge in on a frame the caller classifies as our own echo', async () => {
    const onBargeIn = vi.fn()
    const { result } = renderHook(() => useVoiceTurn({
      frameMs: 20,
      graceMs: 0,
      bargeInFrames: 2,
      speakingGate: 0.085,
      isSelfEcho: () => true,
      onBargeIn,
    }))
    await startCapture(result)

    act(() => { result.current.beginSpeaking({ audio: fakeAudio() }) })
    micRms = 0.6                                    // loud, but it is JARVIS
    act(() => { vi.advanceTimersByTime(20 * 10) })

    expect(onBargeIn).not.toHaveBeenCalled()
    expect(result.current.state).toBe('SPEAKING')
  })

  it('the watchdog returns to LISTENING when playback dies silently', async () => {
    const { result } = renderHook(() => useVoiceTurn({ frameMs: 20 }))
    await startCapture(result)

    // A clip that never started (autoplay blocked, element detached): no
    // onended, no onerror, nothing to end the turn.
    act(() => { result.current.beginSpeaking({ audio: fakeAudio(false) }) })
    expect(result.current.state).toBe('SPEAKING')

    act(() => { vi.advanceTimersByTime(1000) })

    expect(result.current.state).toBe('LISTENING')
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('the watchdog leaves genuinely live playback alone', async () => {
    const { result } = renderHook(() => useVoiceTurn({ frameMs: 20 }))
    await startCapture(result)

    act(() => { result.current.beginSpeaking({ audio: fakeAudio(true) }) })
    act(() => { vi.advanceTimersByTime(3000) })

    expect(result.current.state).toBe('SPEAKING')
  })

  it('a stuck THINKING turn times out back to LISTENING', async () => {
    const { result } = renderHook(() => useVoiceTurn({ frameMs: 20, thinkingTimeoutMs: 200 }))
    await startCapture(result)

    act(() => { result.current.beginThinking({ streamAbort: new AbortController() }) })
    act(() => { vi.advanceTimersByTime(150) })
    expect(result.current.state).toBe('THINKING')

    act(() => { vi.advanceTimersByTime(100) })
    expect(result.current.state).toBe('LISTENING')
  })

  it('stop() is the only thing that closes the device', async () => {
    const { result } = renderHook(() => useVoiceTurn())
    await startCapture(result)

    act(() => { result.current.stop() })

    expect(trackStop).toHaveBeenCalled()
    expect(result.current.captureLive).toBe(false)
    expect(result.current.state).toBe('IDLE')
  })

  it('releases the device on unmount', async () => {
    const { result, unmount } = renderHook(() => useVoiceTurn())
    await startCapture(result)

    unmount()

    expect(trackStop).toHaveBeenCalled()
  })
})
