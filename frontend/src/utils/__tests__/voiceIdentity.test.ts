import { describe, it, expect } from 'vitest'
import {
  adaptVoiceProfile, bandCorrelation, estimateF0, pitchScore,
  voiceSimilarity, type VoiceProfile,
} from '@/utils/voiceIdentity'

const SAMPLE_RATE = 48000
const FRAME = 2048

/**
 * Build a fake AnalyserNode that plays back a synthetic waveform as the byte
 * time-domain data estimateF0 reads. Only getByteTimeDomainData is exercised.
 */
function analyserForTone(hz: number, { harmonics = 3, noise = 0 } = {}) {
  const frame = new Uint8Array(FRAME)
  for (let i = 0; i < FRAME; i++) {
    let v = 0
    // A voice is not a pure sine — summing harmonics makes the test signal
    // behave like real speech, where autocorrelation can lock onto an overtone.
    for (let h = 1; h <= harmonics; h++) {
      v += Math.sin((2 * Math.PI * hz * h * i) / SAMPLE_RATE) / h
    }
    if (noise) v += (Math.random() * 2 - 1) * noise
    frame[i] = Math.max(0, Math.min(255, Math.round(128 + v * 60)))
  }
  return {
    fftSize: FRAME,
    getByteTimeDomainData: (buf: Uint8Array) => buf.set(frame),
  } as unknown as AnalyserNode
}

function silentAnalyser() {
  return {
    fftSize: FRAME,
    getByteTimeDomainData: (buf: Uint8Array) => buf.fill(128),
  } as unknown as AnalyserNode
}

describe('estimateF0', () => {
  it.each([85, 110, 145, 200, 260, 320])('recovers a %i Hz fundamental', (hz) => {
    const f0 = estimateF0(analyserForTone(hz), new Uint8Array(FRAME), SAMPLE_RATE)
    // Within 5% — the lag search is integer-resolution, so exact equality is
    // not achievable at higher pitches.
    expect(Math.abs(f0 - hz) / hz).toBeLessThan(0.05)
  })

  it('survives moderate additive noise', () => {
    const f0 = estimateF0(analyserForTone(120, { noise: 0.25 }), new Uint8Array(FRAME), SAMPLE_RATE)
    expect(Math.abs(f0 - 120) / 120).toBeLessThan(0.08)
  })

  it('reports 0 for silence rather than a spurious pitch', () => {
    expect(estimateF0(silentAnalyser(), new Uint8Array(FRAME), SAMPLE_RATE)).toBe(0)
  })

  it('reports 0 for unvoiced noise', () => {
    const frame = new Uint8Array(FRAME)
    for (let i = 0; i < FRAME; i++) frame[i] = Math.round(128 + (Math.random() * 2 - 1) * 60)
    const analyser = {
      fftSize: FRAME,
      getByteTimeDomainData: (b: Uint8Array) => b.set(frame),
    } as unknown as AnalyserNode
    expect(estimateF0(analyser, new Uint8Array(FRAME), SAMPLE_RATE)).toBe(0)
  })
})

describe('pitchScore', () => {
  const profile = { f0: 120, f0StdDev: 12 } as VoiceProfile

  it('scores an on-pitch frame near 1', () => {
    expect(pitchScore(120, profile)).toBeCloseTo(1, 5)
  })

  it('scores a clearly different speaker near 0', () => {
    // 210 Hz against a 120 Hz profile — a different person, not a bad frame.
    expect(pitchScore(210, profile)).toBe(0)
  })

  it('stays neutral when either side has no pitch', () => {
    expect(pitchScore(0, profile)).toBe(0.5)
    expect(pitchScore(120, {} as VoiceProfile)).toBe(0.5)
  })
})

describe('bandCorrelation', () => {
  const speech = [0.2, 0.9, 1, 0.7, 0.4, 0.3, 0.2, 0.15, 0.1, 0.08, 0.05, 0.03]

  it('is ~1 for a scaled copy — the room attenuates echo but keeps its shape', () => {
    const attenuated = speech.map(v => v * 0.35)
    expect(bandCorrelation(speech, attenuated)).toBeGreaterThan(0.99)
  })

  it('stays high for a mildly coloured copy (real echo path)', () => {
    const coloured = speech.map((v, i) => v * (0.4 + i * 0.01))
    expect(bandCorrelation(speech, coloured)).toBeGreaterThan(0.9)
  })

  it('is low for an unrelated spectrum', () => {
    const other = [0.05, 0.08, 0.1, 0.2, 0.5, 1, 0.9, 0.6, 0.3, 0.2, 0.1, 0.05]
    expect(bandCorrelation(speech, other)).toBeLessThan(0.5)
  })

  it('returns 0 for a flat vector instead of NaN', () => {
    expect(bandCorrelation(speech, Array(12).fill(0.5))).toBe(0)
  })
})

describe('voiceSimilarity', () => {
  const profile: VoiceProfile = {
    bands: [0.2, 0.9, 1, 0.7, 0.4, 0.3, 0.2, 0.15, 0.1, 0.08, 0.05, 0.03],
    bandStdDev: Array(12).fill(0.08),
    centroid: 0.3,
    minEnergy: 10,
    calibratedAt: 0,
  }

  it('scores the enrolled voice highest', () => {
    expect(voiceSimilarity(profile.bands, profile)).toBeCloseTo(1, 5)
  })

  it('ranks the enrolled voice above a different spectrum', () => {
    const stranger = [0.05, 0.08, 0.1, 0.2, 0.5, 1, 0.9, 0.6, 0.3, 0.2, 0.1, 0.05]
    expect(voiceSimilarity(profile.bands, profile)).toBeGreaterThan(voiceSimilarity(stranger, profile))
  })
})

describe('adaptVoiceProfile', () => {
  const base: VoiceProfile = {
    bands: Array(12).fill(0.5),
    bandStdDev: Array(12).fill(0.1),
    centroid: 0.3,
    minEnergy: 10,
    calibratedAt: 0,
    f0: 120,
    f0StdDev: 12,
  }

  it('moves toward a new frame without jumping to it', () => {
    const next = adaptVoiceProfile(base, Array(12).fill(1), 0.05)
    expect(next.bands[0]).toBeGreaterThan(0.5)
    expect(next.bands[0]).toBeLessThan(0.6)
  })

  it('ignores unvoiced frames when tracking pitch', () => {
    // f0=0 means "not measurable", not "0 Hz" — it must not drag the profile down.
    expect(adaptVoiceProfile(base, base.bands, 0.05, 0).f0).toBe(120)
  })

  it('tracks a voiced frame toward the observed pitch', () => {
    const next = adaptVoiceProfile(base, base.bands, 0.1, 140)
    expect(next.f0!).toBeGreaterThan(120)
    expect(next.f0!).toBeLessThan(140)
  })

  it('adopts a pitch when the profile had none', () => {
    const { f0, f0StdDev, ...noPitch } = base
    expect(adaptVoiceProfile(noPitch as VoiceProfile, base.bands, 0.05, 130).f0).toBe(130)
  })
})
