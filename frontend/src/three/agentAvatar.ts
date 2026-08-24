/**
 * agentAvatar — the procedural humans who sit (and walk) around the room.
 *
 * Built from primitives rather than a downloaded rig, so the room stays fully
 * offline and cheap to draw. The trade is realism: these read as expressive and
 * alive, not photoreal. What sells them is motion — blinking, breathing,
 * gesturing, and an actual walk cycle — so the joint layout matters more than
 * the polygon count.
 *
 * Skeleton (all rotations are on parent groups, never on the meshes):
 *
 *   root ── hips ── torso ── neck ── head ── {eyes, brows, nose, mouth, hair}
 *            │        └───── shoulder.{l,r} ── upperArm ── elbow ── forearm
 *            └───── hip.{l,r} ── thigh ── knee ── shin ── foot
 *
 * Every seat shares one geometry set (`createAvatarKit`), so adding a seat costs
 * a handful of Object3Ds and no new GPU buffers. Gender changes transforms,
 * scale and which hair mesh is shown — never the geometry set.
 */
import * as THREE from 'three'

export type Gender = 'male' | 'female'

/** Skin tones, picked deterministically from the agent's name. */
const SKIN_TONES = [0x8d5524, 0xa9683c, 0xc68642, 0xe0ac69, 0x6b4226, 0xf1c27d]
const HAIR_TONES = [0x1c1410, 0x2b1b12, 0x3d2b1f, 0x0f0d0c]

/** Stable per-name hash so an agent keeps the same look across reloads. */
function hashName(name: string): number {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return h
}

export interface AvatarKit {
  head: THREE.SphereGeometry
  jaw: THREE.SphereGeometry
  neck: THREE.CylinderGeometry
  torso: THREE.CapsuleGeometry
  hips: THREE.CapsuleGeometry
  upperArm: THREE.CapsuleGeometry
  forearm: THREE.CapsuleGeometry
  hand: THREE.SphereGeometry
  thigh: THREE.CapsuleGeometry
  shin: THREE.CapsuleGeometry
  foot: THREE.BoxGeometry
  eyeWhite: THREE.SphereGeometry
  iris: THREE.SphereGeometry
  brow: THREE.BoxGeometry
  nose: THREE.ConeGeometry
  mouth: THREE.BoxGeometry
  hairShort: THREE.SphereGeometry
  hairLong: THREE.CapsuleGeometry
  hairBun: THREE.SphereGeometry
  cup: THREE.CylinderGeometry
  phone: THREE.BoxGeometry
  laptopBase: THREE.BoxGeometry
  laptopLid: THREE.BoxGeometry
  laptopScreen: THREE.PlaneGeometry
  laptopShell: THREE.MeshStandardMaterial
  eyeWhiteMat: THREE.MeshStandardMaterial
  irisMat: THREE.MeshStandardMaterial
  mouthMat: THREE.MeshStandardMaterial
  cupMat: THREE.MeshStandardMaterial
  phoneMat: THREE.MeshStandardMaterial
}

/**
 * One shared geometry/material set for every avatar in the scene.
 * Caller owns disposal — pass each returned object through its `track()`.
 */
export function createAvatarKit<T extends { dispose: () => void }>(track: (o: T) => T): AvatarKit {
  const g = <G extends { dispose: () => void }>(o: G) => track(o as unknown as T) as unknown as G
  return {
    head: g(new THREE.SphereGeometry(0.135, 20, 16)),
    jaw: g(new THREE.SphereGeometry(0.112, 16, 12)),    neck: g(new THREE.CylinderGeometry(0.045, 0.055, 0.09, 10)),
    torso: g(new THREE.CapsuleGeometry(0.14, 0.3, 4, 14)),
    hips: g(new THREE.CapsuleGeometry(0.13, 0.1, 4, 12)),
    upperArm: g(new THREE.CapsuleGeometry(0.043, 0.19, 4, 8)),
    forearm: g(new THREE.CapsuleGeometry(0.037, 0.18, 4, 8)),
    hand: g(new THREE.SphereGeometry(0.045, 8, 6)),
    thigh: g(new THREE.CapsuleGeometry(0.058, 0.22, 4, 8)),
    shin: g(new THREE.CapsuleGeometry(0.048, 0.22, 4, 8)),
    foot: g(new THREE.BoxGeometry(0.09, 0.05, 0.19)),
    eyeWhite: g(new THREE.SphereGeometry(0.031, 12, 10)),
    iris: g(new THREE.SphereGeometry(0.015, 10, 8)),
    brow: g(new THREE.BoxGeometry(0.052, 0.011, 0.016)),
    nose: g(new THREE.ConeGeometry(0.018, 0.045, 6)),
    mouth: g(new THREE.BoxGeometry(0.052, 0.012, 0.014)),
    hairShort: g(new THREE.SphereGeometry(0.139, 16, 12, 0, Math.PI * 2, 0, Math.PI * 0.37)),
    hairLong: g(new THREE.CapsuleGeometry(0.115, 0.17, 4, 12)),
    hairBun: g(new THREE.SphereGeometry(0.062, 10, 8)),
    cup: g(new THREE.CylinderGeometry(0.033, 0.028, 0.075, 10)),
    laptopBase: g(new THREE.BoxGeometry(0.42, 0.018, 0.3)),
    laptopLid: g(new THREE.BoxGeometry(0.42, 0.28, 0.014)),
    laptopScreen: g(new THREE.PlaneGeometry(0.38, 0.24)),
    laptopShell: g(new THREE.MeshStandardMaterial({
      color: 0x2b3446, roughness: 0.38, metalness: 0.72,
    })),
    eyeWhiteMat: g(new THREE.MeshStandardMaterial({
      color: 0xf8fafc, roughness: 0.32,
      // A whisper of emissive so the eyes still read across a dimmed room.
      emissive: 0xcbd5e1, emissiveIntensity: 0.22,
    })),
    irisMat: g(new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.22 })),
    mouthMat: g(new THREE.MeshStandardMaterial({ color: 0x4a1f22, roughness: 0.6 })),
    cupMat: g(new THREE.MeshStandardMaterial({ color: 0xf1f5f9, roughness: 0.5 })),
    phone: g(new THREE.BoxGeometry(0.05, 0.095, 0.008)),
    phoneMat: g(new THREE.MeshStandardMaterial({
      color: 0x0b1220, emissive: 0x2563eb, emissiveIntensity: 0.7, roughness: 0.4, metalness: 0.5,
    })),
  }
}

/** Every joint the animator drives. Meshes stay untouched. */
export interface AvatarRig {
  root: THREE.Group
  hips: THREE.Group
  torso: THREE.Group
  neck: THREE.Group
  head: THREE.Group
  leftShoulder: THREE.Group
  rightShoulder: THREE.Group
  leftElbow: THREE.Group
  rightElbow: THREE.Group
  leftHip: THREE.Group
  rightHip: THREE.Group
  leftKnee: THREE.Group
  rightKnee: THREE.Group
  /** Scaled on Y to blink. */
  leftEye: THREE.Mesh
  rightEye: THREE.Mesh
  leftBrow: THREE.Mesh
  rightBrow: THREE.Mesh
  /** Scaled on Y to talk. */
  mouth: THREE.Mesh
  /** Held while drinking; hidden otherwise. */
  cup: THREE.Mesh
  /** Held up while working outside on the deck; hidden otherwise. */
  phone: THREE.Mesh
  bodyMat: THREE.MeshStandardMaterial
  skinMat: THREE.MeshStandardMaterial
  gender: Gender
  /** Standing hip height — the walk cycle lifts the body to this. */
  standHeight: number
}

export interface BuildAvatarOptions {
  kit: AvatarKit
  /** Accent colour for clothing; usually the seat colour. */
  color: THREE.Color
  gender: Gender
  /** Seeds skin/hair choice so a name always looks the same. */
  name: string
  castShadow: boolean
  track: <T extends { dispose: () => void }>(o: T) => T
}

/**
 * Build one humanoid. Returns the rig; the caller parents `rig.root`.
 * Origin is on the floor, facing +Z.
 */
export function buildAvatar(opts: BuildAvatarOptions): AvatarRig {
  const { kit, color, gender, name, castShadow, track } = opts
  const female = gender === 'female'
  const seed = hashName(name)

  const skinMat = track(new THREE.MeshStandardMaterial({
    color: SKIN_TONES[seed % SKIN_TONES.length],
    roughness: 0.68,
    metalness: 0.02,
  }))
  const hairMat = track(new THREE.MeshStandardMaterial({
    // Unsigned shift: `>>` would go negative for hashes above 2^31 and index
    // off the end of the array.
    color: HAIR_TONES[(seed >>> 3) % HAIR_TONES.length],
    roughness: 0.85,
    metalness: 0.05,
  }))
  // Clothing carries the seat colour so a glance still says who is who.
  const bodyMat = track(new THREE.MeshStandardMaterial({
    color,
    emissive: color.clone().multiplyScalar(0.16),
    emissiveIntensity: 0.6,
    roughness: 0.56,
    metalness: 0.22,
  }))

  const mesh = (geo: THREE.BufferGeometry, mat: THREE.Material, shadow = false) => {
    const m = new THREE.Mesh(geo, mat)
    if (shadow && castShadow) m.castShadow = true
    return m
  }

  // Women here are built slightly shorter with narrower shoulders and a
  // higher waist. Small deltas — enough to read at a glance, not caricature.
  const scale = female ? 0.955 : 1
  const shoulderW = female ? 0.135 : 0.163
  const hipY = 0.86 * scale

  const root = new THREE.Group()

  const hips = new THREE.Group()
  hips.position.y = hipY
  root.add(hips)
  hips.add(mesh(kit.hips, bodyMat, true))

  const torso = new THREE.Group()
  hips.add(torso)
  const torsoMesh = mesh(kit.torso, bodyMat, true)
  torsoMesh.position.y = 0.22
  torsoMesh.scale.set(female ? 0.92 : 1.04, 1, female ? 0.9 : 1)
  torso.add(torsoMesh)

  const neck = new THREE.Group()
  neck.position.y = 0.42
  torso.add(neck)
  neck.add(mesh(kit.neck, skinMat))

  const head = new THREE.Group()
  head.position.y = 0.1
  neck.add(head)

  const skull = mesh(kit.head, skinMat, true)
  skull.scale.set(0.94, 1.06, 1)
  head.add(skull)
  const jaw = mesh(kit.jaw, skinMat)
  // Nudged forward so the chin plane reads in profile instead of melting
  // into the skull sphere.
  jaw.position.set(0, -0.045, 0.026)
  jaw.scale.set(0.9, 0.78, 0.94)
  head.add(jaw)

  // ── Face. Everything sits on +Z so the head's forward is the face. ──
  // Positions are placed *on* the skull ellipsoid (rx 0.127 / ry 0.143 /
  // rz 0.135): each feature's centre is set so it protrudes a few millimetres
  // past the surface — buried features are invisible, proud features catch
  // the light and read from across the room.
  const eyeFor = (side: -1 | 1) => {
    const eye = mesh(kit.eyeWhite, kit.eyeWhiteMat)
    eye.position.set(side * 0.048, 0.016, 0.112)
    eye.scale.set(1, 0.82, 0.62)
    head.add(eye)
    const iris = mesh(kit.iris, kit.irisMat)
    iris.position.set(0, 0, 0.007)
    eye.add(iris)
    return eye
  }
  const leftEye = eyeFor(-1)
  const rightEye = eyeFor(1)

  const browFor = (side: -1 | 1) => {
    const brow = mesh(kit.brow, hairMat)
    brow.position.set(side * 0.048, 0.055, 0.112)
    brow.rotation.z = side * 0.1
    head.add(brow)
    return brow
  }
  const leftBrow = browFor(-1)
  const rightBrow = browFor(1)

  const nose = mesh(kit.nose, skinMat)
  nose.position.set(0, -0.012, 0.126)
  nose.rotation.x = Math.PI / 2
  head.add(nose)

  const mouth = mesh(kit.mouth, kit.mouthMat)
  mouth.position.set(0, -0.062, 0.12)
  head.add(mouth)

  // ── Hair: the clearest gender read at a distance. ──
  // The cap is a shallow skullcap — deep enough to shell the skull at the
  // sides, but with the rim lifted clear above the brows so it can never
  // swallow the face.
  const cap = mesh(kit.hairShort, hairMat)
  cap.position.set(0, 0.012, -0.008)
  cap.scale.setScalar(female ? 1.03 : 1)
  head.add(cap)
  if (female) {
    // Shoulder-length fall behind the head, plus a bun on top.
    const fall = mesh(kit.hairLong, hairMat)
    fall.position.set(0, -0.075, -0.055)
    fall.scale.set(1, 0.86, 0.62)
    head.add(fall)
    const bun = mesh(kit.hairBun, hairMat)
    bun.position.set(0, 0.108, -0.062)
    head.add(bun)
  }

  // ── Arms ──
  const buildArm = (side: -1 | 1) => {
    const shoulder = new THREE.Group()
    shoulder.position.set(side * shoulderW, 0.36, 0)
    torso.add(shoulder)

    const upper = mesh(kit.upperArm, bodyMat, true)
    upper.position.y = -0.11
    shoulder.add(upper)

    const elbow = new THREE.Group()
    elbow.position.y = -0.22
    shoulder.add(elbow)

    const fore = mesh(kit.forearm, skinMat)
    fore.position.y = -0.1
    elbow.add(fore)

    const hand = mesh(kit.hand, skinMat)
    hand.position.y = -0.21
    hand.scale.set(1, 1.15, 0.7)
    elbow.add(hand)

    return { shoulder, elbow, hand }
  }
  const left = buildArm(-1)
  const right = buildArm(1)

  // A cup that appears only at the coffee machine / water cooler.
  const cup = mesh(kit.cup, kit.cupMat)
  cup.position.set(0, -0.055, 0.02)
  cup.visible = false
  right.hand.add(cup)

  // A phone that appears only while working outside on the deck.
  const phone = mesh(kit.phone, kit.phoneMat)
  phone.position.set(0, -0.03, 0.05)
  phone.rotation.x = -0.5
  phone.visible = false
  right.hand.add(phone)

  // ── Legs ──
  const buildLeg = (side: -1 | 1) => {
    const hip = new THREE.Group()
    hip.position.set(side * (female ? 0.072 : 0.08), -0.04, 0)
    hips.add(hip)

    const thigh = mesh(kit.thigh, bodyMat, true)
    thigh.position.y = -0.14
    hip.add(thigh)

    const knee = new THREE.Group()
    knee.position.y = -0.28
    hip.add(knee)

    const shin = mesh(kit.shin, bodyMat)
    shin.position.y = -0.14
    knee.add(shin)

    const foot = mesh(kit.foot, kit.irisMat)
    foot.position.set(0, -0.29, 0.045)
    knee.add(foot)

    return { hip, knee }
  }
  const leftLeg = buildLeg(-1)
  const rightLeg = buildLeg(1)

  return {
    root,
    hips,
    torso,
    neck,
    head,
    leftShoulder: left.shoulder,
    rightShoulder: right.shoulder,
    leftElbow: left.elbow,
    rightElbow: right.elbow,
    leftHip: leftLeg.hip,
    rightHip: rightLeg.hip,
    leftKnee: leftLeg.knee,
    rightKnee: rightLeg.knee,
    leftEye,
    rightEye,
    leftBrow,
    rightBrow,
    mouth,
    cup,
    phone,
    bodyMat,
    skinMat,
    gender,
    standHeight: hipY,
  }
}

// ── Laptops ─────────────────────────────────────────────────────────────────

export interface Laptop {
  group: THREE.Group
  texture: THREE.CanvasTexture
  /** Seeds the chart so no two laptops show the same thing. */
  seed: number
  /** Last painted time, so repaints can be rate-limited. */
  paintedAt: number
}

/**
 * An open laptop for one seat. The screen is its own small canvas — seven of
 * these repainted a few times a second is far cheaper than it sounds, and it is
 * what makes the table look like people are working rather than posing.
 */
export function buildLaptop(kit: AvatarKit, accent: THREE.Color, seed: number, track: <T extends { dispose: () => void }>(o: T) => T): Laptop {
  const group = new THREE.Group()

  const base = new THREE.Mesh(kit.laptopBase, kit.laptopShell)
  group.add(base)

  // Keyboard inlay, so the base is not a featureless slab.
  const keys = new THREE.Mesh(
    kit.laptopScreen,
    track(new THREE.MeshStandardMaterial({ color: 0x151c29, roughness: 0.8 })),
  )
  keys.rotation.x = -Math.PI / 2
  keys.scale.set(0.92, 0.72, 1)
  keys.position.set(0, 0.011, 0.02)
  group.add(keys)

  const hinge = new THREE.Group()
  hinge.position.set(0, 0.008, -0.14)
  // Screens tilt back a little; a dead-vertical lid looks like a monolith.
  hinge.rotation.x = -0.32
  group.add(hinge)

  const lid = new THREE.Mesh(kit.laptopLid, kit.laptopShell)
  lid.position.y = 0.14
  hinge.add(lid)

  const canvas = document.createElement('canvas')
  canvas.width = 192
  canvas.height = 120
  const texture = track(new THREE.CanvasTexture(canvas))
  texture.colorSpace = THREE.SRGBColorSpace
  texture.userData.canvas = canvas
  texture.userData.ctx = canvas.getContext('2d')
  texture.userData.accent = `#${accent.getHexString()}`

  const screen = new THREE.Mesh(
    kit.laptopScreen,
    track(new THREE.MeshBasicMaterial({ map: texture, toneMapped: false })),
  )
  screen.position.set(0, 0.14, 0.009)
  hinge.add(screen)

  return { group, texture, seed, paintedAt: -1 }
}

/**
 * What a laptop screen shows. Derived from the agent's seat so a Risk officer's
 * screen never looks like a Market analyst's — the whole point of a room full
 * of specialists is that each is reading something different.
 */
export interface LaptopContent {
  /** Agent role, e.g. "market_analyst" | "risk_manager" | "signal_generator". */
  role: string
  /** The pair the room is on, drawn in the title bar. */
  symbol?: string | null
  /** Latest action lean, for the roles that show a verdict. */
  action?: string | null
  /** 0..1 conviction, drives gauges and bars. */
  confidence?: number | null
}

/** Coarse screen category for a role — decides which panel `paintLaptop` draws. */
function laptopKind(role: string): 'chart' | 'sentiment' | 'risk' | 'signal' | 'news' | 'orders' {
  const r = (role || '').toLowerCase()
  if (r.includes('risk')) return 'risk'
  if (r.includes('sentiment') || r.includes('mood')) return 'sentiment'
  if (r.includes('news')) return 'news'
  if (r.includes('exec') || r.includes('trade') || r.includes('order')) return 'orders'
  if (r.includes('signal') || r.includes('strateg') || r.includes('decision')) return 'signal'
  return 'chart' // market / technical / analyst / everything else
}

function drawTitleBar(
  ctx: CanvasRenderingContext2D, w: number, accent: string, busy: boolean,
  label: string, symbol?: string | null,
) {
  ctx.fillStyle = '#111c2e'
  ctx.fillRect(0, 0, w, 14)
  ctx.fillStyle = busy ? accent : '#334155'
  ctx.fillRect(4, 5, 5, 5)
  ctx.fillStyle = '#93a4bd'
  ctx.font = '7px system-ui, sans-serif'
  ctx.textAlign = 'left'
  ctx.fillText(label.toUpperCase(), 14, 10)
  if (symbol) {
    ctx.fillStyle = '#e2e8f0'
    ctx.font = 'bold 8px ui-monospace, monospace'
    ctx.textAlign = 'right'
    ctx.fillText(symbol, w - 5, 10)
  }
}

/**
 * Repaint one laptop screen. The panel drawn depends on the agent's role:
 * chartists get a scrolling candle chart, the risk desk a gauge and exposure
 * bars, the signal desk a big BUY/SELL verdict, and so on. Deterministic in
 * `seed` and `t`, so a screen animates smoothly instead of flickering into a
 * new random shape on every repaint.
 */
export function paintLaptop(laptop: Laptop, t: number, busy: boolean, content?: LaptopContent) {
  const ctx = laptop.texture.userData.ctx as CanvasRenderingContext2D | undefined
  const canvas = laptop.texture.userData.canvas as HTMLCanvasElement | undefined
  if (!ctx || !canvas) return
  const accent = (laptop.texture.userData.accent as string) ?? '#38bdf8'
  const w = canvas.width
  const h = canvas.height
  const kind = laptopKind(content?.role ?? '')
  const symbol = content?.symbol ?? null
  const action = (content?.action ?? '').toLowerCase()
  const conf = Math.max(0, Math.min(1, content?.confidence ?? 0))

  ctx.fillStyle = '#060b14'
  ctx.fillRect(0, 0, w, h)

  const wave = (i: number) =>
    Math.sin((i * 0.7) + laptop.seed) * 0.5
    + Math.sin((i * 0.23) + laptop.seed * 1.7) * 0.3
    + Math.sin(i * 1.9 + laptop.seed * 0.4) * 0.12
  const speed = busy ? 2.4 : 0.8

  if (kind === 'chart') {
    drawTitleBar(ctx, w, accent, busy, 'market', symbol)
    const cols = 18
    const baseline = 74
    for (let i = 0; i < cols; i++) {
      const idx = i + Math.floor(t * speed)
      const v = wave(idx)
      const bh = 6 + Math.abs(v) * 34
      const x = 6 + i * 10
      const y = baseline - v * 26 - bh / 2
      ctx.fillStyle = v >= 0 ? '#22c55e' : '#ef4444'
      ctx.fillRect(x, y, 6, bh)
      ctx.fillRect(x + 2.5, y - 4, 1, bh + 8)
    }
    ctx.strokeStyle = accent
    ctx.lineWidth = 1.5
    ctx.beginPath()
    for (let i = 0; i < cols; i++) {
      const idx = i + Math.floor(t * speed)
      const x = 9 + i * 10
      const y = baseline - wave(idx) * 22
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    }
    ctx.stroke()
  } else if (kind === 'sentiment') {
    drawTitleBar(ctx, w, accent, busy, 'sentiment', symbol)
    // Bull/bear split bar plus a jittering mood needle.
    const bull = (Math.sin(t * 0.6 + laptop.seed) + 1) / 2 * 0.5 + 0.25
    ctx.fillStyle = '#14351f'
    ctx.fillRect(8, 30, w - 16, 12)
    ctx.fillStyle = '#22c55e'
    ctx.fillRect(8, 30, (w - 16) * bull, 12)
    ctx.fillStyle = '#ef4444'
    ctx.fillRect(8 + (w - 16) * bull, 30, (w - 16) * (1 - bull), 12)
    ctx.fillStyle = '#cbd5e1'
    ctx.font = '7px system-ui, sans-serif'
    ctx.textAlign = 'left'
    ctx.fillText('BULL', 8, 55)
    ctx.textAlign = 'right'
    ctx.fillText('BEAR', w - 8, 55)
    // Headline heat bars.
    for (let r = 0; r < 4; r++) {
      const mag = (Math.sin(t * (busy ? 2 : 0.8) + r * 1.7 + laptop.seed) + 1) / 2
      ctx.fillStyle = mag > 0.5 ? '#16a34a' : '#b91c1c'
      ctx.fillRect(8, 66 + r * 12, 8 + mag * (w - 30), 7)
    }
  } else if (kind === 'risk') {
    drawTitleBar(ctx, w, accent, busy, 'risk', symbol)
    // A risk gauge (arc) — higher confidence, lower risk needle swing.
    const cx = w / 2
    const cy = 78
    const rad = 34
    ctx.lineWidth = 8
    ctx.strokeStyle = '#1e293b'
    ctx.beginPath()
    ctx.arc(cx, cy, rad, Math.PI, Math.PI * 2)
    ctx.stroke()
    const risk = 0.25 + (Math.sin(t * 0.9 + laptop.seed) + 1) / 2 * 0.5
    ctx.strokeStyle = risk > 0.66 ? '#ef4444' : risk > 0.4 ? '#f59e0b' : '#22c55e'
    ctx.beginPath()
    ctx.arc(cx, cy, rad, Math.PI, Math.PI + Math.PI * risk)
    ctx.stroke()
    ctx.fillStyle = '#e2e8f0'
    ctx.font = 'bold 13px ui-monospace, monospace'
    ctx.textAlign = 'center'
    ctx.fillText(`${Math.round(risk * 100)}%`, cx, cy - 2)
    ctx.fillStyle = '#64748b'
    ctx.font = '7px system-ui, sans-serif'
    ctx.fillText('EXPOSURE', cx, cy + 12)
  } else if (kind === 'signal') {
    // Big verdict tile — the decision desk's whole job on one screen.
    const buy = action.includes('buy') || action.includes('long')
    const sell = action.includes('sell') || action.includes('short')
    const col = buy ? '#22c55e' : sell ? '#ef4444' : '#64748b'
    drawTitleBar(ctx, w, accent, busy, 'signal', symbol)
    ctx.fillStyle = col
    ctx.globalAlpha = 0.16
    ctx.fillRect(8, 24, w - 16, 42)
    ctx.globalAlpha = 1
    ctx.strokeStyle = col
    ctx.lineWidth = 2
    ctx.strokeRect(8, 24, w - 16, 42)
    ctx.fillStyle = col
    ctx.font = 'bold 26px system-ui, sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(buy ? 'BUY' : sell ? 'SELL' : 'HOLD', w / 2, 55)
    // Confidence bar.
    ctx.fillStyle = '#1e293b'
    ctx.fillRect(8, 78, w - 16, 10)
    ctx.fillStyle = col
    ctx.fillRect(8, 78, (w - 16) * conf, 10)
    ctx.fillStyle = '#94a3b8'
    ctx.font = '7px system-ui, sans-serif'
    ctx.textAlign = 'left'
    ctx.fillText(`CONVICTION ${Math.round(conf * 100)}%`, 8, 100)
  } else if (kind === 'news') {
    drawTitleBar(ctx, w, accent, busy, 'news', symbol)
    // Scrolling headline lines of varying length.
    for (let r = 0; r < 6; r++) {
      const off = (t * (busy ? 26 : 10) + r * 40) % (w + 60)
      ctx.fillStyle = r % 2 ? '#334155' : '#475569'
      ctx.fillRect(8 - off + w, 26 + r * 13, 30 + ((r * 37) % 90), 6)
      ctx.fillStyle = accent
      ctx.fillRect(2, 26 + r * 13, 3, 6)
    }
  } else {
    // orders — a mini order book / fills ladder.
    drawTitleBar(ctx, w, accent, busy, 'orders', symbol)
    for (let r = 0; r < 8; r++) {
      const bid = r < 4
      const mag = (Math.sin(t * (busy ? 2.4 : 1) + r + laptop.seed) + 1) / 2
      ctx.fillStyle = bid ? '#14351f' : '#3a1414'
      const bw = 20 + mag * (w / 2 - 24)
      const y = 22 + r * 11
      if (bid) ctx.fillRect(w / 2, y, bw, 8)
      else ctx.fillRect(w / 2 - bw, y, bw, 8)
      ctx.fillStyle = bid ? '#22c55e' : '#ef4444'
      ctx.fillRect(w / 2 - 1, y, 2, 8)
    }
  }

  // Log lines at the bottom — the "thinking" tell, shared by every screen.
  ctx.fillStyle = '#1e293b'
  for (let r = 0; r < 3; r++) {
    const lw = 40 + ((Math.sin(t * (busy ? 3 : 1) + r * 2 + laptop.seed) + 1) / 2) * 110
    ctx.fillRect(6, 98 + r * 7, lw, 3)
  }

  laptop.texture.needsUpdate = true
}

// ── Poses ───────────────────────────────────────────────────────────────────
//
// Poses set joints absolutely each frame rather than accumulating, so a state
// change can never leave a limb drifting. `lerpTo` eases toward the target so
// switching pose reads as a movement instead of a snap.

const damp = (current: number, target: number, dt: number, rate = 8) =>
  current + (target - current) * Math.min(1, dt * rate)

function lerpTo(obj: THREE.Object3D, x: number, y: number, z: number, dt: number, rate = 8) {
  obj.rotation.x = damp(obj.rotation.x, x, dt, rate)
  obj.rotation.y = damp(obj.rotation.y, y, dt, rate)
  obj.rotation.z = damp(obj.rotation.z, z, dt, rate)
}

/**
 * Fold the legs and drop the hips to `hipHeight`, so one helper seats an agent
 * on a boardroom chair, a beanbag or a sofa — only the height and the knee
 * spread change between them.
 */
export function poseSeatedAt(rig: AvatarRig, dt: number, hipHeight: number, spread = 0.04) {
  rig.phone.visible = false
  rig.hips.position.y = damp(rig.hips.position.y, hipHeight, dt, 6)
  lerpTo(rig.leftHip, -1.42, 0, spread, dt, 6)
  lerpTo(rig.rightHip, -1.42, 0, -spread, dt, 6)
  lerpTo(rig.leftKnee, 1.35, 0, 0, dt, 6)
  lerpTo(rig.rightKnee, 1.35, 0, 0, dt, 6)
}

/** Knees and hips folded under the table. The rest state for every seat. */
export function poseSeated(rig: AvatarRig, dt: number) {
  poseSeatedAt(rig, dt, rig.standHeight - 0.28, 0.04)
}

/** Legs straight, ready to walk. */
export function poseStanding(rig: AvatarRig, dt: number) {
  rig.phone.visible = false
  rig.hips.position.y = damp(rig.hips.position.y, rig.standHeight, dt, 6)
  lerpTo(rig.leftHip, 0, 0, 0.03, dt, 6)
  lerpTo(rig.rightHip, 0, 0, -0.03, dt, 6)
  lerpTo(rig.leftKnee, 0.05, 0, 0, dt, 6)
  lerpTo(rig.rightKnee, 0.05, 0, 0, dt, 6)
}

/**
 * A walk cycle. `phase` advances with distance travelled, not time, so the
 * feet stay in step with the body however fast it moves.
 */
export function poseWalking(rig: AvatarRig, phase: number, dt: number) {
  rig.phone.visible = false
  const swing = Math.sin(phase)
  const counter = Math.sin(phase + Math.PI)

  rig.hips.position.y = damp(
    rig.hips.position.y,
    rig.standHeight + Math.abs(Math.sin(phase)) * 0.022,
    dt, 12,
  )

  // Legs: the knee only bends on the backswing, which is what stops a walk
  // cycle from looking like a march.
  lerpTo(rig.leftHip, swing * 0.62, 0, 0.03, dt, 14)
  lerpTo(rig.rightHip, counter * 0.62, 0, -0.03, dt, 14)
  lerpTo(rig.leftKnee, Math.max(0, -swing) * 0.85 + 0.06, 0, 0, dt, 14)
  lerpTo(rig.rightKnee, Math.max(0, -counter) * 0.85 + 0.06, 0, 0, dt, 14)

  // Arms counter-swing the legs.
  lerpTo(rig.leftShoulder, counter * 0.42, 0, 0.1, dt, 12)
  lerpTo(rig.rightShoulder, swing * 0.42, 0, -0.1, dt, 12)
  lerpTo(rig.leftElbow, 0.3 + Math.max(0, counter) * 0.25, 0, 0, dt, 12)
  lerpTo(rig.rightElbow, 0.3 + Math.max(0, swing) * 0.25, 0, 0, dt, 12)

  // A little torso counter-rotation and bob.
  lerpTo(rig.torso, 0.04, counter * 0.09, swing * 0.03, dt, 10)
  lerpTo(rig.head, 0, swing * 0.05, 0, dt, 8)
}

/** Raise the cup, sip, lower. `t01` runs 0→1 across the drink. */
export function poseDrinking(rig: AvatarRig, t01: number, dt: number) {
  // Ease in and out so the arm doesn't snap at either end.
  const lift = Math.sin(Math.min(1, Math.max(0, t01)) * Math.PI)
  rig.cup.visible = true
  lerpTo(rig.rightShoulder, -0.35 - lift * 0.95, 0, -0.3 - lift * 0.35, dt, 9)
  lerpTo(rig.rightElbow, -0.5 - lift * 1.5, 0, 0, dt, 9)
  lerpTo(rig.leftShoulder, 0.06, 0, 0.12, dt, 8)
  lerpTo(rig.leftElbow, 0.3, 0, 0, dt, 8)
  lerpTo(rig.head, lift * -0.16, 0, 0, dt, 8)
}

/**
 * Hunched over a controller: hands together at waist height, thumbs busy, with
 * the occasional lean as a corner goes badly.
 */
export function poseGaming(rig: AvatarRig, elapsed: number, dt: number) {
  rig.cup.visible = false
  const twitch = Math.sin(elapsed * 9) * 0.06
  const lean = Math.sin(elapsed * 0.7) * 0.14

  lerpTo(rig.leftShoulder, -0.72 + twitch, 0, 0.46, dt, 7)
  lerpTo(rig.rightShoulder, -0.72 - twitch, 0, -0.46, dt, 7)
  lerpTo(rig.leftElbow, -0.95, 0, 0, dt, 7)
  lerpTo(rig.rightElbow, -0.95, 0, 0, dt, 7)
  lerpTo(rig.torso, 0.16, lean, 0, dt, 4)
  lerpTo(rig.head, 0.06, lean * 0.5, 0, dt, 4)
}

/** Stood at the board, head up, occasionally folding arms while reading. */
export function poseReadingBoard(rig: AvatarRig, elapsed: number, dt: number) {
  rig.cup.visible = false
  const scan = Math.sin(elapsed * 0.8) * 0.2

  lerpTo(rig.head, -0.14, scan, 0, dt, 3)
  lerpTo(rig.torso, -0.03, scan * 0.3, 0, dt, 3)
  // Arms folded — hands meet in front of the chest.
  lerpTo(rig.leftShoulder, -0.62, 0, 0.62, dt, 5)
  lerpTo(rig.rightShoulder, -0.62, 0, -0.62, dt, 5)
  lerpTo(rig.leftElbow, -1.25, 0, 0, dt, 5)
  lerpTo(rig.rightElbow, -1.25, 0, 0, dt, 5)
}

/**
 * Eating at the snack table: the right hand rises to the mouth on each bite and
 * lowers between them, and the jaw chews. `elapsed` is seconds at the station.
 */
export function poseEating(rig: AvatarRig, elapsed: number, dt: number) {
  rig.cup.visible = false
  // A slow bite cycle so the hand clearly travels to the mouth and back.
  const lift = (Math.sin(Math.max(0, elapsed) * 1.8) + 1) / 2 // 0..1
  lerpTo(rig.rightShoulder, -0.4 - lift * 0.9, 0, -0.28 - lift * 0.3, dt, 8)
  lerpTo(rig.rightElbow, -0.55 - lift * 1.4, 0, 0, dt, 8)
  lerpTo(rig.leftShoulder, 0.06, 0, 0.12, dt, 8)
  lerpTo(rig.leftElbow, 0.32, 0, 0, dt, 8)
  lerpTo(rig.head, lift * -0.13, 0, 0, dt, 8)
  // Chew only while the hand is high (mid-bite), quiet otherwise.
  const chew = lift > 0.6 ? 1 + Math.abs(Math.sin(elapsed * 7)) * 2.4 : 1
  rig.mouth.scale.y = damp(rig.mouth.scale.y, chew, dt, 16)
}

/**
 * Relaxed on the sofa or a beanbag: sunk back, arms resting, head lolling as it
 * follows the room. Layered over `poseSeatedAt`, which owns the legs.
 */
export function poseLounging(rig: AvatarRig, elapsed: number, dt: number) {
  rig.cup.visible = false
  const sway = Math.sin(elapsed * 0.6) * 0.16
  lerpTo(rig.torso, -0.16, sway * 0.4, 0, dt, 3)
  lerpTo(rig.head, 0.1 + Math.sin(elapsed * 1.1) * 0.03, sway, 0, dt, 3)
  // Arms flopped out along the seat back.
  lerpTo(rig.leftShoulder, 0.1, 0, 0.4, dt, 4)
  lerpTo(rig.rightShoulder, 0.1, 0, -0.4, dt, 4)
  lerpTo(rig.leftElbow, 0.2, 0, 0, dt, 4)
  lerpTo(rig.rightElbow, 0.2, 0, 0, dt, 4)
}

/**
 * Working off a phone on the deck: both hands up holding the phone at chest
 * height, head bowed to the screen, a thumb-tap twitch. Work continues here —
 * this is the "off the table" pose.
 */
export function poseOnPhone(rig: AvatarRig, elapsed: number, dt: number) {
  rig.cup.visible = false
  rig.phone.visible = true
  const tap = Math.sin(elapsed * 8) * 0.05
  lerpTo(rig.rightShoulder, -0.9, 0, -0.26, dt, 8)
  lerpTo(rig.rightElbow, -1.4 + tap, 0, 0, dt, 8)
  lerpTo(rig.leftShoulder, -0.82, 0, 0.3, dt, 8)
  lerpTo(rig.leftElbow, -1.34, 0, 0, dt, 8)
  lerpTo(rig.head, 0.36, Math.sin(elapsed * 0.6) * 0.05, 0, dt, 6)
  lerpTo(rig.torso, 0.08, 0, 0, dt, 6)
}

export interface FaceOptions {
  /** Seconds since the scene started; drives the blink clock. */
  time: number
  /** Per-avatar offset so a table of agents never blinks in unison. */
  phase: number
  /** Opens the mouth — used while presenting. */
  talking: boolean
  /** Brows down when concentrating, up when presenting. */
  mood: 'neutral' | 'focused' | 'bright'
  dt: number
  /** Cheaper cadence on weak devices. */
  reduced: boolean
}

/**
 * Blinks, brows and mouth. Cheap: three scalar lerps and a modulo.
 *
 * The blink is driven off a per-avatar cycle rather than randomness so it
 * cannot stall or double-fire on a dropped frame.
 */
export function animateFace(rig: AvatarRig, o: FaceOptions) {
  const cycle = o.reduced ? 6.5 : 4.2
  const local = (o.time + o.phase * 3) % cycle
  // Closed for ~120ms at the top of each cycle.
  const closed = local < 0.12
  const lidTarget = closed ? 0.08 : 1
  const rate = closed ? 26 : 15
  rig.leftEye.scale.y = damp(rig.leftEye.scale.y, 0.82 * lidTarget, o.dt, rate)
  rig.rightEye.scale.y = damp(rig.rightEye.scale.y, 0.82 * lidTarget, o.dt, rate)

  const browY = o.mood === 'focused' ? -0.012 : o.mood === 'bright' ? 0.012 : 0
  rig.leftBrow.position.y = damp(rig.leftBrow.position.y, 0.055 + browY, o.dt, 6)
  rig.rightBrow.position.y = damp(rig.rightBrow.position.y, 0.055 + browY, o.dt, 6)

  // Mouth opens on a fast wobble while talking — enough to read as speech
  // without trying to be lip-sync.
  const open = o.talking ? 1 + Math.abs(Math.sin(o.time * 11 + o.phase)) * 3.4 : 1
  rig.mouth.scale.y = damp(rig.mouth.scale.y, open, o.dt, 20)
}

/** Reset arms to the neutral seated rest. Poses layer their own arms on top. */
export function relaxArms(rig: AvatarRig, dt: number, rate = 6) {
  lerpTo(rig.leftShoulder, 0, 0, 0.12, dt, rate)
  lerpTo(rig.rightShoulder, 0, 0, -0.12, dt, rate)
  lerpTo(rig.leftElbow, 0.35, 0, 0, dt, rate)
  lerpTo(rig.rightElbow, 0.35, 0, 0, dt, rate)
}

export { damp, lerpTo }
