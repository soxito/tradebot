/**
 * The props agents walk to, and the screens they read.
 *
 * The contract that matters is small but easy to break: every station must be
 * somewhere an agent can actually stand — off the table, inside the walls — and
 * the boards must survive being handed junk data, because they are fed live
 * quotes that routinely arrive as null.
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

// The global setup stubs `three` for the JARVIS avatar tests; this suite needs
// real geometry maths.
vi.unmock('three')

import * as THREE from 'three'

import {
  ROOM_HEIGHT,
  buildFurniture,
  buildPriceBoard,
  buildWallScreens,
  paintGameFrame,
  type Station,
} from '../roomFurniture'

const ROOM_RADIUS = 13.5
const TABLE_RADIUS = 3.1

/** Text and fills drawn since the last `drawn.clear()`, across all canvases. */
const drawn = {
  text: [] as string[],
  fills: 0,
  /** Only `paintGameFrame` strokes, so this distinguishes a live TV picture
   *  from the flat fill the texture starts life with. */
  strokes: 0,
  clear() { this.text = []; this.fills = 0; this.strokes = 0 },
}

/**
 * jsdom has no 2D canvas, so this stub stands in — but it records rather than
 * discarding, which is what lets a test assert that a price actually reached
 * the screen instead of merely that painting did not throw.
 */
beforeAll(() => {
  const ctx = new Proxy({} as CanvasRenderingContext2D, {
    get: (_t, prop) => {
      if (prop === 'canvas') return { width: 1024, height: 512 }
      if (prop === 'createLinearGradient') return () => ({ addColorStop: () => {} })
      if (prop === 'measureText') return () => ({ width: 0 })
      if (prop === 'fillText' || prop === 'strokeText') {
        return (s: string) => { drawn.text.push(String(s)) }
      }
      if (prop === 'fillRect') return () => { drawn.fills++ }
      if (prop === 'stroke') return () => { drawn.strokes++ }
      return () => {}
    },
    set: () => true,
  })
  HTMLCanvasElement.prototype.getContext = (() => ctx) as unknown as HTMLCanvasElement['getContext']
})

beforeEach(() => drawn.clear())

const noopTrack = <T extends { dispose: () => void }>(o: T): T => o

function makeRoom() {
  const scene = new THREE.Scene()
  const furniture = buildFurniture({
    scene, track: noopTrack, shadows: false, radius: ROOM_RADIUS,
  })
  const board = buildPriceBoard(scene, noopTrack, ROOM_RADIUS, false)
  return { scene, stations: [...furniture.stations, board.station], board }
}

describe('stations', () => {
  it('offers every amenity the room advertises', () => {
    const { stations } = makeRoom()
    const ids = stations.map((s) => s.id).sort()
    expect(ids).toEqual(['board', 'chart', 'coffee', 'games', 'lounge', 'snacks', 'water'])
  })

  it('places every station somewhere an agent can stand', () => {
    const { stations } = makeRoom()
    for (const s of stations) {
      const r = Math.hypot(s.usePoint.x, s.usePoint.z)
      // Off the table — standing inside it would look broken and the walk
      // router would fight itself trying to get there.
      expect(r, `${s.id} overlaps the table`).toBeGreaterThan(TABLE_RADIUS + 0.5)
      // Inside the walls.
      expect(r, `${s.id} is outside the room`).toBeLessThan(ROOM_RADIUS - 0.5)
      // On the floor, not floating.
      expect(s.usePoint.y).toBe(0)
    }
  })

  it('never asks an agent to face the spot it is standing on', () => {
    const { stations } = makeRoom()
    for (const s of stations) {
      const flat = new THREE.Vector3(s.facePoint.x, 0, s.facePoint.z)
      expect(flat.distanceTo(s.usePoint), `${s.id} has no facing`).toBeGreaterThan(0.3)
    }
  })

  it('gives every station a visit long enough to read as deliberate', () => {
    const { stations } = makeRoom()
    for (const s of stations) expect(s.dwell).toBeGreaterThanOrEqual(3)
  })

  it('only raises a cup at the stations that serve a drink', () => {
    const { stations } = makeRoom()
    const drinking = stations.filter((s: Station) => s.drinks).map((s) => s.id).sort()
    expect(drinking).toEqual(['coffee', 'water'])
  })

  it('keeps the stations apart so two agents never stand in each other', () => {
    const { stations } = makeRoom()
    for (let i = 0; i < stations.length; i++) {
      for (let j = i + 1; j < stations.length; j++) {
        expect(
          stations[i].usePoint.distanceTo(stations[j].usePoint),
          `${stations[i].id} and ${stations[j].id} overlap`,
        ).toBeGreaterThan(1)
      }
    }
  })
})

describe('price board', () => {
  it('survives an empty board and a board of dead quotes', () => {
    const { board } = makeRoom()
    expect(() => board.update([], null)).not.toThrow()
    expect(() => board.update(
      [{ symbol: 'XAUUSD', price: null, prev: null }], 'XAUUSD',
    )).not.toThrow()
  })

  it('renders rising, falling and unchanged prices without complaint', () => {
    const { board } = makeRoom()
    expect(() => board.update([
      { symbol: 'XAUUSD', price: 4350.7, prev: 4349.1 },   // up
      { symbol: 'GBPUSD', price: 1.2711, prev: 1.2740 },   // down
      { symbol: 'BTCUSD', price: 64000, prev: 64000 },     // flat
    ], 'XAUUSD')).not.toThrow()
  })

  it('copes with more pairs than the board has rows', () => {
    const { board } = makeRoom()
    const many = Array.from({ length: 25 }, (_, i) => ({
      symbol: `PAIR${i}`, price: i, prev: i - 1,
    }))
    expect(() => board.update(many, 'PAIR0')).not.toThrow()
  })

  it('actually prints the live price on the screen', () => {
    // The reported bug was a board with no prices on it, so asserting that
    // painting "did not throw" is not enough — the number has to be drawn.
    const { board } = makeRoom()
    drawn.clear()
    board.update([{ symbol: 'XAUUSD', price: 4350.735, prev: 4349.1 }], 'XAUUSD')

    expect(drawn.text).toContain('XAUUSD')
    expect(drawn.text.some((s) => s.startsWith('4350.7'))).toBe(true)
  })

  it('prints every pair on the board, not just the focused one', () => {
    const { board } = makeRoom()
    drawn.clear()
    board.update([
      { symbol: 'XAUUSD', price: 4350.7, prev: 4349.1 },
      { symbol: 'GBPUSD', price: 1.2711, prev: 1.274 },
    ], 'XAUUSD')

    expect(drawn.text).toContain('XAUUSD')
    expect(drawn.text).toContain('GBPUSD')
    expect(drawn.text.some((s) => s.startsWith('1.2711'))).toBe(true)
  })

  it('scales the decimals to the instrument', () => {
    const { board } = makeRoom()
    drawn.clear()
    board.update([
      { symbol: 'BTCUSD', price: 64213.5, prev: 64000 },   // big number, 2dp
      { symbol: 'EURUSD', price: 1.08642, prev: 1.086 },   // FX, more places
    ], null)

    expect(drawn.text).toContain('64213.50')
    expect(drawn.text.some((s) => s.startsWith('1.0864'))).toBe(true)
  })

  it('shows a placeholder rather than a blank row when a quote is missing', () => {
    const { board } = makeRoom()
    drawn.clear()
    board.update([{ symbol: 'XAUUSD', price: null, prev: null }], 'XAUUSD')

    expect(drawn.text).toContain('XAUUSD')
    expect(drawn.text).toContain('—')
  })

  it('repaints when a price moves and skips the work when it has not', () => {
    const { board } = makeRoom()
    board.update([{ symbol: 'XAUUSD', price: 4350.7, prev: 4349.1 }], 'XAUUSD')

    drawn.clear()
    board.update([{ symbol: 'XAUUSD', price: 4350.7, prev: 4349.1 }], 'XAUUSD')
    expect(drawn.text, 'an unchanged board should not repaint').toHaveLength(0)

    drawn.clear()
    board.update([{ symbol: 'XAUUSD', price: 4352.2, prev: 4350.7 }], 'XAUUSD')
    expect(drawn.text.some((s) => s.startsWith('4352.2'))).toBe(true)
  })

  it('mounts the board inside the room, at a readable height', () => {
    const { board } = makeRoom()
    const r = Math.hypot(board.station.facePoint.x, board.station.facePoint.z)
    expect(r).toBeLessThan(ROOM_RADIUS)
    expect(board.station.facePoint.y).toBeGreaterThan(1)
    expect(board.station.facePoint.y).toBeLessThan(ROOM_HEIGHT)
  })
})

describe('screens', () => {
  it('shows a standing-by state before any pair is chosen', () => {
    const scene = new THREE.Scene()
    const screens = buildWallScreens(scene, noopTrack, ROOM_RADIUS)
    expect(() => screens.update({
      symbol: null, action: null, confidence: null, detail: null,
    })).not.toThrow()
  })

  it('renders a full verdict without complaint', () => {
    const scene = new THREE.Scene()
    const screens = buildWallScreens(scene, noopTrack, ROOM_RADIUS)
    expect(() => screens.update({
      symbol: 'XAUUSD', action: 'buy', confidence: 0.82, detail: '5 of 7 agree',
    })).not.toThrow()
  })

  it('clamps a confidence that arrives out of range', () => {
    const scene = new THREE.Scene()
    const screens = buildWallScreens(scene, noopTrack, ROOM_RADIUS)
    expect(() => screens.update({
      symbol: 'XAUUSD', action: 'sell', confidence: 4.5, detail: null,
    })).not.toThrow()
  })

  it('hands back the TV picture instead of relying on a name lookup', () => {
    // Fetching the screen by name broke silently when the mesh moved; the
    // texture is now returned directly so a rename cannot black out the set.
    const scene = new THREE.Scene()
    const furniture = buildFurniture({
      scene, track: noopTrack, shadows: false, radius: ROOM_RADIUS,
    })
    expect(furniture.tvTexture).toBeInstanceOf(THREE.CanvasTexture)

    const tv = scene.getObjectByName('tv-screen') as THREE.Mesh
    expect(tv, 'the games corner has no TV').toBeDefined()
    // The mesh must be showing the very texture the room animates.
    expect((tv.material as THREE.MeshBasicMaterial).map).toBe(furniture.tvTexture)
  })

  it('switches the TV on before the first frame of the render loop', () => {
    // The set read as black because the texture only ever got its flat opening
    // fill — the picture was never painted. Strokes are the tell: only a real
    // frame draws the track, so a blank fill alone would not count.
    drawn.clear()
    buildFurniture({
      scene: new THREE.Scene(), track: noopTrack, shadows: false, radius: ROOM_RADIUS,
    })
    expect(drawn.strokes, 'the TV showed no picture at build time').toBeGreaterThan(0)
  })

  it('keeps the picture moving across the whole loop', () => {
    const furniture = buildFurniture({
      scene: new THREE.Scene(), track: noopTrack, shadows: false, radius: ROOM_RADIUS,
    })

    const frameAt = (t: number) => {
      drawn.clear()
      paintGameFrame(furniture.tvTexture, t)
      return drawn.strokes
    }
    for (const t of [0, 1.5, 7, 60]) expect(frameAt(t)).toBeGreaterThan(0)

    // `needsUpdate` is write-only on a Texture; bumping it raises `version`,
    // which is what actually tells the renderer to re-upload the canvas.
    const before = furniture.tvTexture.version
    paintGameFrame(furniture.tvTexture, 61)
    expect(furniture.tvTexture.version).toBeGreaterThan(before)
  })
})
