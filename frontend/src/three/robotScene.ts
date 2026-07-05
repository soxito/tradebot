/**
 * robotScene — canvas-agnostic Three.js builder for the JARVIS cyborg avatar.
 *
 * This is the exact scene + animation loop that used to live inside
 * JarvisRobot.tsx, extracted so it can run against EITHER:
 *   • a normal <canvas> on the main thread (fallback), or
 *   • an OffscreenCanvas inside a Web Worker (jarvisRobot.worker.ts).
 *
 * It touches no DOM/window/navigator — the caller resolves device pixel ratio
 * and the graphics tier and passes them in, and live state/energy is read each
 * frame through the `getState` getter (so the worker just mutates a local var).
 */
import * as THREE from 'three'

export type RobotState = 'idle' | 'walking' | 'listening' | 'thinking' | 'talking'
export type AvatarStyle = 'cyan' | 'purple' | 'gold' | 'crimson' | 'emerald'

/** The worker-safe subset of the perf profile the robot needs (plain data). */
export interface RobotGfx {
  antialias: boolean
  robotDprCap: number
  shadows: boolean
  fpsTarget: number
}

export interface RobotSceneOptions {
  canvas: HTMLCanvasElement | OffscreenCanvas
  size: number
  /** Resolved on the main thread (window.devicePixelRatio) — workers can't read it. */
  dpr: number
  gfx: RobotGfx
  style: AvatarStyle
  /** Read live each frame; energy is 0..1. */
  getState: () => { state: RobotState; energy: number }
}

export interface RobotSceneHandle {
  setSize(size: number): void
  dispose(): void
}

const THEMES: Record<AvatarStyle, { glow: number; accent: number; metal: number; dark: number }> = {
  cyan:    { glow: 0x22d3ee, accent: 0x06b6d4, metal: 0x1e293b, dark: 0x0f172a },
  purple:  { glow: 0xa78bfa, accent: 0x8b5cf6, metal: 0x241b3a, dark: 0x13101f },
  gold:    { glow: 0xfbbf24, accent: 0xf59e0b, metal: 0x2b2410, dark: 0x15110a },
  crimson: { glow: 0xfb7185, accent: 0xef4444, metal: 0x2b1416, dark: 0x150b0c },
  emerald: { glow: 0x34d399, accent: 0x10b981, metal: 0x0f2a20, dark: 0x091510 },
}

/**
 * Build the robot scene and start its render loop against `canvas`.
 * Returns a handle for resize/dispose, or null if WebGL is unavailable.
 */
export function createRobotScene(opts: RobotSceneOptions): RobotSceneHandle | null {
  const { canvas, size, dpr, gfx, style, getState } = opts

  let renderer: THREE.WebGLRenderer
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: gfx.antialias, alpha: true })
  } catch {
    return null
  }
  renderer.setSize(size, size, false)
  renderer.setPixelRatio(Math.min(dpr, gfx.robotDprCap))
  renderer.shadowMap.enabled = gfx.shadows
  renderer.shadowMap.type = THREE.PCFSoftShadowMap

  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100)
  camera.position.set(0, 0.8, 7.5)
  camera.lookAt(0, 0.5, 0)

  const theme = THEMES[style]

  // ── Lighting ────────────────────────────────────────────────────────────────
  scene.add(new THREE.AmbientLight(0x334466, 0.7))
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.2)
  keyLight.position.set(3, 5, 5)
  keyLight.castShadow = gfx.shadows
  scene.add(keyLight)
  const rimLight = new THREE.PointLight(theme.glow, 2.0, 18)
  rimLight.position.set(-3, 2, 3)
  scene.add(rimLight)
  const fillLight = new THREE.PointLight(theme.accent, 0.8, 12)
  fillLight.position.set(2, -1, 4)
  scene.add(fillLight)
  const underLight = new THREE.PointLight(theme.glow, 0.5, 8)
  underLight.position.set(0, -3, 2)
  scene.add(underLight)

  // ── Materials ────────────────────────────────────────────────────────────────
  const metalMat = new THREE.MeshStandardMaterial({
    color: theme.metal, metalness: 0.92, roughness: 0.28,
  })
  const darkMat = new THREE.MeshStandardMaterial({
    color: theme.dark, metalness: 0.85, roughness: 0.4,
  })
  const accentMat = new THREE.MeshStandardMaterial({
    color: theme.accent, metalness: 0.6, roughness: 0.35,
    emissive: theme.accent, emissiveIntensity: 0.3,
  })
  const glowMat = new THREE.MeshStandardMaterial({
    color: theme.glow, emissive: theme.glow, emissiveIntensity: 1.8,
    metalness: 0.1, roughness: 0.15,
  })
  const visorMat = new THREE.MeshStandardMaterial({
    color: 0x020408, metalness: 0.95, roughness: 0.1,
    emissive: theme.glow, emissiveIntensity: 0.18,
    transparent: true, opacity: 0.92,
  })
  const jointMat = new THREE.MeshStandardMaterial({
    color: 0x111827, metalness: 0.9, roughness: 0.3,
  })

  // ── Robot root group ─────────────────────────────────────────────────────────
  const robot = new THREE.Group()
  scene.add(robot)

  // ── LEGS ─────────────────────────────────────────────────────────────────────
  // Hip pivot (connects legs to torso)
  const hipGroup = new THREE.Group()
  hipGroup.position.set(0, -1.05, 0)
  robot.add(hipGroup)

  // Hip plate
  const hipPlate = new THREE.Mesh(new THREE.BoxGeometry(1.3, 0.28, 0.62), metalMat)
  hipGroup.add(hipPlate)

  // Left leg group
  const legL = new THREE.Group()
  legL.position.set(-0.45, -0.14, 0)
  hipGroup.add(legL)
  // Left thigh
  const thighL = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.19, 0.8, 24), metalMat)
  thighL.position.y = -0.4
  legL.add(thighL)
  // Left knee joint
  const kneeL = new THREE.Mesh(new THREE.SphereGeometry(0.19, 20, 20), jointMat)
  kneeL.position.y = -0.8
  legL.add(kneeL)
  // Left shin group (pivots at knee)
  const shinGroupL = new THREE.Group()
  shinGroupL.position.y = -0.8
  legL.add(shinGroupL)
  const shinL = new THREE.Mesh(new THREE.CylinderGeometry(0.17, 0.14, 0.75, 20), metalMat)
  shinL.position.y = -0.375
  shinGroupL.add(shinL)
  // Left foot
  const footL = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.12, 0.5), darkMat)
  footL.position.set(0, -0.78, 0.08)
  shinGroupL.add(footL)
  // Foot glow strip
  const footGlowL = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.03, 0.5), glowMat.clone())
  footGlowL.position.set(0, -0.74, 0.08)
  shinGroupL.add(footGlowL)

  // Right leg group (mirror)
  const legR = new THREE.Group()
  legR.position.set(0.45, -0.14, 0)
  hipGroup.add(legR)
  const thighR = thighL.clone()
  legR.add(thighR)
  const kneeR = kneeL.clone()
  legR.add(kneeR)
  const shinGroupR = new THREE.Group()
  shinGroupR.position.y = -0.8
  legR.add(shinGroupR)
  const shinR = shinL.clone()
  shinGroupR.add(shinR)
  const footR = footL.clone()
  shinGroupR.add(footR)
  const footGlowR = footGlowL.clone()
  shinGroupR.add(footGlowR)

  // ── TORSO ────────────────────────────────────────────────────────────────────
  const torsoGroup = new THREE.Group()
  torsoGroup.position.set(0, 0, 0)
  robot.add(torsoGroup)

  // Main chest box (tapered)
  const chest = new THREE.Mesh(new THREE.BoxGeometry(1.4, 1.3, 0.72), metalMat)
  chest.position.y = 0
  torsoGroup.add(chest)

  // Chest plate detail
  const chestPlate = new THREE.Mesh(new THREE.BoxGeometry(0.88, 0.75, 0.08), darkMat)
  chestPlate.position.set(0, 0.05, 0.37)
  torsoGroup.add(chestPlate)

  // Arc reactor / core energy cell
  const coreOuter = new THREE.Mesh(new THREE.TorusGeometry(0.2, 0.04, 16, 48), accentMat)
  coreOuter.position.set(0, 0.1, 0.38)
  torsoGroup.add(coreOuter)
  const coreInner = new THREE.Mesh(new THREE.CircleGeometry(0.15, 32), glowMat.clone())
  coreInner.position.set(0, 0.1, 0.405)
  torsoGroup.add(coreInner)
  const coreGlowMat = coreInner.material as THREE.MeshStandardMaterial

  // Shoulder plates
  const shoulderGeo = new THREE.SphereGeometry(0.32, 24, 24)
  const shoulderL = new THREE.Mesh(shoulderGeo, accentMat)
  shoulderL.scale.set(1, 0.85, 0.9)
  shoulderL.position.set(-0.82, 0.52, 0)
  torsoGroup.add(shoulderL)
  const shoulderR = shoulderL.clone()
  shoulderR.position.x = 0.82
  torsoGroup.add(shoulderR)

  // Abdomen / waist
  const waist = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.62, 0.35, 28), darkMat)
  waist.position.y = -0.72
  torsoGroup.add(waist)

  // Spine detail stripes on back
  for (let i = 0; i < 4; i++) {
    const stripe = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.2, 0.06), accentMat)
    stripe.position.set(0, 0.25 - i * 0.26, -0.37)
    torsoGroup.add(stripe)
  }

  // ── ARMS ─────────────────────────────────────────────────────────────────────
  // Left arm group (pivots at shoulder)
  const armGroupL = new THREE.Group()
  armGroupL.position.set(-0.85, 0.52, 0)
  robot.add(armGroupL)
  // Upper arm
  const upperArmL = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.14, 0.7, 20), metalMat)
  upperArmL.position.y = -0.35
  armGroupL.add(upperArmL)
  // Elbow
  const elbowL = new THREE.Mesh(new THREE.SphereGeometry(0.15, 16, 16), jointMat)
  elbowL.position.y = -0.72
  armGroupL.add(elbowL)
  // Forearm group (pivots at elbow)
  const foreGroupL = new THREE.Group()
  foreGroupL.position.y = -0.72
  armGroupL.add(foreGroupL)
  const foreArmL = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.11, 0.65, 18), metalMat)
  foreArmL.position.y = -0.325
  foreGroupL.add(foreArmL)
  // Hand
  const handL = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.24, 0.14), darkMat)
  handL.position.y = -0.68
  foreGroupL.add(handL)
  // Finger glow strips
  for (let f = 0; f < 3; f++) {
    const fg = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.1, 0.03), glowMat.clone())
    fg.position.set(-0.06 + f * 0.06, -0.77, 0.07)
    foreGroupL.add(fg)
  }

  // Right arm group (mirror)
  const armGroupR = new THREE.Group()
  armGroupR.position.set(0.85, 0.52, 0)
  robot.add(armGroupR)
  const upperArmR = upperArmL.clone()
  armGroupR.add(upperArmR)
  const elbowR = elbowL.clone()
  armGroupR.add(elbowR)
  const foreGroupR = new THREE.Group()
  foreGroupR.position.y = -0.72
  armGroupR.add(foreGroupR)
  const foreArmR = foreArmL.clone()
  foreGroupR.add(foreArmR)
  const handR = handL.clone()
  foreGroupR.add(handR)
  for (let f = 0; f < 3; f++) {
    const fg = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.1, 0.03), glowMat.clone())
    fg.position.set(-0.06 + f * 0.06, -0.77, 0.07)
    foreGroupR.add(fg)
  }

  // ── HEAD ─────────────────────────────────────────────────────────────────────
  const headGroup = new THREE.Group()
  headGroup.position.set(0, 1.08, 0)
  robot.add(headGroup)

  // Neck
  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.25, 0.3, 20), darkMat)
  neck.position.y = -0.15
  headGroup.add(neck)

  // Skull / helmet
  const skull = new THREE.Mesh(new THREE.SphereGeometry(0.62, 48, 48), metalMat)
  skull.scale.set(1, 0.94, 0.92)
  headGroup.add(skull)

  // Helmet ridges
  for (let i = 0; i < 3; i++) {
    const ridge = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.5, 0.08), accentMat)
    ridge.position.set(-0.2 + i * 0.2, 0.3, 0.55)
    ridge.rotation.z = (i - 1) * 0.15
    headGroup.add(ridge)
  }

  // Side "ears" / audio pods
  const earGeo = new THREE.CylinderGeometry(0.13, 0.13, 0.12, 24)
  const earL = new THREE.Mesh(earGeo, accentMat)
  earL.rotation.z = Math.PI / 2
  earL.position.set(-0.62, 0, 0)
  headGroup.add(earL)
  const earR = earL.clone()
  earR.position.x = 0.62
  headGroup.add(earR)
  // Ear glow rings
  const earRingL = new THREE.Mesh(new THREE.TorusGeometry(0.08, 0.02, 10, 24), glowMat)
  earRingL.position.set(-0.67, 0, 0)
  earRingL.rotation.y = Math.PI / 2
  headGroup.add(earRingL)
  const earRingR = earRingL.clone()
  earRingR.position.x = 0.67
  headGroup.add(earRingR)

  // Top crest + antenna
  const crestTop = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.15, 0.22, 20), accentMat)
  crestTop.position.set(0, 0.6, 0)
  headGroup.add(crestTop)
  const antennaStalk = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.035, 0.42, 12), metalMat)
  antennaStalk.position.set(0, 0.82, 0)
  headGroup.add(antennaStalk)
  const antennaTip = new THREE.Mesh(new THREE.SphereGeometry(0.07, 20, 20), glowMat.clone())
  antennaTip.position.set(0, 1.05, 0)
  headGroup.add(antennaTip)
  const antennaTipMat = antennaTip.material as THREE.MeshStandardMaterial

  // Visor (dark face shield)
  const visor = new THREE.Mesh(
    new THREE.SphereGeometry(0.56, 48, 48, Math.PI * 0.22, Math.PI * 0.56, Math.PI * 0.28, Math.PI * 0.46),
    visorMat
  )
  visor.position.set(0, -0.01, 0.12)
  visor.scale.set(1.05, 1, 1.12)
  headGroup.add(visor)

  // Eyes — glowing behind visor
  const eyeGeo = new THREE.SphereGeometry(0.1, 24, 24)
  const eyeLMat = glowMat.clone() as THREE.MeshStandardMaterial
  const eyeRMat = glowMat.clone() as THREE.MeshStandardMaterial
  const eyeL = new THREE.Mesh(eyeGeo, eyeLMat)
  eyeL.position.set(-0.2, 0.04, 0.5)
  headGroup.add(eyeL)
  const eyeR = new THREE.Mesh(eyeGeo, eyeRMat)
  eyeR.position.set(0.2, 0.04, 0.5)
  headGroup.add(eyeR)

  // Mouth bars — equaliser for talking
  const mouthBars: THREE.Mesh[] = []
  const mouthMats: THREE.MeshStandardMaterial[] = []
  const barCount = 7
  for (let i = 0; i < barCount; i++) {
    const mat = glowMat.clone() as THREE.MeshStandardMaterial
    mouthMats.push(mat)
    const bar = new THREE.Mesh(new THREE.BoxGeometry(0.038, 0.1, 0.03), mat)
    bar.position.set((i - (barCount - 1) / 2) * 0.068, -0.25, 0.53)
    headGroup.add(bar)
    mouthBars.push(bar)
  }
  const mouthPhase = mouthBars.map(() => Math.random() * Math.PI * 2)

  // ── PROCESSING RINGS (thinking) ──────────────────────────────────────────────
  const ringGroup = new THREE.Group()
  robot.add(ringGroup)
  const ringMats: THREE.MeshStandardMaterial[] = []
  for (let i = 0; i < 3; i++) {
    const rm = new THREE.MeshStandardMaterial({
      color: theme.glow, emissive: theme.glow, emissiveIntensity: 1.0,
      transparent: true, opacity: 0,
    })
    ringMats.push(rm)
    const ring = new THREE.Mesh(new THREE.TorusGeometry(1.1 + i * 0.22, 0.015, 8, 64), rm)
    ring.rotation.x = Math.PI / 2 + i * 0.55
    ring.rotation.z = i * 0.8
    ringGroup.add(ring)
  }

  // ── LISTENING PULSE RING ─────────────────────────────────────────────────────
  const pulseMat = new THREE.MeshStandardMaterial({
    color: theme.glow, emissive: theme.glow, emissiveIntensity: 1.2,
    transparent: true, opacity: 0,
  })
  const pulseRing = new THREE.Mesh(new THREE.TorusGeometry(0.85, 0.025, 10, 64), pulseMat)
  pulseRing.rotation.x = Math.PI / 2
  pulseRing.position.y = 1.08
  robot.add(pulseRing)

  // ── GROUND SHADOW ────────────────────────────────────────────────────────────
  const shadowMat = new THREE.MeshBasicMaterial({
    color: 0x000000, transparent: true, opacity: 0.25, side: THREE.DoubleSide,
  })
  const shadowDisc = new THREE.Mesh(new THREE.CircleGeometry(0.6, 32), shadowMat)
  shadowDisc.rotation.x = -Math.PI / 2
  shadowDisc.position.y = -2.45
  scene.add(shadowDisc)

  // ── ANIMATION LOOP ───────────────────────────────────────────────────────────
  let raf = 0
  const clock = new THREE.Clock()
  let blink = 0
  let nextBlink = 2 + Math.random() * 3
  let lastRender = 0
  const robotFrameMs = 1000 / gfx.fpsTarget   // throttle to the device's tier

  const animate = () => {
    raf = requestAnimationFrame(animate)
    const nowMs = (typeof performance !== 'undefined' ? performance.now() : Date.now())
    if (nowMs - lastRender < robotFrameMs) return
    lastRender = nowMs
    const t = clock.getElapsedTime()
    const dt = clock.getDelta()
    const live = getState()
    const st = live.state
    const en = Math.min(1, Math.max(0, live.energy))

    // ── Determine walking vs standing ───────────────────────────────────────
    const isWalking = st === 'idle' || st === 'walking'
    const walkFreq = 2.2

    // ── BODY BOB & SWAY (walking) ────────────────────────────────────────────
    if (isWalking) {
      robot.position.y = Math.sin(t * walkFreq * 2) * 0.045 - 0.05
      torsoGroup.rotation.z = Math.sin(t * walkFreq) * 0.04
      torsoGroup.rotation.y = Math.sin(t * walkFreq * 0.5) * 0.06
    } else {
      // Non-walking: gentle idle breathing
      robot.position.y = Math.sin(t * 1.1) * 0.025
      torsoGroup.rotation.z = Math.sin(t * 0.8) * 0.012
      torsoGroup.rotation.y = 0
    }

    // ── LEG WALK CYCLE ───────────────────────────────────────────────────────
    if (isWalking) {
      const legSwing = 0.45
      const kneeFlexMax = 0.52
      // Left leg: thigh forward when t*walkFreq is at phase 0
      const phL = t * walkFreq
      const phR = phL + Math.PI
      legL.rotation.x = Math.sin(phL) * legSwing
      legR.rotation.x = Math.sin(phR) * legSwing
      // Knee flex: bend when leg is behind
      shinGroupL.rotation.x = Math.max(0, -Math.sin(phL)) * kneeFlexMax
      shinGroupR.rotation.x = Math.max(0, -Math.sin(phR)) * kneeFlexMax
      // Foot angle compensation
      footL.rotation.x = 0.1 + shinGroupL.rotation.x * 0.5
      footR.rotation.x = 0.1 + shinGroupR.rotation.x * 0.5
    } else {
      legL.rotation.x = THREE.MathUtils.lerp(legL.rotation.x, 0, 0.12)
      legR.rotation.x = THREE.MathUtils.lerp(legR.rotation.x, 0, 0.12)
      shinGroupL.rotation.x = THREE.MathUtils.lerp(shinGroupL.rotation.x, 0, 0.12)
      shinGroupR.rotation.x = THREE.MathUtils.lerp(shinGroupR.rotation.x, 0, 0.12)
    }

    // ── ARM SWING (walk) / GESTURE (talking) ────────────────────────────────
    if (isWalking) {
      const armSwing = 0.38
      const phL = t * walkFreq + Math.PI // arms swing opposite to same-side leg
      armGroupL.rotation.x = Math.sin(phL) * armSwing
      armGroupR.rotation.x = Math.sin(phL + Math.PI) * armSwing
      foreGroupL.rotation.x = Math.max(0, Math.sin(phL)) * 0.4
      foreGroupR.rotation.x = Math.max(0, Math.sin(phL + Math.PI)) * 0.4
    } else if (st === 'talking') {
      // Subtle gesturing arms
      armGroupL.rotation.x = Math.sin(t * 1.6) * 0.18 - 0.1
      armGroupR.rotation.x = Math.sin(t * 1.6 + 1.2) * 0.14 - 0.1
      armGroupL.rotation.z = Math.sin(t * 0.9) * 0.08 - 0.05
      armGroupR.rotation.z = -Math.sin(t * 0.9) * 0.08 + 0.05
      foreGroupL.rotation.x = 0.3 + Math.sin(t * 2.0) * 0.15
      foreGroupR.rotation.x = 0.3 + Math.sin(t * 2.0 + 0.8) * 0.12
    } else if (st === 'listening') {
      // Arms slightly raised, attentive
      armGroupL.rotation.x = THREE.MathUtils.lerp(armGroupL.rotation.x, -0.22, 0.08)
      armGroupR.rotation.x = THREE.MathUtils.lerp(armGroupR.rotation.x, -0.22, 0.08)
      foreGroupL.rotation.x = THREE.MathUtils.lerp(foreGroupL.rotation.x, 0.4, 0.08)
      foreGroupR.rotation.x = THREE.MathUtils.lerp(foreGroupR.rotation.x, 0.4, 0.08)
    } else {
      armGroupL.rotation.x = THREE.MathUtils.lerp(armGroupL.rotation.x, 0.05, 0.06)
      armGroupR.rotation.x = THREE.MathUtils.lerp(armGroupR.rotation.x, 0.05, 0.06)
      foreGroupL.rotation.x = THREE.MathUtils.lerp(foreGroupL.rotation.x, 0.15, 0.06)
      foreGroupR.rotation.x = THREE.MathUtils.lerp(foreGroupR.rotation.x, 0.15, 0.06)
      armGroupL.rotation.z = THREE.MathUtils.lerp(armGroupL.rotation.z, -0.05, 0.06)
      armGroupR.rotation.z = THREE.MathUtils.lerp(armGroupR.rotation.z, 0.05, 0.06)
    }

    // ── HEAD ANIMATION ───────────────────────────────────────────────────────
    if (isWalking) {
      // Head bobs slightly, counter-rotates torso sway
      headGroup.rotation.y = -torsoGroup.rotation.y * 0.4 + Math.sin(t * 0.7) * 0.05
      headGroup.rotation.x = Math.sin(t * 2.2 * 2) * 0.02
    } else if (st === 'listening') {
      headGroup.rotation.y = Math.sin(t * 0.8) * 0.12
      headGroup.rotation.x = THREE.MathUtils.lerp(headGroup.rotation.x, 0.12, 0.05)
    } else if (st === 'thinking') {
      headGroup.rotation.y = Math.sin(t * 0.5) * 0.18
      headGroup.rotation.x = THREE.MathUtils.lerp(headGroup.rotation.x, 0.15, 0.04)
    } else if (st === 'talking') {
      headGroup.rotation.y = Math.sin(t * 0.7) * 0.08
      headGroup.rotation.x = Math.sin(t * 1.4) * 0.05
    } else {
      headGroup.rotation.y = THREE.MathUtils.lerp(headGroup.rotation.y, Math.sin(t * 0.4) * 0.08, 0.04)
      headGroup.rotation.x = THREE.MathUtils.lerp(headGroup.rotation.x, 0, 0.04)
    }

    // ── BLINK ────────────────────────────────────────────────────────────────
    blink += dt
    const blinking = blink > nextBlink && blink < nextBlink + 0.12
    const blinkIntensity = blinking ? 0 : 1
    if (blink > nextBlink + 0.18) {
      blink = 0
      nextBlink = 2.5 + Math.random() * 4
    }
    eyeLMat.emissiveIntensity = blinkIntensity * (st === 'thinking' ? (0.6 + Math.random() * 0.4) : 1.8)
    eyeRMat.emissiveIntensity = eyeLMat.emissiveIntensity

    // ── MOUTH BARS (talking) ─────────────────────────────────────────────────
    mouthBars.forEach((bar, i) => {
      let targetScaleY: number
      if (st === 'talking') {
        const freq = 3.5 + i * 0.7
        const baseAmp = 0.5 + en * 1.8
        targetScaleY = Math.max(0.05, Math.abs(Math.sin(t * freq + mouthPhase[i])) * baseAmp + 0.1)
      } else {
        targetScaleY = 0.05
      }
      bar.scale.y = THREE.MathUtils.lerp(bar.scale.y, targetScaleY, 0.22)
      const mat = mouthMats[i]
      mat.emissiveIntensity = st === 'talking' ? (0.8 + en * 1.2) : 0.3
    })

    // ── THINKING RINGS ───────────────────────────────────────────────────────
    ringMats.forEach((rm, i) => {
      const targetOp = st === 'thinking' ? 0.7 : 0
      rm.opacity = THREE.MathUtils.lerp(rm.opacity, targetOp, 0.06)
      if (st === 'thinking') {
        ringGroup.children[i].rotation.y = t * (0.8 + i * 0.35)
        ringGroup.children[i].rotation.x = Math.PI / 2 + Math.sin(t * 0.5 + i) * 0.4
      }
    })
    ringGroup.position.y = 1.0

    // ── LISTENING PULSE ──────────────────────────────────────────────────────
    if (st === 'listening') {
      const pulseFreq = 1.5 + en * 2
      const pulseVal = (Math.sin(t * pulseFreq) + 1) * 0.5
      pulseMat.opacity = pulseVal * (0.4 + en * 0.4)
      pulseRing.scale.setScalar(1 + Math.sin(t * pulseFreq * 0.7) * 0.12 + en * 0.2)
    } else {
      pulseMat.opacity = THREE.MathUtils.lerp(pulseMat.opacity, 0, 0.08)
    }

    // ── CORE GLOW ────────────────────────────────────────────────────────────
    if (st === 'talking') {
      coreGlowMat.emissiveIntensity = 1.4 + en * 1.2 + Math.sin(t * 4) * 0.3
    } else if (st === 'thinking') {
      coreGlowMat.emissiveIntensity = 0.8 + Math.sin(t * 6) * 0.5
    } else if (st === 'listening') {
      coreGlowMat.emissiveIntensity = 1.0 + Math.sin(t * 3) * 0.3
    } else {
      coreGlowMat.emissiveIntensity = 0.6 + Math.sin(t * 1.2) * 0.2
    }

    // ── ANTENNA TIP ──────────────────────────────────────────────────────────
    antennaTipMat.emissiveIntensity = st === 'listening'
      ? 2.5 + Math.sin(t * 4) * 0.5
      : st === 'thinking'
      ? 1.8 + Math.sin(t * 8) * 0.8
      : 1.0 + Math.sin(t * 2) * 0.3

    // ── RIM LIGHT PULSE ──────────────────────────────────────────────────────
    rimLight.intensity = st === 'talking'
      ? 2.0 + en * 1.5 + Math.sin(t * 5) * 0.3
      : st === 'thinking'
      ? 1.8 + Math.sin(t * 3) * 0.4
      : st === 'listening'
      ? 2.2 + Math.sin(t * 2.5) * 0.4
      : 1.5 + Math.sin(t * 1.0) * 0.2

    renderer.render(scene, camera)
  }

  animate()

  return {
    setSize(newSize: number) {
      renderer.setSize(newSize, newSize, false)
    },
    dispose() {
      cancelAnimationFrame(raf)
      renderer.dispose()
      scene.traverse((obj) => {
        if ((obj as THREE.Mesh).geometry) (obj as THREE.Mesh).geometry.dispose()
        if ((obj as THREE.Mesh).material) {
          const mat = (obj as THREE.Mesh).material
          if (Array.isArray(mat)) mat.forEach(m => m.dispose())
          else (mat as THREE.Material).dispose()
        }
      })
    },
  }
}
