/**
 * S.O.X Command Room — Strategic Operations eXchange
 *
 * Key improvements over the original JARVIS Room:
 *  • Renamed throughout: JARVIS → S.O.X
 *  • Enhanced canvas orb (~960 particles): sonar-ring expansion (LISTENING),
 *    spark-ejection particles (THINKING), EQ bar visualisers L+R (TALKING),
 *    electromagnetic jitter (THINKING), brighter centre glow.
 *  • Futuristic SVG background (hex grid, arc-reactor rings, data streams,
 *    corner HUD brackets, scanlines) referenced from /sox-bg.svg.
 *  • All panels are drag-and-drop widgets — pointer-capture drag, positions
 *    persisted to localStorage.
 *  • Extra HUD chrome: rotating compass rose, scan-line sweep,
 *    coordinate + uplink readouts, 4-ring reticle with tick marks.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import dynamic from 'next/dynamic'
import Head from 'next/head'
import { useRouter } from 'next/router'
import { apiClient } from '@/services/api'
import { useAdaptiveQuality } from '@/hooks/useAdaptiveQuality'
import { PerfProfile, PerfTier, PERF_PROFILES } from '@/utils/devicePerformance'
import { supportsOffscreenCanvas } from '@/utils/workerSupport'

const PaulChat = dynamic(() => import('@/components/PaulChat'), { ssr: false })
const TradingViewWidget = dynamic(() => import('@/components/TradingViewWidget'), { ssr: false })
const ExtensionInstallPrompt = dynamic(() => import('@/components/ExtensionInstallPrompt'), { ssr: false })
const FaceVisionPanel = dynamic(() => import('@/components/FaceVisionPanel'), { ssr: false })

function soxSpeak(text: string) {
  if (typeof window === 'undefined' || !text) return
  try {
    window.postMessage({ __jarvisPage: true, type: 'jarvis-speak', text }, window.location.origin)
  } catch { /* noop */ }
}

// ── Types ────────────────────────────────────────────────────────────────────
type WidgetType = 'price' | 'chart'
interface RoomWidget { id: string; type: WidgetType; symbol: string }
interface Ticker { symbol: string; price: number; changePct: number; loading: boolean; error?: boolean }
interface CryptoPosition {
  exchange: string; symbol: string; raw_symbol?: string; side: string
  size: number; entry_price: number; mark_price: number; pnl: number; pnl_pct: number
  leverage?: number | null; notional?: number | null; liquidation_price?: number | null
}
interface Mt5Position {
  ticket: number; symbol: string; side: string; volume: number
  price_open: number; price_current: number; sl?: number | null; tp?: number | null; profit: number
}
interface Mt5Account {
  account_id: number; name: string; login: string; server: string
  balance: number; equity: number; floating_pnl: number; currency: string
  leverage: number; positions: Mt5Position[]; position_count: number
}
interface UnifiedMonitor {
  crypto_positions: CryptoPosition[]; crypto_total_pnl: number; crypto_total_notional: number
  mt5_accounts: Mt5Account[]; mt5_total_balance: number; mt5_total_equity: number
  mt5_total_floating_pnl: number; mt5_position_count: number
  total_position_count: number; total_pnl: number; fetched_at: string
}
interface VolumeAlert {
  id: string; exchange: string; symbol: string; side: string
  message: string; suggestedSl: number; volX: number; createdAt: number
}
interface SystemStats {
  available: boolean; cpu_percent: number; cpu_count: number
  per_core?: number[]; load_percent?: number | null
  mem_percent: number; mem_used: number; mem_total: number
  mem_available?: number; swap_percent?: number
  proc_cpu_percent?: number; proc_mem?: number; reason?: string
}

// ── Constants ─────────────────────────────────────────────────────────────────
const WIDGETS_KEY = 'sox-room-widgets-v1'
const PANEL_POS_KEY = 'sox-panel-positions-v2'

const DEFAULT_WIDGETS: RoomWidget[] = [
  { id: 'w-btc',  type: 'price', symbol: 'BTCUSDT' },
  { id: 'w-eth',  type: 'price', symbol: 'ETHUSDT' },
  { id: 'w-sol',  type: 'price', symbol: 'SOLUSDT' },
  { id: 'w-bnb',  type: 'price', symbol: 'BNBUSDT' },
  { id: 'w-xrp',  type: 'price', symbol: 'XRPUSDT' },
  { id: 'w-doge', type: 'price', symbol: 'DOGEUSDT' },
]

// ── Helpers ───────────────────────────────────────────────────────────────────
function normalizeSymbol(raw: string): string {
  const s = raw.toUpperCase().replace(/[^A-Z0-9]/g, '')
  if (!s) return ''
  if (/(USDT|USDC|BUSD|BTC|ETH|USD)$/.test(s)) return s
  return `${s}USDT`
}
function splitPair(sym: string): { base: string; quote: string } {
  const m = sym.match(/^(.*?)(USDT|USDC|BUSD|USD|BTC|ETH)$/)
  if (m) return { base: m[1], quote: m[2] }
  return { base: sym, quote: '' }
}
function isLongSide(side: string): boolean {
  const s = (side || '').toLowerCase()
  return s === 'long' || s === 'buy' || s === 'bid'
}
function gfxTierColor(tier: PerfTier): string {
  switch (tier) {
    case 'low':    return '#f87171'
    case 'medium': return '#fbbf24'
    case 'high':   return '#7df3dd'
    default:       return '#4ade80'  // ultra
  }
}

// ── S.O.X orb state machine ───────────────────────────────────────────────────
type SoxState = 'idle' | 'listening' | 'thinking' | 'talking'

const STATE_TARGETS: Record<SoxState, { energy: number; gold: number; wave: number; core: number; ring: number }> = {
  idle:      { energy: 0.35, gold: 0.08, wave: 0.0, core: 0.55, ring: 0.0 },
  listening: { energy: 0.65, gold: 0.04, wave: 1.0, core: 1.0,  ring: 0.0 },
  thinking:  { energy: 0.80, gold: 1.00, wave: 0.0, core: 0.88, ring: 0.0 },
  talking:   { energy: 1.00, gold: 0.48, wave: 0.0, core: 1.0,  ring: 1.0 },
}

// ── REBUILT S.O.X energy-core canvas animation ──────────────────────────────
// Matches the three-state reference GIF:
//   TALKING  — wave ribbon mesh + bright solid inner gold ring + outer cloud
//   THINKING — dense sparkling gold particle cloud + blue inner ring
//   LISTENING — sharp concentric sonar rings + outer frame rings + cloud
//   ALL states — outer nebula cloud halo (counter-rotating layers, 300 particles)
function useSoxOrb(
  canvasRef: React.RefObject<HTMLCanvasElement | null>,
  stateRef:  React.MutableRefObject<SoxState>,
  qualityRef?: React.MutableRefObject<PerfProfile>,
) {
  const rafRef = useRef<number>(0)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    // ── Off-thread render path: OffscreenCanvas + Web Worker ──────────────────
    // Moves the ~980-particle animation off the main thread so the room loads
    // fast and stays responsive. Falls through to the main-thread loop below
    // when the browser lacks OffscreenCanvas support (e.g. older Safari).
    if (supportsOffscreenCanvas(canvas)) {
      let worker: Worker | null = null
      try {
        worker = new Worker(new URL('../workers/soxOrb.worker.ts', import.meta.url))
      } catch {
        worker = null
      }
      if (worker) {
        const w = worker
        const readW = () => canvas.clientWidth || window.innerWidth
        const readH = () => canvas.clientHeight || window.innerHeight
        let offscreen: OffscreenCanvas | null = null
        try {
          offscreen = canvas.transferControlToOffscreen()
        } catch {
          offscreen = null
        }
        if (offscreen) {
          w.postMessage({
            type: 'init',
            canvas: offscreen,
            cssW: readW(),
            cssH: readH(),
            deviceDpr: window.devicePixelRatio || 1,
            profile: qualityRef?.current ?? PERF_PROFILES.high,
            state: stateRef.current,
          }, [offscreen])

          const onResize = () => w.postMessage({
            type: 'resize', cssW: readW(), cssH: readH(), deviceDpr: window.devicePixelRatio || 1,
          })
          const onVisibility = () => w.postMessage({ type: 'visibility', hidden: document.hidden })
          window.addEventListener('resize', onResize)
          document.addEventListener('visibilitychange', onVisibility)

          // Forward state + quality-tier changes to the worker. Both transition
          // infrequently, so a light 150 ms poll (no main-thread rAF or canvas
          // work) keeps the orb in sync at near-zero cost; the worker's own lerp
          // smooths the visual transition.
          let lastState = stateRef.current
          let lastTier: PerfTier = (qualityRef?.current ?? PERF_PROFILES.high).tier
          const sync = window.setInterval(() => {
            const s = stateRef.current
            if (s !== lastState) { lastState = s; w.postMessage({ type: 'state', state: s }) }
            const prof = qualityRef?.current
            if (prof && prof.tier !== lastTier) { lastTier = prof.tier; w.postMessage({ type: 'quality', profile: prof }) }
          }, 150)

          return () => {
            clearInterval(sync)
            window.removeEventListener('resize', onResize)
            document.removeEventListener('visibilitychange', onVisibility)
            try { w.postMessage({ type: 'stop' }) } catch { /* noop */ }
            w.terminate()
          }
        }
        // Transfer failed → drop the worker and use the main-thread path.
        w.terminate()
      }
    }

    // ── Main-thread fallback (no OffscreenCanvas) ─────────────────────────────
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const q0 = qualityRef?.current ?? PERF_PROFILES.high
    let W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, q0.dprCap)
    let appliedDprCap = q0.dprCap
    const resize = () => {
      const q = qualityRef?.current ?? PERF_PROFILES.high
      appliedDprCap = q.dprCap
      W = canvas.clientWidth; H = canvas.clientHeight
      DPR = Math.min(window.devicePixelRatio || 1, q.dprCap)
      canvas.width  = Math.max(1, Math.floor(W * DPR))
      canvas.height = Math.max(1, Math.floor(H * DPR))
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0)
      ctx.fillStyle = '#020207'; ctx.fillRect(0, 0, W, H)
    }
    resize()
    window.addEventListener('resize', resize)

    // ── particle type ────────────────────────────────────────────────────────
    type P = { a: number; r: number; s: number; len: number; w: number }
    const make = (n: number, rMin: number, rMax: number, sMin: number, sMax: number): P[] =>
      Array.from({ length: n }, () => ({
        a:   Math.random() * Math.PI * 2,
        r:   rMin + Math.random() * (rMax - rMin),
        s:   sMin + Math.random() * (sMax - sMin),
        len: 0.05 + Math.random() * 0.16,
        w:   0.3  + Math.random() * 1.4,
      }))

    // Core particle layers (~980 total)
    const outer  = make(420, 0.78, 1.06, 0.0012, 0.0035)  // teal outer turbulence
    const goldP  = make(340, 0.36, 0.78, 0.0016, 0.0052)  // gold swirl
    const coreP  = make(160, 0.06, 0.26, 0.0024, 0.0065)  // blue core
    const streak = make(80,  0.50, 0.92, 0.0030, 0.0085)  // talking streaks

    // ── Outer cloud halo (400 nebula particles, r = 1.05–1.60 × R) ──────────
    // Counter-rotating layers forming the visible outer "frame" ring/cloud.
    // Narrower ring (1.05–1.60) for higher density = clearly visible cloud.
    type Cloud = { a: number; r: number; da: number; size: number; alpha: number }
    const cloud: Cloud[] = Array.from({ length: 400 }, (_, i) => ({
      a:     Math.random() * Math.PI * 2,
      r:     1.05 + Math.random() * 0.55,    // 1.05R – 1.60R (concentrated)
      da:    (i % 2 === 0 ? 1 : -1) * (0.00020 + Math.random() * 0.00030),
      size:  0.8 + Math.random() * 3.0,
      alpha: 0.25 + Math.random() * 0.75,   // higher base alpha
    }))

    // ── Sonar rings (LISTENING, 9 rings) ──────────────────────────────────────
    type Ring = { phase: number }
    const sonarRings: Ring[] = Array.from({ length: 9 }, (_, i) => ({ phase: i / 9 }))

    // ── Spark particles (THINKING) ────────────────────────────────────────────
    type Spark = {
      x: number; y: number; vx: number; vy: number
      life: number; maxLife: number; size: number
    }
    const sparks: Spark[] = []

    // ── Wave ribbon definitions (TALKING) ─────────────────────────────────────
    // 8 sinusoidal ribbons tracing closed paths around the orb — teal outer (6)
    // + gold inner (2) — matching the flowing mesh in the reference image.
    const ribbons = [
      { baseR: 0.74, amp: 0.19, freq: 4, phOff: 0.0,  dir:  1, cm: 0.00 },
      { baseR: 0.70, amp: 0.23, freq: 5, phOff: 1.2,  dir: -1, cm: 0.00 },
      { baseR: 0.80, amp: 0.15, freq: 6, phOff: 2.4,  dir:  1, cm: 0.00 },
      { baseR: 0.76, amp: 0.17, freq: 3, phOff: 3.6,  dir: -1, cm: 0.00 },
      { baseR: 0.64, amp: 0.25, freq: 4, phOff: 0.8,  dir:  1, cm: 0.05 },
      { baseR: 0.86, amp: 0.13, freq: 7, phOff: 1.8,  dir: -1, cm: 0.05 },
      { baseR: 0.57, amp: 0.13, freq: 5, phOff: 2.0,  dir:  1, cm: 0.90 }, // gold inner
      { baseR: 0.54, amp: 0.16, freq: 4, phOff: 3.0,  dir: -1, cm: 0.90 }, // gold inner
    ] as const

    let t = 0
    let energy = 0.08, goldMix = 0.06, waveMix = 0, coreMix = 0.38, ringMix = 0

    const lerp   = (a: number, b: number, k: number) => a + (b - a) * k
    const mixRGB = (
      a: [number,number,number],
      b: [number,number,number],
      k: number,
    ): [number,number,number] =>
      [Math.round(lerp(a[0],b[0],k)), Math.round(lerp(a[1],b[1],k)), Math.round(lerp(a[2],b[2],k))]

    const TEAL:   [number,number,number] = [45, 226, 197]
    const GOLD:   [number,number,number] = [245, 158, 11]
    const BLUE:   [number,number,number] = [96,  165, 250]
    const ORANGE: [number,number,number] = [251, 146, 60]
    const WHITE:  [number,number,number] = [220, 240, 255]

    let lastFrame = 0

    const draw = (now?: number) => {
      rafRef.current = requestAnimationFrame(draw)
      if (typeof document !== 'undefined' && document.hidden) return
      // Live quality profile — scales frame-rate, particles & DPR to the machine.
      const q = qualityRef?.current ?? PERF_PROFILES.high
      const glow = q.orbGlow   // shadowBlur is a canvas perf-killer on weak GPUs
      const frameMs = 1000 / q.fpsTarget
      const ts = now || (typeof performance !== 'undefined' ? performance.now() : Date.now())
      if (ts - lastFrame < frameMs) return
      lastFrame = ts
      // Re-apply canvas resolution when the tier (and thus DPR cap) changes.
      if (q.dprCap !== appliedDprCap) resize()
      t += 0.68   // slightly slower t to stay equivalent to old 40fps feel

      const tgt = STATE_TARGETS[stateRef.current] || STATE_TARGETS.idle
      energy  = lerp(energy,  tgt.energy, 0.045)
      goldMix = lerp(goldMix, tgt.gold,   0.036)
      waveMix = lerp(waveMix, tgt.wave,   0.062)
      coreMix = lerp(coreMix, tgt.core,   0.045)
      ringMix = lerp(ringMix, tgt.ring,   0.045)

      // Electromagnetic jitter (THINKING)
      const jx = goldMix > 0.5 ? (Math.random() - 0.5) * 2.0 * ((goldMix - 0.5) / 0.5) : 0
      const jy = goldMix > 0.5 ? (Math.random() - 0.5) * 2.0 * ((goldMix - 0.5) / 0.5) : 0

      // Motion-blur veil
      ctx.globalCompositeOperation = 'source-over'
      ctx.fillStyle = 'rgba(2,2,7,0.13)'; ctx.fillRect(0, 0, W, H)

      const cx = W / 2 + jx
      const cy = H * 0.44 + jy
      const breathe = 1 + Math.sin(t * 0.013) * 0.022 + energy * 0.052
      const R    = Math.min(W, H) * 0.33 * breathe   // larger orb
      const spin = 1 + energy * 1.8

      ctx.globalCompositeOperation = 'lighter'

      // ══════════════════════════════════════════════════════════════════════
      // 1. OUTER CLOUD HALO — the nebula ring visible in all states
      // ══════════════════════════════════════════════════════════════════════
      {
        // Colour shifts per state: teal (idle/listening), teal→gold (thinking),
        //                          teal (talking — the ribbon mesh dominates)
        const cloudC = goldMix > 0.55
          ? mixRGB(TEAL, ORANGE, (goldMix - 0.55) / 0.45)
          : waveMix > 0.3 ? mixRGB(TEAL, BLUE, waveMix * 0.45) : TEAL
        // Frame rings — always visible, anchor the cloud outline
        ctx.lineWidth   = 0.8
        ctx.strokeStyle = `rgba(${cloudC[0]},${cloudC[1]},${cloudC[2]},${(0.10 + energy * 0.12).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 1.12, 0, Math.PI * 2); ctx.stroke()
        ctx.lineWidth   = 0.5
        ctx.strokeStyle = `rgba(${cloudC[0]},${cloudC[1]},${cloudC[2]},${(0.06 + energy * 0.08).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 1.42, 0, Math.PI * 2); ctx.stroke()

        const cloudN = Math.floor(cloud.length * q.cloudScale)
        for (let ci = 0; ci < cloudN; ci++) {
          const p = cloud[ci]
          p.a += p.da * (1 + energy * 0.7)
          const pulse = 1 + Math.sin(t * 0.006 + p.a * 2.5) * 0.04 * energy
          const rr = R * p.r * pulse
          const x  = cx + Math.cos(p.a) * rr
          const y  = cy + Math.sin(p.a) * rr * 0.95
          // Fixed alpha formula — no triple-multiplication
          const a  = p.alpha * (0.06 + energy * 0.10)
          ctx.fillStyle = `rgba(${cloudC[0]},${cloudC[1]},${cloudC[2]},${a.toFixed(3)})`
          ctx.beginPath()
          ctx.arc(x, y, p.size * (0.65 + energy * 0.55), 0, Math.PI * 2)
          ctx.fill()
        }
      }

      // ══════════════════════════════════════════════════════════════════════
      // 2. SONAR RINGS (LISTENING) — clean concentric circles with glow
      // ══════════════════════════════════════════════════════════════════════
      if (waveMix > 0.02) {
        // Static outer frame rings (visible in reference as the "frame" boundary)
        ctx.lineWidth   = 2.2
        ctx.strokeStyle = `rgba(${TEAL[0]},${TEAL[1]},${TEAL[2]},${(0.28 * waveMix).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.92, 0, Math.PI * 2); ctx.stroke()
        ctx.lineWidth   = 1.4
        ctx.strokeStyle = `rgba(${TEAL[0]},${TEAL[1]},${TEAL[2]},${(0.18 * waveMix).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 1.18, 0, Math.PI * 2); ctx.stroke()

        // Expanding sonar rings
        for (const ring of sonarRings) {
          ring.phase = (ring.phase + 0.0026) % 1
          const rr = R * (0.28 + ring.phase * 2.7)
          const a  = Math.max(0, (1 - ring.phase) * 0.78 * waveMix)
          if (a < 0.007) continue
          ctx.shadowColor = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${(a * 0.55).toFixed(3)})`
          ctx.shadowBlur  = glow ? 7 * (1 - ring.phase) : 0
          ctx.strokeStyle = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${a.toFixed(3)})`
          ctx.lineWidth   = Math.max(0.25, 2.6 - ring.phase * 2.4)
          ctx.beginPath(); ctx.arc(cx, cy, rr, 0, Math.PI * 2); ctx.stroke()
          ctx.shadowBlur  = 0
        }

        // Sound-wave arcs fanning right
        ctx.lineWidth = 2.2
        for (let i = 0; i < 6; i++) {
          const ph = ((t * 0.016) + i * 0.18) % 1
          const wr = R * (0.55 + ph * 1.35)
          const a  = (1 - ph) * 0.58 * waveMix
          ctx.strokeStyle = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${a.toFixed(3)})`
          ctx.beginPath(); ctx.arc(cx, cy, wr, -Math.PI / 3.0, Math.PI / 3.0); ctx.stroke()
        }
      }

      // ══════════════════════════════════════════════════════════════════════
      // 3. SPARK PARTICLES (THINKING) — dense irregular gold nebula cloud
      // ══════════════════════════════════════════════════════════════════════
      if (goldMix > 0.28) {
        const spawnRate = 0.55 * goldMix
        if (Math.random() < spawnRate && sparks.length < q.sparkCap) {
          // Spawn across a wide area so the cloud matches the reference's blob shape
          const spawnR = Math.random() < 0.65 ? R * 0.35 : R * (0.35 + Math.random() * 0.75)
          const spawnA = Math.random() * Math.PI * 2
          const speed  = 0.5 + Math.random() * 3.0
          const angle  = Math.random() * Math.PI * 2
          sparks.push({
            x: cx + Math.cos(spawnA) * spawnR,
            y: cy + Math.sin(spawnA) * spawnR,
            vx: Math.cos(angle) * speed,
            vy: Math.sin(angle) * speed,
            life: 0,
            maxLife: 22 + Math.random() * 55,
            size:    0.5 + Math.random() * 3.2,
          })
        }
        for (let i = sparks.length - 1; i >= 0; i--) {
          const s = sparks[i]
          s.x += s.vx; s.y += s.vy
          s.vx *= 0.955; s.vy *= 0.955
          s.life++
          if (s.life >= s.maxLife) { sparks.splice(i, 1); continue }
          const p  = s.life / s.maxLife
          const sa = (1 - p) * 0.95 * goldMix
          // Colour arc: gold → orange → white-hot fade
          const sc = p < 0.5 ? mixRGB(GOLD, ORANGE, p * 2) : mixRGB(ORANGE, WHITE, (p - 0.5) * 2)
          ctx.fillStyle = `rgba(${sc[0]},${sc[1]},${sc[2]},${sa.toFixed(3)})`
          ctx.beginPath()
          ctx.arc(s.x, s.y, s.size * (0.8 + (1 - p) * 0.6), 0, Math.PI * 2)
          ctx.fill()
        }
      } else if (sparks.length > 0) {
        sparks.splice(0, Math.ceil(sparks.length * 0.10))
      }

      // ══════════════════════════════════════════════════════════════════════
      // 4. MAIN PARTICLE LAYERS
      // ══════════════════════════════════════════════════════════════════════
      const pScale = q.particleScale
      const layer = (
        parts: P[],
        color: [number,number,number],
        dir: number,
        turb: number,
        aScale = 1,
      ) => {
        const n = Math.floor(parts.length * pScale)
        for (let li = 0; li < n; li++) {
          const p = parts[li]
          p.a += p.s * spin * dir
          const wob = Math.sin(p.a * 5 + t * 0.038 + p.r * 7) * (turb * (1 + goldMix * 0.85))
          const rr = R * (p.r + wob)
          const x  = cx + Math.cos(p.a) * rr, y  = cy + Math.sin(p.a) * rr * 0.95
          const tx = cx + Math.cos(p.a + p.len) * rr, ty = cy + Math.sin(p.a + p.len) * rr * 0.95
          const fl = 0.35 + Math.abs(Math.sin(p.a * 3 + t * 0.07)) * 0.65
          const a  = (0.12 + fl * 0.54) * (0.58 + energy * 0.42) * aScale
          ctx.strokeStyle = `rgba(${color[0]},${color[1]},${color[2]},${a.toFixed(3)})`
          ctx.lineWidth = p.w * (0.70 + energy * 0.50)
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(tx, ty); ctx.stroke()
        }
      }

      layer(goldP,  GOLD, 1, 0.060)
      layer(outer, mixRGB(TEAL, GOLD, goldMix), -1, 0.090)
      layer(coreP, BLUE, 1, 0.040)
      if (ringMix > 0.04) layer(streak, mixRGB(TEAL, GOLD, 0.28), 1, 0.020, ringMix * 0.75)

      // ══════════════════════════════════════════════════════════════════════
      // 5. WAVE RIBBON MESH (TALKING)
      // Each ribbon traces a closed sinusoidal path: r = baseR + amp·sin(freq·θ + phase)
      // Multiple overlapping ribbons create the flowing mesh visible in the reference.
      // ══════════════════════════════════════════════════════════════════════
      if (ringMix > 0.04) {
        const SEGS = q.ribbonSegs
        for (const rib of ribbons) {
          const col   = mixRGB(TEAL, GOLD, rib.cm)
          const alpha = (0.26 + energy * 0.14) * ringMix
          ctx.strokeStyle = `rgba(${col[0]},${col[1]},${col[2]},${alpha.toFixed(3)})`
          ctx.lineWidth   = (rib.cm > 0.5 ? 1.4 : 0.8) + energy * 0.5
          ctx.beginPath()
          for (let i = 0; i <= SEGS; i++) {
            const theta = (i / SEGS) * Math.PI * 2
            const ph    = t * 0.007 * rib.dir + rib.phOff
            const r     = R * (rib.baseR + rib.amp * Math.sin(rib.freq * theta + ph))
            const x     = cx + Math.cos(theta) * r
            const y     = cy + Math.sin(theta) * r * 0.95
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
          }
          ctx.closePath()
          ctx.stroke()
        }
      }

      // ══════════════════════════════════════════════════════════════════════
      // 6. INNER GOLD RING (TALKING)
      // The reference shows a bright, solid orange/gold ring — the most
      // prominent visual feature in the TALKING state.
      // ══════════════════════════════════════════════════════════════════════
      if (ringMix > 0.03) {
        // Innermost bright ring (solid, full circle)
        ctx.shadowColor = 'rgba(255,180,0,0.95)'
        ctx.shadowBlur  = glow ? 48 * ringMix : 0
        ctx.lineWidth   = 4.8 * ringMix
        ctx.strokeStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.92 * ringMix).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.60, 0, Math.PI * 2); ctx.stroke()

        // Second ring (slightly wider, dimmer)
        ctx.shadowBlur  = glow ? 28 * ringMix : 0
        ctx.lineWidth   = 2.2 * ringMix
        ctx.strokeStyle = `rgba(255,200,80,${(0.58 * ringMix).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.68, 0, Math.PI * 2); ctx.stroke()
        ctx.shadowBlur  = 0

        // Animated sweep arc (outer orbit)
        const gs = (t * 0.014) % (Math.PI * 2)
        ctx.lineWidth   = 2.4
        ctx.strokeStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.58 * ringMix).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.92, gs + 0.28, gs + Math.PI * 2 - 0.28); ctx.stroke()

        // Faint outer boundary ring
        ctx.lineWidth   = 1.0
        ctx.strokeStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.20 * ringMix).toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 1.12, 0, Math.PI * 2); ctx.stroke()
      }

      // ══════════════════════════════════════════════════════════════════════
      // 7. EQ BARS (TALKING — left teal / right gold)
      // ══════════════════════════════════════════════════════════════════════
      if (ringMix > 0.06) {
        const bars = 22, bw = 3.0, bg = 2.0, maxH = R * 0.55
        for (let i = 0; i < bars; i++) {
          const h  = maxH * (0.08 + Math.abs(Math.sin(t * 0.10 + i * 0.35 + 0.4))) * ringMix
          ctx.fillStyle = `rgba(${TEAL[0]},${TEAL[1]},${TEAL[2]},${(0.72 * ringMix).toFixed(3)})`
          ctx.fillRect(cx - R * 1.25 - (bw + bg) * (bars - i), cy - h / 2, bw, h)
          ctx.fillStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.72 * ringMix).toFixed(3)})`
          ctx.fillRect(cx + R * 1.25 + (bw + bg) * i,          cy - h / 2, bw, h)
        }
      }

      // ══════════════════════════════════════════════════════════════════════
      // 8. THINKING: blue inner ring glowing through the particle cloud
      // ══════════════════════════════════════════════════════════════════════
      if (goldMix > 0.42) {
        const bra = Math.min(1, (goldMix - 0.42) / 0.58) * 0.68
        ctx.shadowColor = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${(bra * 0.85).toFixed(3)})`
        ctx.shadowBlur  = glow ? 22 : 0
        ctx.lineWidth   = 2.8
        ctx.strokeStyle = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${bra.toFixed(3)})`
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.50, 0, Math.PI * 2); ctx.stroke()
        ctx.shadowBlur  = 0
      }

      // ══════════════════════════════════════════════════════════════════════
      // 9. CENTRAL GLOW + NUCLEUS
      // ══════════════════════════════════════════════════════════════════════
      const glowC = goldMix > 0.52
        ? mixRGB([220, 130, 40], ORANGE, Math.min(1, (goldMix - 0.52) / 0.48))
        : mixRGB([88, 148, 235], [165, 210, 255], coreMix)
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.64)
      g.addColorStop(0,    `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${(0.42 + coreMix * 0.40).toFixed(3)})`)
      g.addColorStop(0.45, 'rgba(50,110,210,0.07)')
      g.addColorStop(1,    'rgba(0,0,0,0)')
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * 0.64, 0, Math.PI * 2); ctx.fill()

      // Glowing nucleus — multi-stop radial gradient for the bright orb centre
      const nc = goldMix > 0.65 ? ORANGE : BLUE
      const ng = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.24)
      ng.addColorStop(0,   `rgba(255,255,255,${(0.85 + coreMix * 0.15).toFixed(3)})`)
      ng.addColorStop(0.3, `rgba(${Math.min(255,nc[0]+50)},${Math.min(255,nc[1]+50)},${Math.min(255,nc[2]+50)},${(0.60 + coreMix * 0.25).toFixed(3)})`)
      ng.addColorStop(0.7, `rgba(${nc[0]},${nc[1]},${nc[2]},${(0.20 + coreMix * 0.22).toFixed(3)})`)
      ng.addColorStop(1,   'rgba(0,0,0,0)')
      ctx.fillStyle = ng; ctx.beginPath(); ctx.arc(cx, cy, R * 0.24, 0, Math.PI * 2); ctx.fill()
    }
    draw()

    return () => { cancelAnimationFrame(rafRef.current); window.removeEventListener('resize', resize) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}

// ── Draggable panel wrapper ───────────────────────────────────────────────────
type PanelPos = { x: number; y: number }

function DraggablePanel({
  id, title, defaultX, defaultY, width = 290, color = 'rgba(45,226,197,0.30)',
  syncDot, syncDotOk, onHeaderExtra, children,
}: {
  id: string; title: string; defaultX: number; defaultY: number; width?: number
  color?: string; syncDot?: boolean; syncDotOk?: boolean
  onHeaderExtra?: React.ReactNode; children: React.ReactNode
}) {
  const [pos,  setPos]  = useState<PanelPos>({ x: defaultX, y: defaultY })
  const [open, setOpen] = useState(true)
  const dragRef = useRef<{ sp: PanelPos; sm: { x: number; y: number } } | null>(null)

  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(PANEL_POS_KEY) || '{}')
      if (saved[id]) { setPos(saved[id]); return }
      if (defaultX < 0) setPos({ x: Math.max(0, window.innerWidth + defaultX), y: defaultY })
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  const savePos = useCallback((p: PanelPos) => {
    try {
      const all = JSON.parse(localStorage.getItem(PANEL_POS_KEY) || '{}')
      all[id] = p; localStorage.setItem(PANEL_POS_KEY, JSON.stringify(all))
    } catch { /* ignore */ }
  }, [id])

  const onPD = (e: React.PointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('button,input,select,textarea')) return
    dragRef.current = { sp: { ...pos }, sm: { x: e.clientX, y: e.clientY } }
    e.currentTarget.setPointerCapture(e.pointerId)
    ;(e.currentTarget as HTMLDivElement).style.cursor = 'grabbing'
  }
  const onPM = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return
    const nx = dragRef.current.sp.x + (e.clientX - dragRef.current.sm.x)
    const ny = dragRef.current.sp.y + (e.clientY - dragRef.current.sm.y)
    setPos({
      x: Math.max(0, Math.min((typeof window !== 'undefined' ? window.innerWidth  : 1920) - width,   nx)),
      y: Math.max(0, Math.min((typeof window !== 'undefined' ? window.innerHeight : 1080) - 54, ny)),
    })
  }
  const onPU = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current) { savePos(pos); dragRef.current = null }
    ;(e.currentTarget as HTMLDivElement).style.cursor = 'grab'
  }

  return (
    <div style={{ position: 'fixed', left: pos.x, top: pos.y, width, zIndex: 15, fontFamily: 'monospace', animation: 'sox-widget-in 0.35s ease both' }}>
      <div
        onPointerDown={onPD} onPointerMove={onPM} onPointerUp={onPU}
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '5px 10px', cursor: 'grab', userSelect: 'none',
          background: 'rgba(4,10,22,0.94)', border: `1px solid ${color}`,
          borderBottom: open ? '1px solid rgba(255,255,255,0.06)' : `1px solid ${color}`,
          borderRadius: open ? '10px 10px 0 0' : 10, backdropFilter: 'blur(16px)',
        }}
      >
        <span style={{ fontSize: 10, letterSpacing: '0.16em', color: '#7df3dd', display: 'flex', alignItems: 'center', gap: 7 }}>
          <span style={{ fontSize: 14, opacity: 0.45 }}>⠿</span>
          {title}
          {syncDot !== undefined && (
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: syncDotOk ? '#4ade80' : '#f59e0b',
              boxShadow: `0 0 6px ${syncDotOk ? '#4ade80' : '#f59e0b'}`,
              animation: 'jarvis-pulse 1.4s ease-in-out infinite', display: 'inline-block',
            }} />
          )}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {onHeaderExtra}
          <button onClick={() => setOpen(v => !v)} style={{ background: 'none', border: 'none', color: '#5fa8c9', cursor: 'pointer', fontSize: 12, padding: '1px 4px', lineHeight: 1 }}>
            {open ? '▲' : '▼'}
          </button>
        </div>
      </div>
      {open && (
        <div style={{
          background: 'rgba(3,7,17,0.92)', border: `1px solid ${color}`, borderTop: 'none',
          borderRadius: '0 0 10px 10px', backdropFilter: 'blur(16px)',
          maxHeight: 'calc(100vh - 130px)', overflowY: 'auto',
          padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 7,
          boxShadow: `0 10px 40px rgba(0,0,0,0.55)`,
        }}>
          {children}
        </div>
      )}
    </div>
  )
}

// ── Goals & Todos panel (OpenHuman-style goal system + kanban) ──────────────
interface SoxGoal { id: number; title: string; detail?: string; status: string; scope?: string; priority: number; progress: number; token_budget?: number; spent_tokens?: number }
interface SoxTodo { id: number; goal_id?: number; title: string; status: string; created_by: string; needs_approval: boolean; approval_mode?: string }
interface SoxActivity { id: number; kind: string; task_name?: string; decision?: string; state: string; summary?: string; created_at?: string }

const TODO_COLS: { key: string; label: string }[] = [
  { key: 'todo', label: 'To Do' },
  { key: 'in_progress', label: 'Doing' },
  { key: 'awaiting_approval', label: 'Approve' },
  { key: 'done', label: 'Done' },
]
// Quick-move targets shown per card (excludes the card's current column).
const MOVE_TARGETS: { key: string; label: string; bg: string; fg: string }[] = [
  { key: 'todo', label: '◀', bg: 'rgba(255,255,255,0.06)', fg: '#94a3b8' },
  { key: 'in_progress', label: '▶', bg: 'rgba(96,165,250,0.14)', fg: '#93c5fd' },
  { key: 'done', label: '✓', bg: 'rgba(74,222,128,0.14)', fg: '#4ade80' },
]

function GoalsTodosPanel({ sessionKey }: { sessionKey: string }) {
  const [goals, setGoals] = useState<SoxGoal[]>([])
  const [todos, setTodos] = useState<SoxTodo[]>([])
  const [activity, setActivity] = useState<SoxActivity[]>([])
  const [newGoal, setNewGoal] = useState('')
  const [newTodo, setNewTodo] = useState('')
  const [idleThinking, setIdleThinking] = useState(false)

  const load = useCallback(async () => {
    try {
      const [g, t, a] = await Promise.all([
        apiClient.jarvis.getGoals(sessionKey),
        apiClient.jarvis.getTodos(sessionKey),
        apiClient.jarvis.activityFeed(8),
      ])
      setGoals(g.data?.goals || [])
      setTodos(t.data?.todos || [])
      setActivity(a.data?.items || [])
    } catch { /* backend offline — stay quiet */ }
  }, [sessionKey])

  useEffect(() => {
    load()
    const id = setInterval(load, 15000)
    return () => clearInterval(id)
  }, [load])

  // Idle status poll → drives the "thinking" chip
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try { const r = await apiClient.jarvis.idleStatus(); if (alive) setIdleThinking(!!r.data?.thinking) } catch { /* noop */ }
    }
    poll()
    const id = setInterval(poll, 6000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const activeGoal = goals.find(g => g.status === 'active') || goals[0]

  const addGoal = async () => {
    const title = newGoal.trim(); if (!title) return
    setNewGoal('')
    try { await apiClient.jarvis.createGoal({ title, session_key: sessionKey, scope: 'thread' }); load() } catch { /* noop */ }
  }
  const addTodo = async () => {
    const title = newTodo.trim(); if (!title) return
    setNewTodo('')
    try { await apiClient.jarvis.createTodo({ title, session_key: sessionKey, goal_id: activeGoal?.id }); load() } catch { /* noop */ }
  }
  const moveTodo = async (t: SoxTodo, status: string) => {
    try { await apiClient.jarvis.updateTodo(t.id, { status }); load() } catch { /* noop */ }
  }
  const runIdle = async () => {
    try { await apiClient.jarvis.idleRun(sessionKey); setTimeout(load, 800) } catch { /* noop */ }
  }
  const reflect = async () => {
    try { await apiClient.jarvis.reflectGoals(); setTimeout(load, 600) } catch { /* noop */ }
  }

  const inp: React.CSSProperties = { flex: 1, background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(45,226,197,0.22)', borderRadius: 8, padding: '7px 10px', color: '#e2e8f0', fontSize: 12, fontFamily: 'monospace', outline: 'none' }
  const chip: React.CSSProperties = { fontSize: 9, padding: '2px 7px', borderRadius: 6, letterSpacing: '0.08em', fontFamily: 'monospace' }
  const pct = Math.round((activeGoal?.progress || 0) * 100)
  const budgetPct = activeGoal?.token_budget ? Math.min(100, Math.round(((activeGoal.spent_tokens || 0) / activeGoal.token_budget) * 100)) : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
      {/* Active goal */}
      <div style={{ background: 'rgba(6,14,26,0.88)', border: '1px solid rgba(250,204,21,0.30)', borderRadius: 10, padding: '9px 11px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 9, color: '#fde68a', letterSpacing: '0.14em' }}>ACTIVE GOAL</span>
          <span style={{ ...chip, background: idleThinking ? 'rgba(250,204,21,0.18)' : 'rgba(255,255,255,0.05)', color: idleThinking ? '#fde047' : '#64748b' }}>
            {idleThinking ? '◍ thinking…' : 'idle'}
          </span>
        </div>
        {activeGoal ? (
          <>
            <div style={{ fontSize: 13, color: '#e2e8f0', fontWeight: 700, marginTop: 5 }}>🎯 {activeGoal.title}</div>
            <div style={{ height: 5, background: 'rgba(255,255,255,0.08)', borderRadius: 4, marginTop: 7, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${pct}%`, background: 'linear-gradient(90deg,#facc15,#f59e0b)' }} />
            </div>
            {budgetPct != null && (
              <div style={{ fontSize: 8.5, color: budgetPct >= 100 ? '#f87171' : '#64748b', marginTop: 4, fontFamily: 'monospace' }}>
                budget {activeGoal.spent_tokens || 0}/{activeGoal.token_budget} tok{budgetPct >= 100 ? ' · budget-limited' : ''}
              </div>
            )}
          </>
        ) : (
          <div style={{ fontSize: 11, color: '#475569', marginTop: 6 }}>No goal yet. Add one below — JARVIS works it during idle time.</div>
        )}
        <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <input value={newGoal} onChange={e => setNewGoal(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') addGoal() }} placeholder="New goal…" style={inp} />
          <button onClick={runIdle} title="Run one read-only research step now" style={{ background: 'rgba(250,204,21,0.16)', border: '1px solid rgba(250,204,21,0.45)', borderRadius: 8, color: '#fde047', fontSize: 11, padding: '0 10px', cursor: 'pointer', fontFamily: 'monospace' }}>Work</button>
          <button onClick={reflect} title="Reflect: review long-term goals against memory" style={{ background: 'rgba(167,139,250,0.16)', border: '1px solid rgba(167,139,250,0.45)', borderRadius: 8, color: '#c4b5fd', fontSize: 11, padding: '0 10px', cursor: 'pointer', fontFamily: 'monospace' }}>Reflect</button>
        </div>
      </div>

      {/* Kanban */}
      <div style={{ display: 'flex', gap: 6 }}>
        {TODO_COLS.map(col => (
          <div key={col.key} style={{ flex: 1, background: 'rgba(6,14,24,0.7)', border: '1px solid rgba(45,226,197,0.16)', borderRadius: 9, padding: 6, minHeight: 70 }}>
            <div style={{ fontSize: 8.5, color: '#7df3dd', letterSpacing: '0.1em', marginBottom: 5, textAlign: 'center' }}>{col.label.toUpperCase()}</div>
            {todos.filter(t => t.status === col.key).map(t => (
              <div key={t.id} style={{ background: 'rgba(255,255,255,0.04)', border: `1px solid ${t.created_by === 'jarvis' ? 'rgba(250,204,21,0.3)' : 'rgba(255,255,255,0.09)'}`, borderRadius: 7, padding: '5px 6px', marginBottom: 5, fontSize: 10, color: '#cbd5e1', fontFamily: 'monospace' }}>
                <div style={{ lineHeight: 1.3 }}>{t.created_by === 'jarvis' ? '🤖 ' : ''}{t.title}</div>
                <div style={{ display: 'flex', gap: 4, marginTop: 4, flexWrap: 'wrap' }}>
                  {col.key === 'awaiting_approval' ? (
                    <>
                      <button onClick={() => moveTodo(t, 'done')} title="Approve" style={{ ...chip, background: 'rgba(74,222,128,0.14)', color: '#4ade80', border: 'none', cursor: 'pointer' }}>✓ approve</button>
                      <button onClick={() => moveTodo(t, 'rejected')} title="Reject" style={{ ...chip, background: 'rgba(248,113,113,0.14)', color: '#f87171', border: 'none', cursor: 'pointer' }}>✗ reject</button>
                    </>
                  ) : (
                    MOVE_TARGETS.filter(m => m.key !== col.key).map(m => (
                      <button key={m.key} onClick={() => moveTodo(t, m.key)} style={{ ...chip, background: m.bg, color: m.fg, border: 'none', cursor: 'pointer' }}>{m.label}</button>
                    ))
                  )}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        <input value={newTodo} onChange={e => setNewTodo(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') addTodo() }} placeholder="Add a task…" style={inp} />
        <button onClick={addTodo} disabled={!newTodo.trim()} style={{ background: newTodo.trim() ? 'rgba(45,226,197,0.2)' : 'rgba(255,255,255,0.04)', border: '1px solid rgba(45,226,197,0.42)', borderRadius: 8, color: '#7df3dd', fontSize: 12, padding: '0 12px', cursor: newTodo.trim() ? 'pointer' : 'not-allowed', fontFamily: 'monospace' }}>Add</button>
      </div>

      {/* Subconscious activity feed — the "keeps thinking" heartbeat */}
      {activity.length > 0 && (
        <div style={{ background: 'rgba(6,14,24,0.7)', border: '1px solid rgba(167,139,250,0.18)', borderRadius: 9, padding: '7px 9px' }}>
          <div style={{ fontSize: 8.5, color: '#c4b5fd', letterSpacing: '0.12em', marginBottom: 5 }}>🫀 SUBCONSCIOUS</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, maxHeight: 96, overflowY: 'auto' }}>
            {activity.map(a => (
              <div key={a.id} style={{ fontSize: 9.5, color: '#94a3b8', fontFamily: 'monospace', lineHeight: 1.3 }}>
                <span style={{ color: a.state === 'acted' ? '#4ade80' : a.state === 'awaiting_approval' ? '#fbbf24' : '#475569' }}>●</span>{' '}
                {a.summary || a.task_name}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function SoxRoom() {
  const router    = useRouter()
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  // Adaptive graphics — auto-scales the orb & robot to the machine's specs
  // (e.g. Apple M2 8GB → high, M2 Pro 16GB → ultra) and downgrades live if
  // frames drop, so the room never lags.
  const { profileRef: gfxProfileRef, tier: gfxTier, fps: gfxFps, label: gfxLabel } = useAdaptiveQuality()

  // S.O.X live state
  const stateRef = useRef<SoxState>('idle')
  const [soxState, setSoxState] = useState<SoxState>('idle')
  useSoxOrb(canvasRef, stateRef, gfxProfileRef)

  useEffect(() => {
    const sp = { v: false }, th = { v: false }, li = { v: false }, idle = { v: false }
    const recompute = () => {
      const next: SoxState = sp.v ? 'talking' : (th.v || idle.v) ? 'thinking' : li.v ? 'listening' : 'idle'
      stateRef.current = next; setSoxState(next)
    }
    const onMsg = (e: MessageEvent) => {
      if (typeof window !== 'undefined' && e.origin !== window.location.origin) return
      const d = e.data
      if (!d || typeof d !== 'object' || !d.__jarvisPage) return
      if (d.type === 'speak-status')    { sp.v = !!d.speaking;   recompute() }
      if (d.type === 'jarvis-activity') { li.v = !!d.listening; th.v = !!d.thinking; recompute() }
    }
    window.addEventListener('message', onMsg)
    // Idle-worker poll — when JARVIS is researching a goal in the background,
    // the orb glows 'thinking' even while the user is quiet.
    let idleAlive = true
    const pollIdle = async () => {
      try { const r = await apiClient.jarvis.idleStatus(); if (idleAlive) { idle.v = !!r.data?.thinking; recompute() } } catch { /* noop */ }
    }
    pollIdle()
    const idleTimer = setInterval(pollIdle, 6000)
    return () => { idleAlive = false; clearInterval(idleTimer); window.removeEventListener('message', onMsg) }
  }, [])

  // ── Widgets ───────────────────────────────────────────────────────────────
  const [widgets,   setWidgets]   = useState<RoomWidget[]>(DEFAULT_WIDGETS)
  const [tickers,   setTickers]   = useState<Record<string, Ticker>>({})
  const [adderOpen, setAdderOpen] = useState(false)
  const [newSymbol, setNewSymbol] = useState('')
  const [newType,   setNewType]   = useState<WidgetType>('price')
  const [mounted,   setMounted]   = useState(false)

  useEffect(() => {
    setMounted(true)
    try {
      const raw = localStorage.getItem(WIDGETS_KEY)
      if (raw) { const p = JSON.parse(raw); if (Array.isArray(p) && p.length) setWidgets(p) }
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    if (!mounted) return
    try { localStorage.setItem(WIDGETS_KEY, JSON.stringify(widgets)) } catch { /* ignore */ }
  }, [widgets, mounted])

  const priceSymbols    = widgets.filter(w => w.type === 'price').map(w => w.symbol)
  const priceSymbolsKey = priceSymbols.join(',')

  const fetchPrices = useCallback(async (symbols: string[]) => {
    if (!symbols.length) return
    await Promise.all(symbols.map(async (sym) => {
      try {
        const res = await fetch(`https://api.binance.com/api/v3/ticker/24hr?symbol=${sym}`, { signal: AbortSignal.timeout(8000) })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const d = await res.json()
        setTickers(prev => ({ ...prev, [sym]: { symbol: sym, price: parseFloat(d.lastPrice), changePct: parseFloat(d.priceChangePercent), loading: false } }))
      } catch {
        setTickers(prev => ({ ...prev, [sym]: { symbol: sym, price: prev[sym]?.price ?? 0, changePct: prev[sym]?.changePct ?? 0, loading: false, error: true } }))
      }
    }))
  }, [])

  useEffect(() => {
    if (!priceSymbolsKey) return
    const syms = priceSymbolsKey.split(',')
    fetchPrices(syms)
    const id = setInterval(() => fetchPrices(syms), 5000)
    return () => clearInterval(id)
  }, [priceSymbolsKey, fetchPrices])

  const addWidget    = useCallback(() => {
    const sym = normalizeSymbol(newSymbol)
    if (!sym) return
    setWidgets(prev => [...prev, { id: `w-${Date.now()}-${Math.random().toString(36).slice(2,6)}`, type: newType, symbol: sym }])
    setNewSymbol(''); setAdderOpen(false)
  }, [newSymbol, newType])
  const removeWidget = useCallback((id: string) => setWidgets(prev => prev.filter(w => w.id !== id)), [])

  const fmtPrice = (n: number) => {
    if (!isFinite(n) || n === 0) return '—'
    if (n >= 1000) return n.toLocaleString('en-US', { maximumFractionDigits: 2 })
    if (n >= 1)    return n.toLocaleString('en-US', { maximumFractionDigits: 4 })
    return n.toLocaleString('en-US', { maximumFractionDigits: 8 })
  }
  const fmtUsd = (n: number) => {
    const v = isFinite(n) ? n : 0
    return `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 2 })}`
  }

  // ── Unified monitor ────────────────────────────────────────────────────────
  const [monitor,    setMonitor]    = useState<UnifiedMonitor | null>(null)
  const [monitorErr, setMonitorErr] = useState(false)
  const refreshRef = useRef<(sync?: boolean) => void>(() => {})

  const refreshMonitor = useCallback(async (sync?: boolean) => {
    try {
      const res = await apiClient.jarvis.unifiedMonitor(sync)
      setMonitor(res.data as UnifiedMonitor); setMonitorErr(false)
    } catch { setMonitorErr(true) }
  }, [])
  refreshRef.current = refreshMonitor

  useEffect(() => {
    refreshMonitor(true)
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      refreshMonitor(false)
    }, 4000)
    return () => clearInterval(id)
  }, [refreshMonitor])

  // ── Positions table ────────────────────────────────────────────────────────
  const [posTab,  setPosTab]  = useState<'crypto' | 'mt5'>('crypto')
  const [posPage, setPosPage] = useState(0)
  const POS_PER_PAGE = 5

  // ── System stats ───────────────────────────────────────────────────────────
  const [sysStats, setSysStats] = useState<SystemStats | null>(null)
  useEffect(() => {
    let alive = true
    const pull = async () => {
      if (typeof document !== 'undefined' && document.hidden) return
      try { const res = await apiClient.jarvis.systemStats(); if (alive) setSysStats(res.data as SystemStats) } catch { /* keep */ }
    }
    pull(); const id = setInterval(pull, 3000); return () => { alive = false; clearInterval(id) }
  }, [])

  // ── Execute command ────────────────────────────────────────────────────────
  const runCommand = useCallback(async (command: string, exchange?: string) => {
    try {
      const res = await apiClient.jarvis.executeCommand(command, exchange)
      const d = res.data
      if (d?.speech) soxSpeak(d.speech)
      setTimeout(() => refreshRef.current?.(true), 1200)
      return d
    } catch { soxSpeak('I could not execute that instruction, Sir.'); return null }
  }, [])

  // ── Volume divergence watch ────────────────────────────────────────────────
  const [alerts, setAlerts] = useState<VolumeAlert[]>([])
  const alertCooldownRef = useRef<Record<string, number>>({})
  const dismissedRef     = useRef<Record<string, number>>({})

  const holdings = (() => {
    const out: { exchange: string; symbol: string; side: string }[] = []
    for (const p of monitor?.crypto_positions || [])
      if (p.symbol && p.exchange) out.push({ exchange: p.exchange, symbol: p.symbol, side: p.side })
    return out
  })()
  const holdingsKey = holdings.map(h => `${h.exchange}|${h.symbol}|${h.side}`).join(',')

  const dismissAlert   = useCallback((id: string, sym?: string) => {
    if (sym) dismissedRef.current[sym] = Date.now() + 10 * 60_000
    setAlerts(prev => prev.filter(a => a.id !== id))
  }, [])
  const acceptSl = useCallback(async (a: VolumeAlert) => {
    soxSpeak(`Setting a protective stop on ${splitPair(a.symbol).base} at ${a.suggestedSl}, Sir.`)
    await runCommand(`set stop loss at ${a.suggestedSl} on ${a.symbol}`, a.exchange)
    setAlerts(prev => prev.filter(x => x.id !== a.id))
  }, [runCommand])
  const closeFromAlert = useCallback(async (a: VolumeAlert) => {
    soxSpeak(`Closing your ${splitPair(a.symbol).base} position now, Sir.`)
    await runCommand(`close ${a.symbol}`, a.exchange)
    setAlerts(prev => prev.filter(x => x.id !== a.id))
  }, [runCommand])

  useEffect(() => {
    if (!holdingsKey) return
    let cancelled = false
    const scan = async () => {
      const list = holdingsKey.split(',').map(s => { const [exchange, symbol, side] = s.split('|'); return { exchange, symbol, side } })
      for (const { exchange, symbol, side } of list) {
        const now = Date.now()
        if ((alertCooldownRef.current[symbol] || 0) > now - 180_000) continue
        if ((dismissedRef.current[symbol] || 0) > now) continue
        try {
          const res = await apiClient.getOHLCV(exchange, symbol, '1m', 20)
          const rows: { open: number; high: number; low: number; close: number; volume: number }[] = (res.data?.data as any[]) || []
          if (!Array.isArray(rows) || rows.length < 6) continue
          const vols = rows.map(r => Number(r.volume) || 0)
          const last = rows[rows.length - 1]
          const lastVol = vols[vols.length - 1]
          const avg = vols.slice(0,-1).reduce((s,v) => s+v, 0) / Math.max(1, vols.length - 1)
          if (avg <= 0) continue
          const volX = lastVol / avg
          const open = Number(last.open) || 0, close = Number(last.close) || 0
          const movePct = Math.abs(close - open) / (open || 1)
          const long    = isLongSide(side)
          const adverse = long ? close < open : close > open
          if (volX >= 3 && adverse && movePct >= 0.0008) {
            if (cancelled) return
            const recent    = rows.slice(-5)
            const swingLow  = Math.min(...recent.map(r => Number(r.low)  || close))
            const swingHigh = Math.max(...recent.map(r => Number(r.high) || close))
            const sl = long ? +(swingLow * 0.999).toPrecision(6) : +(swingHigh * 1.001).toPrecision(6)
            const base = splitPair(symbol).base
            const dir  = long ? 'selling' : 'buying'
            const message = `Warning, Sir. A sudden surge of ${dir} volume — about ${volX.toFixed(1)} times the average — is pushing against your ${long ? 'long' : 'short'} ${base} position. I suggest a protective stop at ${sl}. Shall I set the stop, close the position, or hold?`
            alertCooldownRef.current[symbol] = now
            soxSpeak(message)
            setAlerts(prev => {
              if (prev.some(x => x.symbol === symbol)) return prev
              return [...prev, { id: `va-${symbol}-${now}`, exchange, symbol, side, message, suggestedSl: sl, volX, createdAt: now }]
            })
          }
        } catch { /* ignore */ }
      }
    }
    scan(); const id = setInterval(scan, 15000); return () => { cancelled = true; clearInterval(id) }
  }, [holdingsKey])

  // Decorative ping
  const [ping, setPing] = useState(4)
  useEffect(() => {
    const id = setInterval(() => setPing(2 + Math.floor(Math.random() * 7)), 1200)
    return () => clearInterval(id)
  }, [])

  // ── Style helpers ──────────────────────────────────────────────────────────
  const pnlColor   = (v: number)   => v > 0 ? '#4ade80' : v < 0 ? '#f87171' : '#94a3b8'
  const usageColor = (pct: number) => pct >= 85 ? '#f87171' : pct >= 60 ? '#f59e0b' : '#4ade80'
  const fmtGB      = (b?: number)  => `${((b ?? 0) / 1073741824).toFixed(1)} GB`
  const bar = (pct: number) => (
    <div style={{ height: 5, background: 'rgba(255,255,255,0.08)', borderRadius: 3, overflow: 'hidden', marginTop: 4 }}>
      <div style={{ height: '100%', width: `${Math.min(100, Math.max(0, pct))}%`, background: usageColor(pct), borderRadius: 3, transition: 'width 0.5s ease', boxShadow: `0 0 8px ${usageColor(pct)}` }} />
    </div>
  )

  // ── Positions data ─────────────────────────────────────────────────────────
  const m          = monitor
  const cryptoRows = (m?.crypto_positions || []).slice().sort((a, b) => a.pnl - b.pnl)
  const mt5Rows    = (m?.mt5_accounts || [])
    .flatMap(acct => acct.positions.map(p => ({ ...p, accountName: acct.name, accountId: acct.account_id })))
    .sort((a, b) => a.profit - b.profit)
  const totalRows  = posTab === 'crypto' ? cryptoRows.length : mt5Rows.length
  const totalPages = Math.max(1, Math.ceil(totalRows / POS_PER_PAGE))
  const page       = Math.min(posPage, totalPages - 1)
  const cryptoPage = cryptoRows.slice(page * POS_PER_PAGE, (page + 1) * POS_PER_PAGE)
  const mt5Page    = mt5Rows.slice(page * POS_PER_PAGE, (page + 1) * POS_PER_PAGE)
  const grandEquity = (m?.mt5_total_equity ?? 0) + (m?.crypto_total_notional ?? 0)
  const grandPnl    = m?.total_pnl ?? 0

  const th: React.CSSProperties = { textAlign: 'left', fontWeight: 600, color: '#5fa8c9', padding: '0 4px 4px', fontSize: 9, letterSpacing: '0.06em' }
  const td: React.CSSProperties = { padding: '5px 4px', fontSize: 11, borderTop: '1px solid rgba(255,255,255,0.05)' }

  const STATE_META: Record<SoxState, { label: string; sub: string; color: string; glow: string }> = {
    idle:      { label: 'STANDBY',   sub: 'Say "S.O.X" to activate.',    color: '#5fa8c9', glow: 'rgba(95,168,201,0.55)'  },
    listening: { label: 'LISTENING', sub: "I'm listening.",               color: '#7df3dd', glow: 'rgba(45,226,197,0.75)'  },
    thinking:  { label: 'THINKING',  sub: 'Analyzing data\u2026',         color: '#f59e0b', glow: 'rgba(245,158,11,0.75)'  },
    talking:   { label: 'TALKING',   sub: 'How can I assist you, Sir?',   color: '#7df3dd', glow: 'rgba(45,226,197,0.75)'  },
  }
  const meta = STATE_META[soxState]

  // ── RENDER ─────────────────────────────────────────────────────────────────
  return (
    <>
      <Head>
        <title>S.O.X Command Room &mdash; TradeBot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      {/* ── Futuristic SVG background ── */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/sox-bg.svg" alt=""
        style={{ position: 'fixed', inset: 0, zIndex: 0, width: '100%', height: '100%', objectFit: 'cover', pointerEvents: 'none' }}
      />

      {/* ── Animated scan-line sweep ── */}
      <div style={{
        position: 'fixed', left: 0, right: 0, top: 0, height: 2, zIndex: 4, pointerEvents: 'none',
        background: 'linear-gradient(90deg, transparent, rgba(45,226,197,0.07) 20%, rgba(45,226,197,0.16) 50%, rgba(45,226,197,0.07) 80%, transparent)',
        animation: 'sox-scan 8s linear infinite',
      }} />

      {/* ── Canvas orb ── */}
      <canvas ref={canvasRef} style={{
        position: 'fixed', inset: 0, zIndex: 1, width: '100%', height: '100%', display: 'block',
        transform: 'translateZ(0)', willChange: 'transform', backfaceVisibility: 'hidden',
      }} />

      {/* ── Rotating reticle rings (4) with tick marks ── */}
      <div style={{ position: 'fixed', left: '50%', top: '44%', transform: 'translate(-50%,-50%)', zIndex: 2, pointerEvents: 'none' }}>
        {([
          { size: 560,  dur: 58,  color: 'rgba(45,226,197,0.30)',  dash: '2 13', rev: false, ticks: true },
          { size: 720,  dur: 92,  color: 'rgba(245,158,11,0.22)',  dash: '1 24', rev: true,  ticks: false },
          { size: 900,  dur: 135, color: 'rgba(96,165,250,0.17)',  dash: '3 42', rev: false, ticks: false },
          { size: 1080, dur: 200, color: 'rgba(45,226,197,0.09)',  dash: '1 32', rev: true,  ticks: false },
        ] as const).map((r, i) => (
          <svg key={i} width={r.size} height={r.size} viewBox={`0 0 ${r.size} ${r.size}`}
            style={{ position: 'absolute', left: -r.size/2, top: -r.size/2, animation: `jarvis-spin ${r.dur}s linear infinite${r.rev ? ' reverse' : ''}` }}>
            <circle cx={r.size/2} cy={r.size/2} r={r.size/2-4} fill="none" stroke={r.color} strokeWidth={1.5} strokeDasharray={r.dash} />
            {r.ticks && [0, 90, 180, 270].map(deg => {
              const rad = deg * Math.PI / 180
              const c = r.size / 2, R2 = r.size / 2 - 4
              const x1 = c + Math.cos(rad) * (R2 - 10), y1 = c + Math.sin(rad) * (R2 - 10)
              const x2 = c + Math.cos(rad) * (R2 + 10), y2 = c + Math.sin(rad) * (R2 + 10)
              return <line key={deg} x1={x1} y1={y1} x2={x2} y2={y2} stroke="rgba(45,226,197,0.55)" strokeWidth="2.5" />
            })}
          </svg>
        ))}
      </div>

      {/* ── Corner HUD brackets ── */}
      {([
        { top: 14, left: 14,   rot: 0   },
        { top: 14, right: 14,  rot: 90  },
        { bottom: 14, right: 14, rot: 180 },
        { bottom: 14, left: 14,  rot: 270 },
      ] as const).map((c, i) => (
        <div key={i} style={{
          position: 'fixed', zIndex: 3, width: 54, height: 54, pointerEvents: 'none',
          borderTop: '2px solid rgba(45,226,197,0.55)', borderLeft: '2px solid rgba(45,226,197,0.55)',
          transform: `rotate(${c.rot}deg)`,
          ...('top' in c    ? { top: c.top }       : {}),
          ...('bottom' in c ? { bottom: c.bottom }  : {}),
          ...('left' in c   ? { left: c.left }       : {}),
          ...('right' in c  ? { right: c.right }     : {}),
        }}>
          <div style={{ position: 'absolute', top: 10, left: 10, width: 8, height: 8, background: 'rgba(45,226,197,0.55)' }} />
        </div>
      ))}

      {/* ── S.O.X title plate ── */}
      <div style={{ position: 'fixed', left: '50%', top: 18, transform: 'translateX(-50%)', zIndex: 5, textAlign: 'center', pointerEvents: 'none', fontFamily: 'monospace' }}>
        <div style={{ fontSize: 26, fontWeight: 900, letterSpacing: '0.55em', color: '#7df3dd', textShadow: '0 0 22px rgba(45,226,197,0.8), 0 0 48px rgba(45,226,197,0.28)', paddingLeft: '0.55em' }}>
          S.O.X
        </div>
        <div style={{ fontSize: 9, letterSpacing: '0.24em', color: '#5fa8c9', marginTop: 5, opacity: 0.82 }}>
          STRATEGIC OPERATIONS EXCHANGE &middot; AUTONOMOUS COMMAND
        </div>
      </div>

      {/* ── State label under the orb ── */}
      <div style={{ position: 'fixed', left: '50%', top: '68%', transform: 'translateX(-50%)', zIndex: 5, textAlign: 'center', pointerEvents: 'none', fontFamily: 'monospace', transition: 'color 0.4s' }}>
        <div style={{ fontSize: 36, fontWeight: 900, letterSpacing: '0.55em', color: '#7df3dd', textShadow: '0 0 26px rgba(45,226,197,0.75)', paddingLeft: '0.55em' }}>
          S.O.X
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 14, marginTop: 12 }}>
          <span style={{ width: 44, height: 1, background: `linear-gradient(90deg, transparent, ${meta.glow})` }} />
          <span style={{ fontSize: 14, fontWeight: 700, letterSpacing: '0.36em', color: meta.color, textShadow: `0 0 16px ${meta.glow}`, paddingLeft: '0.36em', animation: soxState === 'thinking' ? 'jarvis-flicker 1.1s ease-in-out infinite' : undefined }}>
            {meta.label}
          </span>
          <span style={{ width: 44, height: 1, background: `linear-gradient(90deg, ${meta.glow}, transparent)` }} />
        </div>
        <div style={{ fontSize: 13, color: meta.color, marginTop: 11, opacity: 0.85, letterSpacing: '0.04em' }}>
          {meta.sub}
        </div>
      </div>

      {/* ── Back button ── */}
      <button
        onClick={() => router.push('/')}
        style={{ position: 'fixed', top: 16, left: 70, zIndex: 20, display: 'flex', alignItems: 'center', gap: 8, background: 'rgba(6,14,26,0.85)', border: '1px solid rgba(45,226,197,0.38)', borderRadius: 10, padding: '8px 16px', color: '#7df3dd', fontFamily: 'monospace', fontSize: 12, letterSpacing: '0.06em', cursor: 'pointer', backdropFilter: 'blur(10px)', transition: 'all 0.2s' }}
        onMouseEnter={e => (e.currentTarget.style.background = 'rgba(45,226,197,0.16)')}
        onMouseLeave={e => (e.currentTarget.style.background = 'rgba(6,14,26,0.85)')}
      >
        &larr; Back to App
      </button>

      {/* ═══ DRAGGABLE — LIVE MARKETS ═══ */}
      <DraggablePanel
        id="sox-markets" title="LIVE MARKETS" defaultX={16} defaultY={76} width={278}
        onHeaderExtra={
          <button onClick={() => setAdderOpen(v => !v)} style={{ background: 'rgba(45,226,197,0.14)', border: '1px solid rgba(45,226,197,0.42)', borderRadius: 7, color: '#7df3dd', fontSize: 10, padding: '2px 9px', cursor: 'pointer' }}>
            + Widget
          </button>
        }
      >
        {adderOpen && (
          <div style={{ background: 'rgba(6,14,24,0.9)', border: '1px solid rgba(45,226,197,0.28)', borderRadius: 10, padding: 10, display: 'flex', flexDirection: 'column', gap: 7 }}>
            <div style={{ display: 'flex', gap: 6 }}>
              {(['price', 'chart'] as WidgetType[]).map(tp => (
                <button key={tp} onClick={() => setNewType(tp)} style={{ flex: 1, padding: '6px 0', borderRadius: 8, cursor: 'pointer', fontFamily: 'monospace', fontSize: 11, background: newType === tp ? 'rgba(45,226,197,0.22)' : 'rgba(255,255,255,0.04)', border: `1px solid ${newType === tp ? 'rgba(45,226,197,0.6)' : 'rgba(255,255,255,0.1)'}`, color: newType === tp ? '#7df3dd' : '#94a3b8' }}>
                  {tp === 'price' ? 'Price' : 'Chart'}
                </button>
              ))}
            </div>
            <input value={newSymbol} onChange={e => setNewSymbol(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') addWidget() }} placeholder="e.g. ADA, LINK, BTCUSDT"
              style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(45,226,197,0.22)', borderRadius: 8, padding: '8px 10px', color: '#e2e8f0', fontSize: 12, fontFamily: 'monospace', outline: 'none' }}
            />
            <button onClick={addWidget} disabled={!newSymbol.trim()} style={{ background: newSymbol.trim() ? 'rgba(45,226,197,0.22)' : 'rgba(255,255,255,0.04)', border: '1px solid rgba(45,226,197,0.45)', borderRadius: 8, padding: '7px 0', color: '#7df3dd', fontFamily: 'monospace', fontSize: 12, cursor: newSymbol.trim() ? 'pointer' : 'not-allowed' }}>
              Add
            </button>
          </div>
        )}

        {widgets.map(w => {
          const { base, quote } = splitPair(w.symbol)
          if (w.type === 'price') {
            const tk = tickers[w.symbol]
            const up = (tk?.changePct ?? 0) >= 0
            return (
              <div key={w.id} style={{ position: 'relative', background: 'rgba(6,14,26,0.88)', border: `1px solid ${up ? 'rgba(74,222,128,0.28)' : 'rgba(248,113,113,0.28)'}`, borderRadius: 10, padding: '9px 12px', backdropFilter: 'blur(8px)', fontFamily: 'monospace', boxShadow: '0 2px 12px rgba(0,0,0,0.4)' }}>
                <button onClick={() => removeWidget(w.id)} style={{ position: 'absolute', top: 6, right: 8, background: 'none', border: 'none', color: '#475569', cursor: 'pointer', fontSize: 13, lineHeight: 1 }}>&#x2715;</button>
                <div style={{ fontSize: 10, color: '#7df3dd', letterSpacing: '0.10em' }}>{base}<span style={{ color: '#475569' }}>/{quote}</span></div>
                <div style={{ fontSize: 19, fontWeight: 700, color: '#e2e8f0', marginTop: 2 }}>{tk && !tk.loading ? `$${fmtPrice(tk.price)}` : '\u00B7\u00B7\u00B7'}</div>
                <div style={{ fontSize: 11, marginTop: 2, color: tk?.error ? '#f59e0b' : up ? '#4ade80' : '#f87171' }}>
                  {tk?.error ? 'unavailable' : tk ? `${up ? '▲' : '▼'} ${Math.abs(tk.changePct).toFixed(2)}%` : 'loading\u2026'}
                </div>
              </div>
            )
          }
          return (
            <div key={w.id} style={{ position: 'relative', background: 'rgba(6,14,26,0.9)', border: '1px solid rgba(96,165,250,0.28)', borderRadius: 10, overflow: 'hidden', height: 200 }}>
              <button onClick={() => removeWidget(w.id)} style={{ position: 'absolute', top: 4, right: 6, zIndex: 2, background: 'rgba(0,0,0,0.5)', border: 'none', color: '#cbd5e1', cursor: 'pointer', fontSize: 13, borderRadius: 6, padding: '0 6px', lineHeight: '18px' }}>&#x2715;</button>
              <div style={{ position: 'absolute', top: 4, left: 8, zIndex: 2, fontFamily: 'monospace', fontSize: 10, color: '#93c5fd', letterSpacing: '0.08em' }}>{base}/{quote}</div>
              <div style={{ width: '100%', height: '100%' }}>
                <TradingViewWidget symbol={`${base}/${quote}`} exchange="binance" timeframe="1h" />
              </div>
            </div>
          )
        })}
      </DraggablePanel>

      {/* ═══ DRAGGABLE — FACE VISION ═══ */}
      <DraggablePanel
        id="sox-facevision" title="FACE VISION" defaultX={16} defaultY={430} width={300}
        color="rgba(6,182,212,0.34)"
      >
        <FaceVisionPanel />
      </DraggablePanel>

      {/* ═══ DRAGGABLE — GOALS & TODOS ═══ */}
      <DraggablePanel
        id="sox-goals" title="GOALS &amp; TODOS" defaultX={-360} defaultY={430} width={342}
        color="rgba(250,204,21,0.32)"
      >
        <GoalsTodosPanel sessionKey="default" />
      </DraggablePanel>

      {/* ═══ DRAGGABLE — ACCOUNTS · LIVE ═══ */}
      <DraggablePanel
        id="sox-accounts" title="ACCOUNTS · LIVE" defaultX={-344} defaultY={76} width={326}
        color="rgba(96,165,250,0.30)" syncDot syncDotOk={!monitorErr}
        onHeaderExtra={
          <button onClick={() => refreshMonitor(true)} style={{ background: 'rgba(45,226,197,0.14)', border: '1px solid rgba(45,226,197,0.4)', borderRadius: 7, color: '#7df3dd', fontSize: 10, padding: '2px 8px', cursor: 'pointer' }}>
            Sync
          </button>
        }
      >
        {/* System stats */}
        <div style={{ background: 'rgba(14,8,30,0.85)', border: '1px solid rgba(124,58,237,0.38)', borderRadius: 10, padding: '9px 11px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 9, color: '#c4b5fd', letterSpacing: '0.14em' }}>SYSTEM &middot; CPU / RAM</span>
            <span style={{ fontSize: 9, color: '#64748b' }}>{sysStats?.available ? `${sysStats.cpu_count} cores` : 'n/a'}</span>
          </div>
          {/* Adaptive graphics tier — auto-tuned to keep the room smooth */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 7, fontSize: 11 }}>
            <span style={{ color: '#94a3b8' }}>GFX</span>
            <span style={{ color: gfxTierColor(gfxTier), fontWeight: 700, fontFamily: 'monospace' }}>
              {gfxTier.toUpperCase()} &middot; {gfxFps || '\u00B7\u00B7'} fps
            </span>
          </div>
          {/* gfxLabel embeds CPU cores / RAM read from `navigator`, which differ
              between the server (fallback specs) and client (real hardware). Only
              render it after mount so SSR and the first client render match. */}
          <div style={{ fontSize: 8.5, color: '#64748b', marginTop: 2, fontFamily: 'monospace' }}>
            {mounted ? `${gfxLabel} \u00B7 auto-tuned` : 'auto-tuned'}
          </div>
          {sysStats?.available ? (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 7, fontSize: 11 }}>
                <span style={{ color: '#94a3b8' }}>CPU</span>
                <span style={{ color: usageColor(sysStats.cpu_percent), fontWeight: 700 }}>{sysStats.cpu_percent.toFixed(0)}%</span>
              </div>
              {bar(sysStats.cpu_percent)}
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 7, fontSize: 11 }}>
                <span style={{ color: '#94a3b8' }}>RAM</span>
                <span style={{ color: usageColor(sysStats.mem_percent), fontWeight: 700 }}>
                  {sysStats.mem_percent.toFixed(0)}% &middot; {fmtGB(sysStats.mem_used)}/{fmtGB(sysStats.mem_total)}
                </span>
              </div>
              {bar(sysStats.mem_percent)}
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 9, color: '#64748b' }}>
                <span>App: {(sysStats.proc_cpu_percent ?? 0).toFixed(0)}% CPU &middot; {fmtGB(sysStats.proc_mem)}</span>
                {sysStats.load_percent != null && <span>load {sysStats.load_percent.toFixed(0)}%</span>}
              </div>
            </>
          ) : (
            <div style={{ fontSize: 10, color: '#475569', marginTop: 7 }}>System stats unavailable.</div>
          )}
        </div>

        {/* Total equity */}
        <div style={{ background: 'rgba(6,14,26,0.88)', border: '1px solid rgba(96,165,250,0.32)', borderRadius: 10, padding: '9px 11px' }}>
          <div style={{ fontSize: 9, color: '#7df3dd', letterSpacing: '0.14em' }}>TOTAL EQUITY</div>
          <div style={{ fontSize: 21, fontWeight: 700, color: '#e2e8f0', marginTop: 2 }}>{m ? fmtUsd(grandEquity) : '\u00B7\u00B7\u00B7'}</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 11 }}>
            <span style={{ color: '#64748b' }}>Floating P&amp;L</span>
            <span style={{ color: pnlColor(grandPnl), fontWeight: 700 }}>{m ? fmtUsd(grandPnl) : '\u2014'}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 3, fontSize: 11 }}>
            <span style={{ color: '#64748b' }}>Open positions</span>
            <span style={{ color: '#cbd5e1' }}>{m?.total_position_count ?? 0}</span>
          </div>
        </div>

        {/* MT5 accounts */}
        <div style={{ fontSize: 9, letterSpacing: '0.14em', color: '#5fa8c9' }}>MT5 ACCOUNTS</div>
        {m && m.mt5_accounts.length > 0 ? m.mt5_accounts.map(acct => (
          <div key={acct.account_id} style={{ background: 'rgba(6,14,26,0.85)', border: '1px solid rgba(245,158,11,0.28)', borderRadius: 10, padding: '9px 11px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <span style={{ fontSize: 12, color: '#fcd34d', fontWeight: 700 }}>{acct.name}</span>
              <span style={{ fontSize: 9, color: '#64748b' }}>{acct.login || acct.server}</span>
            </div>
            {[
              ['Balance',      `${fmtUsd(acct.balance)} ${acct.currency}`, false],
              ['Equity',       fmtUsd(acct.equity),                         false],
              ['Floating P&L', fmtUsd(acct.floating_pnl),                   true],
            ].map(([label, val, isFp]) => (
              <div key={String(label)} style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5, fontSize: 11 }}>
                <span style={{ color: '#64748b' }}>{String(label).replace('&L', '\u0026L')}</span>
                <span style={{ color: isFp ? pnlColor(acct.floating_pnl) : '#e2e8f0', fontWeight: isFp ? 700 : 400 }}>{String(val)}</span>
              </div>
            ))}
            <div style={{ fontSize: 10, color: '#64748b', marginTop: 5 }}>{acct.position_count} open position{acct.position_count === 1 ? '' : 's'}</div>
          </div>
        )) : (
          <div style={{ fontSize: 11, color: '#475569', padding: '4px 2px' }}>{m ? 'No MT5 accounts connected.' : 'Loading\u2026'}</div>
        )}

        {/* Crypto summary */}
        <div style={{ fontSize: 9, letterSpacing: '0.14em', color: '#5fa8c9' }}>CRYPTO (FUTURES)</div>
        <div style={{ background: 'rgba(6,14,26,0.85)', border: '1px solid rgba(45,226,197,0.28)', borderRadius: 10, padding: '9px 11px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
            <span style={{ color: '#64748b' }}>Notional</span>
            <span style={{ color: '#e2e8f0' }}>{m ? fmtUsd(m.crypto_total_notional) : '\u00B7\u00B7\u00B7'}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 3, fontSize: 11 }}>
            <span style={{ color: '#64748b' }}>Unrealised P&amp;L</span>
            <span style={{ color: pnlColor(m?.crypto_total_pnl ?? 0), fontWeight: 700 }}>{m ? fmtUsd(m.crypto_total_pnl) : '\u2014'}</span>
          </div>
        </div>

        {/* Active positions */}
        <div style={{ fontSize: 9, letterSpacing: '0.14em', color: '#5fa8c9' }}>ACTIVE POSITIONS</div>
        <div style={{ background: 'rgba(6,14,26,0.88)', border: '1px solid rgba(96,165,250,0.26)', borderRadius: 10, padding: '8px 8px 10px' }}>
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            {(['crypto', 'mt5'] as const).map(tab => {
              const active = posTab === tab
              const count  = tab === 'crypto' ? cryptoRows.length : mt5Rows.length
              return (
                <button key={tab} onClick={() => { setPosTab(tab); setPosPage(0) }} style={{ flex: 1, cursor: 'pointer', fontSize: 11, fontWeight: 700, padding: '5px 0', borderRadius: 8, letterSpacing: '0.08em', background: active ? 'rgba(96,165,250,0.18)' : 'rgba(255,255,255,0.04)', border: `1px solid ${active ? 'rgba(96,165,250,0.55)' : 'rgba(255,255,255,0.1)'}`, color: active ? '#bfdbfe' : '#64748b' }}>
                  {tab === 'crypto' ? 'CRYPTO' : 'MT5'} ({count})
                </button>
              )
            })}
          </div>

          {posTab === 'crypto' ? (
            cryptoRows.length > 0 ? (
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr><th style={th}>Pair</th><th style={th}>Side</th><th style={{ ...th, textAlign: 'right' }}>P&amp;L</th><th style={{ ...th, textAlign: 'right' }} /></tr></thead>
                <tbody>
                  {cryptoPage.map((p, i) => {
                    const long = isLongSide(p.side)
                    return (
                      <tr key={`c-${p.symbol}-${i}`}>
                        <td style={{ ...td, color: '#e2e8f0', fontWeight: 700 }}>{splitPair(p.symbol).base}</td>
                        <td style={{ ...td, color: long ? '#4ade80' : '#f87171' }}>{long ? 'LONG' : 'SHORT'}{p.leverage ? ` ${p.leverage}x` : ''}</td>
                        <td style={{ ...td, textAlign: 'right', color: pnlColor(p.pnl), fontWeight: 700 }}>{fmtUsd(p.pnl)}</td>
                        <td style={{ ...td, textAlign: 'right', whiteSpace: 'nowrap' }}>
                          <button onClick={() => runCommand(`close ${p.symbol}`, p.exchange)} style={{ background: 'rgba(248,113,113,0.16)', border: '1px solid rgba(248,113,113,0.4)', borderRadius: 6, color: '#fca5a5', fontSize: 10, padding: '2px 6px', cursor: 'pointer', marginRight: 4 }}>&#x2715;</button>
                          <button onClick={() => runCommand(`how is ${p.symbol} doing`, p.exchange)} style={{ background: 'rgba(96,165,250,0.16)', border: '1px solid rgba(96,165,250,0.4)', borderRadius: 6, color: '#93c5fd', fontSize: 10, padding: '2px 6px', cursor: 'pointer' }}>&#x27F3;</button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            ) : <div style={{ fontSize: 11, color: '#475569', padding: '6px 2px' }}>{m ? 'No open crypto positions.' : 'Loading\u2026'}</div>
          ) : (
            mt5Rows.length > 0 ? (
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr><th style={th}>Symbol</th><th style={th}>Side</th><th style={{ ...th, textAlign: 'right' }}>P&amp;L</th></tr></thead>
                <tbody>
                  {mt5Page.map((p, i) => {
                    const long = isLongSide(p.side)
                    return (
                      <tr key={`m-${p.accountId}-${p.ticket}-${i}`}>
                        <td style={{ ...td, color: '#e2e8f0', fontWeight: 700 }}>{p.symbol}</td>
                        <td style={{ ...td, color: long ? '#4ade80' : '#f87171' }}>{long ? 'BUY' : 'SELL'} {p.volume}</td>
                        <td style={{ ...td, textAlign: 'right', color: pnlColor(p.profit), fontWeight: 700 }}>{fmtUsd(p.profit)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            ) : <div style={{ fontSize: 11, color: '#475569', padding: '6px 2px' }}>{m ? 'No open MT5 positions.' : 'Loading\u2026'}</div>
          )}

          {totalRows > POS_PER_PAGE && (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 8 }}>
              <button onClick={() => setPosPage(p => Math.max(0, p - 1))} disabled={page <= 0} style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 7, color: page <= 0 ? '#475569' : '#cbd5e1', fontSize: 10, padding: '4px 10px', cursor: page <= 0 ? 'default' : 'pointer' }}>&#x2039; Prev</button>
              <span style={{ fontSize: 10, color: '#64748b' }}>Page {page + 1} / {totalPages}</span>
              <button onClick={() => setPosPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1} style={{ background: 'rgba(45,226,197,0.14)', border: '1px solid rgba(45,226,197,0.4)', borderRadius: 7, color: page >= totalPages - 1 ? '#475569' : '#7df3dd', fontSize: 10, padding: '4px 10px', cursor: page >= totalPages - 1 ? 'default' : 'pointer' }}>Next &#x203A;</button>
            </div>
          )}
        </div>
      </DraggablePanel>

      {/* ── Volume alerts ── */}
      {alerts.length > 0 && (
        <div style={{ position: 'fixed', top: 84, left: '50%', transform: 'translateX(-50%)', zIndex: 40, width: 'min(520px, calc(100vw - 380px))', display: 'flex', flexDirection: 'column', gap: 10, fontFamily: 'monospace' }}>
          {alerts.map(a => (
            <div key={a.id} style={{ background: 'rgba(28,10,10,0.94)', border: '1px solid rgba(248,113,113,0.6)', borderRadius: 14, padding: '14px 16px', backdropFilter: 'blur(12px)', boxShadow: '0 0 32px rgba(248,113,113,0.22)', animation: 'jarvis-flicker 2.4s ease-in-out infinite' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span style={{ fontSize: 16 }}>&#x26A0;</span>
                <span style={{ fontSize: 12, fontWeight: 800, color: '#fca5a5', letterSpacing: '0.18em' }}>VOLUME WARNING &middot; {splitPair(a.symbol).base}</span>
                <span style={{ marginLeft: 'auto', fontSize: 10, color: '#f59e0b' }}>{a.volX.toFixed(1)}&times; avg</span>
              </div>
              <div style={{ fontSize: 12, color: '#fde2e2', lineHeight: 1.5 }}>{a.message}</div>
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <button onClick={() => acceptSl(a)} style={{ flex: 1, background: 'rgba(245,158,11,0.18)', border: '1px solid rgba(245,158,11,0.6)', borderRadius: 9, color: '#fcd34d', fontSize: 11, fontWeight: 700, padding: '7px 0', cursor: 'pointer' }}>Set Stop @ {a.suggestedSl}</button>
                <button onClick={() => closeFromAlert(a)} style={{ flex: 1, background: 'rgba(248,113,113,0.18)', border: '1px solid rgba(248,113,113,0.6)', borderRadius: 9, color: '#fca5a5', fontSize: 11, fontWeight: 700, padding: '7px 0', cursor: 'pointer' }}>Close Position</button>
                <button onClick={() => dismissAlert(a.id, a.symbol)} style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.15)', borderRadius: 9, color: '#94a3b8', fontSize: 11, padding: '7px 12px', cursor: 'pointer' }}>Hold</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Rotating compass rose ── */}
      <div style={{ position: 'fixed', bottom: 42, left: '50%', transform: 'translateX(-50%)', zIndex: 4, pointerEvents: 'none' }}>
        <svg width="74" height="74" viewBox="0 0 74 74" style={{ animation: 'sox-compass 120s linear infinite', display: 'block' }}>
          <circle cx="37" cy="37" r="35" fill="none" stroke="rgba(45,226,197,0.22)" strokeWidth="0.8" strokeDasharray="2 5"/>
          <line x1="37" y1="2"  x2="37" y2="14"  stroke="rgba(45,226,197,0.7)"  strokeWidth="1.6"/>
          <line x1="37" y1="60" x2="37" y2="72"  stroke="rgba(45,226,197,0.38)" strokeWidth="1"/>
          <line x1="2"  y1="37" x2="14" y2="37"  stroke="rgba(45,226,197,0.38)" strokeWidth="1"/>
          <line x1="60" y1="37" x2="72" y2="37"  stroke="rgba(45,226,197,0.38)" strokeWidth="1"/>
          <line x1="9"  y1="9"  x2="15" y2="15"  stroke="rgba(45,226,197,0.22)" strokeWidth="0.7"/>
          <line x1="59" y1="9"  x2="65" y2="15"  stroke="rgba(45,226,197,0.22)" strokeWidth="0.7"/>
          <line x1="9"  y1="65" x2="15" y2="59"  stroke="rgba(45,226,197,0.22)" strokeWidth="0.7"/>
          <line x1="59" y1="65" x2="65" y2="59"  stroke="rgba(45,226,197,0.22)" strokeWidth="0.7"/>
          <path d="M37 19 L33 37 L37 35 L41 37 Z" fill="rgba(45,226,197,0.82)"/>
          <path d="M37 55 L33 37 L37 39 L41 37 Z" fill="rgba(45,226,197,0.25)"/>
          <circle cx="37" cy="37" r="3" fill="none" stroke="rgba(45,226,197,0.52)" strokeWidth="1"/>
          <text x="34" y="12" fontSize="6" fill="rgba(45,226,197,0.65)" fontFamily="monospace">N</text>
        </svg>
      </div>

      {/* ── HUD decorative readouts ── */}
      <div style={{ position: 'fixed', bottom: 14, left: 80, zIndex: 4, pointerEvents: 'none', fontFamily: 'monospace', fontSize: 9, color: 'rgba(45,226,197,0.32)', letterSpacing: '0.08em' }}>
        LAT 26.2041&deg; &middot; LON 28.0473&deg; &middot; ALT 1450m
      </div>
      <div style={{ position: 'fixed', bottom: 14, right: 80, zIndex: 4, pointerEvents: 'none', fontFamily: 'monospace', fontSize: 9, color: 'rgba(45,226,197,0.32)', letterSpacing: '0.08em' }}>
        UPLINK &#x25C6; {ping}ms &middot; SECTOR ALPHA-7 &middot; NOMINAL
      </div>

      {/* ── Bottom status bar ── */}
      <div style={{ position: 'fixed', bottom: 16, left: '50%', transform: 'translateX(-50%)', zIndex: 5, display: 'flex', alignItems: 'center', gap: 8, pointerEvents: 'none', fontFamily: 'monospace', fontSize: 11, color: '#5fa8c9', letterSpacing: '0.12em' }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#4ade80', boxShadow: '0 0 8px #4ade80', animation: 'jarvis-pulse 1.4s ease-in-out infinite' }} />
        SYSTEMS ONLINE &middot; SAY &ldquo;S.O.X&rdquo; OR TAP THE ASSISTANT TO ACTIVATE
      </div>

      {/* ── PaulChat (voice + AI) ── */}
      <PaulChat hideRobot />
      <ExtensionInstallPrompt />
      <button
        onClick={() => window.dispatchEvent(new CustomEvent('jarvis-open-extension-install'))}
        title="Install S.O.X voice extension"
        style={{ position: 'fixed', top: 16, right: 16, zIndex: 20, display: 'flex', alignItems: 'center', gap: 8, background: 'rgba(6,14,26,0.85)', border: '1px solid rgba(168,85,247,0.44)', borderRadius: 10, padding: '8px 14px', color: '#d8b4fe', fontFamily: 'monospace', fontSize: 12, letterSpacing: '0.06em', cursor: 'pointer', backdropFilter: 'blur(10px)' }}
      >
        &#x229E; Voice Extension
      </button>

      {/* ── Keyframes ── */}
      <style>{`
        @keyframes jarvis-spin    { from { transform: rotate(0deg); }  to { transform: rotate(360deg); } }
        @keyframes jarvis-pulse   { 0%,100% { opacity: 0.52; } 50% { opacity: 1; } }
        @keyframes jarvis-flicker { 0%,100% { opacity: 1; } 44% { opacity: 0.52; } 56% { opacity: 0.82; } }
        @keyframes sox-scan       { from { top: -2px; } to { top: 100%; } }
        @keyframes sox-widget-in  { from { opacity: 0; transform: translateY(10px) scale(0.97); } to { opacity: 1; transform: none; } }
        @keyframes sox-compass    { from { transform: rotate(0deg); }  to { transform: rotate(360deg); } }
      `}</style>
    </>
  )
}

SoxRoom.noLayout = true
