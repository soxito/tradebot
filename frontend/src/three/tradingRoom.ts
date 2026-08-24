/**
 * tradingRoom — the 3D agent boardroom.
 *
 * A furnished room with one procedural human per agent seat and JARVIS at the
 * head. Seat state (idle / analyzing / presenting / resting / error) is read
 * live each frame through `getSeats`, so React never re-creates the scene when
 * an event lands — it just mutates the data the animation loop reads.
 *
 * Two state machines run per avatar and they are deliberately separate:
 *
 *   • SeatState  — what the agent is *doing for you*. Owned by the backend.
 *   • Activity   — where their *body* is (seated, walking, at the coffee bar).
 *                  Owned entirely by this file.
 *
 * An agent only ever leaves its chair while its SeatState is idle or resting,
 * so wandering can never hide work in progress. See `maybeStartBreak`.
 */
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

import {
  animateFace,
  buildAvatar,
  buildLaptop,
  createAvatarKit,
  paintLaptop,
  poseDrinking,
  poseEating,
  poseGaming,
  poseLounging,
  poseOnPhone,
  poseReadingBoard,
  poseSeated,
  poseSeatedAt,
  poseStanding,
  poseWalking,
  relaxArms,
  damp,
  lerpTo,
  type AvatarRig,
  type Gender,
  type Laptop,
  type LaptopContent,
} from './agentAvatar'
import {
  buildFurniture,
  buildNewsScreen,
  buildPriceBoard,
  buildRoomShell,
  buildWallScreens,
  paintTvGame,
  TV_GAMES,
  ROOM_HEIGHT,
  type BoardQuote,
  type ChartCandle,
  type NewsItem,
  type NewsScreenHandle,
  type PriceBoardHandle,
  type ScreenInfo,
  type Station,
  type WallScreensHandle,
  type CycleScreenInfo,
} from './roomFurniture'

export type SeatState = 'idle' | 'analyzing' | 'presenting' | 'resting' | 'error'

export interface SeatInput {
  role: string
  human_name: string
  title: string
  color: string
  seat: number
  state: SeatState
  /** 0..1 — drives the holo panel fill above the seat. */
  confidence?: number
  action?: string
  /** Body build + hair. Rendering only. */
  gender?: Gender
}

export interface RoomGfx {
  antialias: boolean
  dprCap: number
  shadows: boolean
  fpsTarget: number
}

export interface TradingRoomOptions {
  canvas: HTMLCanvasElement
  width: number
  height: number
  dpr: number
  gfx: RoomGfx
  reducedMotion: boolean
  getSeats: () => SeatInput[]
  /** What the wall screens show. Polled each frame; repaints only on change. */
  getScreenInfo?: () => ScreenInfo
  /** Live quotes for the big board. Polled each frame; repaints only on change. */
  getQuotes?: () => BoardQuote[]
  /** Real OHLC bars for the back-wall chart. Polled; repaints only on change. */
  getChartCandles?: () => ChartCandle[]
  /** Recent headlines for the news screen. Polled; repaints only on change. */
  getNews?: () => NewsItem[]
  /** The turn currently being spoken at the board (drives the speech bubbles). */
  getSpeech?: () => SpeechTurn | null
  /** The Bitcoin cycle read painted on the back-wall chart header. */
  getCycleInfo?: () => CycleScreenInfo | null
  /** Called with a role when the user clicks that avatar. */
  onSeatClick?: (role: string) => void
}

export interface TradingRoomHandle {
  resize(width: number, height: number): void
  setPaused(paused: boolean): void
  focusSeat(role: string | null): void
  getFps(): number
  dispose(): void
}

const TABLE_RADIUS = 3.1
const SEAT_RADIUS = 4.05
// Big enough that the camera can still dolly back far enough to frame the whole
// seating ring while staying inside the walls — see `frameScene`.
const ROOM_RADIUS = 13.5
const MAX_SEATS = 8

/** Radius of the ring walkers follow to get round the table without clipping. */
const WALK_RING = TABLE_RADIUS + 1.75
const WALK_SPEED = 1.35   // metres/sec
const TURN_RATE = 5.5     // radians/sec
/** How far the chair rolls in toward the table when its owner starts working. */
const WORK_PULL = 0.42    // metres

type Activity = 'seated' | 'standing_up' | 'walk_out' | 'using' | 'walk_back' | 'sitting_down'

interface Avatar {
  role: string
  /** Positioned/rotated by the locomotion director. */
  group: THREE.Group
  rig: AvatarRig
  halo: THREE.Mesh
  panel: THREE.Mesh
  panelFill: THREE.Mesh
  label: THREE.Sprite
  /** Dialogue bubble shown while this seat is presenting. */
  speech: SpeechBubble
  /** Epoch seconds after which the current turn stops being displayed. */
  speechUntil: number
  chair: THREE.Group
  /** Sits on the table in front of the seat; hidden while its owner is away. */
  laptop: Laptop
  /** Random offset so avatars don't breathe or blink in lockstep. */
  phase: number

  // ── Locomotion ──
  activity: Activity
  /** Where this avatar sits, and the heading it faces when seated. */
  homePos: THREE.Vector3
  homeYaw: number
  /** Unit direction from the seat toward the table centre (for the work pull). */
  inward: THREE.Vector3
  /** 0..1 eased pull that rolls the chair up to the table while working. */
  deskOffset: number
  /** Remaining waypoints for the current leg. */
  path: THREE.Vector3[]
  station: Station | null
  /** Which seat slot of a multi-seat station this agent claimed (else null). */
  seatSlot: number | null
  /** True while this agent has stepped out onto the deck to work on a phone. */
  outside: boolean
  /** Resolved floor spot to stand/sit on for the current station visit. */
  useTarget: THREE.Vector3
  /** World point to face while using the current station. */
  faceTarget: THREE.Vector3
  /** Counts down the dwell at a station, or the stand/sit transitions. */
  timer: number
  /** Seconds until this avatar is allowed to consider another break. */
  breakCooldown: number
  /** Advances with distance walked, so footfalls match speed. */
  walkPhase: number
  facing: number
}

/** Name plate drawn to a canvas and hung above the seat. */
function makeLabel(name: string, title: string, color: string): THREE.Sprite {
  const canvas = document.createElement('canvas')
  canvas.width = 512
  canvas.height = 160
  const ctx = canvas.getContext('2d')!

  ctx.fillStyle = 'rgba(8, 15, 28, 0.82)'
  ctx.beginPath()
  ctx.roundRect(6, 6, canvas.width - 12, canvas.height - 12, 22)
  ctx.fill()
  ctx.strokeStyle = color
  ctx.lineWidth = 4
  ctx.stroke()

  ctx.textAlign = 'center'
  ctx.fillStyle = '#f1f5f9'
  ctx.font = 'bold 62px system-ui, sans-serif'
  ctx.fillText(name, canvas.width / 2, 76)
  ctx.fillStyle = color
  ctx.font = '34px system-ui, sans-serif'
  ctx.fillText(title.toUpperCase(), canvas.width / 2, 126)

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }),
  )
  sprite.scale.set(1.8, 0.56, 1)
  sprite.renderOrder = 10
  return sprite
}

/** A line of dialogue being presented at the board, straight off the SSE feed. */
export interface SpeechTurn {
  role: string
  human_name?: string
  color?: string
  action?: string
  confidence?: number
  text: string
  at: number
}

/** How long one speaking turn stays visible above its avatar. */
export const SPEECH_TTL = 9

type SpeechBubble = THREE.Sprite & {
  say(text: string, accent: string): void
  clear(): void
}

/**
 * Word-wrap `text` into lines no wider than `maxChars`.
 * Breaks on spaces where it can; long tokens are hard-split.
 */
function wrapText(text: string, maxChars: number): string[] {
  const words = text.split(/\s+/).filter(Boolean)
  const lines: string[] = []
  let line = ''
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word
    if (candidate.length <= maxChars || !line) {
      // A single token longer than the budget gets hard-split below.
      if (candidate.length <= maxChars) { line = candidate; continue }
      if (line) { lines.push(line); line = '' }
      for (let i = 0; i < word.length; i += maxChars) {
        lines.push(word.slice(i, i + maxChars))
      }
      continue
    }
    lines.push(line)
    line = word
  }
  if (line) lines.push(line)
  return lines.slice(0, 4)
}

/** Speech bubble drawn to a canvas — tail at the bottom, speaker colour border. */
function makeSpeechBubble(): SpeechBubble {
  const canvas = document.createElement('canvas')
  canvas.width = 640
  canvas.height = 320
  const ctx = canvas.getContext('2d')!

  const paint = (text: string, accent: string) => {
    ctx.clearRect(0, 0, canvas.width, canvas.height)

    const lines = wrapText(text, 44)
    const lineHeight = 42
    const padX = 34
    const padTop = 26
    const boxH = padTop * 2 + lines.length * lineHeight
    const tailH = 26

    // Bubble body.
    ctx.fillStyle = 'rgba(9, 17, 32, 0.92)'
    ctx.strokeStyle = accent
    ctx.lineWidth = 4
    ctx.beginPath()
    ctx.roundRect(8, 8, canvas.width - 16, boxH, 24)
    ctx.fill()
    ctx.stroke()

    // Tail pointing down at the speaker.
    ctx.fillStyle = 'rgba(9, 17, 32, 0.92)'
    ctx.beginPath()
    ctx.moveTo(canvas.width / 2 - 18, boxH - 2)
    ctx.lineTo(canvas.width / 2, boxH + tailH)
    ctx.lineTo(canvas.width / 2 + 18, boxH - 2)
    ctx.closePath()
    ctx.fill()
    ctx.strokeStyle = accent
    ctx.lineWidth = 4
    ctx.beginPath()
    ctx.moveTo(canvas.width / 2 - 18, boxH - 2)
    ctx.lineTo(canvas.width / 2, boxH + tailH)
    ctx.lineTo(canvas.width / 2 + 18, boxH - 2)
    ctx.stroke()

    ctx.textAlign = 'center'
    ctx.fillStyle = '#e2e8f0'
    ctx.font = '30px system-ui, sans-serif'
    const startY = padTop + 22
    lines.forEach((l, i) => {
      ctx.fillText(l, canvas.width / 2, startY + i * lineHeight)
    })
  }

  let paintedFor = ''
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }),
  )
  sprite.renderOrder = 11
  sprite.visible = false

  /** Repaint only when the message actually changed. */
  return Object.assign(sprite, {
    say(text: string, accent: string) {
      const key = `${accent}|${text}`
      if (key === paintedFor) return
      paintedFor = key
      paint(text, accent)
      texture.needsUpdate = true
    },
    clear() {
      paintedFor = ''
      sprite.visible = false
    },
  })
}

/** Shortest signed angle from a to b, so turns never take the long way round. */
export function angleDelta(a: number, b: number): number {
  let d = (b - a) % (Math.PI * 2)
  if (d > Math.PI) d -= Math.PI * 2
  if (d < -Math.PI) d += Math.PI * 2
  return d
}

/**
 * Route between two floor points without walking through the table.
 *
 * A segment whose midpoint already clears the table is taken straight; anything
 * else is pushed out onto `WALK_RING` and stepped round in arcs, so the detour
 * curves rather than cutting the corner.
 */
export function routeAround(from: THREE.Vector3, to: THREE.Vector3): THREE.Vector3[] {
  const fromAngle = Math.atan2(from.z, from.x)
  const toAngle = Math.atan2(to.z, to.x)
  const ringPoint = (a: number) =>
    new THREE.Vector3(Math.cos(a) * WALK_RING, 0, Math.sin(a) * WALK_RING)

  const mid = from.clone().add(to).multiplyScalar(0.5)
  if (Math.hypot(mid.x, mid.z) > TABLE_RADIUS + 0.9) return [to.clone()]

  const delta = angleDelta(fromAngle, toAngle)
  // Two intermediate arcs are enough to read as walking round the table.
  return [
    ringPoint(fromAngle),
    ringPoint(fromAngle + delta * 0.33),
    ringPoint(fromAngle + delta * 0.66),
    ringPoint(toAngle),
    to.clone(),
  ]
}

/** Where a laptop rests on the table top. */
const DESK_RADIUS = TABLE_RADIUS - 0.55
const DESK_HEIGHT = 1.14

/**
 * Stand a laptop on the table in front of `seatPos`, screen toward the agent.
 *
 * The lid faces the group's local +Z (hinge behind it, keyboard in front), and
 * a group yawed by θ points its +Z at `(sin θ, cos θ)`. The seat lies further
 * out along the same radius as the laptop, so that yaw is exactly the seat's
 * bearing — turning it by a further half-circle shows the agent the back of
 * their own screen.
 */
export function placeLaptopForSeat(group: THREE.Object3D, seatPos: THREE.Vector3): THREE.Object3D {
  const bearing = Math.atan2(seatPos.x, seatPos.z)
  group.position.set(
    Math.sin(bearing) * DESK_RADIUS, DESK_HEIGHT, Math.cos(bearing) * DESK_RADIUS,
  )
  group.rotation.y = bearing
  group.updateMatrixWorld(true)
  return group
}

/** How close the camera may get to a surface before it is pushed back. */
const CAMERA_MARGIN = 1.5
/** Never dip below eye height — under the floor there is nothing to see. */
const CAMERA_FLOOR = 0.9

export interface CameraCage {
  /** Max horizontal distance from the room's centre. */
  radius: number
  floor: number
  ceiling: number
}

export const ROOM_CAGE: CameraCage = {
  radius: ROOM_RADIUS - CAMERA_MARGIN,
  floor: CAMERA_FLOOR,
  ceiling: ROOM_HEIGHT - CAMERA_MARGIN,
}

/**
 * Keep the camera inside the room, whatever the orbit is doing.
 *
 * Without this the camera leaves through a wall or the ceiling and the view
 * fills with an unlit back face — the room appears to lose its texture. Height
 * is clamped outright; horizontal escape is corrected by pulling the camera
 * back along its own bearing, which preserves the direction the user chose.
 *
 * Mutates `position` in place and returns it.
 */
export function constrainCameraToRoom(
  position: THREE.Vector3, cage: CameraCage = ROOM_CAGE,
): THREE.Vector3 {
  position.y = Math.min(Math.max(position.y, cage.floor), cage.ceiling)

  const radius = Math.hypot(position.x, position.z)
  if (radius > cage.radius) {
    const scale = cage.radius / radius
    position.x *= scale
    position.z *= scale
  }
  return position
}

/**
 * The furthest the camera may orbit from `target` while staying in the cage.
 *
 * A focused seat sits well off-centre, so a distance measured from the target
 * can still put the camera outside the room. Budgeting from the target's own
 * offset is what stops zoom-out from punching through a wall.
 */
export function maxOrbitDistance(target: THREE.Vector3, cage: CameraCage = ROOM_CAGE): number {
  const targetRadius = Math.hypot(target.x, target.z)
  return Math.max(5.5, cage.radius - targetRadius)
}

export function createTradingRoom(opts: TradingRoomOptions): TradingRoomHandle | null {
  const { canvas, gfx, reducedMotion, getSeats, getScreenInfo, getQuotes, getChartCandles, getNews, getSpeech, getCycleInfo, onSeatClick } = opts

  let renderer: THREE.WebGLRenderer
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: gfx.antialias, alpha: true })
  } catch {
    return null
  }

  const disposables: Array<{ dispose: () => void }> = []
  const track = <T extends { dispose: () => void }>(o: T): T => {
    disposables.push(o)
    return o
  }

  renderer.setPixelRatio(Math.min(opts.dpr, gfx.dprCap))
  renderer.setSize(opts.width, opts.height, false)
  renderer.shadowMap.enabled = gfx.shadows
  renderer.shadowMap.type = THREE.PCFSoftShadowMap

  const scene = new THREE.Scene()
  // Light fog only — a furnished room wants its far wall visible.
  scene.fog = new THREE.FogExp2(0x060a14, 0.022)

  const camera = new THREE.PerspectiveCamera(46, opts.width / opts.height, 0.1, 140)

  const controls = new OrbitControls(camera, canvas)
  controls.target.set(0, 1.55, 0)
  controls.enableDamping = true
  controls.dampingFactor = 0.06
  // Close enough for a proper look at a focused agent. The cage keeps the far
  // end honest, so this only has to be comfortable.
  controls.minDistance = 2.6
  // Recomputed every frame from the live target — see `maxOrbitDistance`.
  controls.maxDistance = ROOM_CAGE.radius
  controls.maxPolarAngle = Math.PI * 0.49
  controls.enablePan = false
  controls.autoRotate = !reducedMotion
  controls.autoRotateSpeed = 0.35

  // Distance that fits the whole seating ring, in BOTH axes — a tall narrow
  // panel has a much smaller horizontal FOV and needs to dolly out further.
  const SCENE_RADIUS = SEAT_RADIUS + 1.4
  let userMovedCamera = false
  controls.addEventListener('start', () => { userMovedCamera = true })

  function frameScene() {
    if (userMovedCamera) return
    const halfFov = THREE.MathUtils.degToRad(camera.fov / 2)
    const vFit = SCENE_RADIUS / Math.tan(halfFov)
    const hFit = SCENE_RADIUS / (Math.tan(halfFov) * camera.aspect)
    const dist = THREE.MathUtils.clamp(Math.max(vFit, hFit), 6.5, ROOM_CAGE.radius)
    // Raised enough to see over the table into the room — the coffee bar and
    // lounge are part of the scene now, not just the ring of chairs.
    camera.position.set(0, dist * 0.52, dist * 0.86)
    constrainCameraToRoom(camera.position)
    camera.lookAt(controls.target)
  }
  frameScene()

  // ── Lighting ──────────────────────────────────────────────────────────────
  scene.add(new THREE.HemisphereLight(0x8fb4e8, 0x141008, 0.6))

  const keyLight = new THREE.DirectionalLight(0xf0e2cf, 0.95)
  keyLight.position.set(5, 9, 5)
  keyLight.castShadow = gfx.shadows
  if (gfx.shadows) {
    keyLight.shadow.mapSize.set(1024, 1024)
    keyLight.shadow.camera.near = 1
    keyLight.shadow.camera.far = 30
  }
  scene.add(keyLight)

  const rimLight = new THREE.PointLight(0x22d3ee, 1.1, 24, 2)
  rimLight.position.set(-5, 4.5, -4)
  scene.add(rimLight)

  const tableGlow = new THREE.PointLight(0x38bdf8, 1.7, 12, 2)
  tableGlow.position.set(0, 2.2, 0)
  scene.add(tableGlow)

  // ── Floor ─────────────────────────────────────────────────────────────────
  const floorGeo = track(new THREE.PlaneGeometry(ROOM_RADIUS * 2, ROOM_RADIUS * 2))
  const floorMat = track(
    new THREE.MeshStandardMaterial({ color: 0x121a28, roughness: 0.86, metalness: 0.22 }),
  )
  const floor = new THREE.Mesh(floorGeo, floorMat)
  floor.rotation.x = -Math.PI / 2
  floor.receiveShadow = gfx.shadows
  scene.add(floor)

  const grid = new THREE.PolarGridHelper(SEAT_RADIUS + 1.2, 12, 5, 64, 0x1e3a5f, 0x14304d)
  ;(grid.material as THREE.Material).transparent = true
  ;(grid.material as THREE.Material).opacity = 0.22
  grid.position.y = 0.012
  scene.add(grid)

  // ── Room shell, furniture, screens ────────────────────────────────────────
  const shell = buildRoomShell({ scene, track, shadows: gfx.shadows, radius: ROOM_RADIUS })
  const furniture = buildFurniture({ scene, track, shadows: gfx.shadows, radius: ROOM_RADIUS })
  const screens: WallScreensHandle = buildWallScreens(scene, track, ROOM_RADIUS)
  const board: PriceBoardHandle = buildPriceBoard(scene, track, ROOM_RADIUS, gfx.shadows)
  const newsScreen: NewsScreenHandle = buildNewsScreen(scene, track, ROOM_RADIUS, gfx.shadows)

  // Anchors for stepping out onto the deck, and a bigger camera cage that lets
  // the user follow an agent outside without the walls clipping.
  const outside = shell.outside
  const OUTSIDE_DWELL = 18
  const OUTSIDE_CAGE: CameraCage = {
    radius: outside.reach + 2.5,
    floor: ROOM_CAGE.floor,
    ceiling: ROOM_CAGE.ceiling,
  }

  // The board is a destination like any other, so agents wander over to read it.
  const stations: Station[] = [...furniture.stations, board.station]

  // The TV keeps playing whether or not anyone is watching.
  const tvTexture = furniture.tvTexture
  let tvPaintedAt = -1
  // Rotating game state for the lounge TV — one game every 5 minutes.
  let tvGameIndex = Math.floor(Math.random() * TV_GAMES.length)
  let tvGameSwitchAt = 0

  // ── Table ─────────────────────────────────────────────────────────────────
  const tableTopGeo = track(new THREE.CylinderGeometry(TABLE_RADIUS, TABLE_RADIUS, 0.16, 64))
  const tableTopMat = track(
    new THREE.MeshStandardMaterial({ color: 0x111a2e, roughness: 0.28, metalness: 0.72 }),
  )
  const tableTop = new THREE.Mesh(tableTopGeo, tableTopMat)
  tableTop.position.y = 1.05
  tableTop.castShadow = gfx.shadows
  tableTop.receiveShadow = gfx.shadows
  scene.add(tableTop)

  const tableRingGeo = track(new THREE.TorusGeometry(TABLE_RADIUS - 0.06, 0.045, 12, 96))
  const tableRingMat = track(new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.85 }))
  const tableRing = new THREE.Mesh(tableRingGeo, tableRingMat)
  tableRing.rotation.x = -Math.PI / 2
  tableRing.position.y = 1.14
  scene.add(tableRing)

  const pedestalGeo = track(new THREE.CylinderGeometry(0.55, 0.95, 1.05, 32))
  const pedestalMat = track(new THREE.MeshStandardMaterial({ color: 0x0d1526, roughness: 0.5, metalness: 0.6 }))
  const pedestal = new THREE.Mesh(pedestalGeo, pedestalMat)
  pedestal.position.y = 0.52
  pedestal.castShadow = gfx.shadows
  scene.add(pedestal)

  // Centre hologram — the pair under discussion.
  const coreGeo = track(new THREE.IcosahedronGeometry(0.42, 1))
  const coreMat = track(
    new THREE.MeshStandardMaterial({
      color: 0x22d3ee,
      emissive: 0x0e7490,
      emissiveIntensity: 1.5,
      wireframe: true,
    }),
  )
  const core = new THREE.Mesh(coreGeo, coreMat)
  core.position.y = 1.85
  scene.add(core)

  // ── Avatars ───────────────────────────────────────────────────────────────
  const avatars = new Map<string, Avatar>()
  const avatarRoot = new THREE.Group()
  scene.add(avatarRoot)

  const kit = createAvatarKit(track)
  const sharedHaloGeo = track(new THREE.TorusGeometry(0.32, 0.022, 8, 40))
  const sharedChairSeatGeo = track(new THREE.BoxGeometry(0.62, 0.1, 0.58))
  const sharedChairBackGeo = track(new THREE.BoxGeometry(0.62, 0.72, 0.1))
  const sharedChairPostGeo = track(new THREE.CylinderGeometry(0.05, 0.05, 0.55, 8))
  const sharedChairBaseGeo = track(new THREE.CylinderGeometry(0.28, 0.3, 0.05, 12))
  const sharedPanelGeo = track(new THREE.PlaneGeometry(0.92, 0.5))
  const sharedFillGeo = track(new THREE.PlaneGeometry(0.84, 0.08))
  const chairMat = track(new THREE.MeshStandardMaterial({ color: 0x162236, roughness: 0.65, metalness: 0.35 }))

  function buildSeat(seat: SeatInput, index: number, total: number): Avatar {
    const color = new THREE.Color(seat.color)
    const angle = (index / Math.max(total, 1)) * Math.PI * 2 + Math.PI / 2

    const homePos = new THREE.Vector3(
      Math.cos(angle) * SEAT_RADIUS, 0, Math.sin(angle) * SEAT_RADIUS,
    )
    // Face the middle of the table.
    const homeYaw = Math.atan2(-homePos.x, -homePos.z)

    const group = new THREE.Group()
    group.position.copy(homePos)
    group.rotation.y = homeYaw

    // The chair stays behind when its occupant walks off.
    const chair = new THREE.Group()
    chair.position.copy(homePos)
    chair.rotation.y = homeYaw
    const chairSeat = new THREE.Mesh(sharedChairSeatGeo, chairMat)
    chairSeat.position.y = 0.58
    chairSeat.castShadow = gfx.shadows
    chair.add(chairSeat)
    const chairBack = new THREE.Mesh(sharedChairBackGeo, chairMat)
    chairBack.position.set(0, 0.98, -0.29)
    chair.add(chairBack)
    const chairPost = new THREE.Mesh(sharedChairPostGeo, chairMat)
    chairPost.position.y = 0.28
    chair.add(chairPost)
    const chairBase = new THREE.Mesh(sharedChairBaseGeo, chairMat)
    chairBase.position.y = 0.03
    chair.add(chairBase)
    avatarRoot.add(chair)

    const rig = buildAvatar({
      kit,
      color,
      gender: seat.gender ?? 'male',
      name: seat.human_name,
      castShadow: gfx.shadows,
      track,
    })
    group.add(rig.root)

    // The laptop belongs to the table, not to the body — it stays put when its
    // owner gets up for coffee.
    const laptop = buildLaptop(kit, color, index * 1.7 + 0.4, track)
    placeLaptopForSeat(laptop.group, homePos)
    if (gfx.shadows) laptop.group.children.forEach((c) => { c.castShadow = true })
    avatarRoot.add(laptop.group)

    const halo = new THREE.Mesh(
      sharedHaloGeo,
      track(new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.0 })),
    )
    halo.rotation.x = Math.PI / 2
    halo.position.y = 1.78
    group.add(halo)

    const label = makeLabel(seat.human_name, seat.title, seat.color)
    label.position.set(0, 2.5, 0)
    track(label.material)
    if (label.material.map) track(label.material.map)
    group.add(label)

    // Dialogue bubble above the name plate while the seat holds the floor.
    const speech = makeSpeechBubble()
    speech.position.set(0, 3.55, 0)
    track(speech.material)
    if (speech.material.map) track(speech.material.map)
    group.add(speech)

    // Holo panel floating beside the seat — action + confidence bar.
    const panel = new THREE.Mesh(
      sharedPanelGeo,
      track(new THREE.MeshBasicMaterial({ color: 0x0b1526, transparent: true, opacity: 0.55, side: THREE.DoubleSide })),
    )
    panel.position.set(0, 2.08, 0.05)
    group.add(panel)

    const panelFill = new THREE.Mesh(
      sharedFillGeo,
      track(new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 })),
    )
    panelFill.position.set(0, 1.92, 0.06)
    panelFill.scale.x = 0.02
    group.add(panelFill)

    group.userData.role = seat.role
    avatarRoot.add(group)

    return {
      role: seat.role,
      group,
      rig,
      halo,
      panel,
      panelFill,
      label,
      speech,
      speechUntil: 0,
      chair,
      laptop,
      phase: Math.random() * Math.PI * 2,
      activity: 'seated',
      homePos,
      homeYaw,
      inward: homePos.clone().setY(0).normalize().multiplyScalar(-1),
      deskOffset: 0,
      path: [],
      station: null,
      seatSlot: null,
      outside: false,
      useTarget: new THREE.Vector3(),
      faceTarget: new THREE.Vector3(),
      timer: 0,
      // Stagger the first possible break so they don't all rise together.
      breakCooldown: 12 + Math.random() * 40,
      walkPhase: 0,
      facing: homeYaw,
    }
  }

  /** Gender/name changes need a fresh body, so the signature is part of the key. */
  const seatSignature = (s: SeatInput) => `${s.gender ?? 'male'}|${s.human_name}|${s.title}|${s.color}`
  const signatures = new Map<string, string>()

  function syncAvatars(seats: SeatInput[]) {
    const wanted = seats.slice(0, MAX_SEATS)
    const wantedRoles = new Set(wanted.map((s) => s.role))

    for (const [role, av] of avatars) {
      if (!wantedRoles.has(role)) {
        avatarRoot.remove(av.group)
        avatarRoot.remove(av.chair)
        avatarRoot.remove(av.laptop.group)
        avatars.delete(role)
        signatures.delete(role)
      }
    }
    wanted.forEach((seat, i) => {
      const sig = seatSignature(seat)
      const existing = avatars.get(seat.role)
      if (existing && signatures.get(seat.role) !== sig) {
        // Rebuilt rather than mutated: the body proportions, hair and name plate
        // are all baked at build time.
        avatarRoot.remove(existing.group)
        avatarRoot.remove(existing.chair)
        avatarRoot.remove(existing.laptop.group)
        avatars.delete(seat.role)
      }
      if (!avatars.has(seat.role)) {
        avatars.set(seat.role, buildSeat(seat, i, wanted.length))
        signatures.set(seat.role, sig)
      }
    })
  }

  // ── Data-flow particles between seats and the core ────────────────────────
  const PARTICLES = reducedMotion ? 0 : gfx.shadows ? 90 : 45
  let particles: THREE.Points | null = null
  if (PARTICLES > 0) {
    const pGeo = track(new THREE.BufferGeometry())
    const positions = new Float32Array(PARTICLES * 3)
    for (let i = 0; i < PARTICLES; i++) {
      const a = Math.random() * Math.PI * 2
      const r = 1.2 + Math.random() * (SEAT_RADIUS - 1.2)
      positions[i * 3] = Math.cos(a) * r
      positions[i * 3 + 1] = 1.3 + Math.random() * 1.1
      positions[i * 3 + 2] = Math.sin(a) * r
    }
    pGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    particles = new THREE.Points(
      pGeo,
      track(new THREE.PointsMaterial({ color: 0x67e8f9, size: 0.045, transparent: true, opacity: 0.7 })),
    )
    scene.add(particles)
  }

  // ── Click picking ─────────────────────────────────────────────────────────
  const raycaster = new THREE.Raycaster()
  const pointer = new THREE.Vector2()
  const handleClick = (ev: PointerEvent) => {
    if (!onSeatClick) return
    const rect = canvas.getBoundingClientRect()
    pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1
    pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1
    raycaster.setFromCamera(pointer, camera)
    const hit = raycaster.intersectObjects(avatarRoot.children, true)[0]
    if (!hit) return
    let node: THREE.Object3D | null = hit.object
    while (node && !node.userData.role) node = node.parent
    if (node?.userData.role) onSeatClick(node.userData.role as string)
  }
  canvas.addEventListener('pointerdown', handleClick)

  // ── Locomotion director ───────────────────────────────────────────────────
  //
  // Wandering is a garnish, not a feature: it must never cost a frame budget
  // that the working agents need, and it must never empty the table. Both are
  // handled by capping how many can be away at once.
  const maxWanderers = reducedMotion ? 0 : gfx.shadows ? 2 : 1

  /** Only these states are free to leave the chair. */
  const canLeave = (s: SeatState) => s === 'idle' || s === 'resting'

  function maybeStartBreak(av: Avatar, seat: SeatInput, dt: number) {
    if (maxWanderers === 0) return
    av.breakCooldown -= dt
    if (av.breakCooldown > 0) return
    if (!canLeave(seat.state)) {
      // Busy — check again shortly rather than leaving the instant work ends.
      av.breakCooldown = 8
      return
    }
    const away = [...avatars.values()].filter((a) => a.activity !== 'seated').length
    if (away >= maxWanderers) {
      av.breakCooldown = 6
      return
    }
    // Sometimes step outside to work off the deck on a phone — the work carries
    // on away from the table. Take a free deck spot so agents never overlap.
    if (outside.spots.length && Math.random() < 0.22) {
      const takenPts = [...avatars.values()]
        .filter((a) => a !== av && a.outside)
        .map((a) => a.useTarget)
      const spot = outside.spots.find((sp) => takenPts.every((tp) => tp.distanceTo(sp) > 0.8))
      if (!spot) { av.breakCooldown = 6; return }
      av.outside = true
      av.station = null
      av.seatSlot = null
      av.useTarget.copy(spot)
      av.faceTarget.copy(outside.facePoint)
      av.activity = 'standing_up'
      av.timer = 0.7
      return
    }
    // If a teammate is already at the games couches with a spare seat, often
    // join them so co-op / versus rounds actually happen; otherwise wander to a
    // random amenity.
    const gamesStation = stations.find((s) => s.id === 'games')
    const atGames = gamesStation
      ? [...avatars.values()].filter((a) => a !== av && a.station === gamesStation && a.seatSlot != null)
      : []
    const canJoinGames = !!gamesStation && !!gamesStation.seatSlots
      && atGames.length >= 1 && atGames.length < gamesStation.seatSlots.length
    const station = (canJoinGames && Math.random() < 0.6)
      ? gamesStation!
      : stations[Math.floor(Math.random() * stations.length)]
    if (station.seatSlots && station.seatSlots.length) {
      // Multi-seat station (the beanbag couches): take a free seat so two
      // agents never sit on the same one. Wait if every couch is occupied.
      const taken = new Set<number>()
      for (const other of avatars.values()) {
        if (other !== av && other.station === station && other.seatSlot != null) {
          taken.add(other.seatSlot)
        }
      }
      let free = -1
      for (let i = 0; i < station.seatSlots.length; i++) {
        if (!taken.has(i)) { free = i; break }
      }
      if (free < 0) { av.breakCooldown = 6; return }
      av.seatSlot = free
      av.useTarget.copy(station.seatSlots[free])
      av.faceTarget.copy(station.seatFacings?.[free] ?? station.facePoint)
    } else {
      av.seatSlot = null
      av.useTarget.copy(station.usePoint)
      av.faceTarget.copy(station.facePoint)
    }
    av.station = station
    av.activity = 'standing_up'
    av.timer = 0.7
  }

  /** Advance along `av.path`, steering and stepping. Returns true when done. */
  function advanceAlongPath(av: Avatar, dt: number): boolean {
    const next = av.path[0]
    if (!next) return true

    const pos = av.group.position
    const dx = next.x - pos.x
    const dz = next.z - pos.z
    const dist = Math.hypot(dx, dz)

    if (dist < 0.12) {
      av.path.shift()
      return av.path.length === 0
    }

    const step = Math.min(WALK_SPEED * dt, dist)
    pos.x += (dx / dist) * step
    pos.z += (dz / dist) * step

    // Turn toward travel, shortest way round.
    const want = Math.atan2(dx, dz)
    av.facing += angleDelta(av.facing, want) * Math.min(1, dt * TURN_RATE)
    av.group.rotation.y = av.facing

    // Phase advances with distance so the feet match the speed exactly.
    av.walkPhase += step * 5.2
    return false
  }

  /** Turn on the spot toward a world point. Returns true once roughly aligned. */
  function faceToward(av: Avatar, target: THREE.Vector3, dt: number): boolean {
    const want = Math.atan2(target.x - av.group.position.x, target.z - av.group.position.z)
    const delta = angleDelta(av.facing, want)
    av.facing += delta * Math.min(1, dt * TURN_RATE)
    av.group.rotation.y = av.facing
    return Math.abs(delta) < 0.12
  }

  function updateLocomotion(av: Avatar, seat: SeatInput, dt: number) {
    switch (av.activity) {
      case 'seated':
        maybeStartBreak(av, seat, dt)
        break

      case 'standing_up': {
        av.timer -= dt
        poseStanding(av.rig, dt)
        if (av.timer <= 0 && (av.station || av.outside)) {
          if (av.outside) {
            // Out through the doorway, then across the deck to the spot.
            av.path = [
              ...routeAround(av.group.position, outside.doorInside),
              outside.doorOutside.clone(),
              av.useTarget.clone(),
            ]
          } else {
            av.path = routeAround(av.group.position, av.useTarget)
          }
          av.activity = 'walk_out'
        }
        break
      }

      case 'walk_out': {
        const arrived = advanceAlongPath(av, dt)
        poseWalking(av.rig, av.walkPhase, dt)
        if (arrived) {
          av.activity = 'using'
          av.timer = av.outside ? OUTSIDE_DWELL : (av.station?.dwell ?? 4)
        }
        break
      }

      case 'using': {
        av.timer -= dt
        if (av.outside) {
          // On the deck: face the skyline and work on the phone. Work continues.
          faceToward(av, av.faceTarget, dt)
          poseStanding(av.rig, dt)
          poseOnPhone(av.rig, OUTSIDE_DWELL - av.timer, dt)
        } else if (av.station) {
          faceToward(av, av.faceTarget, dt)
          const elapsed = av.station.dwell - av.timer
          if (av.station.seated) {
            // Beanbag or sofa: fold the legs and drop to the seat, then layer
            // the activity's upper body on top.
            poseSeatedAt(av.rig, dt, av.station.sitHeight ?? 0.4, 0.16)
            if (av.station.id === 'games') poseGaming(av.rig, elapsed, dt)
            else poseLounging(av.rig, elapsed, dt)
          } else {
            poseStanding(av.rig, dt)
            if (av.station.drinks) {
              // Map the dwell onto 0..1 so the sip eases in and out.
              poseDrinking(av.rig, elapsed / Math.max(av.station.dwell, 0.001), dt)
            } else if (av.station.eats) {
              poseEating(av.rig, elapsed, dt)
            } else if (av.station.id === 'board' || av.station.id === 'chart') {
              poseReadingBoard(av.rig, elapsed, dt)
            } else {
              relaxArms(av.rig, dt)
            }
          }
        } else {
          poseStanding(av.rig, dt)
        }
        if (av.timer <= 0) {
          av.rig.cup.visible = false
          av.rig.phone.visible = false
          // Free the seat slot so another agent may take this couch.
          av.seatSlot = null
          if (av.outside) {
            // Back in through the doorway, then route to the seat.
            av.path = [
              outside.doorOutside.clone(),
              outside.doorInside.clone(),
              ...routeAround(outside.doorInside, av.homePos),
            ]
          } else {
            av.path = routeAround(av.group.position, av.homePos)
          }
          av.activity = 'walk_back'
        }
        break
      }

      case 'walk_back': {
        const arrived = advanceAlongPath(av, dt)
        poseWalking(av.rig, av.walkPhase, dt)
        if (arrived) {
          av.activity = 'sitting_down'
          av.timer = 0.7
        }
        break
      }

      case 'sitting_down': {
        av.timer -= dt
        // Settle exactly back onto the chair — small drift would compound.
        av.group.position.lerp(av.homePos, Math.min(1, dt * 6))
        av.facing += angleDelta(av.facing, av.homeYaw) * Math.min(1, dt * TURN_RATE)
        av.group.rotation.y = av.facing
        poseSeated(av.rig, dt)
        if (av.timer <= 0) {
          av.group.position.copy(av.homePos)
          av.facing = av.homeYaw
          av.group.rotation.y = av.homeYaw
          av.activity = 'seated'
          av.station = null
          av.outside = false
          av.breakCooldown = 45 + Math.random() * 90
        }
        break
      }
    }
  }

  // ── Animation loop ────────────────────────────────────────────────────────
  const clock = new THREE.Clock()
  const frameInterval = 1 / Math.max(gfx.fpsTarget, 24)
  let accumulator = 0
  let paused = false
  let raf = 0
  let fps = 0
  let fpsFrames = 0
  let fpsElapsed = 0
  let focusedRole: string | null = null

  const tmpTarget = new THREE.Vector3()

  function animate() {
    raf = requestAnimationFrame(animate)
    const delta = Math.min(clock.getDelta(), 0.1)
    if (paused) return
    accumulator += delta
    if (accumulator < frameInterval) return
    const dt = accumulator
    accumulator = 0

    fpsFrames++
    fpsElapsed += dt
    if (fpsElapsed >= 0.5) {
      fps = Math.round(fpsFrames / fpsElapsed)
      fpsFrames = 0
      fpsElapsed = 0
    }

    const t = clock.elapsedTime
    const seats = getSeats()
    syncAvatars(seats)

    const info = getScreenInfo?.()
    if (info) screens.update(info, getQuotes?.())
    if (getQuotes) board.update(getQuotes(), info?.symbol ?? null)
    if (getNews) newsScreen.update(getNews())

    // The big back-wall screen is a live chart of the pair being worked on.
    {
      const focus = info?.symbol ?? null
      const q = focus ? getQuotes?.().find((x) => x.symbol === focus) : undefined
      shell.chart.update(focus, q?.price ?? null, q?.prev ?? null, getChartCandles?.(), getCycleInfo?.() ?? null)
    }

    // The lounge TV plays an actual game, rotating through the catalogue every
    // 5 minutes. A second player (co-op teammate or versus opponent) is drawn
    // only when two agents have actually walked over and sat down to play.
    if (tvTexture) {
      if (t >= tvGameSwitchAt) {
        tvGameSwitchAt = t + 300
        tvGameIndex = (tvGameIndex + 1) % TV_GAMES.length
      }
      if (t - tvPaintedAt > 0.1) {
        tvPaintedAt = t
        let gamesPlayers = 0
        for (const a of avatars.values()) {
          if (a.station?.id === 'games' && a.activity === 'using') gamesPlayers++
        }
        paintTvGame(tvTexture, t, tvGameIndex, Math.min(2, gamesPlayers))
      }
    }

    core.rotation.y += dt * 0.6
    core.rotation.x += dt * 0.25
    const activeCount = seats.filter((s) => s.state === 'analyzing').length
    tableGlow.intensity = 1.5 + activeCount * 0.35 + Math.sin(t * 2) * 0.15
    ;(tableRing.material as THREE.MeshBasicMaterial).opacity = 0.55 + Math.sin(t * 1.6) * 0.2

    // One poll per frame: whoever is speaking at the board right now.
    const speechTurn = getSpeech?.() ?? null
    const nowSec = Date.now() / 1000

    for (const seat of seats) {
      const av = avatars.get(seat.role)
      if (!av) continue
      const p = av.phase
      const rig = av.rig
      const haloMat = av.halo.material as THREE.MeshBasicMaterial
      const focused = focusedRole === seat.role
      const seated = av.activity === 'seated'

      updateLocomotion(av, seat, dt)

      // Seat-state animation only applies in the chair; a walking agent is
      // already fully posed by the locomotion director.
      if (seated) {
        poseSeated(rig, dt)
        switch (seat.state) {
          case 'analyzing': {
            // Leaning in, head down, one hand near the chin.
            lerpTo(rig.torso, 0.22 + Math.sin(t * 2.2 + p) * 0.03, 0, 0, dt)
            lerpTo(rig.head, 0.3 + Math.sin(t * 3.1 + p) * 0.05, Math.sin(t * 0.9 + p) * 0.12, 0, dt)
            lerpTo(rig.rightShoulder, -0.62, 0, -0.5, dt)
            lerpTo(rig.rightElbow, -1.85, 0, 0, dt)
            lerpTo(rig.leftShoulder, 0.12, 0, 0.18, dt)
            lerpTo(rig.leftElbow, 0.55, 0, 0, dt)
            haloMat.opacity = 0.35 + Math.sin(t * 4 + p) * 0.25
            av.halo.rotation.z += dt * 1.8
            rig.bodyMat.emissiveIntensity = 1.0 + Math.sin(t * 5 + p) * 0.3
            break
          }
          case 'presenting': {
            // Upright, gesturing at the table.
            lerpTo(rig.torso, -0.04, Math.sin(t * 1.1 + p) * 0.06, 0, dt)
            lerpTo(rig.head, -0.08, Math.sin(t * 1.6 + p) * 0.22, 0, dt)
            lerpTo(rig.rightShoulder, -0.75 + Math.sin(t * 4.5 + p) * 0.3, 0, -0.42, dt)
            lerpTo(rig.rightElbow, -0.85 + Math.sin(t * 4.5 + p) * 0.25, 0, 0, dt)
            lerpTo(rig.leftShoulder, -0.4 + Math.sin(t * 4.5 + p + 1) * 0.22, 0, 0.35, dt)
            lerpTo(rig.leftElbow, -0.6, 0, 0, dt)
            haloMat.opacity = 0.7
            av.halo.rotation.z += dt * 3.2
            rig.bodyMat.emissiveIntensity = 1.4
            break
          }
          case 'error': {
            lerpTo(rig.torso, 0.05, 0, 0, dt)
            lerpTo(rig.head, 0.12, 0, 0, dt)
            relaxArms(rig, dt)
            haloMat.opacity = 0.5 + Math.sin(t * 9) * 0.5
            rig.bodyMat.emissiveIntensity = 0.35
            break
          }
          case 'resting': {
            // Slumped back, arms down.
            lerpTo(rig.torso, -0.12, 0, 0, dt, 4)
            lerpTo(rig.head, 0.16 + Math.sin(t * 1.1 + p) * 0.02, Math.sin(t * 0.5 + p) * 0.08, 0, dt, 4)
            relaxArms(rig, dt, 4)
            haloMat.opacity = 0.12
            rig.bodyMat.emissiveIntensity = 0.45
            break
          }
          default: {
            // idle — quiet breathing and the occasional glance
            lerpTo(rig.torso, Math.sin(t * 1.3 + p) * 0.02, Math.sin(t * 0.4 + p) * 0.05, 0, dt, 4)
            lerpTo(rig.head, Math.sin(t * 1.3 + p) * 0.03, Math.sin(t * 0.55 + p) * 0.25, 0, dt, 4)
            relaxArms(rig, dt, 4)
            haloMat.opacity = 0.16
            rig.bodyMat.emissiveIntensity = 0.65
          }
        }
      }

      // Laptops idle-loop when their owner is away and speed up while working.
      const busy = seated && (seat.state === 'analyzing' || seat.state === 'presenting')

      // Roll the chair (and its occupant) up to the table while working, and let
      // it drift back out to a resting distance when idle or when the agent
      // stands to walk off. The laptop stays put on the table, so a working
      // agent reads as leaning in to their screen.
      av.deskOffset = damp(av.deskOffset, busy ? 1 : 0, dt, 2.5)
      const pull = av.deskOffset * WORK_PULL
      if (seated) {
        av.group.position.set(
          av.homePos.x + av.inward.x * pull, 0, av.homePos.z + av.inward.z * pull,
        )
      }
      // The empty chair follows the pull too, so it is never left behind at the
      // table or shoved into an agent who has walked away.
      av.chair.position.set(
        av.homePos.x + av.inward.x * pull, 0, av.homePos.z + av.inward.z * pull,
      )
      av.chair.rotation.y = av.homeYaw

      const laptopInterval = busy ? 0.12 : 0.4
      if (t - av.laptop.paintedAt > laptopInterval) {
        av.laptop.paintedAt = t
        paintLaptop(av.laptop, t, busy, {
          role: seat.role,
          symbol: info?.symbol ?? null,
          action: seat.action ?? info?.action ?? null,
          confidence: seat.confidence ?? info?.confidence ?? 0,
        })
      }
      // The lid closes over while nobody is sitting there.
      av.laptop.group.scale.y = damp(av.laptop.group.scale.y, seated ? 1 : 0.55, dt, 3)

      animateFace(rig, {
        time: t,
        phase: p,
        talking: seated && seat.state === 'presenting',
        mood: seat.state === 'analyzing' ? 'focused'
          : seat.state === 'presenting' ? 'bright'
          : 'neutral',
        dt,
        reduced: !gfx.shadows,
      })

      if (focused) {
        haloMat.opacity = Math.max(haloMat.opacity, 0.85)
        rig.bodyMat.emissiveIntensity += 0.6
      }

      // The halo and readouts ride along when their owner leaves the chair.
      av.halo.position.y = damp(av.halo.position.y, seated ? 1.78 : 1.92, dt, 6)

      // Confidence bar grows from the left edge of the panel.
      const target = Math.max(0.02, Math.min(1, seat.confidence ?? 0))
      av.panelFill.scale.x += (target - av.panelFill.scale.x) * Math.min(1, dt * 4)
      av.panelFill.position.x = -0.42 + (0.84 * av.panelFill.scale.x) / 2

      // Panels always face the camera so text-free bars stay readable.
      av.panel.lookAt(camera.position)
      av.panelFill.quaternion.copy(av.panel.quaternion)

      // Counteract perspective so every name plate reads at the same size.
      av.group.getWorldPosition(tmpTarget)
      const labelScale = THREE.MathUtils.clamp(camera.position.distanceTo(tmpTarget) * 0.115, 0.9, 2.2)
      av.label.scale.set(labelScale, labelScale * 0.31, 1)

      // Speech bubble — shown while this seat holds the floor. A fresh turn
      // speaks its full text; a seat that is still presenting after the turn
      // expires (or whose reasoning never produced a speaking event) falls
      // back to a compact verdict line, so nobody presents in silence.
      // Scales with distance like the name plate so it stays readable.
      const turn = speechTurn && speechTurn.role === seat.role ? speechTurn : null
      const turnFresh = turn !== null && nowSec - turn.at < SPEECH_TTL
      if (turn !== null && turnFresh) {
        av.speech.say(turn.text, seat.color)
        av.speech.visible = true
        const bubbleScale = THREE.MathUtils.clamp(camera.position.distanceTo(tmpTarget) * 0.16, 1.1, 3.0)
        // Fade the tail end of the turn rather than popping it away.
        const age = nowSec - turn.at
        const fade = age > SPEECH_TTL - 1.5 ? (SPEECH_TTL - age) / 1.5 : 1
        ;(av.speech.material as THREE.SpriteMaterial).opacity = Math.max(0.15, fade)
        av.speech.scale.set(bubbleScale, bubbleScale * 0.5, 1)
      } else if (seated && seat.state === 'presenting') {
        const conf = seat.confidence ?? 0
        const pct = Math.round(conf * (conf <= 1 ? 100 : 1))
        av.speech.say(`${(seat.action ?? 'hold').toUpperCase()} — conviction ${pct}%`, seat.color)
        av.speech.visible = true
        const bubbleScale = THREE.MathUtils.clamp(camera.position.distanceTo(tmpTarget) * 0.16, 1.1, 3.0)
        ;(av.speech.material as THREE.SpriteMaterial).opacity = 1
        av.speech.scale.set(bubbleScale, bubbleScale * 0.5, 1)
      } else {
        av.speech.clear()
      }
    }

    if (particles) {
      const arr = particles.geometry.getAttribute('position') as THREE.BufferAttribute
      for (let i = 0; i < arr.count; i++) {
        const x = arr.getX(i)
        const z = arr.getZ(i)
        const r = Math.hypot(x, z)
        const a = Math.atan2(z, x) + dt * (0.35 + (i % 5) * 0.05)
        const nr = r > 1.15 ? r - dt * 0.55 : SEAT_RADIUS
        arr.setX(i, Math.cos(a) * nr)
        arr.setZ(i, Math.sin(a) * nr)
        arr.setY(i, 1.3 + Math.sin(t * 1.5 + i) * 0.35)
      }
      arr.needsUpdate = true
    }

    if (focusedRole) {
      const av = avatars.get(focusedRole)
      if (av) {
        tmpTarget.copy(av.group.position).setY(1.4)
        controls.target.lerp(tmpTarget, Math.min(1, dt * 2))
      }
    } else {
      tmpTarget.set(0, 1.55, 0)
      controls.target.lerp(tmpTarget, Math.min(1, dt * 2))
    }

    // Follow an agent out onto the deck: while a focused agent is outside, the
    // camera is allowed past the room walls out to the deck edge.
    const focusedAv = focusedRole ? avatars.get(focusedRole) : null
    const cage = focusedAv && focusedAv.outside ? OUTSIDE_CAGE : ROOM_CAGE

    // Budget the zoom from wherever the target is now: focusing a seat moves it
    // several metres off-centre, and a distance measured from there would
    // otherwise reach straight through the wall behind it.
    controls.maxDistance = maxOrbitDistance(controls.target, cage)
    controls.update()
    // OrbitControls owns the orbit; the room owns the bounds. Clamping after the
    // update is what guarantees no angle, zoom or focus can leave the room —
    // except when following an agent onto the deck (the wider cage).
    constrainCameraToRoom(camera.position, cage)
    renderer.render(scene, camera)
  }
  animate()

  return {
    resize(width, height) {
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height, false)
      frameScene()
    },
    setPaused(next) {
      paused = next
      if (!next) clock.getDelta() // drop the elapsed gap so nothing jumps
    },
    focusSeat(role) {
      focusedRole = role
      controls.autoRotate = !reducedMotion && !role
    },
    getFps: () => fps,
    dispose() {
      cancelAnimationFrame(raf)
      canvas.removeEventListener('pointerdown', handleClick)
      controls.dispose()
      disposables.forEach((d) => d.dispose())
      renderer.dispose()
    },
  }
}
