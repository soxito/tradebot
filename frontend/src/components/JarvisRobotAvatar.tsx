/**
 * JarvisRobotAvatar — Full-body walking cyborg that patrols the page.
 *
 * Behaviours:
 *  • Walking/Idle: robot walks along the bottom of the main content area,
 *    picking a new target every few seconds. Targets are chosen from the space
 *    that is actually free — never on top of the chat button or chat panel.
 *  • Talking/Listening/Thinking: robot stops and plays the appropriate animation.
 *  • Hole-vanish Easter egg: after ~90s idle, the robot digs a glowing hole,
 *    disappears inside, then re-emerges when the user shouts "Jarvis".
 *  • Drag: user can grab the robot and place it anywhere along the band. The
 *    placement holds for PARK_MS before roaming resumes.
 *  • Click: toggles the chat (only if not dragged).
 *  • Extension lock: when the extension robot is active the page chat mic/speaker
 *    is deactivated via a custom event.
 *
 * Geometry contract
 * -----------------
 * The robot roams inside `[data-jarvis-stage]` (the <main> element in Layout)
 * and avoids every `[data-jarvis-avoid]` rect (the chat button and the open
 * chat panel). Both are measured live, so a collapsing sidebar, a resize or a
 * panel that opens underneath the robot are all handled by the same code path.
 *
 * The wrapper's `transform` has exactly ONE writer: the rAF loop. Nothing else
 * — no CSS animation, no React re-render — may touch it, or the robot teleports.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import JarvisRobot, { RobotState, AvatarStyle } from './JarvisRobot'
import OpenHumanMascot, { MascotMood } from './OpenHumanMascot'
import { usePageVisibility } from '@/hooks/usePageVisibility'
import type { VariantId } from '@/three/robotVariants'

interface Props {
  state: RobotState
  energy?: number
  avatarStyle?: AvatarStyle
  /** Force a look. Omit to follow the persisted `jarvis.robotVariant`. */
  variant?: VariantId
  onClick?: () => void
  size?: number
  extRobotActive?: boolean   // true when extension robot has taken over
  useOpenHumanMascot?: boolean // use OpenHuman mascot instead of Three.js robot
}

// Map robot animation states → OpenHuman mascot moods
const ROBOT_TO_MOOD: Record<RobotState, MascotMood> = {
  idle:      'idle',
  walking:   'idle',
  listening: 'listening',
  thinking:  'thinking',
  talking:   'talking',
}

const POS_KEY = 'jarvis.robot.pos'
const VANISH_IDLE_MS = 90_000   // vanish after 90s of pure idle
const ROAM_INTERVAL_MS = 6_000  // pick a new walk target every 6s

// ── Geometry ────────────────────────────────────────────────────────────────
const STAGE_PAD = 24        // keep this far inside the content area
const AVOID_PAD = 16        // breathing room around anything it must not touch
const Y_WOBBLE = 10         // vertical drift while walking the bottom band
const WALK_PX_PER_S = 70    // time-based, so speed is frame-rate independent
const ESCAPE_MULT = 2.4     // faster when it has to get out of somebody's way
const ARRIVE_EPS = 6        // "close enough" — stop rather than quiver
const EASE_DIST = 48        // decelerate over the last stretch
const PARK_MS = 8_000       // how long a dragged placement is respected
const GEOMETRY_MS = 500     // re-measure stage + avoid rects this often

/** The robot sits below the chat panel and every modal (all `z-50`), so even
 *  if a rect appears where it stands it can never paint over the UI. */
const Z_ROBOT = 40
const Z_HOLE = 39

type Rect = { left: number; top: number; right: number; bottom: number }

const overlaps = (a: Rect, b: Rect) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top

const viewportRect = (): Rect => ({
  left: 0,
  top: 0,
  right: typeof window === 'undefined' ? 1920 : window.innerWidth,
  bottom: typeof window === 'undefined' ? 1080 : window.innerHeight,
})

/** Keyframes are identical for every instance — inject them once, into <head>,
 *  instead of re-inserting a <style> node on every mount. */
const KEYFRAMES_ID = 'jarvis-robot-keyframes'
const KEYFRAMES = `
@keyframes jarvis-hole-pulse {
  from { transform: scaleX(1) scaleY(1); box-shadow: 0 0 24px 8px #06b6d4, 0 0 60px 20px rgba(6,182,212,0.4); }
  to   { transform: scaleX(1.15) scaleY(1.25); box-shadow: 0 0 40px 16px #22d3ee, 0 0 80px 30px rgba(34,211,238,0.5); }
}
@keyframes jarvis-emerge {
  from { transform: scaleY(0) translateY(60px); opacity: 0; }
  to   { transform: scaleY(1) translateY(0); opacity: 1; }
}
`
function ensureKeyframes() {
  if (typeof document === 'undefined') return
  if (document.getElementById(KEYFRAMES_ID)) return
  const el = document.createElement('style')
  el.id = KEYFRAMES_ID
  el.textContent = KEYFRAMES
  document.head.appendChild(el)
}

// Emit a custom event so PaulChat can deactivate page mic/speaker
function emitRobotLock(locked: boolean) {
  try {
    window.dispatchEvent(new CustomEvent('jarvis-robot-lock', { detail: { locked } }))
  } catch { /* noop */ }
}

export default function JarvisRobotAvatar({
  state,
  energy = 0,
  avatarStyle = 'cyan',
  variant,
  onClick,
  size = 160,
  extRobotActive = false,
  useOpenHumanMascot = false,
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null)

  // Position refs (the RAF loop is the sole writer — never re-render to move)
  const posRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 })
  const targetRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 })
  const dragRef = useRef<{ active: boolean; moved: boolean; offX: number; offY: number }>({
    active: false, moved: false, offX: 0, offY: 0,
  })
  /** Timestamp until which roaming stays suspended after a deliberate drag. */
  const parkedUntilRef = useRef(0)
  /** True while the robot is standing inside an avoid rect and walking out. */
  const blockedRef = useRef(false)

  // Live geometry, refreshed by one interval + resize + a ResizeObserver.
  const stageRef = useRef<Rect>(viewportRect())
  const avoidRef = useRef<Rect[]>([])

  // Vanish state
  const [vanished, setVanished] = useState(false)
  const vanishedRef = useRef(false)
  /** Where the robot was standing when it vanished. The hole and the summon
   *  hint render from this instead of reading posRef during render, which is a
   *  value React does not track and cannot re-render for. */
  const [vanishAnchor, setVanishAnchor] = useState<{ x: number; y: number }>({ x: 0, y: 0 })

  // Whether the 3D scene itself should stop drawing. It trails `vanished` on
  // the way out so the robot fades with its shadow rather than popping off
  // mid-fade, and leads it on the way back in so the first visible frame is
  // already drawn. FADE_MS must match the wrapper's opacity transition.
  const FADE_MS = 600
  const [sceneHidden, setSceneHidden] = useState(false)
  useEffect(() => {
    if (!vanished) { setSceneHidden(false); return }
    const t = setTimeout(() => setSceneHidden(true), FADE_MS)
    return () => clearTimeout(t)
  }, [vanished])
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const roamTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stateRef = useRef<RobotState>(state)
  useEffect(() => { stateRef.current = state }, [state])

  const extActiveRef = useRef(extRobotActive)
  useEffect(() => {
    extActiveRef.current = extRobotActive
    emitRobotLock(extRobotActive)
  }, [extRobotActive])

  const visible = usePageVisibility()

  useEffect(ensureKeyframes, [])

  // ── Live geometry ─────────────────────────────────────────────────────────
  // The stage is the main content column; the avoid rects are the chat widgets.
  // Reading both from the DOM (rather than hard-coding pixel clearances) is what
  // makes "never walk on the chat" true for the 380px panel as well as the 52px
  // button, and keeps working when the sidebar collapses or the window resizes.
  const readGeometry = useCallback(() => {
    if (typeof document === 'undefined') return
    const vp = viewportRect()
    const stageEl = document.querySelector('[data-jarvis-stage]')
    const r = stageEl?.getBoundingClientRect()
    // Clamp to the viewport: a scrolling page can report a stage taller than the
    // screen, and a robot positioned off the bottom edge is a robot you cannot
    // see or click.
    stageRef.current = r && r.width > size + STAGE_PAD * 2
      ? {
          left: Math.max(vp.left, r.left),
          top: Math.max(vp.top, r.top),
          right: Math.min(vp.right, r.right),
          bottom: Math.min(vp.bottom, r.bottom),
        }
      : vp

    const rects: Rect[] = []
    document.querySelectorAll('[data-jarvis-avoid]').forEach((el) => {
      const b = el.getBoundingClientRect()
      if (b.width <= 0 || b.height <= 0) return   // display:none — not in the way
      rects.push({
        left: b.left - AVOID_PAD, top: b.top - AVOID_PAD,
        right: b.right + AVOID_PAD, bottom: b.bottom + AVOID_PAD,
      })
    })
    avoidRef.current = rects
  }, [size])

  /** The bottom band of the stage: the strip the robot is allowed to walk. */
  const band = useCallback(() => {
    const s = stageRef.current
    const minX = s.left + STAGE_PAD
    const maxX = Math.max(minX, s.right - size - STAGE_PAD)
    const baseY = Math.max(s.top + STAGE_PAD, s.bottom - size - STAGE_PAD)
    return { minX, maxX, baseY }
  }, [size])

  /** Horizontal spans of the band where a size×size robot touches nothing.
   *  Only rects that overlap the band vertically can block it, so this reduces
   *  to subtracting intervals from [minX, maxX]. */
  const freeSpans = useCallback((): Array<[number, number]> => {
    const { minX, maxX, baseY } = band()
    const boxTop = baseY - Y_WOBBLE
    const boxBottom = baseY + Y_WOBBLE + size
    let spans: Array<[number, number]> = [[minX, maxX]]

    for (const r of avoidRef.current) {
      if (r.bottom <= boxTop || r.top >= boxBottom) continue  // clears the band
      const blockedFrom = r.left - size   // left edge that would still overlap
      const blockedTo = r.right
      const next: Array<[number, number]> = []
      for (const [a, b] of spans) {
        if (blockedTo <= a || blockedFrom >= b) { next.push([a, b]); continue }
        if (blockedFrom > a) next.push([a, Math.min(b, blockedFrom)])
        if (blockedTo < b) next.push([Math.max(a, blockedTo), b])
      }
      spans = next
    }
    return spans.filter(([a, b]) => b - a >= 1)
  }, [band, size])

  /** Bottom-centre of whatever free space exists — the robot's home. */
  const homePos = useCallback(() => {
    const { minX, maxX, baseY } = band()
    const spans = freeSpans()
    if (!spans.length) return { x: (minX + maxX) / 2, y: baseY }
    const widest = spans.reduce((w, s) => (s[1] - s[0] > w[1] - w[0] ? s : w))
    return { x: (widest[0] + widest[1]) / 2, y: baseY }
  }, [band, freeSpans])

  // ── Initial position (saved, clamped into the band, else home) ─────────────
  // Held in state ONLY as the first-paint transform. It never changes, so React
  // re-renders leave the style property untouched and the RAF loop keeps sole
  // ownership of `transform`.
  const [initialPos] = useState<{ x: number; y: number }>(() => {
    if (typeof window === 'undefined') return { x: 0, y: 0 }
    try {
      const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null')
      if (saved && typeof saved.x === 'number' && typeof saved.y === 'number') {
        return {
          x: Math.min(Math.max(8, saved.x), window.innerWidth - size - 8),
          y: Math.min(Math.max(8, saved.y), window.innerHeight - size - 8),
        }
      }
    } catch { /* noop */ }
    return {
      x: (window.innerWidth - size) / 2,
      y: window.innerHeight - size - STAGE_PAD,
    }
  })

  // ── Pick a new roam target inside the free part of the band ────────────────
  const pickTarget = useCallback(() => {
    if (typeof window === 'undefined') return
    const { minX, maxX, baseY } = band()
    const spans = freeSpans()
    const y = baseY + (Math.random() - 0.5) * Y_WOBBLE * 2

    if (!spans.length) {
      // Everything is blocked (a very narrow window). Hug the left edge, which
      // is furthest from the bottom-right chat stack.
      targetRef.current = { x: minX, y }
      return
    }
    // Weight by span width so the robot spends its time in the open, rather
    // than treating a 10px sliver as an equally likely destination.
    const total = spans.reduce((sum, [a, b]) => sum + (b - a), 0)
    let roll = Math.random() * total
    for (const [a, b] of spans) {
      const w = b - a
      if (roll <= w) {
        targetRef.current = { x: Math.min(Math.max(minX, a + Math.random() * w), maxX), y }
        return
      }
      roll -= w
    }
    const [a, b] = spans[spans.length - 1]
    targetRef.current = { x: (a + b) / 2, y }
  }, [band, freeSpans])

  const pickTargetRef = useRef(pickTarget)
  pickTargetRef.current = pickTarget

  // ── Geometry refresh + first placement ────────────────────────────────────
  useEffect(() => {
    if (typeof window === 'undefined') return
    readGeometry()

    // Bring a restored/initial position into the current band before the first
    // walk step, so the robot never starts from somewhere it may not stand.
    const { minX, maxX, baseY } = band()
    posRef.current = {
      x: Math.min(Math.max(minX, initialPos.x), maxX),
      y: Math.min(Math.max(baseY - Y_WOBBLE, initialPos.y), baseY + Y_WOBBLE),
    }
    pickTargetRef.current()

    const onResize = () => { readGeometry(); pickTargetRef.current() }
    window.addEventListener('resize', onResize)
    const poll = setInterval(readGeometry, GEOMETRY_MS)

    let ro: ResizeObserver | undefined
    const stageEl = document.querySelector('[data-jarvis-stage]')
    if (stageEl && typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => readGeometry())
      ro.observe(stageEl)
    }
    return () => {
      window.removeEventListener('resize', onResize)
      clearInterval(poll)
      ro?.disconnect()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size])

  // ── Roam timer: pick new target every ROAM_INTERVAL_MS ─────────────────────
  useEffect(() => {
    if (!visible || vanished) {
      if (roamTimerRef.current) clearInterval(roamTimerRef.current)
      roamTimerRef.current = null
      return
    }
    roamTimerRef.current = setInterval(() => {
      if (dragRef.current.active) return
      if (Date.now() < parkedUntilRef.current) return
      pickTargetRef.current()
    }, ROAM_INTERVAL_MS)
    return () => { if (roamTimerRef.current) clearInterval(roamTimerRef.current) }
  }, [visible, vanished])

  // ── Main position RAF loop ─────────────────────────────────────────────────
  // Time-based, not per-frame: a 144Hz monitor and a throttled background tab
  // move the robot at the same speed. It eases into its target and then stands
  // completely still, which is what removes the constant micro-jitter.
  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!visible || vanished) return

    let raf = 0
    // Clock note: the elapsed time comes from Date.now(), not the rAF timestamp
    // or performance.now(). Millisecond resolution is ample at 60fps, and it is
    // the one clock that is consistent everywhere — mixing the rAF timestamp
    // with performance.now() yields a dt of ~0 whenever the two are driven by
    // different sources, which silently freezes the walk.
    let last = Date.now()
    const STOP_STATES: RobotState[] = ['listening', 'thinking', 'talking']

    const tick = () => {
      raf = requestAnimationFrame(tick)
      const now = Date.now()
      const dt = Math.min(Math.max(now - last, 0) / 1000, 0.05)  // cap after a stall
      last = now

      if (!dragRef.current.active) {
        const { minX, maxX, baseY } = band()
        let { x, y } = posRef.current

        // 1. Get out of the way first. A chat panel that opens on top of the
        //    robot must move the robot, not be walked over.
        const box = { left: x, top: y, right: x + size, bottom: y + size }
        const blocker = avoidRef.current.find((r) => overlaps(box, r))
        if (blocker) {
          // Pick the side it can actually reach. Choosing purely by distance
          // sends it into the band edge when the blocker sits near the left of
          // the stage — it then grinds against the clamp and never gets clear.
          const outLeft = blocker.left - size - 1
          const outRight = blocker.right + 1
          const canLeft = outLeft >= minX
          const canRight = outRight <= maxX
          const dir = canLeft && canRight ? (x - outLeft <= outRight - x ? -1 : 1)
            : canLeft ? -1
            : canRight ? 1
            : 0   // boxed in — hold still rather than vibrate against the wall
          x += dir * WALK_PX_PER_S * ESCAPE_MULT * dt
          // Re-aim once on the way out, not every frame.
          if (!blockedRef.current && Date.now() >= parkedUntilRef.current) {
            pickTargetRef.current()
          }
          blockedRef.current = true
        } else {
          blockedRef.current = false
          // 2. Otherwise walk toward the current target, easing on arrival.
          if (!STOP_STATES.includes(stateRef.current)) {
            const dx = targetRef.current.x - x
            const dy = targetRef.current.y - y
            const dist = Math.hypot(dx, dy)
            if (dist > ARRIVE_EPS) {
              const ease = Math.min(1, dist / EASE_DIST)
              const step = Math.min(WALK_PX_PER_S * ease * dt, dist)
              x += (dx / dist) * step
              y += (dy / dist) * step
            }
          }
        }

        posRef.current = {
          x: Math.min(Math.max(minX, x), maxX),
          y: Math.min(Math.max(baseY - Y_WOBBLE, y), baseY + Y_WOBBLE),
        }
      }

      if (wrapRef.current) {
        const { x, y } = posRef.current
        wrapRef.current.style.transform = `translate(${x}px, ${y}px)`
      }
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size, visible, vanished, band])

  // ── Summon: bring the robot back from the hole ─────────────────────────────
  // Every route back to visibility goes through here. It used to be inlined in
  // the wake-word listener only, which made the Easter egg a ONE-WAY DOOR: with
  // no microphone (denied, disabled, or an unsupported browser) nothing could
  // ever fire a wake event, so the robot stayed gone for the rest of the session
  // and the glowing hole sat on top of the page forever.
  const summon = useCallback(() => {
    if (!vanishedRef.current) return
    vanishedRef.current = false
    setVanished(false)
    if (typeof window === 'undefined') return
    readGeometry()
    posRef.current = homePos()
    targetRef.current = posRef.current
    if (wrapRef.current) {
      const { x, y } = posRef.current
      wrapRef.current.style.transform = `translate(${x}px, ${y}px)`
    }
  }, [homePos, readGeometry])
  const summonRef = useRef(summon)
  summonRef.current = summon

  // ── Vanish idle timer ─────────────────────────────────────────────────────
  // Re-armed by BOTH the voice state and real user activity. Watching `state`
  // alone meant "idle" was measured purely in voice terms, so someone actively
  // clicking around the dashboard with the mic off still lost the robot after
  // 90 seconds — the single most confusing way for it to look broken.
  const armVanishTimer = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    idleTimerRef.current = setTimeout(() => {
      if (stateRef.current === 'idle' || stateRef.current === 'walking') {
        vanishedRef.current = true
        setVanishAnchor({ ...posRef.current })
        setVanished(true)
      }
    }, VANISH_IDLE_MS)
  }, [])
  const armVanishTimerRef = useRef(armVanishTimer)
  armVanishTimerRef.current = armVanishTimer

  useEffect(() => {
    if (state !== 'idle' && state !== 'walking') {
      // Active — cancel the countdown and come back if we were hiding.
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      summonRef.current()
    } else {
      armVanishTimerRef.current()
    }
    return () => { if (idleTimerRef.current) clearTimeout(idleTimerRef.current) }
  }, [state])

  // Any real interaction with the page counts as "the user is still here".
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onActivity = () => {
      // Only re-arm while visible: activity must not silently cancel a vanish
      // that has already happened (the user summons that back deliberately).
      if (vanishedRef.current) return
      armVanishTimerRef.current()
    }
    const opts = { passive: true } as const
    window.addEventListener('pointerdown', onActivity, opts)
    window.addEventListener('keydown', onActivity, opts)
    window.addEventListener('scroll', onActivity, opts)
    return () => {
      window.removeEventListener('pointerdown', onActivity)
      window.removeEventListener('keydown', onActivity)
      window.removeEventListener('scroll', onActivity)
    }
  }, [])

  // ── Listen for "Jarvis" shout to re-emerge ──────────────────────────────────
  // The extension/page will fire a window event when the wake word is detected
  useEffect(() => {
    const handleWake = (e: Event) => {
      const ev = e as CustomEvent
      if (ev.detail?.type === 'wake' || ev.detail?.wakeWord === 'jarvis') {
        summonRef.current()
      }
    }

    // Listen for the extension message relay AND the page custom event
    window.addEventListener('jarvis-wake', handleWake)
    window.addEventListener('jarvis-ext-wake', handleWake)

    // Also listen for page postMessage broadcasts from content.js
    const onMsg = (e: MessageEvent) => {
      if (!e.data || e.data.__jarvisPage !== true) return
      if (e.data.type === 'wake' || e.data.type === 'command-start') {
        handleWake(new CustomEvent('wake', { detail: { type: 'wake' } }))
      }
    }
    window.addEventListener('message', onMsg)

    return () => {
      window.removeEventListener('jarvis-wake', handleWake)
      window.removeEventListener('jarvis-ext-wake', handleWake)
      window.removeEventListener('message', onMsg)
    }
  }, [])

  // ── Drag handlers ───────────────────────────────────────────────────────────
  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    d.active = true; d.moved = false
    d.offX = e.clientX - posRef.current.x
    d.offY = e.clientY - posRef.current.y
    // Capture on the element that carries the listeners. `e.target` is usually
    // the inner <canvas>, which would send the moves somewhere else entirely.
    try { wrapRef.current?.setPointerCapture(e.pointerId) } catch { /* noop */ }
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d.active) return
    const { minX, maxX, baseY } = band()
    const nx = Math.min(Math.max(minX, e.clientX - d.offX), maxX)
    const ny = Math.min(Math.max(baseY - Y_WOBBLE, e.clientY - d.offY), baseY + Y_WOBBLE)
    if (Math.abs(nx - posRef.current.x) > 2 || Math.abs(ny - posRef.current.y) > 2) d.moved = true
    posRef.current = { x: nx, y: ny }
    if (wrapRef.current) wrapRef.current.style.transform = `translate(${nx}px, ${ny}px)`
  }, [band])

  /** Ends a drag from any route — pointerup, cancel, or lost capture. A
   *  cancelled pointer used to leave `active` true forever, which froze the
   *  robot for the rest of the session. */
  const endDrag = useCallback((e: React.PointerEvent, fire: boolean) => {
    const d = dragRef.current
    if (!d.active) return
    d.active = false
    try { wrapRef.current?.releasePointerCapture(e.pointerId) } catch { /* noop */ }
    if (d.moved) {
      // Respect the placement: hold this spot, then resume roaming from here.
      targetRef.current = { ...posRef.current }
      parkedUntilRef.current = Date.now() + PARK_MS
      try { localStorage.setItem(POS_KEY, JSON.stringify(posRef.current)) } catch { /* noop */ }
    } else if (fire) {
      onClick?.()
    }
  }, [onClick])

  const onPointerUp = useCallback((e: React.PointerEvent) => endDrag(e, true), [endDrag])
  const onPointerCancel = useCallback((e: React.PointerEvent) => endDrag(e, false), [endDrag])

  // Effective robot state — map 'idle'→'walking' when not stopping
  const displayState: RobotState =
    (state === 'idle' || state === 'walking')
      ? 'walking'
      : state

  // Status label
  const label =
    state === 'listening' ? 'Listening…'
    : state === 'thinking' ? 'Thinking…'
    : state === 'talking'  ? 'Speaking…'
    : ''

  // ── Hole portal ───────────────────────────────────────────────────────────
  const holeStyle: React.CSSProperties = {
    position: 'fixed',
    left: vanishAnchor.x + size / 2 - 50,
    top:  vanishAnchor.y + size - 20,
    width: 100, height: 40,
    borderRadius: '50%',
    background: 'radial-gradient(ellipse at center, #000 30%, #06b6d4 70%, transparent 100%)',
    boxShadow: '0 0 24px 8px #06b6d4, 0 0 60px 20px rgba(6,182,212,0.4)',
    zIndex: Z_HOLE,
    animation: 'jarvis-hole-pulse 1.2s ease-in-out infinite alternate',
    // Clickable: the hole is the visible affordance for a robot that is hiding,
    // and clicking the thing it left behind is the obvious way to ask for it
    // back. It was pointer-events:none, which — with the wake word as the only
    // other route — is what made the disappearance permanent without a mic.
    pointerEvents: 'auto',
    cursor: 'pointer',
  }

  // Keep the summon hint on screen. Anchored under a robot that already sits a
  // fixed margin from the bottom, `y + size + 24` lands past the viewport edge
  // and the text renders clipped in half.
  const hintTop = Math.min(
    vanishAnchor.y + size + 24,
    (typeof window !== 'undefined' ? window.innerHeight : 1080) - 18,
  )

  return (
    <>
      {/* Glowing hole portal + summon hint. Both live outside the robot wrapper
          because that wrapper is faded and visibility:hidden while vanished —
          anything nested inside it is invisible exactly when this needs to show. */}
      {vanished && (
        <div
          style={holeStyle}
          onClick={summon}
          role="button"
          tabIndex={0}
          aria-label="Summon JARVIS"
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') summon() }}
          title="Click to summon JARVIS"
        />
      )}
      {vanished && (
        <div
          onClick={summon}
          style={{
            position: 'fixed',
            left: vanishAnchor.x + size / 2,
            top:  hintTop,
            transform: 'translateX(-50%)',
            color: '#06b6d4', fontSize: 9, whiteSpace: 'nowrap', opacity: 0.7,
            fontFamily: 'monospace', pointerEvents: 'auto', cursor: 'pointer',
            zIndex: Z_HOLE,
          }}
        >
          Click or say &quot;Jarvis&quot; to summon
        </div>
      )}

      {/* Robot wrapper. `transform` is written ONLY by the RAF loop — the
          initial value below never changes, so React never rewrites it. */}
      <div
        ref={wrapRef}
        data-testid="jarvis-robot"
        style={{
          position: 'fixed', left: 0, top: 0,
          transform: `translate(${initialPos.x}px, ${initialPos.y}px)`,
          zIndex: Z_ROBOT,
          width: size, height: size,
          cursor: 'grab', touchAction: 'none',
          // No drop-shadow while hidden: the shadow is cast by the wrapper, so
          // leaving it on paints a dark blur where the robot used to be. The
          // 3D scene's own contact shadow is removed in step via `hidden`.
          filter: vanished ? 'none' : 'drop-shadow(0 12px 32px rgba(0,0,0,.55))',
          opacity: vanished ? 0 : 1,
          // Once hidden the wrapper must stop swallowing clicks — it is a
          // size×size invisible box sitting above the page.
          pointerEvents: vanished ? 'none' : 'auto',
          visibility: vanished ? 'hidden' : 'visible',
          // Delay the visibility flip until the fade-out finishes, but apply it
          // instantly on the way back in so the robot is not invisible for the
          // first 600 ms of its return.
          transition: vanished
            ? `opacity ${FADE_MS}ms, visibility 0s linear ${FADE_MS}ms`
            : `opacity ${FADE_MS}ms, visibility 0s`,
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onLostPointerCapture={onPointerCancel}
        title="JARVIS — drag to move, click to chat"
      >
        {/* The emerge animation lives on an INNER element on purpose. Run on the
            wrapper it animates `transform`, which beats the inline transform the
            RAF loop writes — the robot teleported to the viewport corner for
            half a second on every single voice turn. */}
        <div
          style={{
            width: '100%', height: '100%',
            transformOrigin: 'bottom center',
            animation: !vanished && state !== 'idle' ? 'jarvis-emerge 0.5s ease-out' : undefined,
          }}
        >
          {useOpenHumanMascot ? (
            <OpenHumanMascot
              mood={ROBOT_TO_MOOD[state] ?? 'idle'}
              size={size}
              hidden={sceneHidden}
            />
          ) : (
            <JarvisRobot
              state={displayState}
              energy={energy}
              avatarStyle={avatarStyle}
              variant={variant}
              size={size}
              hidden={sceneHidden}
            />
          )}
        </div>

        {label && (
          <div style={{
            position: 'absolute', bottom: -8, left: '50%', transform: 'translateX(-50%)',
            background: 'rgba(8,14,26,.92)', border: '1px solid rgba(255,255,255,.12)',
            borderRadius: 12, padding: '3px 10px', fontSize: 10, fontWeight: 700,
            color: state === 'listening' ? '#86efac' : state === 'thinking' ? '#c4b5fd' : '#fbbf24',
            whiteSpace: 'nowrap', pointerEvents: 'none', letterSpacing: '0.04em',
            fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
            boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
          }}>
            {label}
          </div>
        )}

      </div>
    </>
  )
}
