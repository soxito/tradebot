/**
 * devicePerformance — detects the host machine's graphics capability and maps
 * it to a quality tier so the S.O.X / JARVIS room scales its animations to the
 * hardware and never lags.
 *
 * Distinguishes e.g. an Apple M2 8 GB (2022) from an M2 Pro 16 GB (2023):
 *   • M2  base  → 8 CPU cores  → "high"  tier
 *   • M2  Pro   → 10-12 cores  → "ultra" tier
 * plus a runtime FPS monitor (see useAdaptiveQuality) that downgrades further
 * if actual frames drop, so weak / thermally-throttled machines stay smooth.
 */

export type PerfTier = 'low' | 'medium' | 'high' | 'ultra'

export const PERF_TIERS: PerfTier[] = ['low', 'medium', 'high', 'ultra']

export interface PerfProfile {
  tier: PerfTier
  /** Fraction of the max orb particle layers to render (0-1). */
  particleScale: number
  /** Fraction of the outer nebula cloud particles to render (0-1). */
  cloudScale: number
  /** Hard cap on live THINKING spark particles. */
  sparkCap: number
  /** Segment count for the TALKING wave-ribbon mesh. */
  ribbonSegs: number
  /** devicePixelRatio ceiling for the 2D orb canvas. */
  dprCap: number
  /** devicePixelRatio ceiling for the Three.js robot. */
  robotDprCap: number
  /** Target animation frame-rate (frames throttle to this). */
  fpsTarget: number
  /** Whether the Three.js robot renders shadow maps. */
  shadows: boolean
  /** Whether the Three.js robot uses MSAA antialiasing. */
  antialias: boolean
}

export const PERF_PROFILES: Record<PerfTier, PerfProfile> = {
  low: {
    tier: 'low',
    particleScale: 0.32,
    cloudScale: 0.30,
    sparkCap: 60,
    ribbonSegs: 56,
    dprCap: 1,
    robotDprCap: 1,
    fpsTarget: 30,
    shadows: false,
    antialias: false,
  },
  medium: {
    tier: 'medium',
    particleScale: 0.55,
    cloudScale: 0.50,
    sparkCap: 110,
    ribbonSegs: 84,
    dprCap: 1.5,
    robotDprCap: 1.25,
    fpsTarget: 45,
    shadows: false,
    antialias: true,
  },
  high: {
    tier: 'high',
    particleScale: 0.82,
    cloudScale: 0.82,
    sparkCap: 170,
    ribbonSegs: 118,
    dprCap: 2,
    robotDprCap: 1.5,
    fpsTarget: 60,
    shadows: true,
    antialias: true,
  },
  ultra: {
    tier: 'ultra',
    particleScale: 1.0,
    cloudScale: 1.0,
    sparkCap: 200,
    ribbonSegs: 130,
    dprCap: 2,
    robotDprCap: 2,
    fpsTarget: 60,
    shadows: true,
    antialias: true,
  },
}

export interface DeviceInfo {
  cores: number
  /** GB from navigator.deviceMemory (Chromium only, capped at 8), else null. */
  memGB: number | null
  dpr: number
  isMobile: boolean
  /** Rough Apple-Silicon / macOS hint from the UA + platform. */
  isApple: boolean
}

export function detectDevice(): DeviceInfo {
  if (typeof navigator === 'undefined' || typeof window === 'undefined') {
    return { cores: 8, memGB: null, dpr: 1, isMobile: false, isApple: false }
  }
  const nav = navigator as Navigator & { deviceMemory?: number }
  const ua = nav.userAgent || ''
  const cores = Math.max(1, nav.hardwareConcurrency || 4)
  const memGB = typeof nav.deviceMemory === 'number' ? nav.deviceMemory : null
  const dpr = window.devicePixelRatio || 1
  const isMobile = /Android|iPhone|iPad|iPod|Mobile|Tablet/i.test(ua)
  const isApple = /Mac|iPhone|iPad|iPod/i.test(ua) || /Mac/i.test(nav.platform || '')
  return { cores, memGB, dpr, isMobile, isApple }
}

/**
 * Static capability guess from CPU cores + reported memory. This is the
 * "ceiling" the adaptive FPS monitor is allowed to climb back up to; the
 * monitor may drop below it if real frames struggle.
 */
export function detectStaticTier(info: DeviceInfo = detectDevice()): PerfTier {
  const { cores, memGB, isMobile } = info

  // Phones / tablets: keep it light regardless of core count.
  if (isMobile) return cores >= 8 ? 'medium' : 'low'

  // Memory-constrained machines get capped hard.
  if (memGB != null && memGB <= 2) return 'low'

  // Core-count driven tiering. Deliberately conservative because the room runs
  // a particle canvas AND a Three.js robot at the same time — an 8-core /
  // 8 GB laptop (e.g. Apple M2 2022) must stay on 'medium' to never freeze.
  //   ≤4 cores → low, ≤8 → medium (M2 8GB), ≤10 → high (M2 Pro 10-core),
  //   ≥12 → ultra (M2 Pro 12-core / M3 Pro-Max+)
  let tier: PerfTier
  if (cores <= 4) tier = 'low'
  else if (cores <= 8) tier = 'medium'
  else if (cores <= 10) tier = 'high'
  else tier = 'ultra'

  // A 4 GB machine shouldn't run above low even with many cores.
  if (memGB != null && memGB <= 4 && tier !== 'low') {
    tier = 'medium'
  }
  return tier
}

export function detectStaticProfile(info: DeviceInfo = detectDevice()): PerfProfile {
  return PERF_PROFILES[detectStaticTier(info)]
}

/** Human-readable label e.g. "ULTRA · 12-core · 16GB". */
export function describeTier(tier: PerfTier, info: DeviceInfo = detectDevice()): string {
  const parts = [tier.toUpperCase(), `${info.cores}-core`]
  if (info.memGB != null) parts.push(`${info.memGB}GB+`)
  return parts.join(' · ')
}
