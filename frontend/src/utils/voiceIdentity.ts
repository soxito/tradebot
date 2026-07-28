/**
 * voiceIdentity — speaker identification and self-voice (echo) rejection.
 *
 * Pure audio-feature maths extracted from PaulChat so it can be tested without
 * a browser. Two jobs:
 *
 *  • Decide whether the audio in the microphone is the calibrated user, so a
 *    television or a second person in the room cannot issue commands.
 *  • Decide whether it is JARVIS's own reply coming back through the speakers,
 *    so the assistant never answers itself.
 *
 * Everything here operates on the byte arrays an AnalyserNode already produces;
 * no extra audio graph is required.
 */

// ── Voice profile / speaker identification ────────────────────────────────────
// Uses Web Audio API to build a frequency-band fingerprint of the user's voice.
// Only speech matching this fingerprint is accepted, cancelling out TV, AC, etc.

export const VOICE_PROFILE_KEY = 'paul.voiceProfile.v2'
export const AUDIO_BANDS = 12  // frequency band buckets for the fingerprint

/**
 * FFT size for every analyser that feeds speaker ID.
 *
 * Pitch detection sets the floor here, not the spectrum: YIN can only measure
 * periods that fit in half the time-domain buffer, so at a 48 kHz sample rate a
 * 512-sample analyser bottoms out around 190 Hz — above most adult male voices,
 * which would silently report "no pitch" for the very speakers pitch is meant
 * to identify. 2048 samples reach below 70 Hz.
 *
 * Band extraction is unaffected: bands are proportional slices of the spectrum,
 * so a profile keeps the same shape at any FFT size and stays comparable to one
 * captured before this changed.
 */
export const VOICE_FFT_SIZE = 2048

export interface VoiceProfile {
  bands: number[]          // average energy per band (normalised 0–1)
  bandStdDev?: number[]    // std deviation per band — natural voice variation
  centroid: number         // spectral centroid (0–1)
  minEnergy: number        // minimum energy threshold
  calibratedAt: number
  /**
   * Median fundamental frequency (Hz) of the calibrated voice, and the spread
   * around it. Pitch is the single most discriminative speaker feature — two
   * people with similar spectral shape almost always differ here, and a
   * synthesised voice sits at a near-constant pitch that no human sustains.
   * Optional so profiles calibrated before this existed keep working.
   */
  f0?: number
  f0StdDev?: number
}

// Human speech fundamentals span roughly 70 Hz (deep male) to 350 Hz (high
// female / child). Searching outside this range invites octave errors.
const F0_MIN_HZ = 70
const F0_MAX_HZ = 350
// YIN accepts the first dip below this value as the period. Above it the frame
// is treated as unvoiced (a consonant, or noise) and reports no pitch.
const F0_YIN_THRESHOLD = 0.15
// Frames quieter than this RMS carry no measurable pitch.
const F0_MIN_RMS = 0.01

/**
 * Estimate the fundamental frequency of a time-domain frame, in Hz.
 *
 * Uses YIN's cumulative mean normalised difference function rather than plain
 * autocorrelation. Raw autocorrelation peaks just as strongly at twice and
 * three times the true period, so it routinely reports a voice an octave (or a
 * twelfth) below where it actually sits — the estimate for a 260 Hz speaker
 * lands near 87 Hz. CMNDF suppresses those subharmonics, and taking the *first*
 * qualifying dip rather than the global minimum removes the rest.
 *
 * Returns 0 when the frame is unvoiced or too quiet, so callers can tell "no
 * pitch" from "low pitch". Operates on the byte time-domain data an
 * AnalyserNode already produces, so no extra audio graph is required.
 */
export function estimateF0(analyser: AnalyserNode, timeBuf: Uint8Array<ArrayBuffer>, sampleRate: number): number {
  analyser.getByteTimeDomainData(timeBuf)
  const n = timeBuf.length
  // Centre on zero and measure signal strength in one pass.
  const sig = new Float32Array(n)
  let rms = 0
  for (let i = 0; i < n; i++) {
    const v = (timeBuf[i] - 128) / 128
    sig[i] = v
    rms += v * v
  }
  rms = Math.sqrt(rms / n)
  if (rms < F0_MIN_RMS) return 0   // silence — nothing to measure

  const minLag = Math.max(2, Math.floor(sampleRate / F0_MAX_HZ))
  const maxLag = Math.min(Math.floor(sampleRate / F0_MIN_HZ), (n >> 1) - 1)
  if (maxLag <= minLag) return 0

  // Difference function: how unlike itself the frame is when shifted by `lag`.
  // A true period produces a deep trough.
  const half = n >> 1
  const diff = new Float32Array(maxLag + 1)
  for (let lag = 1; lag <= maxLag; lag++) {
    let sum = 0
    for (let i = 0; i < half; i++) {
      const d = sig[i] - sig[i + lag]
      sum += d * d
    }
    diff[lag] = sum
  }

  // Cumulative mean normalisation — divides each trough by the average trough
  // depth so far, which is what flattens the subharmonic copies.
  const cmnd = new Float32Array(maxLag + 1)
  cmnd[0] = 1
  let running = 0
  for (let lag = 1; lag <= maxLag; lag++) {
    running += diff[lag]
    cmnd[lag] = running === 0 ? 1 : (diff[lag] * lag) / running
  }

  // First dip below the threshold, descended to its local minimum. Searching
  // forward (not for the global minimum) is what keeps the estimate on the
  // fundamental instead of an octave below it.
  let bestLag = -1
  for (let lag = minLag; lag <= maxLag; lag++) {
    if (cmnd[lag] < F0_YIN_THRESHOLD) {
      while (lag + 1 <= maxLag && cmnd[lag + 1] < cmnd[lag]) lag++
      bestLag = lag
      break
    }
  }
  if (bestLag < 0) return 0   // unvoiced — no period clear enough to trust

  // Parabolic interpolation around the trough: the lag search is
  // integer-resolution, which at 300 Hz is a ~2 % quantisation error on its own.
  let period = bestLag
  if (bestLag > 1 && bestLag < maxLag) {
    const a = cmnd[bestLag - 1], b = cmnd[bestLag], c = cmnd[bestLag + 1]
    const denom = 2 * (2 * b - a - c)
    if (Math.abs(denom) > 1e-9) period = bestLag + (c - a) / denom
  }
  if (period <= 0) return 0

  const hz = sampleRate / period
  if (hz < F0_MIN_HZ || hz > F0_MAX_HZ) return 0
  return hz
}

/**
 * How well a measured pitch fits a profile, 0–1. Returns a neutral 0.5 when
 * either side has no usable pitch, so an unvoiced frame neither confirms nor
 * denies identity rather than being counted as a mismatch.
 */
export function pitchScore(f0: number, profile: VoiceProfile): number {
  if (!f0 || !profile.f0) return 0.5
  const tolerance = Math.max(18, (profile.f0StdDev ?? 25) * 2.5)
  return Math.max(0, 1 - Math.abs(f0 - profile.f0) / tolerance)
}

export function loadVoiceProfile(): VoiceProfile | null {
  if (typeof window === 'undefined') return null
  try { const s = localStorage.getItem(VOICE_PROFILE_KEY); return s ? JSON.parse(s) : null }
  catch { return null }
}
export function saveVoiceProfile(p: VoiceProfile) {
  if (typeof window === 'undefined') return
  try { localStorage.setItem(VOICE_PROFILE_KEY, JSON.stringify(p)) } catch { /* ignore */ }
}
export function deleteVoiceProfile() {
  if (typeof window === 'undefined') return
  try { localStorage.removeItem(VOICE_PROFILE_KEY) } catch { /* ignore */ }
}

/**
 * Statistical band-distance matching for speaker identification.
 * Each frequency band is checked: is the current value within N standard
 * deviations of the calibrated profile mean?
 * Returns 0–1 where 1 = every band exactly matches the profile.
 *
 * MUCH more robust than cosine similarity for rejecting TV/background voices
 * because it measures per-band deviation scaled to that person’s natural
 * voice variation, making it unique to the calibrated speaker.
 */
export function voiceSimilarity(current: number[], profile: VoiceProfile): number {
  // Fall back to a conservative tolerance if calibrated without std dev
  const stdDev = profile.bandStdDev ?? Array(current.length).fill(0.25)  // generous fallback for legacy profiles
  let score = 0
  for (let i = 0; i < current.length; i++) {
    const deviation = Math.abs(current[i] - profile.bands[i])
    // 3.0 std-deviation tolerance + small fixed floor to avoid over-strict matching
    const tolerance = stdDev[i] * 3.0 + 0.05
    score += Math.max(0, 1 - deviation / tolerance)
  }
  return score / current.length
}

/** Extract normalised frequency-band energies from an AnalyserNode. */
export function extractBands(analyser: AnalyserNode, buf: Uint8Array<ArrayBuffer>): number[] {
  analyser.getByteFrequencyData(buf)
  const binSize = Math.floor(buf.length / AUDIO_BANDS)
  const raw = Array.from({ length: AUDIO_BANDS }, (_, b) => {
    let sum = 0
    for (let j = b * binSize; j < Math.min((b + 1) * binSize, buf.length); j++) sum += buf[j]
    return sum / binSize
  })
  const max = Math.max(...raw, 1)
  return raw.map(v => v / max)
}

// ── Self-voice (echo) rejection ───────────────────────────────────────────────
// The single biggest source of "JARVIS hears itself" is the speaker→mic path:
// while a reply is read aloud, the room's own audio re-enters the microphone and
// the recognizer transcribes it as if the user had spoken. Muting the mic fixes
// that but kills barge-in, so instead we identify the echo and drop it.
//
// Two independent signals are used:
//
//  1. LIVE REFERENCE — when a reply plays through an <audio> element (AI voice)
//     the exact signal JARVIS is emitting is available. Tapping it with a second
//     AnalyserNode gives a frame-accurate fingerprint of the echo; a mic frame
//     whose spectrum tracks the reference is echo by definition.
//  2. LEARNED SELF-PROFILE — the Web Speech synthesiser cannot be tapped, so the
//     echo's fingerprint is instead learned from the mic while JARVIS speaks and
//     the user is quiet. A mic frame that resembles this negative profile more
//     than it resembles the user's profile is echo.

export const SELF_PROFILE_KEY = 'paul.selfVoiceProfile.v1'
// Correlation above which a mic frame is considered a copy of the TTS reference.
export const ECHO_REF_THRESHOLD = 0.86
// How strongly the mic frame must favour the self-profile over the user profile
// before it is treated as echo (margin avoids rejecting a genuine barge-in).
export const ECHO_SELF_MARGIN = 0.06

/**
 * Pearson correlation between two normalised band vectors, mapped to 0–1.
 * Correlation (not distance) is the right measure for the echo reference: the
 * mic copy of the speaker output is attenuated and coloured by the room, so its
 * absolute levels differ while its *shape* stays the same.
 */
export function bandCorrelation(a: number[], b: number[]): number {
  const n = Math.min(a.length, b.length)
  if (!n) return 0
  let ma = 0, mb = 0
  for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i] }
  ma /= n; mb /= n
  let num = 0, da = 0, db = 0
  for (let i = 0; i < n; i++) {
    const x = a[i] - ma, y = b[i] - mb
    num += x * y; da += x * x; db += y * y
  }
  const den = Math.sqrt(da * db)
  if (den < 1e-9) return 0
  return Math.max(0, num / den)
}

export function loadSelfProfile(): VoiceProfile | null {
  if (typeof window === 'undefined') return null
  try { const s = localStorage.getItem(SELF_PROFILE_KEY); return s ? JSON.parse(s) : null }
  catch { return null }
}
export function saveSelfProfile(p: VoiceProfile) {
  if (typeof window === 'undefined') return
  try { localStorage.setItem(SELF_PROFILE_KEY, JSON.stringify(p)) } catch { /* ignore */ }
}

// ── Continuous voice-profile learning ─────────────────────────────────────────
// Slowly blends fresh, high-confidence frames of the user's voice into the stored
// profile so JARVIS keeps adapting to the user's voice (and mic/room) the more
// they talk to it. Uses an exponential moving average so a single noisy frame can
// never corrupt the fingerprint, and recomputes the per-band tolerance and
// spectral centroid from the updated bands.
export function adaptVoiceProfile(profile: VoiceProfile, bands: number[], alpha = 0.05, f0 = 0): VoiceProfile {
  const newBands = profile.bands.map((b, i) => b * (1 - alpha) + (bands[i] ?? b) * alpha)
  const std = profile.bandStdDev ?? Array(newBands.length).fill(0.1)
  const newStd = std.map((s, i) => {
    const dev = Math.abs((bands[i] ?? newBands[i]) - newBands[i])
    return Math.max(0.03, Math.min(0.5, s * (1 - alpha) + dev * alpha))
  })
  const sum = newBands.reduce((a, b) => a + b, 0) || 0.01
  const centroid = newBands.reduce((a, v, i) => a + v * i, 0) / (newBands.length * sum)
  // Track pitch too, but only from voiced frames (f0 > 0) — an unvoiced frame
  // reports 0 and would otherwise drag the stored pitch toward silence.
  let newF0 = profile.f0
  let newF0Std = profile.f0StdDev
  if (f0 > 0) {
    newF0 = newF0 ? newF0 * (1 - alpha) + f0 * alpha : f0
    const dev = Math.abs(f0 - newF0)
    newF0Std = Math.max(8, Math.min(60, (newF0Std ?? 25) * (1 - alpha) + dev * alpha))
  }
  return { ...profile, bands: newBands, bandStdDev: newStd, centroid, f0: newF0, f0StdDev: newF0Std }
}
