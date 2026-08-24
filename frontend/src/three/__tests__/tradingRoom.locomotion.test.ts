/**
 * The geometry and posing behind agents leaving their chairs.
 *
 * None of this needs WebGL — it is all Object3D maths — so it is the part of
 * the room that can actually be pinned down by tests rather than by looking at
 * it. A regression here shows up as an agent moonwalking through the table.
 */
import { describe, expect, it, vi } from 'vitest'

// The global setup stubs `three` for the JARVIS avatar tests. This suite is
// checking real Object3D maths, so it needs the genuine library.
vi.unmock('three')

import * as THREE from 'three'

import {
  ROOM_CAGE,
  angleDelta,
  constrainCameraToRoom,
  maxOrbitDistance,
  placeLaptopForSeat,
  routeAround,
} from '../tradingRoom'
import {
  animateFace,
  buildAvatar,
  createAvatarKit,
  poseDrinking,
  poseSeated,
  poseStanding,
  poseWalking,
  type AvatarRig,
} from '../agentAvatar'

const TABLE_RADIUS = 3.1

function makeRig(gender: 'male' | 'female' = 'male'): AvatarRig {
  const kit = createAvatarKit((o) => o)
  return buildAvatar({
    kit,
    color: new THREE.Color('#3b82f6'),
    gender,
    name: 'Sakhile',
    castShadow: false,
    track: (o) => o,
  })
}

/** Run a pose to convergence the way the render loop would. */
function settle(fn: (dt: number) => void, seconds = 2, step = 1 / 60) {
  for (let t = 0; t < seconds; t += step) fn(step)
}

describe('angleDelta', () => {
  it('takes the short way round instead of the long way', () => {
    // 350° → 10° is a 20° turn forward, not 340° backward.
    expect(angleDelta(THREE.MathUtils.degToRad(350), THREE.MathUtils.degToRad(10)))
      .toBeCloseTo(THREE.MathUtils.degToRad(20), 5)
  })

  it('never returns a turn bigger than half a circle', () => {
    for (let a = -720; a <= 720; a += 17) {
      for (let b = -720; b <= 720; b += 23) {
        const d = angleDelta(THREE.MathUtils.degToRad(a), THREE.MathUtils.degToRad(b))
        expect(Math.abs(d)).toBeLessThanOrEqual(Math.PI + 1e-9)
      }
    }
  })
})

describe('routeAround', () => {
  const seat = (angle: number, r = 4.05) =>
    new THREE.Vector3(Math.cos(angle) * r, 0, Math.sin(angle) * r)

  it('walks straight when the path already misses the table', () => {
    // Two neighbouring points on the same side — no detour needed.
    const path = routeAround(seat(0), seat(0.3))
    expect(path).toHaveLength(1)
  })

  it('detours around the table instead of straight through it', () => {
    // Directly opposite seats: a straight line would cross the centre.
    const path = routeAround(seat(0), seat(Math.PI))
    expect(path.length).toBeGreaterThan(1)

    for (const p of path.slice(0, -1)) {
      expect(Math.hypot(p.x, p.z)).toBeGreaterThan(TABLE_RADIUS)
    }
  })

  it('keeps every leg of a cross-table walk clear of the table top', () => {
    const from = seat(0)
    const path = routeAround(from, seat(Math.PI))

    // Sample along each leg — the corners clearing the table is not enough,
    // the straight lines between them must clear it too.
    let cursor = from
    for (const next of path) {
      for (let s = 0; s <= 1; s += 0.05) {
        const x = cursor.x + (next.x - cursor.x) * s
        const z = cursor.z + (next.z - cursor.z) * s
        expect(Math.hypot(x, z)).toBeGreaterThan(TABLE_RADIUS - 0.35)
      }
      cursor = next
    }
  })

  it('ends exactly on the requested destination', () => {
    const target = new THREE.Vector3(7.05, 0, -1.6)
    const path = routeAround(seat(Math.PI * 0.9), target)
    const last = path[path.length - 1]
    expect(last.x).toBeCloseTo(target.x, 6)
    expect(last.z).toBeCloseTo(target.z, 6)
  })
})

describe('laptops', () => {
  const SEAT_RADIUS = 4.05
  const TABLE_RADIUS = 3.1

  /** Places a laptop with the scene's own function — not a copy of it. */
  function placeLaptop(angle: number) {
    const seat = new THREE.Vector3(Math.cos(angle) * SEAT_RADIUS, 0, Math.sin(angle) * SEAT_RADIUS)
    const group = placeLaptopForSeat(new THREE.Group(), seat)
    return { seat, group }
  }

  it('points every screen at the agent sitting behind it', () => {
    // The lid faces the group's local +Z. It was rotated by an extra half turn,
    // so all seven agents were shown the back of their own laptop.
    for (let i = 0; i < 7; i++) {
      const angle = (i / 7) * Math.PI * 2 + Math.PI / 2
      const { seat, group } = placeLaptop(angle)

      const screenDir = new THREE.Vector3(0, 0, 1)
        .applyQuaternion(group.quaternion).setY(0).normalize()
      const toAgent = seat.clone().sub(group.position).setY(0).normalize()

      // Dot ≈ 1 means the screen looks straight at its owner.
      expect(screenDir.dot(toAgent), `seat ${i} sees the back of the lid`).toBeGreaterThan(0.99)
    }
  })

  it('stands each laptop on the table, not through it or off the edge', () => {
    for (let i = 0; i < 7; i++) {
      const { group } = placeLaptop((i / 7) * Math.PI * 2 + Math.PI / 2)
      const r = Math.hypot(group.position.x, group.position.z)
      expect(r).toBeLessThan(TABLE_RADIUS)
      expect(r).toBeGreaterThan(TABLE_RADIUS - 1.2)
      // Table top sits at 1.05; the laptop rests just above it.
      expect(group.position.y).toBeGreaterThan(1.05)
    }
  })

  it('sits each laptop between the table centre and its owner', () => {
    for (let i = 0; i < 7; i++) {
      const angle = (i / 7) * Math.PI * 2 + Math.PI / 2
      const { seat, group } = placeLaptop(angle)
      // Same bearing from the centre as the seat it belongs to.
      const seatBearing = Math.atan2(seat.x, seat.z)
      const deskBearing = Math.atan2(group.position.x, group.position.z)
      expect(Math.abs(angleDelta(seatBearing, deskBearing))).toBeLessThan(0.01)
    }
  })
})

describe('camera containment', () => {
  const inside = (p: THREE.Vector3) =>
    Math.hypot(p.x, p.z) <= ROOM_CAGE.radius + 1e-6
    && p.y >= ROOM_CAGE.floor - 1e-6
    && p.y <= ROOM_CAGE.ceiling + 1e-6

  it('leaves a camera that is already inside exactly where it is', () => {
    const p = new THREE.Vector3(3, 2.5, 4)
    const before = p.clone()
    constrainCameraToRoom(p)
    expect(p.x).toBeCloseTo(before.x, 9)
    expect(p.y).toBeCloseTo(before.y, 9)
    expect(p.z).toBeCloseTo(before.z, 9)
  })

  it('pulls a camera back through the wall it escaped', () => {
    const p = new THREE.Vector3(40, 2, 0)
    constrainCameraToRoom(p)
    expect(inside(p)).toBe(true)
  })

  it('keeps the bearing when pulling the camera back in', () => {
    // The user chose that direction — only the distance is negotiable.
    const p = new THREE.Vector3(30, 2, 30)
    const bearing = Math.atan2(p.z, p.x)
    constrainCameraToRoom(p)
    expect(Math.atan2(p.z, p.x)).toBeCloseTo(bearing, 6)
  })

  it('stops the camera rising through the ceiling', () => {
    const p = new THREE.Vector3(0, 99, 5)
    constrainCameraToRoom(p)
    expect(p.y).toBe(ROOM_CAGE.ceiling)
  })

  it('stops the camera sinking below the floor', () => {
    const p = new THREE.Vector3(0, -12, 5)
    constrainCameraToRoom(p)
    expect(p.y).toBe(ROOM_CAGE.floor)
  })

  it('contains the camera from every angle and every distance', () => {
    for (let az = 0; az < Math.PI * 2; az += 0.2) {
      for (const dist of [0.5, 5, 12, 30, 200]) {
        for (const y of [-5, 0.2, 3, 20]) {
          const p = new THREE.Vector3(Math.cos(az) * dist, y, Math.sin(az) * dist)
          constrainCameraToRoom(p)
          expect(inside(p)).toBe(true)
        }
      }
    }
  })
})

describe('orbit budget', () => {
  it('allows the widest orbit when looking at the middle of the room', () => {
    expect(maxOrbitDistance(new THREE.Vector3(0, 1.55, 0)))
      .toBeCloseTo(ROOM_CAGE.radius, 6)
  })

  it('shrinks the orbit as the focus moves off-centre', () => {
    const centre = maxOrbitDistance(new THREE.Vector3(0, 1.55, 0))
    const seat = maxOrbitDistance(new THREE.Vector3(4.05, 1.4, 0))
    expect(seat).toBeLessThan(centre)
  })

  it('keeps a fully zoomed-out camera inside the room for any focused seat', () => {
    // This is the bug the cage exists for: zoom is measured from the target, so
    // a focused seat plus max zoom used to reach straight through the wall.
    for (let a = 0; a < Math.PI * 2; a += 0.3) {
      const target = new THREE.Vector3(Math.cos(a) * 4.05, 1.4, Math.sin(a) * 4.05)
      const dist = maxOrbitDistance(target)

      // Worst case: the camera sits directly opposite the room's centre.
      const away = target.clone().setY(0).normalize()
      const worst = target.clone().add(away.multiplyScalar(dist))
      expect(Math.hypot(worst.x, worst.z)).toBeLessThanOrEqual(ROOM_CAGE.radius + 1e-6)
    }
  })

  it('never collapses the orbit to something unusable', () => {
    const cornered = maxOrbitDistance(new THREE.Vector3(100, 1.4, 100))
    expect(cornered).toBeGreaterThanOrEqual(5.5)
  })
})

describe('poses', () => {
  it('seats an avatar lower than it stands', () => {
    const rig = makeRig()
    settle((dt) => poseStanding(rig, dt))
    const standing = rig.hips.position.y

    settle((dt) => poseSeated(rig, dt))
    expect(rig.hips.position.y).toBeLessThan(standing)
  })

  it('folds the knees when seated and straightens them when standing', () => {
    const rig = makeRig()
    settle((dt) => poseSeated(rig, dt))
    expect(rig.leftKnee.rotation.x).toBeGreaterThan(1)
    expect(rig.leftHip.rotation.x).toBeLessThan(-1)

    settle((dt) => poseStanding(rig, dt))
    expect(rig.leftKnee.rotation.x).toBeLessThan(0.2)
    expect(Math.abs(rig.leftHip.rotation.x)).toBeLessThan(0.2)
  })

  it('swings the legs in opposition while walking', () => {
    const rig = makeRig()
    // A quarter through the cycle the legs are at their furthest apart.
    settle((dt) => poseWalking(rig, Math.PI / 2, dt))
    expect(Math.sign(rig.leftHip.rotation.x)).not.toBe(Math.sign(rig.rightHip.rotation.x))
  })

  it('counter-swings each arm against the leg on the same side', () => {
    const rig = makeRig()
    settle((dt) => poseWalking(rig, Math.PI / 2, dt))
    expect(Math.sign(rig.leftShoulder.rotation.x))
      .not.toBe(Math.sign(rig.leftHip.rotation.x))
  })

  it('only shows the cup while actually drinking', () => {
    const rig = makeRig()
    expect(rig.cup.visible).toBe(false)
    poseDrinking(rig, 0.5, 1 / 60)
    expect(rig.cup.visible).toBe(true)
  })

  it('raises the cup highest mid-sip, not at the start or end', () => {
    const start = makeRig()
    settle((dt) => poseDrinking(start, 0, dt))
    const mid = makeRig()
    settle((dt) => poseDrinking(mid, 0.5, dt))

    // Elbow folds further (more negative) at the top of the sip.
    expect(mid.rightElbow.rotation.x).toBeLessThan(start.rightElbow.rotation.x)
  })
})

describe('face', () => {
  const face = (rig: AvatarRig, time: number, over = 0.2) =>
    settle((dt) => animateFace(rig, {
      time, phase: 0, talking: false, mood: 'neutral', dt, reduced: false,
    }), over)

  it('closes the eyes at the top of the blink cycle and opens them after', () => {
    const rig = makeRig()
    face(rig, 0.02)           // inside the ~120ms blink window
    const closed = rig.leftEye.scale.y

    face(rig, 2.0)            // well clear of it
    expect(rig.leftEye.scale.y).toBeGreaterThan(closed)
  })

  it('opens the mouth while talking and closes it when quiet', () => {
    const rig = makeRig()
    settle((dt) => animateFace(rig, {
      time: 1, phase: 0, talking: true, mood: 'bright', dt, reduced: false,
    }), 0.5)
    const talking = rig.mouth.scale.y

    settle((dt) => animateFace(rig, {
      time: 1, phase: 0, talking: false, mood: 'neutral', dt, reduced: false,
    }), 0.5)
    expect(rig.mouth.scale.y).toBeLessThan(talking)
  })

  it('lowers the brows when concentrating and lifts them when presenting', () => {
    const focused = makeRig()
    settle((dt) => animateFace(focused, {
      time: 1, phase: 0, talking: false, mood: 'focused', dt, reduced: false,
    }))
    const bright = makeRig()
    settle((dt) => animateFace(bright, {
      time: 1, phase: 0, talking: false, mood: 'bright', dt, reduced: false,
    }))

    expect(focused.leftBrow.position.y).toBeLessThan(bright.leftBrow.position.y)
  })
})

describe('build', () => {
  it('gives every agent a full skeleton regardless of gender', () => {
    for (const gender of ['male', 'female'] as const) {
      const rig = makeRig(gender)
      for (const joint of [
        rig.hips, rig.torso, rig.head,
        rig.leftShoulder, rig.rightShoulder, rig.leftElbow, rig.rightElbow,
        rig.leftHip, rig.rightHip, rig.leftKnee, rig.rightKnee,
      ]) {
        expect(joint).toBeInstanceOf(THREE.Object3D)
      }
      expect(rig.gender).toBe(gender)
    }
  })

  it('builds visibly different bodies for male and female', () => {
    const male = makeRig('male')
    const female = makeRig('female')
    expect(female.standHeight).toBeLessThan(male.standHeight)
    // The longer hair and bun are extra meshes on the head.
    expect(female.head.children.length).toBeGreaterThan(male.head.children.length)
  })

  it('always resolves a colour for skin and hair', () => {
    // A signed shift on a large hash once indexed off the end of the palette
    // and handed Three.js `undefined`.
    for (const name of ['Sakhile', 'Lerato', 'Zanele', 'Thabo', 'Kabelo', 'Naledi', 'Puso', 'z'.repeat(40)]) {
      const rig = buildAvatar({
        kit: createAvatarKit((o) => o),
        color: new THREE.Color('#fff'),
        gender: 'male',
        name,
        castShadow: false,
        track: (o) => o,
      })
      expect(rig.skinMat.color).toBeInstanceOf(THREE.Color)
      const hair = rig.head.children.find(
        (c) => c instanceof THREE.Mesh && c.material !== rig.skinMat,
      ) as THREE.Mesh | undefined
      expect(hair).toBeDefined()
    }
  })
})
