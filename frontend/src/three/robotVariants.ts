/**
 * robotVariants — the JARVIS look registry.
 *
 * One entry per visual identity. Each entry owns BOTH a robot build and an orb
 * build so the two always share a palette and a material language: pick
 * "Aurora" for the robot and "Aurora" for the orb and they read as the same
 * machine. The two selections are persisted independently (see the localStorage
 * keys below) so a mismatched pair is allowed — it just looks like two devices.
 *
 * Everything here is worker-safe: no `document`, no `window`, no external
 * assets. Surface maps are generated as DataTextures from a value-noise field,
 * and the environment used for IBL is a small procedural scene handed to a
 * PMREMGenerator by the caller (see three/variantScene.ts).
 *
 * Techniques used across the three robots:
 *   • PBR shells with packed procedural ORM + normal maps
 *   • clearcoat (Sentinel), anisotropy (Aurora), iridescence (Ember)
 *   • a Fresnel rim injected into MeshStandardMaterial via onBeforeCompile
 *   • emissive accent geometry (InstancedMesh) driven by voice amplitude
 *   • idle breathing, pointer head-tracking and a blink/pulse cycle
 */
import * as THREE from 'three'
import type { RobotState } from './robotScene'

// ── Public identity ──────────────────────────────────────────────────────────
export type VariantId = 'sentinel' | 'aurora' | 'ember'

export const ROBOT_VARIANT_KEY = 'jarvis.robotVariant'
export const ORB_VARIANT_KEY = 'jarvis.orbVariant'

export interface VariantPalette {
  /** Primary emissive / light colour. */
  key: number
  /** Secondary accent, used for trim and rings. */
  accent: number
  /** Base metal of the shell. */
  shell: number
  /** Deep shadow plastic / carbon. */
  deep: number
  /** Fresnel rim colour. */
  rim: number
}

/** Per-frame inputs handed to every build's `update`. */
export interface VariantFrame {
  /** Seconds since the scene started. */
  t: number
  /** Seconds since the previous rendered frame. */
  dt: number
  /** Voice amplitude, 0..1. */
  energy: number
  state: RobotState
  /** Pointer position in NDC, −1..1. Zero when there is no pointer. */
  px: number
  py: number
  /** 0 = eyes open, 1 = eyes shut. Driven by the shared blink cycle. */
  blink: number
  /** True when the user asked for reduced motion — hold still. */
  reduced: boolean
  /** Camera position in world space — the volumetric orb marches in object space. */
  cameraWorld: THREE.Vector3
}

/** Quality knobs resolved from the device graphics tier. */
export interface VariantQuality {
  /** Ray-march steps for the volumetric orb. */
  volumeSteps: number
  /** Point count for the swarm orb. */
  swarmCount: number
  /** Nested shells for the refractive glass orb. */
  glassLayers: number
  /** Geometry segment multiplier, 0.5..1. */
  seg: number
}

export function qualityForTier(tier: string): VariantQuality {
  switch (tier) {
    case 'low':    return { volumeSteps: 12, swarmCount: 5000,  glassLayers: 1, seg: 0.55 }
    case 'medium': return { volumeSteps: 18, swarmCount: 10000, glassLayers: 2, seg: 0.75 }
    case 'ultra':  return { volumeSteps: 36, swarmCount: 26000, glassLayers: 3, seg: 1.0 }
    default:       return { volumeSteps: 26, swarmCount: 17000, glassLayers: 3, seg: 1.0 }
  }
}

export interface VariantBuild {
  root: THREE.Group
  /** How the harness should frame this build. */
  camera: { fov: number; z: number; y: number; targetY: number }
  /** Bloom tuning for the harness post chain. */
  bloom: { strength: number; radius: number; threshold: number }
  /** Chromatic aberration in UV units at the frame edge. */
  aberration: number
  /** Rough draw-call count, for the perf readout. */
  drawCalls: number
  update(f: VariantFrame): void
  dispose(): void
}

export interface JarvisVariant {
  id: VariantId
  label: string
  description: string
  palette: VariantPalette
  buildRobot(q: VariantQuality): VariantBuild
  buildOrb(q: VariantQuality): VariantBuild
}

// ── Disposal bookkeeping ─────────────────────────────────────────────────────
/**
 * Everything a build allocates on the GPU goes in here. Three does not free
 * geometries, materials or textures when an Object3D is removed from a scene,
 * so a build that skipped this would leak a full set of buffers on every route
 * change or variant switch.
 */
class Bin {
  private items: { dispose(): void }[] = []
  add<T extends { dispose(): void }>(x: T): T {
    this.items.push(x)
    return x
  }
  dispose() {
    for (const i of this.items) {
      try { i.dispose() } catch { /* already gone */ }
    }
    this.items.length = 0
  }
}

// ── Procedural noise → surface maps ──────────────────────────────────────────
function hash01(x: number, y: number, seed: number): number {
  const s = Math.sin(x * 127.1 + y * 311.7 + seed * 74.7) * 43758.5453123
  return s - Math.floor(s)
}
function smoothT(t: number): number {
  return t * t * (3 - 2 * t)
}
function valueNoise(x: number, y: number, seed: number): number {
  const xi = Math.floor(x), yi = Math.floor(y)
  const u = smoothT(x - xi), v = smoothT(y - yi)
  const a = hash01(xi, yi, seed),         b = hash01(xi + 1, yi, seed)
  const c = hash01(xi, yi + 1, seed),     d = hash01(xi + 1, yi + 1, seed)
  return (a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v
}
function fbm2(x: number, y: number, seed: number, octaves: number): number {
  let sum = 0, amp = 0.5, norm = 0
  for (let o = 0; o < octaves; o++) {
    sum += valueNoise(x, y, seed + o * 13) * amp
    norm += amp
    x *= 2.03; y *= 2.03; amp *= 0.5
  }
  return sum / norm
}

interface SurfaceMaps {
  /** Packed AO(r) / roughness(g) / metalness(b) — three reads .g and .b. */
  orm: THREE.DataTexture
  normal: THREE.DataTexture
}

/**
 * Build a packed ORM + tangent-space normal map from a value-noise height
 * field. `stretch` above 1 smears the noise along U, which is what turns an
 * isotropic speckle into brushed metal.
 */
function buildSurface(bin: Bin, o: {
  seed: number
  size?: number
  scale?: number
  stretch?: number
  rough: [number, number]
  metal: [number, number]
  bump?: number
  octaves?: number
}): SurfaceMaps {
  const N = o.size ?? 128
  const scale = o.scale ?? 6
  const stretch = o.stretch ?? 1
  const bump = o.bump ?? 1.6
  const oct = o.octaves ?? 4

  const height = new Float32Array(N * N)
  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const u = (x / N) * scale / stretch
      const v = (y / N) * scale
      height[y * N + x] = fbm2(u, v, o.seed, oct)
    }
  }

  const ormData = new Uint8Array(N * N * 4)
  const nrmData = new Uint8Array(N * N * 4)
  const at = (x: number, y: number) => height[((y + N) % N) * N + ((x + N) % N)]

  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const i = (y * N + x) * 4
      const h = height[y * N + x]

      ormData[i] = 255                                                  // AO — unused, kept white
      ormData[i + 1] = Math.round(255 * (o.rough[0] + (o.rough[1] - o.rough[0]) * h))
      ormData[i + 2] = Math.round(255 * (o.metal[0] + (o.metal[1] - o.metal[0]) * (1 - h)))
      ormData[i + 3] = 255

      // Sobel-lite gradient → tangent-space normal.
      const dx = (at(x + 1, y) - at(x - 1, y)) * bump
      const dy = (at(x, y + 1) - at(x, y - 1)) * bump
      let nx = -dx, ny = -dy, nz = 1
      const len = Math.hypot(nx, ny, nz)
      nx /= len; ny /= len; nz /= len
      nrmData[i] = Math.round((nx * 0.5 + 0.5) * 255)
      nrmData[i + 1] = Math.round((ny * 0.5 + 0.5) * 255)
      nrmData[i + 2] = Math.round((nz * 0.5 + 0.5) * 255)
      nrmData[i + 3] = 255
    }
  }

  const mk = (data: Uint8Array) => {
    const t = new THREE.DataTexture(data, N, N, THREE.RGBAFormat)
    t.wrapS = t.wrapT = THREE.RepeatWrapping
    t.minFilter = THREE.LinearMipmapLinearFilter
    t.magFilter = THREE.LinearFilter
    t.generateMipmaps = true
    t.needsUpdate = true
    return bin.add(t)
  }
  return { orm: mk(ormData), normal: mk(nrmData) }
}

// ── Fresnel rim (onBeforeCompile) ────────────────────────────────────────────
export interface RimUniforms {
  uRimColor: { value: THREE.Color }
  uRimPower: { value: number }
  uRimStrength: { value: number }
}

/**
 * Inject a view-dependent Fresnel rim into any lit material.
 *
 * The term is added to `totalEmissiveRadiance` right after the emissive map is
 * resolved, so it survives tone mapping and is picked up by the bloom pass —
 * which is what makes the silhouette read against a dark room instead of
 * dissolving into it.
 */
export function addFresnel(
  mat: THREE.MeshStandardMaterial,
  color: number,
  power = 2.6,
  strength = 0.85,
): RimUniforms {
  const u: RimUniforms = {
    uRimColor: { value: new THREE.Color(color) },
    uRimPower: { value: power },
    uRimStrength: { value: strength },
  }
  mat.onBeforeCompile = (shader) => {
    shader.uniforms.uRimColor = u.uRimColor
    shader.uniforms.uRimPower = u.uRimPower
    shader.uniforms.uRimStrength = u.uRimStrength
    shader.fragmentShader = shader.fragmentShader
      .replace(
        '#include <common>',
        `#include <common>
uniform vec3 uRimColor;
uniform float uRimPower;
uniform float uRimStrength;`,
      )
      .replace(
        '#include <emissivemap_fragment>',
        `#include <emissivemap_fragment>
{
  vec3 rimView = normalize( vViewPosition );
  float rimF = pow( 1.0 - clamp( dot( normalize( normal ), rimView ), 0.0, 1.0 ), uRimPower );
  totalEmissiveRadiance += uRimColor * rimF * uRimStrength;
}`,
      )
  }
  // All rim materials compile to the same source, so they may share a program —
  // the uniforms above stay per-material. Without a custom key three would
  // reuse the *un-injected* program for a material with identical parameters.
  mat.customProgramCacheKey = () => 'jarvis-fresnel-rim'
  return u
}

// ── Shared helpers ───────────────────────────────────────────────────────────
const ORIGIN = new THREE.Vector3()
const lerp = THREE.MathUtils.lerp
const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v)

/** An unlit emissive material — bright enough that the bloom threshold catches it. */
function glowMaterial(bin: Bin, color: number, opacity = 1): THREE.MeshBasicMaterial {
  return bin.add(new THREE.MeshBasicMaterial({
    color,
    toneMapped: false,
    transparent: opacity < 1,
    opacity,
  }))
}

/** Give an InstancedMesh a per-instance colour buffer we can animate. */
function withInstanceColor(mesh: THREE.InstancedMesh, base: THREE.Color): void {
  const n = mesh.count
  const arr = new Float32Array(n * 3)
  for (let i = 0; i < n; i++) {
    arr[i * 3] = base.r; arr[i * 3 + 1] = base.g; arr[i * 3 + 2] = base.b
  }
  mesh.instanceColor = new THREE.InstancedBufferAttribute(arr, 3)
  mesh.instanceColor.needsUpdate = true
}

/** Per-band amplitude for an emissive equaliser, deterministic per index. */
function bandLevel(t: number, i: number, energy: number, state: RobotState): number {
  if (state === 'talking') {
    const f = 3.2 + i * 0.63
    return clamp01(Math.abs(Math.sin(t * f + i * 1.7)) * (0.25 + energy * 1.5) + energy * 0.2)
  }
  if (state === 'listening') return clamp01(0.18 + Math.abs(Math.sin(t * 1.6 + i * 0.5)) * (0.15 + energy * 0.5))
  if (state === 'thinking') return clamp01(0.12 + Math.abs(Math.sin(t * 4.2 + i * 0.9)) * 0.35)
  return clamp01(0.08 + Math.sin(t * 0.9 + i * 0.4) * 0.05)
}

/** Head tracking: aim at the pointer, but never far enough to look possessed. */
function trackPointer(
  head: THREE.Object3D,
  f: VariantFrame,
  yawRange: number,
  pitchRange: number,
  idleYaw: number,
): void {
  if (f.reduced) {
    head.rotation.y = lerp(head.rotation.y, 0, 0.12)
    head.rotation.x = lerp(head.rotation.x, 0, 0.12)
    return
  }
  const targetY = f.px * yawRange + Math.sin(f.t * 0.37) * idleYaw
  const targetX = -f.py * pitchRange + Math.sin(f.t * 0.53) * idleYaw * 0.4
  head.rotation.y = lerp(head.rotation.y, targetY, 0.07)
  head.rotation.x = lerp(head.rotation.x, targetX, 0.07)
}

// ═════════════════════════════════════════════════════════════════════════════
// SENTINEL — armoured guardian. Clearcoat plate armour, cyan spine equaliser.
// ═════════════════════════════════════════════════════════════════════════════
function buildSentinelRobot(pal: VariantPalette, q: VariantQuality): VariantBuild {
  const bin = new Bin()
  const root = new THREE.Group()
  const S = (n: number) => Math.max(6, Math.round(n * q.seg))

  const surf = buildSurface(bin, {
    seed: 11, scale: 7, stretch: 1, rough: [0.16, 0.46], metal: [0.72, 1.0], bump: 2.0,
  })

  const shellMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: pal.shell,
    metalness: 1, roughness: 1,
    metalnessMap: surf.orm, roughnessMap: surf.orm,
    normalMap: surf.normal, normalScale: new THREE.Vector2(0.55, 0.55),
    clearcoat: 1, clearcoatRoughness: 0.07,
    envMapIntensity: 1.35,
  }))
  const shellRim = addFresnel(shellMat, pal.rim, 2.9, 0.7)

  const darkMat = bin.add(new THREE.MeshStandardMaterial({
    color: pal.deep, metalness: 0.85, roughness: 0.55,
    normalMap: surf.normal, normalScale: new THREE.Vector2(0.8, 0.8),
    envMapIntensity: 0.8,
  }))
  addFresnel(darkMat, pal.accent, 3.4, 0.4)

  const accentMat = bin.add(new THREE.MeshStandardMaterial({
    color: pal.accent, metalness: 0.9, roughness: 0.22,
    emissive: pal.accent, emissiveIntensity: 0.45,
    envMapIntensity: 1.5,
  }))
  const accentRim = addFresnel(accentMat, pal.key, 2.2, 1.1)

  const visorMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: 0x04070c, metalness: 1, roughness: 0.05,
    clearcoat: 1, clearcoatRoughness: 0.02,
    iridescence: 0.7, iridescenceIOR: 1.6, iridescenceThicknessRange: [120, 420],
    envMapIntensity: 2.0,
  }))

  const eyeMat = glowMaterial(bin, pal.key)
  const coreMat = glowMaterial(bin, pal.key)
  const haloMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.key, toneMapped: false, transparent: true, opacity: 0.0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }))

  // ── Head ───────────────────────────────────────────────────────────────────
  const head = new THREE.Group()
  head.position.set(0, 1.12, 0)
  root.add(head)

  const helmetGeo = bin.add(new THREE.SphereGeometry(0.6, S(44), S(30)))
  const helmet = new THREE.Mesh(helmetGeo, shellMat)
  helmet.scale.set(1, 1.03, 0.94)
  head.add(helmet)

  const crownGeo = bin.add(new THREE.TorusGeometry(0.5, 0.05, S(12), S(40)))
  const crown = new THREE.Mesh(crownGeo, accentMat)
  crown.rotation.x = Math.PI / 2
  crown.position.y = 0.3
  head.add(crown)

  const visorGeo = bin.add(new THREE.SphereGeometry(
    0.605, S(44), S(26), Math.PI * 0.24, Math.PI * 0.52, Math.PI * 0.3, Math.PI * 0.4,
  ))
  const visor = new THREE.Mesh(visorGeo, visorMat)
  visor.scale.set(1.02, 1, 1.04)
  head.add(visor)

  // The eye sits *in front of* the visor, not behind it: the visor is a near
  // black mirror, so an eye tucked underneath is simply invisible.
  const eyeGeo = bin.add(new THREE.BoxGeometry(0.46, 0.055, 0.03))
  const eye = new THREE.Mesh(eyeGeo, eyeMat)
  eye.position.set(0, 0.03, 0.615)
  head.add(eye)

  const jawGeo = bin.add(new THREE.BoxGeometry(0.3, 0.12, 0.22))
  const jaw = new THREE.Mesh(jawGeo, darkMat)
  jaw.position.set(0, -0.4, 0.36)
  head.add(jaw)

  const podGeo = bin.add(new THREE.CylinderGeometry(0.13, 0.13, 0.1, S(20)))
  const pods = new THREE.InstancedMesh(podGeo, accentMat, 2)
  {
    const m = new THREE.Matrix4()
    const e = new THREE.Euler(0, 0, Math.PI / 2)
    const qt = new THREE.Quaternion().setFromEuler(e)
    const one = new THREE.Vector3(1, 1, 1)
    m.compose(new THREE.Vector3(-0.6, 0, 0), qt, one); pods.setMatrixAt(0, m)
    m.compose(new THREE.Vector3(0.6, 0, 0), qt, one); pods.setMatrixAt(1, m)
    pods.instanceMatrix.needsUpdate = true
  }
  head.add(pods)

  const neckGeo = bin.add(new THREE.CylinderGeometry(0.19, 0.26, 0.34, S(20)))
  const neck = new THREE.Mesh(neckGeo, darkMat)
  neck.position.y = 0.82
  root.add(neck)

  // ── Torso ──────────────────────────────────────────────────────────────────
  const torso = new THREE.Group()
  root.add(torso)

  const chestProfile: THREE.Vector2[] = [
    new THREE.Vector2(0.02, 0.72), new THREE.Vector2(0.42, 0.66),
    new THREE.Vector2(0.62, 0.42), new THREE.Vector2(0.66, 0.05),
    new THREE.Vector2(0.58, -0.32), new THREE.Vector2(0.42, -0.62),
    new THREE.Vector2(0.30, -0.80), new THREE.Vector2(0.02, -0.84),
  ]
  const chestGeo = bin.add(new THREE.LatheGeometry(chestProfile, S(40)))
  const chest = new THREE.Mesh(chestGeo, shellMat)
  chest.scale.set(1.18, 1, 0.82)
  torso.add(chest)

  const collarGeo = bin.add(new THREE.TorusGeometry(0.48, 0.06, S(12), S(34)))
  const collar = new THREE.Mesh(collarGeo, accentMat)
  collar.rotation.x = Math.PI / 2
  collar.position.y = 0.63
  collar.scale.set(1.18, 1, 1)
  torso.add(collar)

  const coreRingGeo = bin.add(new THREE.TorusGeometry(0.2, 0.038, S(14), S(40)))
  const coreRing = new THREE.Mesh(coreRingGeo, accentMat)
  coreRing.position.set(0, 0.1, 0.56)
  torso.add(coreRing)

  const coreGeo = bin.add(new THREE.SphereGeometry(0.13, S(24), S(16)))
  const core = new THREE.Mesh(coreGeo, coreMat)
  core.position.set(0, 0.1, 0.56)
  torso.add(core)

  // Spine equaliser — the amplitude readout.
  const BARS = 7
  const barGeo = bin.add(new THREE.BoxGeometry(0.055, 0.2, 0.05))
  const barMat = glowMaterial(bin, pal.key)
  const bars = new THREE.InstancedMesh(barGeo, barMat, BARS * 2)
  withInstanceColor(bars, new THREE.Color(pal.key))
  torso.add(bars)

  const pauldronGeo = bin.add(new THREE.SphereGeometry(0.34, S(24), S(18)))
  const pauldrons = new THREE.InstancedMesh(pauldronGeo, shellMat, 2)
  {
    const m = new THREE.Matrix4()
    const qt = new THREE.Quaternion()
    const sc = new THREE.Vector3(1, 0.8, 0.95)
    m.compose(new THREE.Vector3(-0.86, 0.5, 0), qt, sc); pauldrons.setMatrixAt(0, m)
    m.compose(new THREE.Vector3(0.86, 0.5, 0), qt, sc); pauldrons.setMatrixAt(1, m)
    pauldrons.instanceMatrix.needsUpdate = true
  }
  torso.add(pauldrons)

  // ── Arms ───────────────────────────────────────────────────────────────────
  const upperGeo = bin.add(new THREE.CapsuleGeometry(0.15, 0.42, S(6), S(18)))
  const foreGeo = bin.add(new THREE.CapsuleGeometry(0.125, 0.4, S(6), S(16)))
  const handGeo = bin.add(new THREE.BoxGeometry(0.2, 0.24, 0.15))

  const armL = new THREE.Group(); armL.position.set(-0.88, 0.48, 0); root.add(armL)
  const armR = new THREE.Group(); armR.position.set(0.88, 0.48, 0); root.add(armR)
  const foreL = new THREE.Group(); foreL.position.y = -0.66; armL.add(foreL)
  const foreR = new THREE.Group(); foreR.position.y = -0.66; armR.add(foreR)
  for (const [arm, fore] of [[armL, foreL], [armR, foreR]] as const) {
    const u = new THREE.Mesh(upperGeo, shellMat); u.position.y = -0.33; arm.add(u)
    const fm = new THREE.Mesh(foreGeo, shellMat); fm.position.y = -0.32; fore.add(fm)
    const h = new THREE.Mesh(handGeo, darkMat); h.position.y = -0.66; fore.add(h)
  }

  // ── Halo rings (thinking) ──────────────────────────────────────────────────
  const halos: THREE.Mesh[] = []
  const haloGeo = bin.add(new THREE.TorusGeometry(1.15, 0.012, S(8), S(64)))
  for (let i = 0; i < 3; i++) {
    const ring = new THREE.Mesh(haloGeo, haloMat)
    ring.scale.setScalar(1 + i * 0.18)
    ring.rotation.set(Math.PI / 2 + i * 0.5, 0, i * 0.7)
    ring.position.y = 0.9
    root.add(ring)
    halos.push(ring)
  }

  const barMatrix = new THREE.Matrix4()
  const barPos = new THREE.Vector3()
  const barQuat = new THREE.Quaternion()
  const barScale = new THREE.Vector3(1, 1, 1)
  const barColor = new THREE.Color()
  const keyColor = new THREE.Color(pal.key)
  const accentColor = new THREE.Color(pal.accent)
  let haloOpacity = 0

  return {
    root,
    camera: { fov: 32, z: 6.2, y: 0.25, targetY: 0.2 },
    bloom: { strength: 0.85, radius: 0.7, threshold: 0.62 },
    aberration: 0.0022,
    drawCalls: 19,
    update(f) {
      const en = clamp01(f.energy)
      const still = f.reduced

      // Breathing — the chest expands, never the whole body, so the silhouette
      // stays put while the machine reads as running.
      const breath = still ? 0 : Math.sin(f.t * 0.62) * 0.018 + en * 0.012
      chest.scale.y = 1 + breath
      chest.scale.x = 1.18 - breath * 0.4

      // Hover + bank.
      if (!still) {
        root.position.y = Math.sin(f.t * 0.95) * 0.055 + Math.sin(f.t * 0.61) * 0.03
        torso.rotation.z = Math.sin(f.t * 0.53) * 0.03
        armL.rotation.x = lerp(armL.rotation.x, 0.08 + Math.sin(f.t * 0.55) * 0.09, 0.05)
        armR.rotation.x = lerp(armR.rotation.x, 0.08 + Math.sin(f.t * 0.55 + 0.8) * 0.09, 0.05)
        armL.rotation.z = lerp(armL.rotation.z, -0.1, 0.05)
        armR.rotation.z = lerp(armR.rotation.z, 0.1, 0.05)
        foreL.rotation.x = lerp(foreL.rotation.x, 0.26 + Math.sin(f.t * 0.7) * 0.06, 0.05)
        foreR.rotation.x = lerp(foreR.rotation.x, 0.28 + Math.sin(f.t * 0.7 + 0.6) * 0.06, 0.05)
      } else {
        root.position.y = 0
      }

      trackPointer(head, f, 0.45, 0.26, 0.05)

      // Blink — squash the eye bar toward its own centre and dim it together,
      // because dimming alone reads as a lamp switching off.
      const open = 1 - f.blink
      eye.scale.y = Math.max(0.05, open)
      eyeMat.color.copy(keyColor).multiplyScalar(0.9 + open * 1.4 + en * 0.8)

      // Pulse.
      const pulse = 1 + Math.sin(f.t * (f.state === 'thinking' ? 6.5 : 1.6)) * 0.22 + en * 0.9
      coreMat.color.copy(keyColor).multiplyScalar(0.7 + pulse * 0.7)
      core.scale.setScalar(1 + en * 0.28 + Math.sin(f.t * 2.2) * 0.04)

      // Spine equaliser, mirrored either side of the sternum.
      for (let i = 0; i < BARS; i++) {
        const level = bandLevel(f.t, i, en, f.state)
        const h = 0.14 + level * 0.9
        for (let side = 0; side < 2; side++) {
          const idx = i * 2 + side
          barPos.set(side === 0 ? -0.3 : 0.3, 0.46 - i * 0.16, 0.45)
          barScale.set(1, h, 1)
          barMatrix.compose(barPos, barQuat, barScale)
          bars.setMatrixAt(idx, barMatrix)
          barColor.copy(keyColor).lerp(accentColor, 1 - level).multiplyScalar(0.35 + level * 1.5)
          bars.setColorAt(idx, barColor)
        }
      }
      bars.instanceMatrix.needsUpdate = true
      if (bars.instanceColor) bars.instanceColor.needsUpdate = true

      // Rim breathes with the voice so the silhouette lights up when speaking.
      shellRim.uRimStrength.value = 0.55 + en * 0.9
      accentRim.uRimStrength.value = 0.9 + en * 1.2
      accentMat.emissiveIntensity = 0.35 + en * 0.8

      const wantHalo = f.state === 'thinking' ? 0.55 : 0
      haloOpacity = lerp(haloOpacity, wantHalo, 0.06)
      haloMat.opacity = haloOpacity
      if (!still && haloOpacity > 0.01) {
        halos.forEach((ring, i) => {
          ring.rotation.y = f.t * (0.7 + i * 0.3)
          ring.rotation.x = Math.PI / 2 + Math.sin(f.t * 0.5 + i) * 0.4
        })
      }
    },
    dispose() { bin.dispose() },
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// AURORA — floating drone. Anisotropic brushed shell, refractive faceplate,
// six armour petals that breathe open, filament ring as the amplitude readout.
// ═════════════════════════════════════════════════════════════════════════════
function buildAuroraRobot(pal: VariantPalette, q: VariantQuality): VariantBuild {
  const bin = new Bin()
  const root = new THREE.Group()
  const S = (n: number) => Math.max(6, Math.round(n * q.seg))

  const surf = buildSurface(bin, {
    seed: 37, scale: 9, stretch: 14, rough: [0.08, 0.34], metal: [0.85, 1.0], bump: 1.1,
  })

  const shellMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: pal.shell,
    metalness: 1, roughness: 1,
    metalnessMap: surf.orm, roughnessMap: surf.orm,
    normalMap: surf.normal, normalScale: new THREE.Vector2(0.35, 0.9),
    anisotropy: 1, anisotropyRotation: Math.PI / 2,
    envMapIntensity: 1.6,
  }))
  const shellRim = addFresnel(shellMat, pal.rim, 2.2, 1.0)

  const petalMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: pal.deep, metalness: 1, roughness: 0.28,
    metalnessMap: surf.orm, roughnessMap: surf.orm,
    normalMap: surf.normal, normalScale: new THREE.Vector2(0.3, 0.7),
    anisotropy: 0.85, anisotropyRotation: 0,
    clearcoat: 0.6, clearcoatRoughness: 0.2,
    envMapIntensity: 1.2,
  }))
  const petalRim = addFresnel(petalMat, pal.accent, 2.6, 1.2)

  const glassMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: 0xffffff,
    metalness: 0, roughness: 0.03,
    transmission: q.glassLayers > 1 ? 0.95 : 0,
    opacity: q.glassLayers > 1 ? 1 : 0.35,
    transparent: q.glassLayers <= 1,
    ior: 1.62, thickness: 0.45,
    iridescence: 1, iridescenceIOR: 1.9, iridescenceThicknessRange: [140, 520],
    attenuationColor: new THREE.Color(pal.key), attenuationDistance: 1.2,
    envMapIntensity: 2.0,
  }))

  const eyeMat = glowMaterial(bin, pal.key)
  const filamentMat = glowMaterial(bin, pal.key)
  const ribbonMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.accent, toneMapped: false, transparent: true, opacity: 0.35,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }))

  // ── Head core ──────────────────────────────────────────────────────────────
  const head = new THREE.Group()
  head.position.set(0, 0.85, 0)
  root.add(head)

  const skullGeo = bin.add(new THREE.IcosahedronGeometry(0.52, q.seg >= 1 ? 3 : 2))
  const skull = new THREE.Mesh(skullGeo, shellMat)
  skull.scale.set(1, 0.96, 1)
  head.add(skull)

  const faceGeo = bin.add(new THREE.SphereGeometry(
    0.53, S(40), S(26), Math.PI * 0.28, Math.PI * 0.44, Math.PI * 0.28, Math.PI * 0.44,
  ))
  const face = new THREE.Mesh(faceGeo, glassMat)
  face.scale.set(1.06, 1.02, 1.06)
  head.add(face)

  // The iris sits proud of the faceplate. Tucked behind transmissive glass it
  // loses its edge and the drone stops having a point to look at you with.
  const eyeGeo = bin.add(new THREE.TorusGeometry(0.15, 0.028, S(10), S(28)))
  const eye = new THREE.Mesh(eyeGeo, eyeMat)
  eye.position.set(0, 0.02, 0.585)
  head.add(eye)

  // Six armour petals orbiting the skull, breathing open and shut.
  const PETALS = 6
  const petalGeo = bin.add(new THREE.SphereGeometry(
    0.3, S(20), S(14), 0, Math.PI * 0.7, 0, Math.PI * 0.55,
  ))
  const petals = new THREE.InstancedMesh(petalGeo, petalMat, PETALS)
  head.add(petals)

  // ── Body spindle ───────────────────────────────────────────────────────────
  const spindleProfile: THREE.Vector2[] = [
    new THREE.Vector2(0.02, 0.42), new THREE.Vector2(0.26, 0.3),
    new THREE.Vector2(0.4, 0.02), new THREE.Vector2(0.36, -0.3),
    new THREE.Vector2(0.22, -0.62), new THREE.Vector2(0.02, -0.78),
  ]
  const spindleGeo = bin.add(new THREE.LatheGeometry(spindleProfile, S(36)))
  const spindle = new THREE.Mesh(spindleGeo, shellMat)
  spindle.position.y = -0.15
  root.add(spindle)

  const collarGeo = bin.add(new THREE.TorusGeometry(0.44, 0.05, S(12), S(36)))
  const collar = new THREE.Mesh(collarGeo, petalMat)
  collar.rotation.x = Math.PI / 2
  collar.position.y = 0.24
  root.add(collar)

  // Filament ring — 24 emissive slats around the collar, the amplitude readout.
  const FIL = 24
  const filGeo = bin.add(new THREE.BoxGeometry(0.03, 0.12, 0.03))
  const filament = new THREE.InstancedMesh(filGeo, filamentMat, FIL)
  withInstanceColor(filament, new THREE.Color(pal.key))
  filament.position.y = 0.24
  root.add(filament)

  const coreGeo = bin.add(new THREE.OctahedronGeometry(0.16, 1))
  const coreMat = glowMaterial(bin, pal.accent)
  const core = new THREE.Mesh(coreGeo, coreMat)
  core.position.y = -0.05
  root.add(core)

  // Two additive orbit ribbons.
  const ribbonGeo = bin.add(new THREE.TorusGeometry(1.0, 0.008, S(8), S(72)))
  const ribbons: THREE.Mesh[] = []
  for (let i = 0; i < 2; i++) {
    const r = new THREE.Mesh(ribbonGeo, ribbonMat)
    r.scale.setScalar(1 + i * 0.22)
    r.rotation.set(Math.PI / 2 + i * 0.9, 0, i * 1.1)
    r.position.y = 0.35
    root.add(r)
    ribbons.push(r)
  }

  const mtx = new THREE.Matrix4()
  const pos = new THREE.Vector3()
  const quat = new THREE.Quaternion()
  const eul = new THREE.Euler()
  const scl = new THREE.Vector3(1, 1, 1)
  const col = new THREE.Color()
  const keyColor = new THREE.Color(pal.key)
  const accentColor = new THREE.Color(pal.accent)

  return {
    root,
    camera: { fov: 34, z: 5.4, y: 0.2, targetY: 0.25 },
    bloom: { strength: 1.0, radius: 0.8, threshold: 0.55 },
    aberration: 0.0032,
    drawCalls: 11,
    update(f) {
      const en = clamp01(f.energy)
      const still = f.reduced

      const breath = still ? 0 : Math.sin(f.t * 0.7) * 0.02 + en * 0.02
      spindle.scale.set(1 + breath * 0.5, 1 - breath * 0.3, 1 + breath * 0.5)

      if (!still) {
        root.position.y = Math.sin(f.t * 0.85) * 0.07 + Math.sin(f.t * 0.49) * 0.035
        root.rotation.z = Math.sin(f.t * 0.41) * 0.035
        spindle.rotation.y = f.t * 0.25
      } else {
        root.position.y = 0
        root.rotation.z = 0
      }

      trackPointer(head, f, 0.55, 0.32, 0.06)

      const open = 1 - f.blink
      eye.scale.set(1, Math.max(0.06, open), 1)
      eyeMat.color.copy(keyColor).multiplyScalar(0.8 + open * 1.6 + en * 1.0)

      // Petals fan out with energy and drift open while listening. The 0.8 base
      // clears the 0.52 skull — any tighter and they sink inside it.
      const spread = 0.8 + en * 0.3 + (f.state === 'listening' ? 0.16 : 0)
        + (still ? 0 : Math.sin(f.t * 0.9) * 0.04)
      for (let i = 0; i < PETALS; i++) {
        const a = (i / PETALS) * Math.PI * 2 + (still ? 0 : f.t * 0.18)
        pos.set(Math.cos(a) * spread, Math.sin(a * 2 + f.t * 0.3) * 0.08, Math.sin(a) * spread)
        eul.set(0, -a + Math.PI / 2, Math.PI * 0.5 - spread * 0.5)
        quat.setFromEuler(eul)
        scl.setScalar(0.85 + en * 0.12)
        mtx.compose(pos, quat, scl)
        petals.setMatrixAt(i, mtx)
      }
      petals.instanceMatrix.needsUpdate = true

      // Filament ring: each slat is one band of the equaliser.
      for (let i = 0; i < FIL; i++) {
        const level = bandLevel(f.t, i % 8, en, f.state)
        const a = (i / FIL) * Math.PI * 2
        const r = 0.46 + level * 0.1
        pos.set(Math.cos(a) * r, 0, Math.sin(a) * r)
        eul.set(0, -a, 0)
        quat.setFromEuler(eul)
        scl.set(1, 0.3 + level * 2.2, 1)
        mtx.compose(pos, quat, scl)
        filament.setMatrixAt(i, mtx)
        col.copy(accentColor).lerp(keyColor, level).multiplyScalar(0.3 + level * 1.6)
        filament.setColorAt(i, col)
      }
      filament.instanceMatrix.needsUpdate = true
      if (filament.instanceColor) filament.instanceColor.needsUpdate = true

      const pulse = 0.7 + Math.sin(f.t * (f.state === 'thinking' ? 7 : 1.9)) * 0.25 + en
      coreMat.color.copy(accentColor).multiplyScalar(0.6 + pulse * 0.8)
      core.rotation.set(f.t * 0.6, f.t * 0.9, 0)
      core.scale.setScalar(1 + en * 0.3)

      shellRim.uRimStrength.value = 0.8 + en * 1.1
      petalRim.uRimStrength.value = 1.0 + en * 1.0
      glassMat.iridescence = 0.6 + en * 0.4

      ribbonMat.opacity = 0.22 + en * 0.3 + (f.state === 'thinking' ? 0.25 : 0)
      if (!still) {
        ribbons.forEach((r, i) => {
          r.rotation.y = f.t * (0.5 + i * 0.45) * (i % 2 ? -1 : 1)
        })
      }
    },
    dispose() { bin.dispose() },
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// EMBER — faceted obsidian mech. Iridescent carbon facets, hex torso,
// twelve amber jaw vents as the amplitude readout.
// ═════════════════════════════════════════════════════════════════════════════
function buildEmberRobot(pal: VariantPalette, q: VariantQuality): VariantBuild {
  const bin = new Bin()
  const root = new THREE.Group()
  const S = (n: number) => Math.max(6, Math.round(n * q.seg))

  const surf = buildSurface(bin, {
    seed: 71, scale: 5, stretch: 1, rough: [0.24, 0.62], metal: [0.55, 0.95], bump: 2.6, octaves: 5,
  })

  const carbonMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: pal.deep,
    metalness: 1, roughness: 1,
    metalnessMap: surf.orm, roughnessMap: surf.orm,
    normalMap: surf.normal, normalScale: new THREE.Vector2(0.9, 0.9),
    // Restrained iridescence: at full strength over a faceted shell the thin-film
    // hue swings per facet and the armour reads as bruised rather than coated.
    iridescence: 0.45, iridescenceIOR: 1.5, iridescenceThicknessRange: [140, 360],
    flatShading: true,
    envMapIntensity: 1.5,
  }))
  const carbonRim = addFresnel(carbonMat, pal.rim, 2.4, 0.9)

  const plateMat = bin.add(new THREE.MeshPhysicalMaterial({
    color: pal.shell,
    metalness: 1, roughness: 1,
    metalnessMap: surf.orm, roughnessMap: surf.orm,
    normalMap: surf.normal, normalScale: new THREE.Vector2(0.7, 0.7),
    clearcoat: 0.55, clearcoatRoughness: 0.35,
    envMapIntensity: 1.1,
  }))
  const plateRim = addFresnel(plateMat, pal.accent, 3.0, 0.6)

  const hotMat = bin.add(new THREE.MeshStandardMaterial({
    color: pal.accent, emissive: pal.accent, emissiveIntensity: 1.2,
    metalness: 0.4, roughness: 0.35,
  }))

  const eyeMat = glowMaterial(bin, pal.key)
  const ventMat = glowMaterial(bin, pal.key)
  const coreMat = glowMaterial(bin, pal.key)
  const auraMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.accent, toneMapped: false, transparent: true, opacity: 0.0,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  }))

  // ── Head ───────────────────────────────────────────────────────────────────
  const head = new THREE.Group()
  head.position.set(0, 1.05, 0)
  root.add(head)

  const skullGeo = bin.add(new THREE.IcosahedronGeometry(0.58, 1))
  const skull = new THREE.Mesh(skullGeo, carbonMat)
  skull.scale.set(1, 1.05, 0.92)
  head.add(skull)

  const browGeo = bin.add(new THREE.BoxGeometry(0.86, 0.14, 0.26))
  const brow = new THREE.Mesh(browGeo, plateMat)
  brow.position.set(0, 0.16, 0.4)
  brow.rotation.x = -0.18
  head.add(brow)

  const eyeGeo = bin.add(new THREE.BoxGeometry(0.19, 0.07, 0.04))
  const eyes = new THREE.InstancedMesh(eyeGeo, eyeMat, 2)
  head.add(eyes)

  // Twelve jaw vents, the amplitude readout.
  const VENTS = 12
  const ventGeo = bin.add(new THREE.BoxGeometry(0.045, 0.09, 0.04))
  const vents = new THREE.InstancedMesh(ventGeo, ventMat, VENTS)
  withInstanceColor(vents, new THREE.Color(pal.key))
  head.add(vents)

  const jawGeo = bin.add(new THREE.CylinderGeometry(0.34, 0.24, 0.26, 6, 1))
  const jaw = new THREE.Mesh(jawGeo, plateMat)
  jaw.position.set(0, -0.36, 0.2)
  jaw.rotation.x = 0.25
  head.add(jaw)

  // ── Torso — hexagonal prism lattice ────────────────────────────────────────
  const torso = new THREE.Group()
  root.add(torso)

  const trunkGeo = bin.add(new THREE.CylinderGeometry(0.72, 0.5, 1.5, 6, 1))
  const trunk = new THREE.Mesh(trunkGeo, carbonMat)
  trunk.position.y = -0.15
  trunk.scale.set(1.1, 1, 0.78)
  torso.add(trunk)

  // Hot bands sit in the taper of the trunk. The unit-radius geometry is scaled
  // per band to the trunk's radius at that height — a single fixed radius makes
  // the top band float off the shell and the bottom one sink into it.
  const bandGeo = bin.add(new THREE.CylinderGeometry(1, 1, 0.05, 6, 1, true))
  const bands = new THREE.InstancedMesh(bandGeo, hotMat, 3)
  torso.add(bands)
  {
    const m = new THREE.Matrix4()
    const qt = new THREE.Quaternion()
    // Trunk radius runs 0.72 at y = +0.60 down to 0.50 at y = −0.90.
    const trunkR = (y: number) => 0.5 + 0.22 * THREE.MathUtils.clamp((y + 0.9) / 1.5, 0, 1)
    const place = (i: number, y: number) => {
      const r = trunkR(y) * 1.012
      m.compose(new THREE.Vector3(0, y, 0), qt, new THREE.Vector3(r * 1.1, 1, r * 0.78))
      bands.setMatrixAt(i, m)
    }
    place(0, 0.34); place(1, -0.16); place(2, -0.62)
    bands.instanceMatrix.needsUpdate = true
  }

  const coreGeo = bin.add(new THREE.OctahedronGeometry(0.2, 0))
  const core = new THREE.Mesh(coreGeo, coreMat)
  core.position.set(0, 0.12, 0.6)
  torso.add(core)

  const coreRingGeo = bin.add(new THREE.TorusGeometry(0.3, 0.035, S(8), 6))
  const coreRing = new THREE.Mesh(coreRingGeo, plateMat)
  coreRing.position.set(0, 0.12, 0.58)
  torso.add(coreRing)

  // Shoulder pylons rake up and outward. Laid closer to horizontal they read as
  // flat wings hanging off the torso rather than as armour.
  const pylonGeo = bin.add(new THREE.ConeGeometry(0.26, 0.62, 6, 1))
  const pylons = new THREE.InstancedMesh(pylonGeo, plateMat, 2)
  {
    const m = new THREE.Matrix4()
    const qtL = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, 0, Math.PI * 0.24))
    const qtR = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, 0, -Math.PI * 0.24))
    const sc = new THREE.Vector3(0.95, 1, 0.8)
    m.compose(new THREE.Vector3(-0.72, 0.44, 0), qtL, sc); pylons.setMatrixAt(0, m)
    m.compose(new THREE.Vector3(0.72, 0.44, 0), qtR, sc); pylons.setMatrixAt(1, m)
    pylons.instanceMatrix.needsUpdate = true
  }
  torso.add(pylons)

  const exhaustGeo = bin.add(new THREE.CylinderGeometry(0.07, 0.1, 0.5, 6))
  const exhausts = new THREE.InstancedMesh(exhaustGeo, hotMat, 4)
  withInstanceColor(exhausts, new THREE.Color(pal.accent))
  torso.add(exhausts)
  {
    const m = new THREE.Matrix4()
    const qt = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.35, 0, 0))
    const sc = new THREE.Vector3(1, 1, 1)
    const xs = [-0.42, -0.14, 0.14, 0.42]
    xs.forEach((x, i) => {
      m.compose(new THREE.Vector3(x, 0.5, -0.52), qt, sc)
      exhausts.setMatrixAt(i, m)
    })
    exhausts.instanceMatrix.needsUpdate = true
  }

  const auraGeo = bin.add(new THREE.RingGeometry(1.05, 1.5, S(48), 1))
  const aura = new THREE.Mesh(auraGeo, auraMat)
  aura.rotation.x = -Math.PI / 2
  aura.position.y = -1.35
  root.add(aura)

  const mtx = new THREE.Matrix4()
  const pos = new THREE.Vector3()
  const quat = new THREE.Quaternion()
  const scl = new THREE.Vector3(1, 1, 1)
  const col = new THREE.Color()
  const keyColor = new THREE.Color(pal.key)
  const accentColor = new THREE.Color(pal.accent)
  let auraOpacity = 0

  return {
    root,
    camera: { fov: 33, z: 6.0, y: 0.25, targetY: 0.15 },
    bloom: { strength: 1.1, radius: 0.62, threshold: 0.5 },
    aberration: 0.0045,
    drawCalls: 13,
    update(f) {
      const en = clamp01(f.energy)
      const still = f.reduced

      const breath = still ? 0 : Math.sin(f.t * 0.55) * 0.016 + en * 0.014
      trunk.scale.set(1.1 + breath * 0.5, 1 + breath, 0.78 + breath * 0.4)

      if (!still) {
        root.position.y = Math.sin(f.t * 0.8) * 0.045 + Math.sin(f.t * 0.53) * 0.025
        torso.rotation.y = Math.sin(f.t * 0.27) * 0.09
        torso.rotation.z = Math.sin(f.t * 0.44) * 0.025
      } else {
        root.position.y = 0
      }

      trackPointer(head, f, 0.4, 0.24, 0.045)

      // Eyes: two hard slits. Blink squashes them from the middle.
      const open = 1 - f.blink
      for (let i = 0; i < 2; i++) {
        pos.set(i === 0 ? -0.2 : 0.2, 0.02, 0.52)
        scl.set(1, Math.max(0.06, open), 1)
        mtx.compose(pos, quat, scl)
        eyes.setMatrixAt(i, mtx)
      }
      eyes.instanceMatrix.needsUpdate = true
      eyeMat.color.copy(keyColor).multiplyScalar(0.9 + open * 1.5 + en * 0.9)

      // Jaw vents.
      for (let i = 0; i < VENTS; i++) {
        const level = bandLevel(f.t, i, en, f.state)
        const x = (i - (VENTS - 1) / 2) * 0.062
        pos.set(x, -0.26, 0.5 - Math.abs(x) * 0.4)
        // The floor keeps the vents lit at rest — a bank that collapses to
        // nothing between words reads as a fault, not as silence.
        scl.set(1, 0.45 + level * 2.2, 1)
        mtx.compose(pos, quat, scl)
        vents.setMatrixAt(i, mtx)
        col.copy(accentColor).lerp(keyColor, level).multiplyScalar(0.5 + level * 1.6)
        vents.setColorAt(i, col)
      }
      vents.instanceMatrix.needsUpdate = true
      if (vents.instanceColor) vents.instanceColor.needsUpdate = true

      // Exhaust flare tracks the voice.
      const flare = 0.4 + en * 1.6
      for (let i = 0; i < 4; i++) {
        col.copy(accentColor).multiplyScalar(flare * (0.7 + 0.3 * Math.sin(f.t * 5 + i)))
        exhausts.setColorAt(i, col)
      }
      if (exhausts.instanceColor) exhausts.instanceColor.needsUpdate = true
      hotMat.emissiveIntensity = 0.8 + en * 1.6

      const pulse = 1 + Math.sin(f.t * (f.state === 'thinking' ? 8 : 1.4)) * 0.3 + en
      coreMat.color.copy(keyColor).multiplyScalar(0.6 + pulse * 0.8)
      core.rotation.set(f.t * 0.4, f.t * 0.7, 0)
      core.scale.setScalar(1 + en * 0.35)

      carbonRim.uRimStrength.value = 0.7 + en * 1.0
      plateRim.uRimStrength.value = 0.5 + en * 0.9

      const wantAura = f.state === 'idle' ? 0.08 : 0.22 + en * 0.35
      auraOpacity = lerp(auraOpacity, wantAura, 0.05)
      auraMat.opacity = auraOpacity
      aura.scale.setScalar(1 + en * 0.2)
    },
    dispose() { bin.dispose() },
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// ORB A (Sentinel) — volumetric ray-marched plasma core.
// The sphere is a proxy: the fragment shader marches the analytic unit sphere
// in object space and integrates fbm density, so the interior has real depth
// rather than being a painted gradient.
// ═════════════════════════════════════════════════════════════════════════════
const VOLUME_VERT = /* glsl */`
varying vec3 vObj;
void main() {
  vObj = position;
  gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
}
`

const VOLUME_FRAG = /* glsl */`
precision highp float;
varying vec3 vObj;
uniform vec3 uCamObj;
uniform float uTime;
uniform float uEnergy;
uniform vec3 uKey;
uniform vec3 uAccent;
uniform float uDensity;

float h3( vec3 p ) {
  return fract( sin( dot( p, vec3( 127.1, 311.7, 74.7 ) ) ) * 43758.5453123 );
}
float vnoise( vec3 p ) {
  vec3 i = floor( p );
  vec3 f = fract( p );
  f = f * f * ( 3.0 - 2.0 * f );
  float n000 = h3( i );
  float n100 = h3( i + vec3( 1.0, 0.0, 0.0 ) );
  float n010 = h3( i + vec3( 0.0, 1.0, 0.0 ) );
  float n110 = h3( i + vec3( 1.0, 1.0, 0.0 ) );
  float n001 = h3( i + vec3( 0.0, 0.0, 1.0 ) );
  float n101 = h3( i + vec3( 1.0, 0.0, 1.0 ) );
  float n011 = h3( i + vec3( 0.0, 1.0, 1.0 ) );
  float n111 = h3( i + vec3( 1.0, 1.0, 1.0 ) );
  return mix(
    mix( mix( n000, n100, f.x ), mix( n010, n110, f.x ), f.y ),
    mix( mix( n001, n101, f.x ), mix( n011, n111, f.x ), f.y ),
    f.z
  );
}
float fbm( vec3 p ) {
  float s = 0.0;
  float a = 0.5;
  for ( int i = 0; i < 3; i ++ ) {
    s += vnoise( p ) * a;
    p *= 2.07;
    a *= 0.5;
  }
  return s;
}

void main() {
  vec3 ro = uCamObj;
  vec3 rd = normalize( vObj - ro );

  // Analytic unit-sphere entry/exit for the march bounds.
  float b = dot( ro, rd );
  float c = dot( ro, ro ) - 1.0;
  float disc = b * b - c;
  if ( disc < 0.0 ) discard;
  float sq = sqrt( disc );
  float t0 = max( - b - sq, 0.0 );
  float t1 = - b + sq;
  if ( t1 <= t0 ) discard;

  float span = t1 - t0;
  float stepLen = span / float( VOL_STEPS );
  vec3 acc = vec3( 0.0 );
  float alpha = 0.0;
  float swirl = uTime * 0.16;

  for ( int i = 0; i < VOL_STEPS; i ++ ) {
    float t = t0 + ( float( i ) + 0.5 ) * stepLen;
    vec3 p = ro + rd * t;
    float r = length( p );

    // Twist about Y so the plasma churns instead of sliding.
    float ang = swirl + r * 1.9;
    float cs = cos( ang ), sn = sin( ang );
    vec3 q = vec3( p.x * cs - p.z * sn, p.y, p.x * sn + p.z * cs );

    float n = fbm( q * ( 2.4 + uEnergy * 1.1 ) + vec3( 0.0, uTime * 0.24, 0.0 ) );
    // Value-noise fbm clusters hard around 0.5, so the density window sits
    // across that band — a window up at 0.9 leaves the volume empty. The 0.3
    // floor keeps a continuous medium under the noise, so the core reads as a
    // glowing body with structure rather than as a lopsided cloud.
    float shell = pow( 1.0 - smoothstep( 0.0, 1.0, r ), 1.2 );
    float d = ( 0.45 + 0.55 * smoothstep( 0.3, 0.72, n ) ) * shell * uDensity;
    if ( d <= 0.001 ) continue;

    vec3 col = mix( uAccent, uKey, clamp( n * 1.4 - r * 0.4, 0.0, 1.0 ) );
    col += uKey * pow( 1.0 - r, 3.0 ) * ( 1.4 + uEnergy * 1.5 );

    float a = d * stepLen * 7.0;
    acc += col * a * ( 1.0 - alpha );
    alpha += a * ( 1.0 - alpha );
    if ( alpha > 0.98 ) break;
  }

  if ( alpha < 0.004 ) discard;
  gl_FragColor = vec4( acc, clamp( alpha, 0.0, 1.0 ) );
}
`

function buildSentinelOrb(pal: VariantPalette, q: VariantQuality): VariantBuild {
  const bin = new Bin()
  const root = new THREE.Group()
  const S = (n: number) => Math.max(8, Math.round(n * q.seg))

  const volGeo = bin.add(new THREE.SphereGeometry(1, S(48), S(32)))
  const volMat = bin.add(new THREE.ShaderMaterial({
    vertexShader: VOLUME_VERT,
    fragmentShader: VOLUME_FRAG,
    defines: { VOL_STEPS: q.volumeSteps },
    uniforms: {
      uCamObj: { value: new THREE.Vector3(0, 0, 4) },
      uTime: { value: 0 },
      uEnergy: { value: 0 },
      uDensity: { value: 1 },
      uKey: { value: new THREE.Color(pal.key) },
      uAccent: { value: new THREE.Color(pal.accent) },
    },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.FrontSide,
  }))
  const volume = new THREE.Mesh(volGeo, volMat)
  root.add(volume)

  // Containment cage — a faceted shell with the same Fresnel language as the
  // Sentinel robot, so the two obviously belong to one machine.
  const cageGeo = bin.add(new THREE.IcosahedronGeometry(1.24, 1))
  const cageMat = bin.add(new THREE.MeshStandardMaterial({
    color: pal.shell, metalness: 1, roughness: 0.25,
    transparent: true, opacity: 0.16,
    wireframe: true, envMapIntensity: 1.4,
  }))
  const cageRim = addFresnel(cageMat, pal.rim, 2.0, 1.4)
  const cage = new THREE.Mesh(cageGeo, cageMat)
  root.add(cage)

  const ringGeo = bin.add(new THREE.TorusGeometry(1.45, 0.012, S(8), S(96)))
  const ringMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.key, toneMapped: false, transparent: true, opacity: 0.5,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }))
  const rings: THREE.Mesh[] = []
  for (let i = 0; i < 3; i++) {
    const r = new THREE.Mesh(ringGeo, ringMat)
    r.scale.setScalar(1 + i * 0.16)
    r.rotation.set(Math.PI / 2 + i * 0.42, 0, i * 0.8)
    root.add(r)
    rings.push(r)
  }

  const camObj = new THREE.Vector3()
  const inv = new THREE.Matrix4()

  return {
    root,
    camera: { fov: 40, z: 6.2, y: 0, targetY: 0 },
    bloom: { strength: 1.05, radius: 0.85, threshold: 0.28 },
    aberration: 0.003,
    drawCalls: 5,
    update(f) {
      const en = clamp01(f.energy)
      volMat.uniforms.uTime.value = f.reduced ? 0 : f.t
      volMat.uniforms.uEnergy.value = en
      volMat.uniforms.uDensity.value = 0.85 + en * 0.7
        + (f.state === 'thinking' ? 0.35 : 0)

      const s = 1 + (f.reduced ? 0 : Math.sin(f.t * 0.6) * 0.02) + en * 0.09
      volume.scale.setScalar(s)
      cage.scale.setScalar(s)
      if (!f.reduced) {
        cage.rotation.y = f.t * 0.09
        cage.rotation.x = Math.sin(f.t * 0.2) * 0.2
        rings.forEach((r, i) => { r.rotation.y = f.t * (0.22 + i * 0.13) * (i % 2 ? -1 : 1) })
      }
      cageRim.uRimStrength.value = 1.0 + en * 1.6
      ringMat.opacity = 0.32 + en * 0.4 + (f.state === 'listening' ? 0.2 : 0)

      // The march runs in object space, so the camera has to be brought there.
      volume.updateWorldMatrix(true, false)
      inv.copy(volume.matrixWorld).invert()
      camObj.copy(f.cameraWorld || ORIGIN).applyMatrix4(inv)
      volMat.uniforms.uCamObj.value.copy(camObj)
    },
    dispose() { bin.dispose() },
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// ORB B (Aurora) — layered refractive glass. Three nested transmissive shells
// with different IOR and thickness, so light bends through each layer.
// ═════════════════════════════════════════════════════════════════════════════
function buildAuroraOrb(pal: VariantPalette, q: VariantQuality): VariantBuild {
  const bin = new Bin()
  const root = new THREE.Group()
  const S = (n: number) => Math.max(12, Math.round(n * q.seg))

  const surf = buildSurface(bin, {
    seed: 37, scale: 9, stretch: 14, rough: [0.0, 0.09], metal: [0, 0], bump: 0.5,
  })

  const layers: THREE.Mesh[] = []
  const layerMats: THREE.MeshPhysicalMaterial[] = []
  const specs = [
    { r: 1.28, ior: 1.32, thickness: 0.35, rough: 0.02, irid: 1.0 },
    { r: 1.05, ior: 1.58, thickness: 0.7, rough: 0.05, irid: 0.6 },
    { r: 0.82, ior: 1.9, thickness: 1.1, rough: 0.0, irid: 0.0 },
  ].slice(0, Math.max(1, q.glassLayers))

  const glassGeo = bin.add(new THREE.SphereGeometry(1, S(56), S(38)))
  specs.forEach((spec, i) => {
    const m = bin.add(new THREE.MeshPhysicalMaterial({
      color: 0xffffff,
      metalness: 0, roughness: spec.rough,
      roughnessMap: surf.orm,
      normalMap: surf.normal, normalScale: new THREE.Vector2(0.12, 0.12),
      transmission: 1, ior: spec.ior, thickness: spec.thickness,
      iridescence: spec.irid, iridescenceIOR: 1.9, iridescenceThicknessRange: [180, 600],
      attenuationColor: new THREE.Color(i === 0 ? pal.key : pal.accent),
      attenuationDistance: 1.4 - i * 0.35,
      clearcoat: i === 0 ? 1 : 0, clearcoatRoughness: 0.03,
      envMapIntensity: 2.2,
      transparent: false,
      side: THREE.FrontSide,
    }))
    const mesh = new THREE.Mesh(glassGeo, m)
    mesh.scale.setScalar(spec.r)
    mesh.renderOrder = 10 - i
    root.add(mesh)
    layers.push(mesh)
    layerMats.push(m)
  })

  // Emissive filament suspended in the glass — visible through every layer, so
  // the refraction has something to bend.
  const filamentGeo = bin.add(new THREE.TorusKnotGeometry(0.34, 0.038, S(180), S(14), 2, 3))
  const filamentMat = glowMaterial(bin, pal.key)
  const filament = new THREE.Mesh(filamentGeo, filamentMat)
  root.add(filament)

  const coreGeo = bin.add(new THREE.IcosahedronGeometry(0.19, 2))
  const coreMat = glowMaterial(bin, pal.accent)
  const core = new THREE.Mesh(coreGeo, coreMat)
  root.add(core)

  const haloGeo = bin.add(new THREE.TorusGeometry(1.6, 0.01, S(8), S(96)))
  const haloMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.accent, toneMapped: false, transparent: true, opacity: 0.4,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }))
  const halos: THREE.Mesh[] = []
  for (let i = 0; i < 2; i++) {
    const h = new THREE.Mesh(haloGeo, haloMat)
    h.scale.setScalar(1 + i * 0.18)
    h.rotation.set(Math.PI / 2 + i * 0.7, 0, i * 1.2)
    root.add(h)
    halos.push(h)
  }

  const keyColor = new THREE.Color(pal.key)
  const accentColor = new THREE.Color(pal.accent)

  return {
    root,
    camera: { fov: 40, z: 6.4, y: 0, targetY: 0 },
    bloom: { strength: 0.8, radius: 0.9, threshold: 0.4 },
    aberration: 0.0055,
    drawCalls: 4 + specs.length,
    update(f) {
      const en = clamp01(f.energy)
      const breathe = f.reduced ? 1 : 1 + Math.sin(f.t * 0.55) * 0.018
      layers.forEach((mesh, i) => {
        mesh.scale.setScalar(specs[i].r * (breathe + en * 0.05 * (i + 1)))
        if (!f.reduced) mesh.rotation.y = f.t * (0.06 + i * 0.04) * (i % 2 ? -1 : 1)
      })
      layerMats.forEach((m, i) => {
        m.thickness = specs[i].thickness * (1 + en * 0.5)
        if (i === 0) m.iridescence = 0.55 + en * 0.45
      })

      if (!f.reduced) {
        filament.rotation.set(f.t * 0.35, f.t * 0.5, f.t * 0.18)
      }
      filament.scale.setScalar(1 + en * 0.22)
      filamentMat.color.copy(keyColor).multiplyScalar(0.8 + en * 1.6
        + Math.sin(f.t * (f.state === 'thinking' ? 7 : 1.8)) * 0.25)

      core.scale.setScalar(1 + en * 0.4 + (f.reduced ? 0 : Math.sin(f.t * 2.4) * 0.05))
      coreMat.color.copy(accentColor).multiplyScalar(1 + en * 1.8)

      haloMat.opacity = 0.22 + en * 0.35 + (f.state === 'listening' ? 0.22 : 0)
      if (!f.reduced) {
        halos.forEach((h, i) => { h.rotation.y = f.t * (0.3 + i * 0.22) * (i % 2 ? -1 : 1) })
      }
    },
    dispose() { bin.dispose() },
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// ORB C (Ember) — audio-reactive point-cloud swarm. One draw call, all motion
// computed in the vertex shader from per-point seeds.
// ═════════════════════════════════════════════════════════════════════════════
const SWARM_VERT = /* glsl */`
precision highp float;
attribute vec3 aDir;
attribute float aSeed;
attribute float aRadius;
uniform float uTime;
uniform float uEnergy;
uniform float uSize;
uniform float uPixelRatio;
uniform float uBurst;
varying float vGlow;
varying float vSeed;

vec3 swirl( vec3 p, float a ) {
  float c = cos( a ), s = sin( a );
  return vec3( p.x * c - p.z * s, p.y, p.x * s + p.z * c );
}

void main() {
  float phase = aSeed * 6.2831853;

  // Shell radius pumps with the voice; each point lags by its own seed so the
  // surface ripples instead of scaling as one rigid ball.
  float pump = sin( uTime * 2.2 + phase ) * ( 0.03 + uEnergy * 0.16 );
  float r = aRadius * ( 1.0 + pump + uBurst * 0.22 );

  vec3 p = aDir * r;
  p = swirl( p, uTime * ( 0.12 + 0.28 * aSeed ) + p.y * 0.6 );
  p.y += sin( uTime * 0.9 + phase ) * 0.05;

  vGlow = clamp( pump * 4.0 + uEnergy * 0.9 + ( 1.0 - aRadius ) * 0.6, 0.0, 1.6 );
  vSeed = aSeed;

  vec4 mv = modelViewMatrix * vec4( p, 1.0 );
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uSize * uPixelRatio * ( 1.0 + uEnergy * 0.7 ) / max( 0.5, - mv.z );
}
`

const SWARM_FRAG = /* glsl */`
precision highp float;
uniform vec3 uKey;
uniform vec3 uAccent;
varying float vGlow;
varying float vSeed;

void main() {
  vec2 d = gl_PointCoord - vec2( 0.5 );
  float r2 = dot( d, d );
  if ( r2 > 0.25 ) discard;
  float falloff = 1.0 - smoothstep( 0.0, 0.25, r2 );
  vec3 col = mix( uAccent, uKey, clamp( vGlow * 0.7 + vSeed * 0.3, 0.0, 1.0 ) );
  gl_FragColor = vec4( col * ( 0.35 + vGlow ), falloff * ( 0.35 + vGlow * 0.55 ) );
}
`

function buildEmberOrb(pal: VariantPalette, q: VariantQuality): VariantBuild {
  const bin = new Bin()
  const root = new THREE.Group()
  const S = (n: number) => Math.max(8, Math.round(n * q.seg))

  const N = q.swarmCount
  const dir = new Float32Array(N * 3)
  const seed = new Float32Array(N)
  const radius = new Float32Array(N)
  const positions = new Float32Array(N * 3)

  for (let i = 0; i < N; i++) {
    // Uniform sphere directions, then bias radius so the swarm is a shell with
    // a denser nucleus rather than a uniform ball.
    const u = Math.random() * 2 - 1
    const th = Math.random() * Math.PI * 2
    const s = Math.sqrt(1 - u * u)
    dir[i * 3] = s * Math.cos(th)
    dir[i * 3 + 1] = u
    dir[i * 3 + 2] = s * Math.sin(th)
    const t = Math.random()
    radius[i] = t < 0.24 ? 0.12 + Math.random() * 0.35 : 0.72 + Math.random() * 0.5
    seed[i] = Math.random()
  }

  const geo = bin.add(new THREE.BufferGeometry())
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geo.setAttribute('aDir', new THREE.BufferAttribute(dir, 3))
  geo.setAttribute('aSeed', new THREE.BufferAttribute(seed, 1))
  geo.setAttribute('aRadius', new THREE.BufferAttribute(radius, 1))
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 2)

  const swarmMat = bin.add(new THREE.ShaderMaterial({
    vertexShader: SWARM_VERT,
    fragmentShader: SWARM_FRAG,
    uniforms: {
      uTime: { value: 0 },
      uEnergy: { value: 0 },
      uBurst: { value: 0 },
      uSize: { value: 26 },
      uPixelRatio: { value: 1 },
      uKey: { value: new THREE.Color(pal.key) },
      uAccent: { value: new THREE.Color(pal.accent) },
    },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  }))
  const swarm = new THREE.Points(geo, swarmMat)
  root.add(swarm)

  const coreGeo = bin.add(new THREE.IcosahedronGeometry(0.3, 1))
  const coreMat = bin.add(new THREE.MeshStandardMaterial({
    color: pal.deep, metalness: 1, roughness: 0.3,
    emissive: pal.key, emissiveIntensity: 0.8,
    flatShading: true, envMapIntensity: 1.6,
  }))
  const coreRim = addFresnel(coreMat, pal.key, 1.8, 2.0)
  const core = new THREE.Mesh(coreGeo, coreMat)
  root.add(core)

  const shellGeo = bin.add(new THREE.IcosahedronGeometry(1.5, 1))
  const shellMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.accent, toneMapped: false, wireframe: true,
    transparent: true, opacity: 0.12,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }))
  const shell = new THREE.Mesh(shellGeo, shellMat)
  root.add(shell)

  const ringGeo = bin.add(new THREE.TorusGeometry(1.7, 0.008, S(8), S(96)))
  const ringMat = bin.add(new THREE.MeshBasicMaterial({
    color: pal.key, toneMapped: false, transparent: true, opacity: 0.35,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }))
  const ring = new THREE.Mesh(ringGeo, ringMat)
  ring.rotation.x = Math.PI / 2.3
  root.add(ring)

  let burst = 0
  let prevEnergy = 0

  return {
    root,
    camera: { fov: 40, z: 6.4, y: 0, targetY: 0 },
    bloom: { strength: 1.25, radius: 0.7, threshold: 0.22 },
    aberration: 0.004,
    drawCalls: 4,
    update(f) {
      const en = clamp01(f.energy)
      // A transient kick on rising amplitude — a swarm that only follows the
      // level reads as a slider; the kick is what makes it feel struck.
      const rise = Math.max(0, en - prevEnergy)
      prevEnergy = en
      burst = Math.max(burst * 0.9, rise * 4)

      swarmMat.uniforms.uTime.value = f.reduced ? 0 : f.t
      swarmMat.uniforms.uEnergy.value = en
      swarmMat.uniforms.uBurst.value = burst
      swarmMat.uniforms.uSize.value = f.state === 'thinking' ? 30 : 26

      coreRim.uRimStrength.value = 1.4 + en * 2.2
      coreMat.emissiveIntensity = 0.6 + en * 1.6
        + Math.sin(f.t * (f.state === 'thinking' ? 8 : 1.6)) * 0.2
      core.scale.setScalar(1 + en * 0.3)
      if (!f.reduced) {
        core.rotation.set(f.t * 0.3, f.t * 0.45, 0)
        shell.rotation.set(-f.t * 0.08, f.t * 0.12, 0)
        ring.rotation.z = f.t * 0.24
      }
      shellMat.opacity = 0.08 + en * 0.16
      ringMat.opacity = 0.22 + en * 0.35 + (f.state === 'listening' ? 0.2 : 0)
    },
    dispose() { bin.dispose() },
  }
}

// ── Registry ─────────────────────────────────────────────────────────────────
const PALETTES: Record<VariantId, VariantPalette> = {
  sentinel: { key: 0x3ce9ff, accent: 0x0e93b8, shell: 0x9fb2c4, deep: 0x0a141f, rim: 0x7ff0ff },
  aurora:   { key: 0xd0aaff, accent: 0xff86cf, shell: 0xbfaee0, deep: 0x1a1030, rim: 0xecd2ff },
  ember:    { key: 0xffc06a, accent: 0xff5a1f, shell: 0x7a6f60, deep: 0x140f0a, rim: 0xffd79a },
}

export const VARIANTS: Record<VariantId, JarvisVariant> = {
  sentinel: {
    id: 'sentinel',
    label: 'Sentinel',
    description: 'Clearcoat plate armour in cyan steel, spine equaliser, ray-marched plasma core.',
    palette: PALETTES.sentinel,
    buildRobot: (q) => buildSentinelRobot(PALETTES.sentinel, q),
    buildOrb: (q) => buildSentinelOrb(PALETTES.sentinel, q),
  },
  aurora: {
    id: 'aurora',
    label: 'Aurora',
    description: 'Brushed anisotropic drone with iridescent faceplate and nested refractive glass.',
    palette: PALETTES.aurora,
    buildRobot: (q) => buildAuroraRobot(PALETTES.aurora, q),
    buildOrb: (q) => buildAuroraOrb(PALETTES.aurora, q),
  },
  ember: {
    id: 'ember',
    label: 'Ember',
    description: 'Faceted obsidian mech with iridescent carbon and an audio-reactive particle swarm.',
    palette: PALETTES.ember,
    buildRobot: (q) => buildEmberRobot(PALETTES.ember, q),
    buildOrb: (q) => buildEmberOrb(PALETTES.ember, q),
  },
}

export const VARIANT_LIST: JarvisVariant[] = [VARIANTS.sentinel, VARIANTS.aurora, VARIANTS.ember]

export const DEFAULT_ROBOT_VARIANT: VariantId = 'sentinel'
export const DEFAULT_ORB_VARIANT: VariantId = 'sentinel'

export function isVariantId(v: unknown): v is VariantId {
  return v === 'sentinel' || v === 'aurora' || v === 'ember'
}

/** Read a persisted selection. Safe to call during SSR — returns the fallback. */
export function readVariant(key: string, fallback: VariantId): VariantId {
  if (typeof window === 'undefined') return fallback
  try {
    const raw = window.localStorage.getItem(key)
    return isVariantId(raw) ? raw : fallback
  } catch {
    return fallback
  }
}

export function writeVariant(key: string, value: VariantId): void {
  if (typeof window === 'undefined') return
  try { window.localStorage.setItem(key, value) } catch { /* private mode */ }
}

/** Fired on `window` when either selection changes, so live scenes can swap. */
export const VARIANT_EVENT = 'jarvis-variant-change'

/**
 * Build an environment scene for PMREM. Emissive panels in the variant's own
 * colours, so each look reflects its own studio rather than a generic grey box.
 */
export function buildEnvScene(pal: VariantPalette): { scene: THREE.Scene; dispose(): void } {
  const bin = new Bin()
  const scene = new THREE.Scene()
  scene.background = new THREE.Color(0x05070c)

  const box = bin.add(new THREE.BoxGeometry(1, 1, 1))
  const panel = (color: number, intensity: number, x: number, y: number, z: number, sx: number, sy: number, sz: number) => {
    const m = bin.add(new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(intensity) }))
    const mesh = new THREE.Mesh(box, m)
    mesh.position.set(x, y, z)
    mesh.scale.set(sx, sy, sz)
    scene.add(mesh)
  }

  // Enclosing shell — a dark room so reflections have something to fall off to.
  const wallMat = bin.add(new THREE.MeshBasicMaterial({ color: 0x0a1018, side: THREE.BackSide }))
  const walls = new THREE.Mesh(box, wallMat)
  walls.scale.set(22, 22, 22)
  scene.add(walls)

  panel(0xffffff, 2.6, 0, 7, 2, 9, 0.4, 6)        // key from above
  panel(pal.key, 3.4, -6, 1.5, 3, 0.4, 5, 7)      // cold rim, left
  panel(pal.accent, 2.4, 6, 0.5, -2, 0.4, 4, 7)   // warm kicker, right
  panel(pal.key, 1.4, 0, -5, 3, 8, 0.4, 6)        // bounce from below
  panel(0xffffff, 1.2, 0, 1, -8, 8, 6, 0.4)       // back separation

  return {
    scene,
    dispose() {
      scene.clear()
      bin.dispose()
    },
  }
}
