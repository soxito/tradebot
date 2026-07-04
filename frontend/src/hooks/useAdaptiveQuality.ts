/**
 * useAdaptiveQuality — runtime graphics auto-scaler for the S.O.X / JARVIS room.
 *
 * 1. Picks a starting quality tier from the machine's static specs
 *    (CPU cores + memory) — e.g. Apple M2 8GB → "high", M2 Pro 16GB → "ultra".
 * 2. Continuously samples the real frame-rate. If frames drop below the tier's
 *    target for a few seconds it downgrades one tier (so the machine never lags).
 *    If headroom returns it cautiously climbs back — but never above the
 *    statically-detected ceiling for that hardware.
 *
 * Returns a live `profileRef` (read every animation frame, no re-render churn)
 * plus display state (`tier`, `fps`, `label`) for the HUD.
 */
import { useEffect, useRef, useState } from 'react'
import {
  PerfTier,
  PerfProfile,
  PERF_TIERS,
  PERF_PROFILES,
  detectDevice,
  detectStaticTier,
  describeTier,
} from '@/utils/devicePerformance'

interface AdaptiveQuality {
  /** Live profile — read `.current` inside RAF loops. */
  profileRef: React.MutableRefObject<PerfProfile>
  tier: PerfTier
  fps: number
  label: string
  cores: number
  memGB: number | null
}

const DOWNGRADE_STREAK = 1  // react on the very first bad second (never lag)
const UPGRADE_STREAK = 6    // seconds of headroom before climbing back
const HARD_FLOOR_FPS = 24   // below this, drop straight to the lowest tier

export function useAdaptiveQuality(): AdaptiveQuality {
  const deviceRef = useRef(detectDevice())
  const ceilingIdxRef = useRef(PERF_TIERS.indexOf(detectStaticTier(deviceRef.current)))

  // Start conservatively (never boot straight into a heavy tier — that is what
  // froze weak machines) and let the FPS monitor earn its way up to the
  // hardware ceiling. Cap the starting tier at 'medium'.
  const startIdxRef = useRef(
    Math.min(ceilingIdxRef.current, PERF_TIERS.indexOf('medium')),
  )

  const [tier, setTier] = useState<PerfTier>(PERF_TIERS[startIdxRef.current])
  const [fps, setFps] = useState(0)

  const idxRef = useRef(startIdxRef.current)
  const profileRef = useRef<PerfProfile>(PERF_PROFILES[PERF_TIERS[startIdxRef.current]])

  useEffect(() => {
    let raf = 0
    let frames = 0
    let windowStart = typeof performance !== 'undefined' ? performance.now() : Date.now()
    let badStreak = 0
    let goodStreak = 0

    const setIdx = (next: number) => {
      const clamped = Math.max(0, Math.min(PERF_TIERS.length - 1, next))
      if (clamped === idxRef.current) return
      idxRef.current = clamped
      const t = PERF_TIERS[clamped]
      profileRef.current = PERF_PROFILES[t]
      setTier(t)
    }

    const sample = (now: number) => {
      raf = requestAnimationFrame(sample)
      // Don't count frames while the tab is hidden (RAF is throttled/paused).
      if (typeof document !== 'undefined' && document.hidden) {
        frames = 0
        windowStart = now
        return
      }
      frames++
      const elapsed = now - windowStart
      if (elapsed < 1000) return

      const measured = (frames * 1000) / elapsed
      frames = 0
      windowStart = now
      setFps(prev => (Math.abs(prev - measured) >= 2 ? Math.round(measured) : prev))

      const target = profileRef.current.fpsTarget
      const idx = idxRef.current

      if (measured < HARD_FLOOR_FPS && idx > 0) {
        // Severe — the machine is choking. Drop straight to the lowest tier.
        setIdx(0)
        badStreak = 0
        goodStreak = 0
      } else if (measured < target * 0.8 && idx > 0) {
        // Struggling — step down immediately.
        badStreak++
        goodStreak = 0
        if (badStreak >= DOWNGRADE_STREAK) { setIdx(idx - 1); badStreak = 0 }
      } else if (measured >= 57 && idx < ceilingIdxRef.current) {
        // Comfortable headroom and below our hardware ceiling — climb slowly.
        goodStreak++
        badStreak = 0
        if (goodStreak >= UPGRADE_STREAK) { setIdx(idx + 1); goodStreak = 0 }
      } else {
        badStreak = 0
        goodStreak = 0
      }
    }

    raf = requestAnimationFrame(sample)
    return () => cancelAnimationFrame(raf)
  }, [])

  return {
    profileRef,
    tier,
    fps,
    label: describeTier(tier, deviceRef.current),
    cores: deviceRef.current.cores,
    memGB: deviceRef.current.memGB,
  }
}
