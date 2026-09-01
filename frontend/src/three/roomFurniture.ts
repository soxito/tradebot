/**
 * roomFurniture — the space the agents work in.
 *
 * Three groups of props:
 *   • the shell     — walls, ceiling, window, skirting. Turns the open platform
 *                     into somewhere that furniture can plausibly stand.
 *   • the stations  — coffee, water, snacks, lounge. Each exposes a `usePoint`
 *                     and a facing, which is the entire contract the locomotion
 *                     director needs to send someone over.
 *   • the screens   — wall panels drawn to a canvas, repainted only when the
 *                     text actually changes (never per frame).
 *
 * Everything is procedural — no external assets — and every geometry, material
 * and texture goes through the caller's `track()` so one `dispose()` cleans up.
 */
import * as THREE from 'three'

export type Track = <T extends { dispose: () => void }>(o: T) => T

export interface Station {
  id: 'coffee' | 'water' | 'snacks' | 'lounge' | 'games' | 'board' | 'chart'
  label: string
  /** Where the agent stands to use it (floor level). */
  usePoint: THREE.Vector3
  /** Point the agent turns to face while using it. */
  facePoint: THREE.Vector3
  /** Seconds to linger. */
  dwell: number
  /** Whether using it raises a cup. Snacks and the lounge do not. */
  drinks: boolean
  /** The agent sits down here (beanbag, sofa) instead of standing. */
  seated?: boolean
  /** Hip height while seated at this station — beanbag is lower than a sofa. */
  sitHeight?: number
  /** Raises food to the mouth and chews (the snack table). */
  eats?: boolean
  /** Individual seats (e.g. two beanbags) so two agents never share one seat. */
  seatSlots?: THREE.Vector3[]
  /** Point each seat slot faces while used; defaults to `facePoint`. */
  seatFacings?: THREE.Vector3[]
}

export interface RoomShellOptions {
  scene: THREE.Scene
  track: Track
  shadows: boolean
  /** Inner half-width of the room. Walls sit just outside this. */
  radius: number
}

/** Floor-to-ceiling height. Exported so the camera can stay under the roof. */
export const ROOM_HEIGHT = 5.4

/** One OHLC bar for the back-wall chart — real candles from the market feed. */
export interface ChartCandle {
  open: number
  high: number
  low: number
  close: number
  /** Bar timestamp (ms). Carried so pattern flags can land on the right bar. */
  time?: number
}

/** A named candlestick pattern flagged on the wall chart. */
export interface ChartPatternMarker {
  /** Bar timestamp (ms) — matched to the nearest candle. */
  time: number
  name: string
  direction: 'bull' | 'bear'
}

/** One green/red cycle band painted behind the candles. */
export interface CycleBand {
  start: number  // ms
  end: number    // ms
  phase: 'bull' | 'bear'
  projected?: boolean
}

/** Optional overlays for the wall chart — patterns + cycle season bands. */
export interface ChartOverlays {
  markers?: ChartPatternMarker[]
  bands?: CycleBand[]
}

/** Handle to the big back-wall chart screen. */
/** The Bitcoin cycle read, as painted on the chart header. */
export interface CycleScreenInfo {
  phase: 'bull' | 'bear'
  dayOfCycle: number
  daysToTurn: number
}

export interface ChartScreenHandle {
  /**
   * Feed the focused pair, its live price, and (when available) real OHLC bars.
   * Repaints only when something actually changed. When `candles` are supplied
   * the screen draws an accurate OHLC bar chart; otherwise it falls back to a
   * synthesised line from the price stream.
   */
  update(
    symbol: string | null,
    price: number | null,
    prev: number | null,
    candles?: ChartCandle[] | null,
    cycle?: CycleScreenInfo | null,
    overlays?: ChartOverlays | null,
  ): void
}

export interface RoomShellHandle {
  group: THREE.Group
  /** The big screen on the back wall — a live chart of the pair in focus. */
  chart: ChartScreenHandle
  /** Anchors for agents stepping out through the doorway onto the deck. */
  outside: OutsideAnchors
}

/** Where the doorway and the open-air deck spots are, for the locomotion director. */
export interface OutsideAnchors {
  /** Floor point just inside the doorway. */
  doorInside: THREE.Vector3
  /** Floor point just outside the doorway, on the deck. */
  doorOutside: THREE.Vector3
  /** Standing spots on the deck (one per agent, so they never overlap). */
  spots: THREE.Vector3[]
  /** Point out over the railing the agents look toward. */
  facePoint: THREE.Vector3
  /** Furthest floor distance from centre the deck reaches (for the camera). */
  reach: number
}

/**
 * Walls, ceiling and the big back-wall screen. Built from planes rather than an
 * inverted box so each surface can take its own material. The former night
 * window is now a large live chart of the pair the room is working on.
 */
export function buildRoomShell(o: RoomShellOptions): RoomShellHandle {
  const { track, shadows, radius } = o
  const group = new THREE.Group()

  // Front-facing only, with every wall's normal pointing inward. If the camera
  // ever does end up outside, the wall turns invisible and you keep seeing the
  // room — far better than a full-screen unlit slab.
  const wallMat = track(new THREE.MeshStandardMaterial({
    color: 0x131c2c,
    roughness: 0.92,
    metalness: 0.04,
    side: THREE.FrontSide,
  }))

  const trimMat = track(new THREE.MeshStandardMaterial({
    color: 0x1e2a40, roughness: 0.6, metalness: 0.35,
  }))

  const half = radius
  const wallGeo = track(new THREE.PlaneGeometry(half * 2, ROOM_HEIGHT))

  // Back, left and right walls face inward as full planes. If the camera ever
  // ends up outside, the wall turns invisible and you keep seeing the room.
  const solidPlacements: Array<[number, number, number]> = [
    [0, -half, 0],           // back
    [-half, 0, Math.PI / 2], // left
    [half, 0, -Math.PI / 2], // right
  ]
  for (const [x, z, ry] of solidPlacements) {
    const wall = new THREE.Mesh(wallGeo, wallMat)
    wall.position.set(x, ROOM_HEIGHT / 2, z)
    wall.rotation.y = ry
    wall.receiveShadow = shadows
    group.add(wall)
  }

  // ── Front wall: two segments + a lintel, leaving a central doorway that
  // agents use to step out onto the deck. ──
  const DOOR_W = 2.8
  const DOOR_H = 3.2
  const segW = half - DOOR_W / 2
  const segCenter = (half + DOOR_W / 2) / 2
  const segGeo = track(new THREE.PlaneGeometry(segW, ROOM_HEIGHT))
  for (const sx of [-segCenter, segCenter]) {
    const seg = new THREE.Mesh(segGeo, wallMat)
    seg.position.set(sx, ROOM_HEIGHT / 2, half)
    seg.rotation.y = Math.PI
    seg.receiveShadow = shadows
    group.add(seg)
  }
  const lintel = new THREE.Mesh(
    track(new THREE.PlaneGeometry(DOOR_W, ROOM_HEIGHT - DOOR_H)), wallMat,
  )
  lintel.position.set(0, (DOOR_H + ROOM_HEIGHT) / 2, half)
  lintel.rotation.y = Math.PI
  group.add(lintel)
  // Door frame uprights so the opening reads as a real doorway.
  const jambMat = track(new THREE.MeshStandardMaterial({
    color: 0x1e2a40, roughness: 0.5, metalness: 0.4,
  }))
  const jambGeo = track(new THREE.BoxGeometry(0.12, DOOR_H, 0.14))
  for (const jx of [-DOOR_W / 2, DOOR_W / 2]) {
    const jamb = new THREE.Mesh(jambGeo, jambMat)
    jamb.position.set(jx, DOOR_H / 2, half)
    group.add(jamb)
  }

  // Skirting so the wall/floor joint reads as a real corner (skip the doorway).
  const skirtGeo = track(new THREE.BoxGeometry(half * 2, 0.12, 0.06))
  for (const [x, z, ry] of solidPlacements) {
    const skirt = new THREE.Mesh(skirtGeo, trimMat)
    skirt.position.set(x, 0.06, z)
    skirt.rotation.y = ry
    group.add(skirt)
  }
  const frontSkirtGeo = track(new THREE.BoxGeometry(segW, 0.12, 0.06))
  for (const sx of [-segCenter, segCenter]) {
    const skirt = new THREE.Mesh(frontSkirtGeo, trimMat)
    skirt.position.set(sx, 0.06, half)
    group.add(skirt)
  }

  const ceilGeo = track(new THREE.PlaneGeometry(half * 2, half * 2))
  const ceilMat = track(new THREE.MeshStandardMaterial({
    color: 0x0c1420, roughness: 0.98, side: THREE.FrontSide,
  }))
  const ceiling = new THREE.Mesh(ceilGeo, ceilMat)
  ceiling.rotation.x = Math.PI / 2
  ceiling.position.y = ROOM_HEIGHT
  group.add(ceiling)

  // ── Window: a city skyline at night, painted once to a canvas. ──
  // ── Big chart screen: a live candlestick of the pair the room works on. ──
  const chartCanvas = document.createElement('canvas')
  chartCanvas.width = 1024
  chartCanvas.height = 512
  const chartCtx = chartCanvas.getContext('2d')!
  const chartTex = track(new THREE.CanvasTexture(chartCanvas))
  chartTex.colorSpace = THREE.SRGBColorSpace

  const win = new THREE.Mesh(
    track(new THREE.PlaneGeometry(5.2, 2.4)),
    track(new THREE.MeshBasicMaterial({ map: chartTex, toneMapped: false, fog: false })),
  )
  // Sits clearly in front of the frame's front face (below) so the two never
  // share a depth plane — coplanar meshes z-fight and make the screen flicker.
  win.position.set(0, 2.5, -half + 0.14)
  group.add(win)

  const frameMat = track(new THREE.MeshStandardMaterial({
    color: 0x0b111c, roughness: 0.5, metalness: 0.6,
  }))
  const frameGeo = track(new THREE.BoxGeometry(5.5, 2.7, 0.1))
  const frame = new THREE.Mesh(frameGeo, frameMat)
  frame.position.set(0, 2.5, -half + 0.01)
  group.add(frame)

  // Cool spill from the screen keeps the far wall from going flat black.
  const windowGlow = new THREE.PointLight(0x2f6fbf, 0.75, 15, 2)
  windowGlow.position.set(0, 2.5, -half + 1.2)
  group.add(windowGlow)

  const chart = makeChartScreen(chartCanvas, chartCtx, chartTex)

  // ── Windows in the front wall + the open-air deck beyond the doorway. ──
  const cityTex = track(makeCityTexture())
  const winFrameMat = track(new THREE.MeshStandardMaterial({
    color: 0x0b111c, roughness: 0.5, metalness: 0.6,
  }))
  // Two windows flanking the door, showing the night skyline.
  for (const wx of [-4.4, 4.4]) {
    const glass = new THREE.Mesh(
      track(new THREE.PlaneGeometry(3.4, 2.2)),
      track(new THREE.MeshBasicMaterial({ map: cityTex, toneMapped: false })),
    )
    // On the inner face of the front wall, looking out (normal toward -z).
    glass.position.set(wx, 2.5, half - 0.05)
    glass.rotation.y = Math.PI
    group.add(glass)
    const wframe = new THREE.Mesh(
      track(new THREE.BoxGeometry(3.7, 2.5, 0.09)), winFrameMat,
    )
    wframe.position.set(wx, 2.5, half - 0.02)
    group.add(wframe)
    // Mullions.
    const barMat = track(new THREE.MeshStandardMaterial({ color: 0x0b111c, metalness: 0.6, roughness: 0.5 }))
    const vbar = new THREE.Mesh(track(new THREE.BoxGeometry(0.06, 2.2, 0.06)), barMat)
    vbar.position.set(wx, 2.5, half - 0.07); group.add(vbar)
    const hbar = new THREE.Mesh(track(new THREE.BoxGeometry(3.4, 0.06, 0.06)), barMat)
    hbar.position.set(wx, 2.5, half - 0.07); group.add(hbar)
  }

  // Deck slab just outside the doorway.
  const DECK_DEPTH = 5.5
  const deckZ0 = half
  const deckZ1 = half + DECK_DEPTH
  const deckMat = track(new THREE.MeshStandardMaterial({ color: 0x141b26, roughness: 0.85, metalness: 0.15 }))
  const deck = new THREE.Mesh(track(new THREE.PlaneGeometry(12, DECK_DEPTH)), deckMat)
  deck.rotation.x = -Math.PI / 2
  deck.position.set(0, 0.02, (deckZ0 + deckZ1) / 2)
  deck.receiveShadow = shadows
  group.add(deck)

  // Glass balustrade around the deck edge (far + sides).
  const railMat = track(new THREE.MeshStandardMaterial({
    color: 0x7dd3fc, transparent: true, opacity: 0.14, roughness: 0.1, metalness: 0.2,
  }))
  const railCapMat = track(new THREE.MeshStandardMaterial({ color: 0x334155, metalness: 0.7, roughness: 0.4 }))
  const farRail = new THREE.Mesh(track(new THREE.BoxGeometry(12, 1.0, 0.06)), railMat)
  farRail.position.set(0, 0.5, deckZ1); group.add(farRail)
  const farCap = new THREE.Mesh(track(new THREE.BoxGeometry(12, 0.06, 0.12)), railCapMat)
  farCap.position.set(0, 1.0, deckZ1); group.add(farCap)
  for (const sx of [-6, 6]) {
    const sideRail = new THREE.Mesh(track(new THREE.BoxGeometry(0.06, 1.0, DECK_DEPTH)), railMat)
    sideRail.position.set(sx, 0.5, (deckZ0 + deckZ1) / 2); group.add(sideRail)
  }

  // The outside view: a big city skyline backdrop beyond the railing.
  const backdrop = new THREE.Mesh(
    track(new THREE.PlaneGeometry(34, 12)),
    track(new THREE.MeshBasicMaterial({ map: cityTex, toneMapped: false })),
  )
  backdrop.position.set(0, 4.5, deckZ1 + 3)
  group.add(backdrop)
  // Moonlight wash over the deck.
  const deckLight = new THREE.PointLight(0x9ec3ff, 0.6, 20, 2)
  deckLight.position.set(0, 3.5, deckZ0 + 2)
  group.add(deckLight)
  // A planter each side to dress the deck.
  for (const px of [-4.6, 4.6]) {
    const pot = new THREE.Mesh(
      track(new THREE.CylinderGeometry(0.28, 0.22, 0.5, 10)),
      track(new THREE.MeshStandardMaterial({ color: 0x1f2937, roughness: 0.8 })),
    )
    pot.position.set(px, 0.25, deckZ0 + 1); group.add(pot)
    const bush = new THREE.Mesh(
      track(new THREE.IcosahedronGeometry(0.4, 1)),
      track(new THREE.MeshStandardMaterial({ color: 0x1f6b3f, roughness: 0.85 })),
    )
    bush.position.set(px, 0.75, deckZ0 + 1); bush.scale.set(1, 1.2, 1); group.add(bush)
  }

  const outside: OutsideAnchors = {
    doorInside: new THREE.Vector3(0, 0, half - 1.4),
    doorOutside: new THREE.Vector3(0, 0, half + 1.4),
    spots: [
      new THREE.Vector3(0, 0, deckZ1 - 1.4),
      new THREE.Vector3(-2.8, 0, deckZ1 - 1.7),
      new THREE.Vector3(2.8, 0, deckZ1 - 1.7),
    ],
    facePoint: new THREE.Vector3(0, 1.6, deckZ1 + 6),
    reach: deckZ1,
  }

  // ── Ceiling rig ─────────────────────────────────────────────────────────
  // A ring of recessed panels plus two pendants over the table. The panels are
  // emissive geometry with only a couple of real lights between them: a light
  // per fixture would look identical and cost far more to render.
  const panelGeo = track(new THREE.BoxGeometry(1.5, 0.06, 0.42))
  const panelMat = track(new THREE.MeshBasicMaterial({ color: 0xdce9ff }))
  const rigMat = track(new THREE.MeshStandardMaterial({
    color: 0x1b2536, roughness: 0.5, metalness: 0.6,
  }))

  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2
    const r = Math.min(radius * 0.55, 5.6)
    const panel = new THREE.Mesh(panelGeo, panelMat)
    panel.position.set(Math.cos(a) * r, ROOM_HEIGHT - 0.08, Math.sin(a) * r)
    panel.rotation.y = -a
    group.add(panel)

    const housing = new THREE.Mesh(track(new THREE.BoxGeometry(1.66, 0.1, 0.56)), rigMat)
    housing.position.copy(panel.position).setY(ROOM_HEIGHT - 0.03)
    housing.rotation.y = -a
    group.add(housing)
  }

  // Two soft fills carry the whole ring.
  for (const sign of [-1, 1]) {
    const fill = new THREE.PointLight(0xdbeafe, 0.55, 18, 2)
    fill.position.set(sign * 4, ROOM_HEIGHT - 0.6, sign * 3)
    group.add(fill)
  }

  // Warm pendants over the table itself.
  for (const x of [-2.2, 2.2]) {
    const flex = new THREE.Mesh(
      track(new THREE.CylinderGeometry(0.012, 0.012, ROOM_HEIGHT - 3.5, 6)),
      rigMat,
    )
    flex.position.set(x, (ROOM_HEIGHT + 3.5) / 2, 0)
    group.add(flex)

    const shade = new THREE.Mesh(
      track(new THREE.ConeGeometry(0.26, 0.24, 16, 1, true)),
      track(new THREE.MeshStandardMaterial({
        color: 0x243147, roughness: 0.45, metalness: 0.5, side: THREE.DoubleSide,
      })),
    )
    shade.position.set(x, 3.6, 0)
    group.add(shade)

    const lamp = new THREE.Mesh(
      track(new THREE.SphereGeometry(0.13, 12, 10)),
      track(new THREE.MeshBasicMaterial({ color: 0xffd9a0 })),
    )
    lamp.position.set(x, 3.46, 0)
    group.add(lamp)

    const light = new THREE.PointLight(0xffc98a, 1.15, 13, 2)
    light.position.set(x, 3.4, 0)
    group.add(light)
  }

  o.scene.add(group)
  return { group, chart, outside }
}

/** A night skyline: dark gradient, blocky towers, lit windows. */
function makeCityTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas')
  c.width = 512
  c.height = 256
  const ctx = c.getContext('2d')!

  const sky = ctx.createLinearGradient(0, 0, 0, c.height)
  sky.addColorStop(0, '#0a1428')
  sky.addColorStop(0.55, '#132745')
  sky.addColorStop(1, '#1d3a5c')
  ctx.fillStyle = sky
  ctx.fillRect(0, 0, c.width, c.height)

  // Deterministic layout — a redraw must not reshuffle the skyline.
  let s = 12345
  const rnd = () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff)

  for (let x = 0; x < c.width; ) {
    const w = 22 + rnd() * 40
    const h = 50 + rnd() * 120
    ctx.fillStyle = '#060c16'
    ctx.fillRect(x, c.height - h, w, h)

    ctx.fillStyle = 'rgba(255, 214, 140, 0.85)'
    for (let wy = c.height - h + 8; wy < c.height - 10; wy += 11) {
      for (let wx = x + 5; wx < x + w - 6; wx += 9) {
        if (rnd() > 0.55) ctx.fillRect(wx, wy, 4, 5)
      }
    }
    x += w + 3 + rnd() * 6
  }

  const tex = new THREE.CanvasTexture(c)
  tex.colorSpace = THREE.SRGBColorSpace
  return tex
}

/** Enough precision to be useful without turning gold into a phone number. */
function formatChartPrice(value: number): string {
  const abs = Math.abs(value)
  const dp = abs >= 1000 ? 2 : abs >= 10 ? 3 : abs >= 1 ? 4 : 6
  return value.toFixed(dp)
}

/**
 * The big back-wall screen: a live candlestick chart of the pair in focus.
 *
 * We only receive spot ticks, so a rolling buffer of recent prices is bucketed
 * into candles (open/high/low/close per bucket) — the same shape a real chart
 * shows, built from the live feed. Repaints only when the price actually moves.
 */
function makeChartScreen(
  canvas: HTMLCanvasElement, ctx: CanvasRenderingContext2D, texture: THREE.CanvasTexture,
): ChartScreenHandle {
  const W = canvas.width
  const H = canvas.height
  let symbol: string | null = null
  let bars: ChartCandle[] = []      // real OHLC from the market feed
  const ticks: number[] = []         // fallback line when no candles yet
  let livePrice: number | null = null
  let lastKey = ''
  let cycle: CycleScreenInfo | null = null
  let markers: ChartPatternMarker[] = []
  let bands: CycleBand[] = []
  let lastOverlayKey = ''

  // Turn the price stream into synthetic OHLC bars so the fallback still reads
  // as a bar chart, not a lonely line.
  function synthBars(): ChartCandle[] {
    if (ticks.length < 2) return []
    const target = 44
    const bucket = Math.max(1, Math.floor(ticks.length / target))
    const out: ChartCandle[] = []
    for (let i = 0; i < ticks.length; i += bucket) {
      const s = ticks.slice(i, i + bucket)
      out.push({ open: s[0], high: Math.max(...s), low: Math.min(...s), close: s[s.length - 1] })
    }
    return out
  }

  function paint() {
    const g = ctx.createLinearGradient(0, 0, 0, H)
    g.addColorStop(0, '#050a14'); g.addColorStop(1, '#02040a')
    ctx.fillStyle = g
    ctx.fillRect(0, 0, W, H)

    // Header bar.
    ctx.fillStyle = '#0a111e'
    ctx.fillRect(0, 0, W, 78)
    ctx.textAlign = 'left'
    ctx.fillStyle = '#e2e8f0'
    ctx.font = 'bold 46px system-ui, sans-serif'
    ctx.fillText(symbol ?? 'NO PAIR IN FOCUS', 34, 54)
    // LIVE pill.
    ctx.fillStyle = '#22c55e'
    ctx.beginPath(); ctx.arc(W - 150, 40, 8, 0, Math.PI * 2); ctx.fill()
    ctx.fillStyle = '#94a3b8'
    ctx.font = 'bold 26px system-ui, sans-serif'
    ctx.fillText('OHLC · H1', W - 132, 50)

    const padL = 60, padR = 150, padT = 96, padB = 54
    const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB

    const view = bars.length >= 2 ? bars : synthBars()
    if (view.length < 2 || !symbol) {
      ctx.textAlign = 'left'
      ctx.fillStyle = '#475569'
      ctx.font = '30px system-ui, sans-serif'
      ctx.fillText(symbol ? 'Loading candles…' : 'Select a pair to focus', 34, H / 2)
      texture.needsUpdate = true
      return
    }

    // Live price overrides the last bar's close so the header + marker track the
    // real-time quote, matching how the app's charts show a forming bar.
    const lastClose = livePrice ?? view[view.length - 1].close
    const firstOpen = view[0].open
    const up = lastClose >= firstOpen
    const line = up ? '#22c55e' : '#ef4444'

    // Header price + change.
    ctx.textAlign = 'right'
    ctx.fillStyle = line
    ctx.font = 'bold 40px ui-monospace, monospace'
    ctx.fillText(formatChartPrice(lastClose), W - 34, 44)
    const chg = firstOpen !== 0 ? ((lastClose - firstOpen) / Math.abs(firstOpen)) * 100 : 0
    ctx.font = 'bold 22px ui-monospace, monospace'
    ctx.fillText(`${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`, W - 34, 70)

    // Bitcoin cycle pill — the season the room is trading in, top-centre.
    if (cycle) {
      const label = `CYCLE ${cycle.phase.toUpperCase()} · day ${cycle.dayOfCycle} · ${cycle.daysToTurn >= 0 ? `${cycle.phase === 'bull' ? 'top' : 'bottom'} in ${cycle.daysToTurn}d` : 'turn due'}`
      ctx.font = 'bold 24px system-ui, sans-serif'
      const tw = ctx.measureText(label).width
      const pw = tw + 36
      const px = (W - pw) / 2
      ctx.fillStyle = cycle.phase === 'bull' ? 'rgba(34,197,94,0.16)' : 'rgba(239,68,68,0.16)'
      ctx.strokeStyle = cycle.phase === 'bull' ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)'
      ctx.lineWidth = 2
      ctx.beginPath(); ctx.roundRect(px, padT + 8, pw, 40, 12); ctx.fill(); ctx.stroke()
      ctx.fillStyle = cycle.phase === 'bull' ? '#4ade80' : '#f87171'
      ctx.textAlign = 'center'
      ctx.fillText(label, W / 2, padT + 36)
    }

    let lo = Infinity, hi = -Infinity
    for (const b of view) { if (b.low < lo) lo = b.low; if (b.high > hi) hi = b.high }
    if (livePrice != null) { lo = Math.min(lo, livePrice); hi = Math.max(hi, livePrice) }
    const span = hi - lo || Math.abs(hi) * 0.001 || 1
    const pad = span * 0.1
    const min = lo - pad, max = hi + pad
    const yOf = (v: number) => y1 - ((v - min) / (max - min)) * (y1 - y0)

    // Time → x helpers: bands and pattern flags land on the bar whose
    // timestamp they match, so they track the candles they belong to.
    const times = view.map((b) => b.time ?? 0)
    const timed = times.some((t) => t > 0)
    const xAt = (ms: number) => {
      if (!timed) return null
      if (ms <= times[0]) return x0
      if (ms >= times[times.length - 1]) return x1
      let lo = 0, hi = times.length - 1
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1
        if (times[mid] < ms) lo = mid; else hi = mid
      }
      const frac = (ms - times[lo]) / Math.max(1, times[hi] - times[lo])
      return x0 + (lo + frac) * cw0
    }
    const cw0 = (x1 - x0) / view.length

    // Cycle season bands — the green/red boxes behind the price, dimmed when
    // the segment is still a projection.
    if (timed) for (const band of bands) {
      const bx0 = xAt(band.start)
      const bx1 = xAt(band.end)
      if (bx0 == null || bx1 == null || bx1 <= bx0) continue
      ctx.fillStyle = band.phase === 'bull'
        ? (band.projected ? 'rgba(34,197,94,0.05)' : 'rgba(34,197,94,0.10)')
        : (band.projected ? 'rgba(239,68,68,0.05)' : 'rgba(239,68,68,0.10)')
      ctx.fillRect(bx0, y0, Math.min(bx1, x1) - bx0, y1 - y0)
    }

    // Grid + price axis.
    ctx.strokeStyle = 'rgba(148,163,184,0.12)'
    ctx.fillStyle = '#64748b'
    ctx.font = '20px ui-monospace, monospace'
    ctx.lineWidth = 1
    ctx.textAlign = 'left'
    for (let i = 0; i <= 4; i++) {
      const v = max - ((max - min) * i) / 4
      const gy = yOf(v)
      ctx.beginPath(); ctx.moveTo(x0, gy); ctx.lineTo(x1, gy); ctx.stroke()
      ctx.fillText(formatChartPrice(v), x1 + 10, gy + 6)
    }

    // OHLC bars: a high→low stick with an open tick on the left and a close tick
    // on the right — the classic bar chart the user asked for, drawn from the
    // real candles so it matches the app's other charts tick-for-tick.
    const cw = cw0
    const tick = Math.min(Math.max(3, cw * 0.34), 14)
    const barW = Math.max(1.5, Math.min(3, cw * 0.16))
    view.forEach((b, i) => {
      const cx = x0 + i * cw + cw / 2
      const green = b.close >= b.open
      ctx.strokeStyle = green ? '#22c55e' : '#ef4444'
      ctx.lineWidth = barW
      ctx.lineCap = 'butt'
      // Vertical range.
      ctx.beginPath(); ctx.moveTo(cx, yOf(b.high)); ctx.lineTo(cx, yOf(b.low)); ctx.stroke()
      // Open tick (left) + close tick (right).
      const yO = yOf(b.open), yC = yOf(b.close)
      ctx.beginPath(); ctx.moveTo(cx - tick, yO); ctx.lineTo(cx, yO); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(cx, yC); ctx.lineTo(cx + tick, yC); ctx.stroke()
    })

    // Pattern flags: a small triangle above (bear) or below (bull) the bar the
    // pattern printed on, so the wall chart reads like the reference screen.
    if (timed) for (const m of markers) {
      const mx = xAt(m.time)
      if (mx == null || mx < x0 || mx > x1) continue
      // Nearest bar for the flag's vertical anchor.
      let best = 0, bestDist = Infinity
      for (let i = 0; i < times.length; i++) {
        const d = Math.abs(times[i] - m.time)
        if (d < bestDist) { bestDist = d; best = i }
      }
      const b = view[best]
      const cx = x0 + best * cw + cw / 2
      const bull = m.direction === 'bull'
      const ay = bull ? yOf(b.low) + 14 : yOf(b.high) - 14
      const dir = bull ? 1 : -1
      ctx.fillStyle = bull ? '#22c55e' : '#ef4444'
      ctx.beginPath()
      ctx.moveTo(cx, ay + dir * 7)
      ctx.lineTo(cx - 5, ay - dir * 3)
      ctx.lineTo(cx + 5, ay - dir * 3)
      ctx.closePath(); ctx.fill()
    }

    // Last-price marker line.
    const ly = yOf(lastClose)
    ctx.strokeStyle = line
    ctx.lineWidth = 2
    ctx.setLineDash([6, 6])
    ctx.beginPath(); ctx.moveTo(x0, ly); ctx.lineTo(x1, ly); ctx.stroke()
    ctx.setLineDash([])
    ctx.fillStyle = line
    ctx.fillRect(x1, ly - 15, padR - 12, 30)
    ctx.fillStyle = '#04070e'
    ctx.font = 'bold 20px ui-monospace, monospace'
    ctx.textAlign = 'left'
    ctx.fillText(formatChartPrice(lastClose), x1 + 8, ly + 6)

    texture.needsUpdate = true
  }

  paint()

  return {
    update(sym, price, prev, candles, cycleInfo, overlays) {
      let changed = false

      if (cycleInfo && JSON.stringify(cycleInfo) !== JSON.stringify(cycle)) {
        cycle = cycleInfo
        changed = true
      }

      // Ignore transient null/empty symbols so the chart never flickers back to
      // "Loading" mid-analysis; only switch when a real new pair arrives.
      if (sym && sym !== symbol) {
        symbol = sym
        bars = []
        ticks.length = 0
        livePrice = null
        markers = []
        bands = []
        if (prev != null) ticks.push(prev)
        if (price != null) ticks.push(price)
        changed = true
      }

      // Real candles are the source of truth when we have them.
      if (candles && candles.length) {
        const lastBarClose = candles[candles.length - 1].close
        if (candles.length !== bars.length || bars.length === 0 ||
            bars[bars.length - 1].close !== lastBarClose) {
          bars = candles
          changed = true
        }
      }

      // Overlays: pattern flags + season bands, repainted only on change.
      const overlayKey = JSON.stringify(overlays ?? null)
      if (overlayKey !== lastOverlayKey) {
        lastOverlayKey = overlayKey
        markers = overlays?.markers ?? []
        bands = overlays?.bands ?? []
        changed = true
      }

      // Live price feeds the header/marker and, when no candles exist, the line.
      if (symbol && price != null && price !== livePrice) {
        livePrice = price
        if (!bars.length && (ticks.length === 0 || ticks[ticks.length - 1] !== price)) {
          ticks.push(price)
          if (ticks.length > 220) ticks.shift()
        }
        changed = true
      }

      if (!changed) return // nothing moved — leave the last frame on screen
      const key = `${symbol}|${bars.length}|${ticks.length}|${livePrice ?? ''}` +
        `|${bars.length ? bars[bars.length - 1].close : ''}`
      if (key === lastKey) return
      lastKey = key
      paint()
    },
  }
}

export interface FurnitureOptions {
  scene: THREE.Scene
  track: Track
  shadows: boolean
  /** Distance from centre to place props against the wall. */
  radius: number
}

export interface FurnitureHandle {
  group: THREE.Group
  stations: Station[]
  /** The TV picture. Handed back directly rather than looked up by name. */
  tvTexture: THREE.CanvasTexture
}

/**
 * Coffee bar, water cooler, snack table and a lounge corner.
 *
 * Placed on the half of the room away from the window so they never sit in
 * front of it, and each `usePoint` is pulled a step back toward the table so an
 * agent stands beside the prop rather than inside it.
 */
export function buildFurniture(o: FurnitureOptions): FurnitureHandle {
  const { track, shadows, radius } = o
  const group = new THREE.Group()
  const stations: Station[] = []
  const tvTexture = track(makeGameTexture())

  const woodMat = track(new THREE.MeshStandardMaterial({ color: 0x3b2a1d, roughness: 0.78 }))
  const metalMat = track(new THREE.MeshStandardMaterial({ color: 0x2a3446, roughness: 0.35, metalness: 0.8 }))
  const darkMat = track(new THREE.MeshStandardMaterial({ color: 0x11192a, roughness: 0.6, metalness: 0.3 }))
  const fabricMat = track(new THREE.MeshStandardMaterial({ color: 0x2c3a52, roughness: 0.95 }))
  const leafMat = track(new THREE.MeshStandardMaterial({ color: 0x1f6b3f, roughness: 0.85 }))

  const add = (
    geo: THREE.BufferGeometry, mat: THREE.Material,
    x: number, y: number, z: number, parent: THREE.Object3D = group,
  ) => {
    const m = new THREE.Mesh(geo, mat)
    m.position.set(x, y, z)
    if (shadows) { m.castShadow = true; m.receiveShadow = true }
    parent.add(m)
    return m
  }

  // Props sit on their own ring rather than hard against the wall: in a large
  // room the far wall is too distant for a coffee bar to read, and the walk
  // there would be tedious to watch.
  const wall = Math.min(radius - 0.5, 8.2)

  // ── Coffee bar (right wall) ──
  {
    const x = wall
    const z = -1.6
    const counter = new THREE.Group()
    counter.position.set(x, 0, z)
    counter.rotation.y = -Math.PI / 2
    group.add(counter)

    add(track(new THREE.BoxGeometry(1.7, 0.9, 0.6)), woodMat, 0, 0.45, 0, counter)
    add(track(new THREE.BoxGeometry(1.74, 0.06, 0.64)), metalMat, 0, 0.93, 0, counter)
    // The machine itself.
    add(track(new THREE.BoxGeometry(0.42, 0.5, 0.34)), darkMat, -0.35, 1.21, 0, counter)
    add(track(new THREE.BoxGeometry(0.3, 0.05, 0.02)), metalMat, -0.35, 1.06, 0.17, counter)
    add(track(new THREE.CylinderGeometry(0.028, 0.028, 0.12, 8)), metalMat, -0.35, 1.0, 0.1, counter)
    // A couple of waiting cups.
    const cupGeo = track(new THREE.CylinderGeometry(0.035, 0.03, 0.08, 10))
    const cupMat = track(new THREE.MeshStandardMaterial({ color: 0xe2e8f0, roughness: 0.5 }))
    add(cupGeo, cupMat, 0.25, 1.0, 0.05, counter)
    add(cupGeo, cupMat, 0.4, 1.0, -0.05, counter)
    // Small warm lamp so the bar reads as a destination.
    const glow = new THREE.PointLight(0xffb266, 0.5, 4, 2)
    glow.position.set(x - 0.6, 1.7, z)
    group.add(glow)

    stations.push({
      id: 'coffee',
      label: 'coffee',
      usePoint: new THREE.Vector3(x - 1.15, 0, z),
      facePoint: new THREE.Vector3(x, 1, z),
      dwell: 4.2,
      drinks: true,
    })
  }

  // ── Water cooler (right wall, further along) ──
  {
    const x = wall
    const z = 1.7
    add(track(new THREE.BoxGeometry(0.42, 0.95, 0.42)), darkMat, x - 0.1, 0.48, z)
    const bottle = add(
      track(new THREE.CylinderGeometry(0.19, 0.19, 0.5, 14)),
      track(new THREE.MeshStandardMaterial({
        color: 0x7dd3fc, roughness: 0.12, metalness: 0.1,
        transparent: true, opacity: 0.62,
      })),
      x - 0.1, 1.2, z,
    )
    bottle.castShadow = false
    add(track(new THREE.BoxGeometry(0.1, 0.12, 0.06)), metalMat, x - 0.28, 0.72, z)

    stations.push({
      id: 'water',
      label: 'water',
      usePoint: new THREE.Vector3(x - 1.15, 0, z),
      facePoint: new THREE.Vector3(x, 1, z),
      dwell: 3.4,
      drinks: true,
    })
  }

  // ── Snack table (left wall) ──
  {
    const x = -wall
    const z = -0.4
    add(track(new THREE.BoxGeometry(1.3, 0.06, 0.6)), woodMat, x + 0.15, 0.78, z)
    const legGeo = track(new THREE.CylinderGeometry(0.03, 0.03, 0.78, 8))
    for (const [lx, lz] of [[-0.55, -0.22], [0.55, -0.22], [-0.55, 0.22], [0.55, 0.22]]) {
      add(legGeo, metalMat, x + 0.15 + lx, 0.39, z + lz)
    }
    // Bowls of something worth getting up for.
    const bowlGeo = track(new THREE.SphereGeometry(0.13, 12, 8, 0, Math.PI * 2, Math.PI * 0.5, Math.PI * 0.5))
    add(bowlGeo, track(new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.4 })), x + 0.45, 0.86, z)
    add(bowlGeo, track(new THREE.MeshStandardMaterial({ color: 0xfcd34d, roughness: 0.6 })), x - 0.15, 0.86, z + 0.06)
    add(track(new THREE.SphereGeometry(0.1, 10, 8)),
      track(new THREE.MeshStandardMaterial({ color: 0xd97706, roughness: 0.75 })), x + 0.45, 0.88, z)

    stations.push({
      id: 'snacks',
      label: 'snacks',
      usePoint: new THREE.Vector3(x + 1.25, 0, z),
      facePoint: new THREE.Vector3(x, 0.9, z),
      dwell: 4.8,
      drinks: false,
      eats: true,
    })
  }

  // ── Lounge corner (front-left) ──
  {
    const x = -wall + 0.9
    const z = wall - 1.1

    const sofa = new THREE.Group()
    sofa.position.set(x, 0, z)
    sofa.rotation.y = Math.PI * 0.75
    group.add(sofa)
    add(track(new THREE.BoxGeometry(1.7, 0.34, 0.75)), fabricMat, 0, 0.32, 0, sofa)
    add(track(new THREE.BoxGeometry(1.7, 0.5, 0.18)), fabricMat, 0, 0.65, -0.29, sofa)
    add(track(new THREE.BoxGeometry(0.18, 0.4, 0.75)), fabricMat, -0.76, 0.55, 0, sofa)
    add(track(new THREE.BoxGeometry(0.18, 0.4, 0.75)), fabricMat, 0.76, 0.55, 0, sofa)

    const rug = add(
      track(new THREE.CircleGeometry(1.15, 28)),
      track(new THREE.MeshStandardMaterial({ color: 0x24304a, roughness: 1 })),
      x + 0.5, 0.012, z - 0.5,
    )
    rug.rotation.x = -Math.PI / 2
    rug.castShadow = false

    // Two plants to break up the corner.
    for (const [px, pz] of [[x - 1.0, z + 0.2], [x + 1.5, z - 1.3]]) {
      add(track(new THREE.CylinderGeometry(0.16, 0.13, 0.3, 10)),
        track(new THREE.MeshStandardMaterial({ color: 0x7c4a2d, roughness: 0.9 })), px, 0.15, pz)
      const bush = add(track(new THREE.IcosahedronGeometry(0.3, 1)), leafMat, px, 0.52, pz)
      bush.scale.set(1, 1.35, 1)
      add(track(new THREE.IcosahedronGeometry(0.19, 1)), leafMat, px + 0.14, 0.78, pz - 0.08)
    }

    stations.push({
      id: 'lounge',
      label: 'the sofa',
      // On the seat of the sofa, facing the room.
      usePoint: new THREE.Vector3(x + 0.02, 0, z + 0.02),
      facePoint: new THREE.Vector3(0, 1, 0),
      dwell: 7.5,
      drinks: false,
      seated: true,
      sitHeight: 0.52,
    })
  }

  // ── Games corner: TV on a stand, console, controllers, beanbags (front-right)
  {
    const x = wall - 0.9
    const z = wall - 2.2

    const nook = new THREE.Group()
    nook.position.set(x, 0, z)
    // Angled back toward the middle of the room so the screen faces the seats.
    nook.rotation.y = -Math.PI * 0.75
    group.add(nook)

    // Media unit.
    add(track(new THREE.BoxGeometry(1.8, 0.45, 0.5)), darkMat, 0, 0.22, 0, nook)

    // The TV itself: bezel, then a screen that the room animates.
    add(track(new THREE.BoxGeometry(2.05, 1.2, 0.07)), track(
      new THREE.MeshStandardMaterial({ color: 0x05080e, roughness: 0.35, metalness: 0.7 }),
    ), 0, 1.14, -0.04, nook)
    const tvScreen = add(
      track(new THREE.PlaneGeometry(1.92, 1.07)),
      track(new THREE.MeshBasicMaterial({ map: tvTexture, toneMapped: false, fog: false })),
      0, 1.14, 0.01, nook,
    )
    tvScreen.castShadow = false
    tvScreen.name = 'tv-screen'
    // Paint one frame up front so the set is never a black rectangle before the
    // render loop's first tick reaches it.
    paintGameFrame(tvTexture, 0)

    // Console + two controllers on the shelf.
    add(track(new THREE.BoxGeometry(0.34, 0.07, 0.26)), track(
      new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.4 }),
    ), 0.55, 0.48, 0.02, nook)
    add(track(new THREE.BoxGeometry(0.3, 0.05, 0.06)), track(
      new THREE.MeshStandardMaterial({ color: 0x1d4ed8, emissive: 0x1e3a8a, roughness: 0.4 }),
    ), 0.55, 0.53, 0.02, nook)
    const padGeo = track(new THREE.BoxGeometry(0.17, 0.05, 0.11))
    const padMat = track(new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.55 }))
    add(padGeo, padMat, -0.4, 0.48, 0.06, nook)
    add(padGeo, padMat, -0.16, 0.48, 0.02, nook)

    // A little tray of collectible gems by the console — a full set of ten so
    // the games corner reads as a proper game, not a bare shelf. Octahedrons in
    // gem colours, each catching the TV glow.
    const gemGeo = track(new THREE.OctahedronGeometry(0.05, 0))
    const gemColors = [
      0xef4444, 0xf59e0b, 0xfacc15, 0x22c55e, 0x14b8a6,
      0x38bdf8, 0x6366f1, 0xa855f7, 0xec4899, 0xf8fafc,
    ]
    gemColors.forEach((c, i) => {
      const gemMat = track(new THREE.MeshStandardMaterial({
        color: c, emissive: c, emissiveIntensity: 0.45, roughness: 0.15, metalness: 0.3,
      }))
      // Two rows of five in front of the console.
      const col = i % 5
      const rowZ = i < 5 ? 0.2 : 0.32
      const gem = add(gemGeo, gemMat, -0.28 + col * 0.14, 0.5, rowZ, nook)
      gem.rotation.y = i * 0.6
      gem.name = 'game-gem'
      gem.castShadow = false
    })

    // Two round beanbag couches to flop into. Their world positions are kept so
    // an agent sits exactly ON a beanbag (one per agent, never sharing one).
    const bagGeo = track(new THREE.SphereGeometry(0.36, 12, 10))
    const bagMat = track(new THREE.MeshStandardMaterial({ color: 0x475569, roughness: 1 }))
    const bagLocalZ = 1.6
    const bagXs = [-0.6, 0.5]
    for (const bx of bagXs) {
      const bag = add(bagGeo, bagMat, bx, 0.26, bagLocalZ, nook)
      bag.scale.set(1, 0.72, 1)
    }

    // Nook local→world (Y-rotation θ + translation), so the beanbag seats and
    // the "look at the TV" facing are placed correctly for the angled corner.
    const th = -Math.PI * 0.75
    const cosT = Math.cos(th)
    const sinT = Math.sin(th)
    const toWorld = (lx: number, lz: number, ly = 0) =>
      new THREE.Vector3(x + lx * cosT + lz * sinT, ly, z - lx * sinT + lz * cosT)
    const bagSeats = bagXs.map((bx) => toWorld(bx, bagLocalZ))
    const tvFace = toWorld(0, 0.01, 1.14) // the screen itself — both seats look here

    // Screen glow, so the corner reads as "on" from across the room.
    const tvGlow = new THREE.PointLight(0x4f7cff, 0.75, 6, 2)
    tvGlow.position.set(0, 1.2, 0.7)
    nook.add(tvGlow)

    stations.push({
      id: 'games',
      label: 'the PlayStation',
      // Primary spot = first beanbag; the second is offered as a seat slot so a
      // second agent who walks over sits on the OTHER couch, never on top.
      usePoint: bagSeats[0].clone(),
      facePoint: tvFace.clone(),
      dwell: 24,
      drinks: false,
      // Drop onto a round beanbag couch to play and watch the games TV.
      seated: true,
      sitHeight: 0.46,
      seatSlots: bagSeats,
      seatFacings: bagSeats.map(() => tvFace.clone()),
    })
  }

  // Chart station — agents walk to the back-wall OHLC screen to study it
  stations.push({
    id: 'chart',
    label: 'the chart screen',
    usePoint: new THREE.Vector3(0, 0, -(radius - 4.5)),
    facePoint: new THREE.Vector3(0, 2.5, -(radius - 0.4)),
    dwell: 14,
    drinks: false,
  })

  o.scene.add(group)
  return { group, stations, tvTexture }
}

// ── The big board ───────────────────────────────────────────────────────────

export interface BoardQuote {
  symbol: string
  price: number | null
  prev: number | null
}

export interface PriceBoardHandle {
  station: Station
  /** Repaints only when a displayed value actually changed. */
  update(quotes: BoardQuote[], focused: string | null): void
}

/** Enough precision to be useful without turning gold into a phone number. */
function formatPrice(value: number): string {
  const abs = Math.abs(value)
  const dp = abs >= 1000 ? 2 : abs >= 10 ? 3 : abs >= 1 ? 4 : 6
  return value.toFixed(dp)
}

/**
 * A trading-floor price board — a big lit screen an agent can walk over to.
 *
 * Repainted only when a price changes, so an idle board costs nothing beyond
 * the draw call the scene was already making.
 */
export function buildPriceBoard(
  scene: THREE.Scene, track: Track, radius: number, shadows: boolean,
): PriceBoardHandle {
  const canvas = document.createElement('canvas')
  canvas.width = 1024
  canvas.height = 512
  const ctx = canvas.getContext('2d')!
  const texture = track(new THREE.CanvasTexture(canvas))
  texture.colorSpace = THREE.SRGBColorSpace

  const x = -Math.min(radius - 0.4, 8.4)
  const holder = new THREE.Group()
  holder.position.set(x, 2.5, 2.6)
  holder.rotation.y = Math.PI / 2
  scene.add(holder)

  const bezel = new THREE.Mesh(
    track(new THREE.BoxGeometry(4.5, 2.35, 0.12)),
    track(new THREE.MeshStandardMaterial({ color: 0x05080f, roughness: 0.4, metalness: 0.75 })),
  )
  bezel.position.z = -0.05
  bezel.castShadow = shadows
  holder.add(bezel)

  holder.add(new THREE.Mesh(
    track(new THREE.PlaneGeometry(4.3, 2.15)),
    track(new THREE.MeshBasicMaterial({ map: texture, toneMapped: false, fog: false })),
  ))

  // The board lights its own patch of floor, like a real one would.
  const wash = new THREE.PointLight(0x2f6fbf, 0.9, 8, 2)
  wash.position.set(1.2, 0, 0)
  holder.add(wash)

  let lastKey = ''

  function paint(quotes: BoardQuote[], focused: string | null) {
    ctx.fillStyle = '#03060d'
    ctx.fillRect(0, 0, canvas.width, canvas.height)

    ctx.fillStyle = '#0b1220'
    ctx.fillRect(0, 0, canvas.width, 72)
    ctx.fillStyle = '#38bdf8'
    ctx.font = 'bold 38px system-ui, sans-serif'
    ctx.textAlign = 'left'
    ctx.fillText('LIVE PRICES', 34, 50)
    ctx.fillStyle = '#64748b'
    ctx.font = '24px system-ui, sans-serif'
    ctx.textAlign = 'right'
    ctx.fillText('TRADING FLOOR', canvas.width - 34, 48)

    if (!quotes.length) {
      ctx.textAlign = 'left'
      ctx.fillStyle = '#475569'
      ctx.font = '34px system-ui, sans-serif'
      ctx.fillText('No pair in focus', 34, 180)
      texture.needsUpdate = true
      return
    }

    let y = 138
    for (const q of quotes.slice(0, 6)) {
      const isFocus = focused != null && q.symbol === focused
      if (isFocus) {
        ctx.fillStyle = 'rgba(56, 189, 248, 0.10)'
        ctx.fillRect(20, y - 44, canvas.width - 40, 62)
      }

      ctx.textAlign = 'left'
      ctx.fillStyle = isFocus ? '#e2e8f0' : '#94a3b8'
      ctx.font = `bold ${isFocus ? 44 : 38}px system-ui, sans-serif`
      ctx.fillText(q.symbol, 34, y)

      const up = q.prev != null && q.price != null && q.price > q.prev
      const down = q.prev != null && q.price != null && q.price < q.prev
      ctx.textAlign = 'right'
      ctx.fillStyle = up ? '#22c55e' : down ? '#ef4444' : '#cbd5e1'
      ctx.font = `bold ${isFocus ? 46 : 40}px ui-monospace, monospace`
      ctx.fillText(q.price != null ? formatPrice(q.price) : '—', canvas.width - 110, y)

      // Direction arrow — the thing a trader reads first.
      if (up || down) {
        ctx.beginPath()
        const ay = y - 14
        if (up) {
          ctx.moveTo(canvas.width - 70, ay - 16)
          ctx.lineTo(canvas.width - 46, ay + 10)
          ctx.lineTo(canvas.width - 94, ay + 10)
        } else {
          ctx.moveTo(canvas.width - 70, ay + 12)
          ctx.lineTo(canvas.width - 46, ay - 14)
          ctx.lineTo(canvas.width - 94, ay - 14)
        }
        ctx.closePath()
        ctx.fill()
      }
      y += 66
    }

    texture.needsUpdate = true
  }

  paint([], null)

  return {
    station: {
      id: 'board',
      label: 'the price board',
      // Standing room in front of the screen, a step out from the wall.
      usePoint: new THREE.Vector3(x + 1.7, 0, 2.6),
      facePoint: new THREE.Vector3(x, 2, 2.6),
      dwell: 6.5,
      drinks: false,
    },
    update(quotes, focused) {
      const key = quotes.map((q) => `${q.symbol}:${q.price}:${q.prev}`).join('|') + `#${focused}`
      if (key === lastKey) return
      lastKey = key
      paint(quotes, focused)
    },
  }
}

/** A looping "gameplay" texture for the TV — abstract, cheap, and always on. */
function makeGameTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas')
  c.width = 256
  c.height = 144
  const ctx = c.getContext('2d')!
  ctx.fillStyle = '#0a1a3a'
  ctx.fillRect(0, 0, c.width, c.height)
  const tex = new THREE.CanvasTexture(c)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.userData.canvas = c
  tex.userData.ctx = ctx
  return tex
}

/**
 * Advance the TV picture. Called on a slow cadence from the render loop —
 * a racing-game horizon with a moving track, not a real game.
 */
export function paintGameFrame(texture: THREE.CanvasTexture, t: number) {
  const ctx = texture.userData.ctx as CanvasRenderingContext2D | undefined
  const c = texture.userData.canvas as HTMLCanvasElement | undefined
  if (!ctx || !c) return

  const sky = ctx.createLinearGradient(0, 0, 0, c.height * 0.55)
  sky.addColorStop(0, '#1e3a8a')
  sky.addColorStop(1, '#7c3aed')
  ctx.fillStyle = sky
  ctx.fillRect(0, 0, c.width, c.height * 0.55)

  ctx.fillStyle = '#0f172a'
  ctx.fillRect(0, c.height * 0.55, c.width, c.height * 0.45)

  // Track edges converging on the horizon, swaying so it reads as steering.
  const sway = Math.sin(t * 0.9) * 26
  ctx.strokeStyle = '#f8fafc'
  ctx.lineWidth = 3
  for (const side of [-1, 1]) {
    ctx.beginPath()
    ctx.moveTo(c.width / 2 + sway, c.height * 0.55)
    ctx.lineTo(c.width / 2 + side * c.width * 0.75, c.height)
    ctx.stroke()
  }

  texture.needsUpdate = true
}

/** The games the lounge TV rotates through. Agents sit and play these. */
export interface TvGame { name: string; kind: string }
export const TV_GAMES: TvGame[] = [
  { name: 'STRIKER 26', kind: 'soccer' },
  { name: 'APEX RACER', kind: 'racing' },
  { name: 'DUNGEON SAGA', kind: 'rpg' },
  { name: 'CITY LEGENDS', kind: 'gta' },
  { name: 'JUMP QUEST', kind: 'platformer' },
  { name: 'STAR BREACH', kind: 'space' },
  { name: 'COMBO KINGS', kind: 'fighting' },
  { name: 'ACE TENNIS', kind: 'tennis' },
  { name: 'BLOCK RUSH', kind: 'puzzle' },
  { name: 'IRON GLOVES', kind: 'boxing' },
]

/** Games that are inherently head-to-head, so a 2nd player is an opponent. */
const VERSUS_GAMES = new Set(['fighting', 'tennis', 'boxing'])

/**
 * Paint the lounge TV as an actual game being played. The room rotates
 * `gameIndex` on a slow clock, and `players` is the number of agents actually
 * seated at the games nook right now: a second player (co-op teammate or a
 * versus opponent) only appears when two agents have walked over to play.
 */
export function paintTvGame(
  texture: THREE.CanvasTexture, t: number, gameIndex: number, players: number,
) {
  const ctx = texture.userData.ctx as CanvasRenderingContext2D | undefined
  const c = texture.userData.canvas as HTMLCanvasElement | undefined
  if (!ctx || !c) return
  const W = c.width
  const H = c.height
  const game = TV_GAMES[((gameIndex % TV_GAMES.length) + TV_GAMES.length) % TV_GAMES.length]
  const versus = VERSUS_GAMES.has(game.kind)
  const twoPlayers = players >= 2
  const coop = twoPlayers && !versus

  ctx.fillStyle = '#05070f'
  ctx.fillRect(0, 0, W, H)

  const box = (x: number, y: number, w: number, h: number, col: string) => {
    ctx.fillStyle = col
    ctx.fillRect(x, y, w, h)
  }
  const dot = (x: number, y: number, r: number, col: string) => {
    ctx.fillStyle = col
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill()
  }
  // A little running/idle sprite: body + bobbing head + swinging legs.
  const sprite = (x: number, y: number, col: string, phase: number, scale = 1) => {
    const bob = Math.sin(phase) * 2 * scale
    box(x - 4 * scale, y - 18 * scale + bob, 8 * scale, 12 * scale, col)          // body
    dot(x, y - 22 * scale + bob, 4 * scale, '#f5d0a9')                             // head
    const l = Math.sin(phase) * 4 * scale
    box(x - 4 * scale, y - 6 * scale, 3 * scale, 6 * scale + l, col)              // leg L
    box(x + 1 * scale, y - 6 * scale, 3 * scale, 6 * scale - l, col)             // leg R
  }

  switch (game.kind) {
    case 'soccer': {
      box(0, 20, W, H - 20, '#166534')                                            // pitch
      for (let i = 0; i < 5; i++) box(0, 20 + i * ((H - 20) / 5), W, 1, '#14532d')
      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 1
      ctx.strokeRect(W / 2 - 1, 20, 2, H - 20)
      ctx.beginPath(); ctx.arc(W / 2, (H + 20) / 2, 16, 0, Math.PI * 2); ctx.stroke()
      box(4, H / 2 - 8, 4, 24, '#e2e8f0'); box(W - 8, H / 2 - 8, 4, 24, '#e2e8f0') // goals
      const bx = W / 2 + Math.sin(t * 1.3) * 60
      const by = (H + 20) / 2 + Math.cos(t * 1.7) * 22
      sprite(bx - 20, by, '#38bdf8', t * 9)
      if (twoPlayers) sprite(bx + 20, by + 8, coop ? '#22d3ee' : '#ef4444', t * 9 + 1)
      dot(bx, by, 4, '#f8fafc')
      break
    }
    case 'racing': {
      const skyG = ctx.createLinearGradient(0, 0, 0, H * 0.5)
      skyG.addColorStop(0, '#1e3a8a'); skyG.addColorStop(1, '#f97316')
      ctx.fillStyle = skyG; ctx.fillRect(0, 0, W, H * 0.5)
      box(0, H * 0.5, W, H * 0.5, '#1f2937')
      const sway = Math.sin(t * 0.9) * 24
      ctx.strokeStyle = '#f8fafc'; ctx.lineWidth = 3
      for (const s of [-1, 1]) {
        ctx.beginPath(); ctx.moveTo(W / 2 + sway, H * 0.5)
        ctx.lineTo(W / 2 + s * W * 0.75, H); ctx.stroke()
      }
      ctx.fillStyle = '#fbbf24'
      for (let i = 0; i < 6; i++) {
        const p = ((t * 0.8 + i / 6) % 1) ** 2
        box(W / 2 + sway * (1 - p) - (1 + p * 6), H * 0.5 + p * H * 0.45, 2 + p * 12, 2 + p * 6, '#fbbf24')
      }
      box(W / 2 - 22, H - 26, 20, 12, '#38bdf8')                                  // P1 car
      if (twoPlayers) box(W / 2 + 4, H - 22, 20, 12, coop ? '#22d3ee' : '#ef4444')
      break
    }
    case 'rpg': {
      box(0, 20, W, H - 20, '#1c1917')                                            // dungeon floor
      for (let x = 0; x < W; x += 24) for (let y = 24; y < H; y += 24) ctx.strokeStyle = '#292524', ctx.strokeRect(x, y, 24, 24)
      dot(W - 40, H / 2, 9, '#7f1d1d')                                            // enemy
      dot(W - 40, H / 2 - 2, 3, '#fca5a5')
      sprite(40, H / 2 + 6, '#a855f7', t * 6)                                     // hero
      if (twoPlayers) sprite(64, H / 2 + 10, '#22d3ee', t * 6 + 1)               // co-op mage
      box(8, 26, 60, 6, '#3f1d1d'); box(8, 26, 44, 6, '#ef4444')                 // HP bar
      box(8, 36, 60, 4, '#1e3a5f'); box(8, 36, 30, 4, '#38bdf8')                 // MP bar
      break
    }
    case 'gta': {
      box(0, 20, W, H - 20, '#0f172a')                                           // night city
      for (let i = 0; i < 6; i++) box(10 + i * 42, 24, 30, 40 + (i % 3) * 18, '#1e293b')
      for (let i = 0; i < 30; i++) if ((i * 7 + Math.floor(t)) % 3 === 0) box(14 + (i * 13) % (W - 20), 30 + (i * 9) % 60, 3, 4, '#fde68a')
      box(0, H - 34, W, 34, '#374151')                                           // road
      const cx = (t * 40) % (W + 40) - 20
      box(cx, H - 26, 22, 12, '#eab308'); dot(cx + 5, H - 26, 2, '#111'); dot(cx + 17, H - 26, 2, '#111')
      sprite(W / 2, H - 6, '#22c55e', t * 8)                                     // player on foot
      if (twoPlayers) sprite(W / 2 + 22, H - 6, coop ? '#22d3ee' : '#ef4444', t * 8 + 1)
      break
    }
    case 'platformer': {
      const sky = ctx.createLinearGradient(0, 0, 0, H)
      sky.addColorStop(0, '#0ea5e9'); sky.addColorStop(1, '#1e3a8a')
      ctx.fillStyle = sky; ctx.fillRect(0, 20, W, H - 20)
      const plats: Array<[number, number, number]> = [[10, H - 20, 60], [90, H - 44, 54], [170, H - 30, 60]]
      for (const [px, py, pw] of plats) box(px, py, pw, 8, '#16a34a')
      for (let i = 0; i < 4; i++) dot(30 + i * 46, H - 56 + Math.sin(t * 3 + i) * 3, 3, '#facc15') // coins
      const jump = Math.abs(Math.sin(t * 2.5)) * 18
      sprite(40, H - 24 - jump, '#ef4444', t * 10)
      if (twoPlayers) sprite(110, H - 48 - Math.abs(Math.sin(t * 2.5 + 1)) * 16, '#22d3ee', t * 10 + 1)
      break
    }
    case 'space': {
      for (let i = 0; i < 40; i++) box((i * 37 + Math.floor(t * 30)) % W, (i * 19) % H, 1, 1, '#cbd5e1') // stars
      for (let i = 0; i < 4; i++) { const ey = 30 + i * 24; const ex = (t * 50 + i * 60) % W; box(ex, ey, 12, 8, '#7f1d1d'); dot(ex + 6, ey + 8, 2, '#fca5a5') }
      const p1x = W / 2 - 30 + Math.sin(t * 2) * 40
      box(p1x, H - 22, 14, 10, '#38bdf8'); box(p1x + 6, H - 30, 2, 8, '#f8fafc') // bullet
      if (twoPlayers) { const p2x = W / 2 + 20 + Math.sin(t * 2 + 1) * 40; box(p2x, H - 22, 14, 10, coop ? '#22d3ee' : '#ef4444') }
      break
    }
    case 'fighting': {
      const g = ctx.createLinearGradient(0, 0, 0, H); g.addColorStop(0, '#4c1d95'); g.addColorStop(1, '#0f172a')
      ctx.fillStyle = g; ctx.fillRect(0, 20, W, H - 20)
      box(0, H - 16, W, 16, '#334155')                                           // stage
      const lunge = Math.sin(t * 4) * 10
      sprite(W / 2 - 34 + lunge, H - 16, '#38bdf8', t * 3)
      sprite(W / 2 + 34 - lunge, H - 16, '#ef4444', t * 3 + 1)
      box(8, 26, 100, 8, '#3f1d1d'); box(8, 26, 70, 8, '#22c55e')               // P1 health
      box(W - 108, 26, 100, 8, '#3f1d1d'); box(W - 108, 26, 50, 8, '#22c55e')  // P2 health
      break
    }
    case 'tennis': {
      box(0, 20, W, H - 20, '#1d4ed8')                                          // court
      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 1; ctx.strokeRect(20, 30, W - 40, H - 44)
      box(W / 2 - 1, 26, 2, H - 30, '#e2e8f0')                                  // net
      sprite(34, H / 2 + 10, '#38bdf8', t * 5)
      sprite(W - 34, H / 2 + 10, '#ef4444', t * 5 + 1)
      dot(W / 2 + Math.sin(t * 3) * (W / 2 - 40), H / 2 + Math.cos(t * 3) * 20, 3, '#facc15')
      break
    }
    case 'puzzle': {
      box(W / 2 - 44, 22, 88, H - 30, '#0f172a')                                // well
      const cols = ['#ef4444', '#22c55e', '#38bdf8', '#eab308', '#a855f7']
      for (let r = 0; r < 7; r++) for (let cN = 0; cN < 5; cN++) if ((r * 5 + cN + Math.floor(t)) % 3 === 0) box(W / 2 - 42 + cN * 17, H - 20 - r * 14, 15, 12, cols[(r + cN) % cols.length])
      const fall = ((t * 30) % (H - 60)) | 0
      box(W / 2 - 8, 24 + fall, 15, 12, cols[Math.floor(t) % cols.length])       // falling piece
      break
    }
    default: { // boxing
      const g = ctx.createLinearGradient(0, 0, 0, H); g.addColorStop(0, '#1e293b'); g.addColorStop(1, '#0f172a')
      ctx.fillStyle = g; ctx.fillRect(0, 20, W, H - 20)
      ctx.strokeStyle = '#64748b'; ctx.lineWidth = 2
      for (let r = 0; r < 3; r++) { ctx.beginPath(); ctx.moveTo(0, 30 + r * 8); ctx.lineTo(W, 30 + r * 8); ctx.stroke() } // ropes
      box(0, H - 14, W, 14, '#7f1d1d')                                          // canvas mat
      const jab = Math.max(0, Math.sin(t * 5)) * 10
      sprite(W / 2 - 26 + jab, H - 14, '#38bdf8', t * 2)
      sprite(W / 2 + 26 - jab, H - 14, '#ef4444', t * 2 + 1)
      break
    }
  }

  // HUD: game title + player mode. Drawn last so it sits over the scene.
  ctx.fillStyle = 'rgba(2,6,14,0.72)'
  ctx.fillRect(0, 0, W, 20)
  ctx.textAlign = 'left'
  ctx.fillStyle = '#38bdf8'
  ctx.font = 'bold 12px system-ui, sans-serif'
  ctx.fillText(game.name, 6, 15)
  ctx.textAlign = 'right'
  ctx.fillStyle = coop ? '#22c55e' : versus ? '#f59e0b' : '#94a3b8'
  ctx.font = 'bold 11px system-ui, sans-serif'
  ctx.fillText(coop ? 'CO-OP 2P' : versus ? 'VS 2P' : '1P', W - 6, 15)

  texture.needsUpdate = true
}

// ── News screen ─────────────────────────────────────────────────────────────

export interface NewsItem {
  title: string
  tone?: 'up' | 'down' | 'neutral'
  tag?: string
}

export interface NewsScreenHandle {
  /** Repaints only when the headlines change. */
  update(items: NewsItem[]): void
}

/**
 * A big "MARKET NEWS" board on the right wall — the recent-headlines screen.
 * Fed live from the room's own wire; repainted only when the list changes.
 */
export function buildNewsScreen(
  scene: THREE.Scene, track: Track, radius: number, shadows: boolean,
): NewsScreenHandle {
  const canvas = document.createElement('canvas')
  canvas.width = 1024
  canvas.height = 512
  const ctx = canvas.getContext('2d')!
  const texture = track(new THREE.CanvasTexture(canvas))
  texture.colorSpace = THREE.SRGBColorSpace

  const x = Math.min(radius - 0.4, 8.4)
  const holder = new THREE.Group()
  holder.position.set(x, 2.5, -2.6)
  holder.rotation.y = -Math.PI / 2
  scene.add(holder)

  const bezel = new THREE.Mesh(
    track(new THREE.BoxGeometry(4.5, 2.35, 0.12)),
    track(new THREE.MeshStandardMaterial({ color: 0x05080f, roughness: 0.4, metalness: 0.75 })),
  )
  bezel.position.z = -0.05
  bezel.castShadow = shadows
  holder.add(bezel)
  holder.add(new THREE.Mesh(
    track(new THREE.PlaneGeometry(4.3, 2.15)),
    track(new THREE.MeshBasicMaterial({ map: texture, toneMapped: false, fog: false })),
  ))
  const wash = new THREE.PointLight(0x8b5cf6, 0.8, 8, 2)
  wash.position.set(-1.2, 0, 0)
  holder.add(wash)

  let lastKey = ''

  function paint(items: NewsItem[]) {
    ctx.fillStyle = '#05060d'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = '#0b0f1e'
    ctx.fillRect(0, 0, canvas.width, 72)
    ctx.textAlign = 'left'
    ctx.fillStyle = '#a78bfa'
    ctx.font = 'bold 38px system-ui, sans-serif'
    ctx.fillText('MARKET NEWS', 34, 50)
    ctx.textAlign = 'right'
    ctx.fillStyle = '#64748b'
    ctx.font = '24px system-ui, sans-serif'
    ctx.fillText('RECENT WIRE', canvas.width - 34, 48)

    if (!items.length) {
      ctx.textAlign = 'left'
      ctx.fillStyle = '#475569'
      ctx.font = '32px system-ui, sans-serif'
      ctx.fillText('No recent headlines', 34, 170)
      texture.needsUpdate = true
      return
    }

    let y = 128
    for (const it of items.slice(0, 6)) {
      const tone = it.tone === 'up' ? '#22c55e' : it.tone === 'down' ? '#ef4444' : '#38bdf8'
      ctx.fillStyle = tone
      ctx.fillRect(34, y - 22, 8, 26)
      if (it.tag) {
        ctx.textAlign = 'left'
        ctx.fillStyle = tone
        ctx.font = 'bold 26px ui-monospace, monospace'
        ctx.fillText(it.tag, 54, y)
      }
      ctx.textAlign = 'left'
      ctx.fillStyle = '#cbd5e1'
      ctx.font = '28px system-ui, sans-serif'
      const tagW = it.tag ? ctx.measureText(it.tag).width + 24 : 0
      const maxChars = 58
      const title = it.title.length > maxChars ? it.title.slice(0, maxChars - 1) + '…' : it.title
      ctx.fillText(title, 54 + tagW, y)
      y += 60
    }
    texture.needsUpdate = true
  }

  paint([])

  return {
    update(items) {
      const key = items.map((i) => `${i.tag ?? ''}:${i.title}:${i.tone ?? ''}`).join('|')
      if (key === lastKey) return
      lastKey = key
      paint(items)
    },
  }
}

// ── Wall screens ────────────────────────────────────────────────────────────

export interface ScreenInfo {
  symbol: string | null
  action: string | null
  confidence: number | null
  /** e.g. "4 of 7 agree" */
  detail: string | null
}

export interface WallScreensHandle {
  /** Repaints only when the content differs from what is on screen. */
  update(info: ScreenInfo, quotes?: BoardQuote[]): void
}

/**
 * Two lit panels flanking the window. They show the pair the room is working on
 * plus a live price ticker of the focused pairs, so the screens are never a
 * black "off" rectangle — there is always a price on them.
 *
 * The canvas is only redrawn when the text changes — an unchanged frame costs
 * nothing beyond the draw call that was already happening.
 */
export function buildWallScreens(
  scene: THREE.Scene, track: Track, radius: number,
): WallScreensHandle {
  const canvas = document.createElement('canvas')
  canvas.width = 512
  canvas.height = 288
  const ctx = canvas.getContext('2d')!
  const texture = track(new THREE.CanvasTexture(canvas))
  texture.colorSpace = THREE.SRGBColorSpace

  const mat = track(new THREE.MeshBasicMaterial({ map: texture, toneMapped: false, fog: false }))
  const geo = track(new THREE.PlaneGeometry(2.3, 1.29))
  const frameMat = track(new THREE.MeshStandardMaterial({
    color: 0x070c15, roughness: 0.4, metalness: 0.7,
  }))
  const frameGeo = track(new THREE.BoxGeometry(2.44, 1.43, 0.07))

  // Flanking the window, angled inward. Kept within reading distance of the
  // table rather than pinned to a wall that may be much further back.
  const z = -Math.min(radius - 0.5, 8.6)
  for (const side of [-1, 1] as const) {
    const holder = new THREE.Group()
    holder.position.set(side * 4.1, 2.35, z)
    holder.rotation.y = side * -0.34
    const frame = new THREE.Mesh(frameGeo, frameMat)
    frame.position.z = -0.04
    holder.add(frame)
    holder.add(new THREE.Mesh(geo, mat))
    scene.add(holder)
  }

  let lastKey = ''

  function paint(info: ScreenInfo, quotes: BoardQuote[]) {
    ctx.fillStyle = '#060b14'
    ctx.fillRect(0, 0, canvas.width, canvas.height)

    ctx.strokeStyle = 'rgba(56, 189, 248, 0.5)'
    ctx.lineWidth = 3
    ctx.strokeRect(10, 10, canvas.width - 20, canvas.height - 20)

    ctx.textAlign = 'left'
    ctx.fillStyle = '#38bdf8'
    ctx.font = 'bold 22px system-ui, sans-serif'
    ctx.fillText('TRADING FLOOR', 32, 46)

    // Verdict line — the pair under discussion and its lean, when there is one.
    const action = (info.action ?? '').toLowerCase()
    const tone = action === 'buy' ? '#22c55e' : action === 'sell' ? '#ef4444' : '#eab308'
    if (info.symbol) {
      ctx.fillStyle = '#f1f5f9'
      ctx.font = 'bold 34px system-ui, sans-serif'
      ctx.fillText(info.symbol, 32, 84)
      if (info.action) {
        ctx.fillStyle = tone
        ctx.font = 'bold 26px system-ui, sans-serif'
        ctx.textAlign = 'right'
        ctx.fillText(info.action.toUpperCase(), canvas.width - 32, 84)
        ctx.textAlign = 'left'
      }
      if (info.confidence != null) {
        ctx.fillStyle = '#1e293b'
        ctx.fillRect(32, 96, canvas.width - 64, 8)
        ctx.fillStyle = tone
        ctx.fillRect(32, 96, (canvas.width - 64) * Math.max(0, Math.min(1, info.confidence)), 8)
      }
    } else {
      ctx.fillStyle = '#64748b'
      ctx.font = '22px system-ui, sans-serif'
      ctx.textAlign = 'right'
      ctx.fillText(info.detail ?? 'live market', canvas.width - 32, 46)
      ctx.textAlign = 'left'
    }

    // Live price ticker — always present so the panel is never blank/black.
    const rows = (quotes ?? []).slice(0, 4)
    ctx.fillStyle = '#38bdf8'
    ctx.font = 'bold 18px system-ui, sans-serif'
    ctx.fillText('LIVE PRICES', 32, 138)

    if (!rows.length) {
      ctx.fillStyle = '#475569'
      ctx.font = '20px system-ui, sans-serif'
      ctx.fillText(info.symbol ? 'awaiting quotes…' : 'Standing by', 32, 176)
      texture.needsUpdate = true
      return
    }

    let y = 172
    for (const q of rows) {
      const focus = info.symbol != null && q.symbol === info.symbol
      if (focus) {
        ctx.fillStyle = 'rgba(56, 189, 248, 0.12)'
        ctx.fillRect(24, y - 22, canvas.width - 48, 30)
      }
      ctx.textAlign = 'left'
      ctx.fillStyle = focus ? '#e2e8f0' : '#94a3b8'
      ctx.font = `bold ${focus ? 24 : 21}px system-ui, sans-serif`
      ctx.fillText(q.symbol, 32, y)

      const up = q.prev != null && q.price != null && q.price > q.prev
      const down = q.prev != null && q.price != null && q.price < q.prev
      ctx.textAlign = 'right'
      ctx.fillStyle = up ? '#22c55e' : down ? '#ef4444' : '#cbd5e1'
      ctx.font = `bold ${focus ? 24 : 21}px ui-monospace, monospace`
      ctx.fillText(q.price != null ? formatPrice(q.price) : '—', canvas.width - 44, y)
      if (up || down) {
        ctx.beginPath()
        const ay = y - 8
        if (up) { ctx.moveTo(canvas.width - 30, ay - 9); ctx.lineTo(canvas.width - 20, ay + 5); ctx.lineTo(canvas.width - 40, ay + 5) }
        else { ctx.moveTo(canvas.width - 30, ay + 6); ctx.lineTo(canvas.width - 20, ay - 8); ctx.lineTo(canvas.width - 40, ay - 8) }
        ctx.closePath()
        ctx.fill()
      }
      y += 34
    }

    texture.needsUpdate = true
  }

  paint({ symbol: null, action: null, confidence: null, detail: null }, [])

  return {
    update(info, quotes = []) {
      const priceKey = quotes.map((q) => `${q.symbol}:${q.price}:${q.prev}`).join('|')
      const key = `${info.symbol}|${info.action}|${info.confidence?.toFixed(2)}|${info.detail}#${priceKey}`
      if (key === lastKey) return
      lastKey = key
      paint(info, quotes)
    },
  }
}
