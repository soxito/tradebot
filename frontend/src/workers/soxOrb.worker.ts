/**
 * soxOrb.worker.ts — off-main-thread renderer for the S.O.X energy-core orb.
 *
 * The Jarvis/S.O.X room previously drew ~980 particles on the main thread every
 * frame, which competed with hydration, data fetching and user input (a known
 * freeze source on 8 GB machines). This worker owns an OffscreenCanvas and runs
 * the entire animation off-thread, so the UI thread stays free and responsive.
 *
 * The page keeps an identical main-thread fallback for browsers without
 * OffscreenCanvas support — see useSoxOrb in pages/jarvis-room.tsx.
 *
 * Message protocol (main → worker):
 *   init       { canvas, cssW, cssH, deviceDpr, profile, state }  (canvas transferred)
 *   resize     { cssW, cssH, deviceDpr }
 *   quality    { profile }
 *   state      { state }
 *   visibility { hidden }
 *   stop       {}
 */
import type { PerfProfile } from '../utils/devicePerformance'

type SoxState = 'idle' | 'listening' | 'thinking' | 'talking'

const STATE_TARGETS: Record<SoxState, { energy: number; gold: number; wave: number; core: number; ring: number }> = {
  idle:      { energy: 0.35, gold: 0.08, wave: 0.0, core: 0.55, ring: 0.0 },
  listening: { energy: 0.65, gold: 0.04, wave: 1.0, core: 1.0,  ring: 0.0 },
  thinking:  { energy: 0.80, gold: 1.00, wave: 0.0, core: 0.88, ring: 0.0 },
  talking:   { energy: 1.00, gold: 0.48, wave: 0.0, core: 1.0,  ring: 1.0 },
}

// Safe default until the first `init`/`quality` message supplies the real tier.
const DEFAULT_PROFILE: PerfProfile = {
  tier: 'high',
  particleScale: 0.78, cloudScale: 0.78, sparkCap: 160, ribbonSegs: 112,
  dprCap: 1.75, robotDprCap: 1.5, fpsTarget: 60, shadows: true, antialias: true, orbGlow: true,
}

const lerp = (a: number, b: number, k: number) => a + (b - a) * k
const mixRGB = (
  a: [number, number, number],
  b: [number, number, number],
  k: number,
): [number, number, number] =>
  [Math.round(lerp(a[0], b[0], k)), Math.round(lerp(a[1], b[1], k)), Math.round(lerp(a[2], b[2], k))]

const TEAL:   [number, number, number] = [45, 226, 197]
const GOLD:   [number, number, number] = [245, 158, 11]
const BLUE:   [number, number, number] = [96, 165, 250]
const ORANGE: [number, number, number] = [251, 146, 60]
const WHITE:  [number, number, number] = [220, 240, 255]

// ── Particle types ────────────────────────────────────────────────────────────
type P = { a: number; r: number; s: number; len: number; w: number }
type Cloud = { a: number; r: number; da: number; size: number; alpha: number }
type Ring = { phase: number }
type Spark = { x: number; y: number; vx: number; vy: number; life: number; maxLife: number; size: number }

const make = (n: number, rMin: number, rMax: number, sMin: number, sMax: number): P[] =>
  Array.from({ length: n }, () => ({
    a:   Math.random() * Math.PI * 2,
    r:   rMin + Math.random() * (rMax - rMin),
    s:   sMin + Math.random() * (sMax - sMin),
    len: 0.05 + Math.random() * 0.16,
    w:   0.3 + Math.random() * 1.4,
  }))

// ── Runtime state (populated on init) ──────────────────────────────────────────
let canvas: OffscreenCanvas | null = null
let ctx: OffscreenCanvasRenderingContext2D | null = null
let cssW = 0
let cssH = 0
let deviceDpr = 1
let appliedDprCap = DEFAULT_PROFILE.dprCap
let DPR = 1
let profile: PerfProfile = DEFAULT_PROFILE
let state: SoxState = 'idle'
let hidden = false
let raf = 0
let running = false

// Particle layers — created once on init (radii are normalised, size-independent).
let outer: P[] = []
let goldP: P[] = []
let coreP: P[] = []
let streak: P[] = []
let cloud: Cloud[] = []
let sonarRings: Ring[] = []
const sparks: Spark[] = []

const ribbons = [
  { baseR: 0.74, amp: 0.19, freq: 4, phOff: 0.0, dir:  1, cm: 0.00 },
  { baseR: 0.70, amp: 0.23, freq: 5, phOff: 1.2, dir: -1, cm: 0.00 },
  { baseR: 0.80, amp: 0.15, freq: 6, phOff: 2.4, dir:  1, cm: 0.00 },
  { baseR: 0.76, amp: 0.17, freq: 3, phOff: 3.6, dir: -1, cm: 0.00 },
  { baseR: 0.64, amp: 0.25, freq: 4, phOff: 0.8, dir:  1, cm: 0.05 },
  { baseR: 0.86, amp: 0.13, freq: 7, phOff: 1.8, dir: -1, cm: 0.05 },
  { baseR: 0.57, amp: 0.13, freq: 5, phOff: 2.0, dir:  1, cm: 0.90 },
  { baseR: 0.54, amp: 0.16, freq: 4, phOff: 3.0, dir: -1, cm: 0.90 },
] as const

// Animation accumulators
let t = 0
let energy = 0.08, goldMix = 0.06, waveMix = 0, coreMix = 0.38, ringMix = 0
let lastFrame = 0

function buildParticles() {
  outer  = make(420, 0.78, 1.06, 0.0012, 0.0035)
  goldP  = make(340, 0.36, 0.78, 0.0016, 0.0052)
  coreP  = make(160, 0.06, 0.26, 0.0024, 0.0065)
  streak = make(80,  0.50, 0.92, 0.0030, 0.0085)
  cloud = Array.from({ length: 400 }, (_, i) => ({
    a:     Math.random() * Math.PI * 2,
    r:     1.05 + Math.random() * 0.55,
    da:    (i % 2 === 0 ? 1 : -1) * (0.00020 + Math.random() * 0.00030),
    size:  0.8 + Math.random() * 3.0,
    alpha: 0.25 + Math.random() * 0.75,
  }))
  sonarRings = Array.from({ length: 9 }, (_, i) => ({ phase: i / 9 }))
}

function resize() {
  if (!canvas || !ctx) return
  appliedDprCap = profile.dprCap
  DPR = Math.min(deviceDpr || 1, profile.dprCap)
  canvas.width  = Math.max(1, Math.floor(cssW * DPR))
  canvas.height = Math.max(1, Math.floor(cssH * DPR))
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0)
  ctx.fillStyle = '#020207'
  ctx.fillRect(0, 0, cssW, cssH)
}

function draw(now?: number) {
  if (!running || !ctx) return
  raf = requestAnimationFrame(draw)
  if (hidden) return

  const q = profile
  const glow = q.orbGlow
  const frameMs = 1000 / q.fpsTarget
  const ts = now ?? (typeof performance !== 'undefined' ? performance.now() : Date.now())
  if (ts - lastFrame < frameMs) return
  lastFrame = ts
  if (q.dprCap !== appliedDprCap) resize()
  t += 0.68

  const tgt = STATE_TARGETS[state] || STATE_TARGETS.idle
  energy  = lerp(energy,  tgt.energy, 0.045)
  goldMix = lerp(goldMix, tgt.gold,   0.036)
  waveMix = lerp(waveMix, tgt.wave,   0.062)
  coreMix = lerp(coreMix, tgt.core,   0.045)
  ringMix = lerp(ringMix, tgt.ring,   0.045)

  const jx = goldMix > 0.5 ? (Math.random() - 0.5) * 2.0 * ((goldMix - 0.5) / 0.5) : 0
  const jy = goldMix > 0.5 ? (Math.random() - 0.5) * 2.0 * ((goldMix - 0.5) / 0.5) : 0

  const W = cssW, H = cssH
  ctx.globalCompositeOperation = 'source-over'
  ctx.fillStyle = 'rgba(2,2,7,0.13)'
  ctx.fillRect(0, 0, W, H)

  const cx = W / 2 + jx
  const cy = H * 0.44 + jy
  const breathe = 1 + Math.sin(t * 0.013) * 0.022 + energy * 0.052
  const R    = Math.min(W, H) * 0.33 * breathe
  const spin = 1 + energy * 1.8

  ctx.globalCompositeOperation = 'lighter'

  // 1. OUTER CLOUD HALO
  {
    const cloudC = goldMix > 0.55
      ? mixRGB(TEAL, ORANGE, (goldMix - 0.55) / 0.45)
      : waveMix > 0.3 ? mixRGB(TEAL, BLUE, waveMix * 0.45) : TEAL
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
      const a  = p.alpha * (0.06 + energy * 0.10)
      ctx.fillStyle = `rgba(${cloudC[0]},${cloudC[1]},${cloudC[2]},${a.toFixed(3)})`
      ctx.beginPath()
      ctx.arc(x, y, p.size * (0.65 + energy * 0.55), 0, Math.PI * 2)
      ctx.fill()
    }
  }

  // 2. SONAR RINGS (LISTENING)
  if (waveMix > 0.02) {
    ctx.lineWidth   = 2.2
    ctx.strokeStyle = `rgba(${TEAL[0]},${TEAL[1]},${TEAL[2]},${(0.28 * waveMix).toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.92, 0, Math.PI * 2); ctx.stroke()
    ctx.lineWidth   = 1.4
    ctx.strokeStyle = `rgba(${TEAL[0]},${TEAL[1]},${TEAL[2]},${(0.18 * waveMix).toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.18, 0, Math.PI * 2); ctx.stroke()

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

    ctx.lineWidth = 2.2
    for (let i = 0; i < 6; i++) {
      const ph = ((t * 0.016) + i * 0.18) % 1
      const wr = R * (0.55 + ph * 1.35)
      const a  = (1 - ph) * 0.58 * waveMix
      ctx.strokeStyle = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${a.toFixed(3)})`
      ctx.beginPath(); ctx.arc(cx, cy, wr, -Math.PI / 3.0, Math.PI / 3.0); ctx.stroke()
    }
  }

  // 3. SPARK PARTICLES (THINKING)
  if (goldMix > 0.28) {
    const spawnRate = 0.55 * goldMix
    if (Math.random() < spawnRate && sparks.length < q.sparkCap) {
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
      const sc = p < 0.5 ? mixRGB(GOLD, ORANGE, p * 2) : mixRGB(ORANGE, WHITE, (p - 0.5) * 2)
      ctx.fillStyle = `rgba(${sc[0]},${sc[1]},${sc[2]},${sa.toFixed(3)})`
      ctx.beginPath()
      ctx.arc(s.x, s.y, s.size * (0.8 + (1 - p) * 0.6), 0, Math.PI * 2)
      ctx.fill()
    }
  } else if (sparks.length > 0) {
    sparks.splice(0, Math.ceil(sparks.length * 0.10))
  }

  // 4. MAIN PARTICLE LAYERS
  const pScale = q.particleScale
  const layer = (
    parts: P[],
    color: [number, number, number],
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
      ctx!.strokeStyle = `rgba(${color[0]},${color[1]},${color[2]},${a.toFixed(3)})`
      ctx!.lineWidth = p.w * (0.70 + energy * 0.50)
      ctx!.beginPath(); ctx!.moveTo(x, y); ctx!.lineTo(tx, ty); ctx!.stroke()
    }
  }

  layer(goldP,  GOLD, 1, 0.060)
  layer(outer, mixRGB(TEAL, GOLD, goldMix), -1, 0.090)
  layer(coreP, BLUE, 1, 0.040)
  if (ringMix > 0.04) layer(streak, mixRGB(TEAL, GOLD, 0.28), 1, 0.020, ringMix * 0.75)

  // 5. WAVE RIBBON MESH (TALKING)
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

  // 6. INNER GOLD RING (TALKING)
  if (ringMix > 0.03) {
    ctx.shadowColor = 'rgba(255,180,0,0.95)'
    ctx.shadowBlur  = glow ? 48 * ringMix : 0
    ctx.lineWidth   = 4.8 * ringMix
    ctx.strokeStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.92 * ringMix).toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.60, 0, Math.PI * 2); ctx.stroke()

    ctx.shadowBlur  = glow ? 28 * ringMix : 0
    ctx.lineWidth   = 2.2 * ringMix
    ctx.strokeStyle = `rgba(255,200,80,${(0.58 * ringMix).toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.68, 0, Math.PI * 2); ctx.stroke()
    ctx.shadowBlur  = 0

    const gs = (t * 0.014) % (Math.PI * 2)
    ctx.lineWidth   = 2.4
    ctx.strokeStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.58 * ringMix).toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.92, gs + 0.28, gs + Math.PI * 2 - 0.28); ctx.stroke()

    ctx.lineWidth   = 1.0
    ctx.strokeStyle = `rgba(${GOLD[0]},${GOLD[1]},${GOLD[2]},${(0.20 * ringMix).toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.12, 0, Math.PI * 2); ctx.stroke()
  }

  // 7. EQ BARS (TALKING)
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

  // 8. THINKING inner blue ring
  if (goldMix > 0.42) {
    const bra = Math.min(1, (goldMix - 0.42) / 0.58) * 0.68
    ctx.shadowColor = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${(bra * 0.85).toFixed(3)})`
    ctx.shadowBlur  = glow ? 22 : 0
    ctx.lineWidth   = 2.8
    ctx.strokeStyle = `rgba(${BLUE[0]},${BLUE[1]},${BLUE[2]},${bra.toFixed(3)})`
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.50, 0, Math.PI * 2); ctx.stroke()
    ctx.shadowBlur  = 0
  }

  // 9. CENTRAL GLOW + NUCLEUS
  const glowC = goldMix > 0.52
    ? mixRGB([220, 130, 40], ORANGE, Math.min(1, (goldMix - 0.52) / 0.48))
    : mixRGB([88, 148, 235], [165, 210, 255], coreMix)
  const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.64)
  g.addColorStop(0,    `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${(0.42 + coreMix * 0.40).toFixed(3)})`)
  g.addColorStop(0.45, 'rgba(50,110,210,0.07)')
  g.addColorStop(1,    'rgba(0,0,0,0)')
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * 0.64, 0, Math.PI * 2); ctx.fill()

  const nc = goldMix > 0.65 ? ORANGE : BLUE
  const ng = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.24)
  ng.addColorStop(0,   `rgba(255,255,255,${(0.85 + coreMix * 0.15).toFixed(3)})`)
  ng.addColorStop(0.3, `rgba(${Math.min(255, nc[0] + 50)},${Math.min(255, nc[1] + 50)},${Math.min(255, nc[2] + 50)},${(0.60 + coreMix * 0.25).toFixed(3)})`)
  ng.addColorStop(0.7, `rgba(${nc[0]},${nc[1]},${nc[2]},${(0.20 + coreMix * 0.22).toFixed(3)})`)
  ng.addColorStop(1,   'rgba(0,0,0,0)')
  ctx.fillStyle = ng; ctx.beginPath(); ctx.arc(cx, cy, R * 0.24, 0, Math.PI * 2); ctx.fill()
}

interface InitMsg { type: 'init'; canvas: OffscreenCanvas; cssW: number; cssH: number; deviceDpr: number; profile: PerfProfile; state: SoxState }
interface ResizeMsg { type: 'resize'; cssW: number; cssH: number; deviceDpr: number }
interface QualityMsg { type: 'quality'; profile: PerfProfile }
interface StateMsg { type: 'state'; state: SoxState }
interface VisibilityMsg { type: 'visibility'; hidden: boolean }
interface StopMsg { type: 'stop' }
type InMsg = InitMsg | ResizeMsg | QualityMsg | StateMsg | VisibilityMsg | StopMsg

self.onmessage = (e: MessageEvent<InMsg>) => {
  const d = e.data
  switch (d.type) {
    case 'init': {
      canvas = d.canvas
      ctx = canvas.getContext('2d')
      if (!ctx) return
      cssW = d.cssW; cssH = d.cssH; deviceDpr = d.deviceDpr
      profile = d.profile || DEFAULT_PROFILE
      state = d.state || 'idle'
      buildParticles()
      resize()
      running = true
      cancelAnimationFrame(raf)
      draw()
      break
    }
    case 'resize': {
      cssW = d.cssW; cssH = d.cssH; deviceDpr = d.deviceDpr
      resize()
      break
    }
    case 'quality': {
      profile = d.profile || profile
      break
    }
    case 'state': {
      state = d.state
      break
    }
    case 'visibility': {
      hidden = d.hidden
      break
    }
    case 'stop': {
      running = false
      cancelAnimationFrame(raf)
      break
    }
  }
}
