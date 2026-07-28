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
  /**
   * Hide or show the whole robot, including its ground shadow.
   *
   * Fading the canvas element alone is not enough: the contact shadow is drawn
   * *into* the canvas, so during (and after) a CSS fade it reads as a dark
   * smudge sitting on the page with no robot above it. Hiding at the scene
   * level removes the shadow with the body, and pauses rendering while hidden
   * so an invisible avatar stops costing GPU time.
   */
  setHidden(hidden: boolean): void
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
    // Iridescent sheen for premium cyborg look
    iridescence: 0.3,
    iridescenceIOR: 1.3,
    iridescenceThicknessRange: [100, 400],
  } as any)
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
    // Subsurface scattering hint
    transmission: 0.1,
    thickness: 0.5,
    attenuationColor: new THREE.Color(theme.glow),
    attenuationDistance: 2,
  } as any)
  const jointMat = new THREE.MeshStandardMaterial({
    color: 0x111827, metalness: 0.9, roughness: 0.3,
  })

  // Track cloned materials for proper disposal
  const clonedMaterials: THREE.Material[] = []

  function trackMat(mat: THREE.Material): THREE.Material {
    clonedMaterials.push(mat)
    return mat
  }

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
  const footGlowL = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.03, 0.5), trackMat(new THREE.MeshStandardMaterial({
    color: glowMat.color, emissive: glowMat.emissive, emissiveIntensity: glowMat.emissiveIntensity,
    metalness: glowMat.metalness, roughness: glowMat.roughness,
  })))
  footGlowL.position.set(0, -0.74, 0.08)
  shinGroupL.add(footGlowL)

  // Right leg group (mirror)
  const legR = new THREE.Group()
  legR.position.set(0.45, -0.14, 0)
  hipGroup.add(legR)
  const thighR = new THREE.Mesh(thighL.geometry, trackMat(thighL.material as THREE.Material))
  legR.add(thighR)
  const kneeR = new THREE.Mesh(kneeL.geometry, trackMat(kneeL.material as THREE.Material))
  legR.add(kneeR)
  const shinGroupR = new THREE.Group()
  shinGroupR.position.y = -0.8
  legR.add(shinGroupR)
  const shinR = new THREE.Mesh(shinL.geometry, trackMat(shinL.material as THREE.Material))
  shinGroupR.add(shinR)
  const footR = new THREE.Mesh(footL.geometry, trackMat(footL.material as THREE.Material))
  shinGroupR.add(footR)
  const footGlowR = new THREE.Mesh(footGlowL.geometry, trackMat(footGlowL.material as THREE.Material))
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
  const coreInner = new THREE.Mesh(new THREE.CircleGeometry(0.15, 32), trackMat(new THREE.MeshStandardMaterial({
    color: glowMat.color, emissive: glowMat.emissive, emissiveIntensity: glowMat.emissiveIntensity,
    metalness: glowMat.metalness, roughness: glowMat.roughness,
  })))
  coreInner.position.set(0, 0.1, 0.405)
  torsoGroup.add(coreInner)
  const coreGlowMat = coreInner.material as THREE.MeshStandardMaterial

  // Shoulder plates
  const shoulderGeo = new THREE.SphereGeometry(0.32, 24, 24)
  const shoulderL = new THREE.Mesh(shoulderGeo, accentMat)
  shoulderL.scale.set(1, 0.85, 0.9)
  shoulderL.position.set(-0.82, 0.52, 0)
  torsoGroup.add(shoulderL)
  const shoulderR = new THREE.Mesh(shoulderGeo, trackMat(accentMat))
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
  const upperArmR = new THREE.Mesh(upperArmL.geometry, trackMat(upperArmL.material as THREE.Material))
  armGroupR.add(upperArmR)
  const elbowR = new THREE.Mesh(elbowL.geometry, trackMat(elbowL.material as THREE.Material))
  armGroupR.add(elbowR)
  const foreGroupR = new THREE.Group()
  foreGroupR.position.y = -0.72
  armGroupR.add(foreGroupR)
  const foreArmR = new THREE.Mesh(foreArmL.geometry, trackMat(foreArmL.material as THREE.Material))
  foreGroupR.add(foreArmR)
  const handR = new THREE.Mesh(handL.geometry, trackMat(handL.material as THREE.Material))
  foreGroupR.add(handR)
  for (let f = 0; f < 3; f++) {
    const fg = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.1, 0.03), trackMat(new THREE.MeshStandardMaterial({
      color: glowMat.color, emissive: glowMat.emissive, emissiveIntensity: glowMat.emissiveIntensity,
      metalness: glowMat.metalness, roughness: glowMat.roughness,
    })))
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
  const earR = new THREE.Mesh(earGeo, trackMat(accentMat))
  earR.rotation.z = Math.PI / 2
  earR.position.x = 0.62
  headGroup.add(earR)
  // Ear glow rings
  const earRingL = new THREE.Mesh(new THREE.TorusGeometry(0.08, 0.02, 10, 24), glowMat)
  earRingL.position.set(-0.67, 0, 0)
  earRingL.rotation.y = Math.PI / 2
  headGroup.add(earRingL)
  const earRingR = new THREE.Mesh(new THREE.TorusGeometry(0.08, 0.02, 10, 24), trackMat(glowMat))
  earRingR.position.x = 0.67
  earRingR.rotation.y = Math.PI / 2
  headGroup.add(earRingR)

  // Top crest + antenna
  const crestTop = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.15, 0.22, 20), accentMat)
  crestTop.position.set(0, 0.6, 0)
  headGroup.add(crestTop)
  const antennaStalk = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.035, 0.42, 12), metalMat)
  antennaStalk.position.set(0, 0.82, 0)
  headGroup.add(antennaStalk)
  const antennaTip = new THREE.Mesh(new THREE.SphereGeometry(0.07, 20, 20), trackMat(new THREE.MeshStandardMaterial({
    color: glowMat.color, emissive: glowMat.emissive, emissiveIntensity: glowMat.emissiveIntensity,
    metalness: glowMat.metalness, roughness: glowMat.roughness,
  })))
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

  // Eyes — glowing behind visor.
  //
  // A blink is rendered by squashing the eye vertically as well as dimming it,
  // because dimming alone reads as a light being switched off rather than as an
  // eyelid closing. The two are driven together in the animation loop.
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

  // ── ENERGY AURA (core) ─────────────────────────────────────────────────────────
  const auraGeo = new THREE.RingGeometry(0.4, 0.45, 32)
  const auraMat = new THREE.MeshBasicMaterial({
    color: theme.glow,
    transparent: true,
    opacity: 0,
    side: THREE.DoubleSide,
    depthWrite: false,
  })
  const aura = new THREE.Mesh(auraGeo, auraMat)
  aura.rotation.x = -Math.PI / 2
  aura.position.y = 0.1
  torsoGroup.add(aura)

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

  // New visual polish state
  let breathingScale = 0
  let weightShift = 0
  let antennaBounce = 0
  let prevState: RobotState = 'idle'
  let energyAuraScale = 1
  let energyAuraOpacity = 0

  // Hidden state — see RobotSceneHandle.setHidden.
  let hidden = false

  const animate = () => {
    raf = requestAnimationFrame(animate)
    // Nothing to draw while hidden. The canvas was cleared once on the way in,
    // so it stays transparent — shadow included — at no ongoing GPU cost.
    if (hidden) return
    const nowMs = (typeof performance !== 'undefined' ? performance.now() : Date.now())
    if (nowMs - lastRender < robotFrameMs) return
    lastRender = nowMs
    const t = clock.getElapsedTime()
    const dt = clock.getDelta()
    const live = getState()
    const st = live.state
    const en = Math.min(1, Math.max(0, live.energy))

    // ── HOVER ────────────────────────────────────────────────────────────────
    // The avatar floats rather than walks. Its drift is built from several
    // sine waves at deliberately non-harmonic frequencies (1.00 / 0.63 / 0.41),
    // so the combined path never visibly repeats — a single sine reads as a
    // mechanical bounce, which is what gives away a "floating" object as fake.
    const isHovering = st === 'idle' || st === 'walking'
    // Breathing continues in every state; a body that goes perfectly still
    // between animations looks switched off.
    breathingScale = THREE.MathUtils.lerp(breathingScale, 0.015, 0.02)
    chest.scale.y = 1 + breathingScale * Math.sin(t * 0.5)

    // Height above the resting line — reused below to drive the contact shadow.
    let hoverHeight: number
    if (isHovering) {
      hoverHeight =
        Math.sin(t * 1.00) * 0.10 +
        Math.sin(t * 0.63) * 0.05 +
        Math.sin(t * 0.41) * 0.03
      robot.position.y = hoverHeight
      // Bank slightly into the drift, as something actually holding itself up
      // against gravity would, and yaw very slowly.
      torsoGroup.rotation.z = Math.sin(t * 0.63) * 0.045
      torsoGroup.rotation.y = Math.sin(t * 0.29) * 0.09
      robot.rotation.x = Math.sin(t * 0.47) * 0.035
      hipGroup.rotation.z = Math.sin(t * 0.37) * 0.03
    } else {
      // Attentive states hold station: a tighter, slower hover so the robot
      // reads as focused rather than drifting.
      hoverHeight = Math.sin(t * 0.9) * 0.035
      robot.position.y = hoverHeight
      weightShift = THREE.MathUtils.lerp(weightShift, Math.sin(t * 0.3) * 0.02, 0.01)
      hipGroup.rotation.z = weightShift
      torsoGroup.rotation.z = THREE.MathUtils.lerp(torsoGroup.rotation.z, Math.sin(t * 0.8) * 0.012, 0.05)
      torsoGroup.rotation.y = THREE.MathUtils.lerp(torsoGroup.rotation.y, 0, 0.05)
      robot.rotation.x = THREE.MathUtils.lerp(robot.rotation.x, 0, 0.05)
    }

    // ── LEGS (hanging) ───────────────────────────────────────────────────────
    // With nothing to stand on the legs hang and trail, swinging a little out
    // of phase with the body so they feel carried by the hover rather than
    // driven by it.
    {
      const trailL = Math.sin(t * 0.63 - 0.6) * 0.09
      const trailR = Math.sin(t * 0.63 - 0.9) * 0.09
      legL.rotation.x = THREE.MathUtils.lerp(legL.rotation.x, 0.10 + trailL, 0.06)
      legR.rotation.x = THREE.MathUtils.lerp(legR.rotation.x, 0.10 + trailR, 0.06)
      // A relaxed knee is never locked straight.
      shinGroupL.rotation.x = THREE.MathUtils.lerp(shinGroupL.rotation.x, 0.20 - trailL * 0.5, 0.06)
      shinGroupR.rotation.x = THREE.MathUtils.lerp(shinGroupR.rotation.x, 0.22 - trailR * 0.5, 0.06)
      // Toes point down under their own weight.
      footL.rotation.x = THREE.MathUtils.lerp(footL.rotation.x, -0.28, 0.06)
      footR.rotation.x = THREE.MathUtils.lerp(footR.rotation.x, -0.30, 0.06)
    }

    // ── ARM DRIFT (hover) / GESTURE (talking) ───────────────────────────────
    if (isHovering) {
      // Arms drift and settle rather than swinging — no stride to counterweight.
      const driftL = Math.sin(t * 0.55) * 0.10
      const driftR = Math.sin(t * 0.55 + 0.7) * 0.10
      armGroupL.rotation.x = THREE.MathUtils.lerp(armGroupL.rotation.x, 0.06 + driftL, 0.05)
      armGroupR.rotation.x = THREE.MathUtils.lerp(armGroupR.rotation.x, 0.06 + driftR, 0.05)
      armGroupL.rotation.z = THREE.MathUtils.lerp(armGroupL.rotation.z, -0.09 + driftL * 0.3, 0.05)
      armGroupR.rotation.z = THREE.MathUtils.lerp(armGroupR.rotation.z,  0.09 - driftR * 0.3, 0.05)
      foreGroupL.rotation.x = THREE.MathUtils.lerp(foreGroupL.rotation.x, 0.22 + driftL * 0.5, 0.05)
      foreGroupR.rotation.x = THREE.MathUtils.lerp(foreGroupR.rotation.x, 0.24 + driftR * 0.5, 0.05)
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

    // ── CONTACT SHADOW ───────────────────────────────────────────────────────
    // The strongest cue that something is floating is its shadow: it shrinks,
    // softens and fades as the object rises. Without this the robot reads as
    // sliding on the ground no matter how it moves.
    {
      const lift = THREE.MathUtils.clamp(hoverHeight / 0.18, -1, 1)   // −1..1
      const s = 1 - lift * 0.22
      shadowDisc.scale.set(s, s, 1)
      shadowMat.opacity = 0.25 - lift * 0.09
    }

    // ── HEAD ANIMATION ───────────────────────────────────────────────────────
    if (isHovering) {
      // Head leads the drift a little and counter-rotates the torso's yaw, so
      // the robot looks like it is steering rather than being pushed around.
      headGroup.rotation.y = -torsoGroup.rotation.y * 0.4 + Math.sin(t * 0.43) * 0.07
      headGroup.rotation.x = Math.sin(t * 0.61) * 0.035
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
    // Idle is when a blink actually reads as "alive", so the robot blinks most
    // there and much less while working — a person mid-sentence or concentrating
    // blinks noticeably less than one waiting quietly.
    blink += dt
    const idleish = st === 'idle' || st === 'walking'
    // Closing is faster than opening, as a real eyelid is: a linear
    // open-and-shut looks mechanical.
    const CLOSE_S = 0.055
    const OPEN_S  = 0.11
    const into = blink - nextBlink
    let lidClosed = 0                                    // 0 = open, 1 = shut
    if (into >= 0) {
      lidClosed = into < CLOSE_S
        ? into / CLOSE_S
        : Math.max(0, 1 - (into - CLOSE_S) / OPEN_S)
    }
    if (into > CLOSE_S + OPEN_S) {
      blink = 0
      // Humans blink irregularly; a fixed cadence is the tell that this is a
      // loop. Occasional double-blinks (the short end of the range) help too.
      nextBlink = idleish ? 1.6 + Math.random() * 3.4 : 4 + Math.random() * 5
    }

    // Squash the eye toward the lid line and dim it in step, so it looks like a
    // lid coming down rather than a lamp being switched off.
    const eyeOpen = 1 - lidClosed
    const eyeScaleY = Math.max(0.06, eyeOpen)
    eyeL.scale.y = eyeScaleY
    eyeR.scale.y = eyeScaleY
    const baseGlow = st === 'thinking' ? (0.6 + Math.random() * 0.4) : 1.8
    eyeLMat.emissiveIntensity = baseGlow * eyeOpen
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
    // Bounce on state change
    if (st !== prevState) {
      antennaBounce = 1.0
    }
    antennaBounce = THREE.MathUtils.lerp(antennaBounce, 0, 0.15)
    const bounceOffset = antennaBounce * 0.15
    
    antennaTipMat.emissiveIntensity = st === 'listening'
      ? 2.5 + Math.sin(t * 4) * 0.5 + bounceOffset
      : st === 'thinking'
      ? 1.8 + Math.sin(t * 8) * 0.8 + bounceOffset
      : 1.0 + Math.sin(t * 2) * 0.3 + bounceOffset
    // Bounce position
    antennaTip.position.y = 1.05 + bounceOffset
    prevState = st

    // ── ENERGY AURA (core) ───────────────────────────────────────────────────
    // Expanding ring at core when energy is high
    if (aura) {
      const targetAuraOp = en > 0.7 ? 0.5 : en > 0.4 ? 0.2 : 0.05
      energyAuraOpacity = THREE.MathUtils.lerp(energyAuraOpacity, targetAuraOp, 0.05)
      const targetAuraScale = 1 + en * 1.5
      energyAuraScale = THREE.MathUtils.lerp(energyAuraScale, targetAuraScale, 0.05)
      aura.material.opacity = energyAuraOpacity
      aura.scale.setScalar(energyAuraScale)
    }

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
    setHidden(next: boolean) {
      if (next === hidden) return
      hidden = next
      if (hidden) {
        // Clear to fully transparent so the last drawn frame — body *and*
        // contact shadow — is gone immediately rather than lingering as a
        // dark patch under an invisible robot.
        renderer.clear(true, true, true)
      } else {
        // Reset the frame clock so the first visible frame draws at once
        // instead of waiting out a stale throttle interval.
        lastRender = 0
      }
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
