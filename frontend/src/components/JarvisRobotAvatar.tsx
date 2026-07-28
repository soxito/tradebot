/**
 * JarvisRobotAvatar — Full-body walking cyborg that roams all over the page.
 *
 * Behaviours:
 *  • Walking/Idle: robot walks in a random direction, periodically picks a new
 *    random target anywhere on screen and walks toward it.
 *  • Talking/Listening/Thinking: robot stops and plays the appropriate animation.
 *  • Hole-vanish Easter egg: after ~90s idle, the robot digs a glowing hole,
 *    disappears inside, then re-emerges when the user shouts "Jarvis".
 *  • Drag: user can grab the robot and place it anywhere.
 *  • Click: toggles the chat (only if not dragged).
 *  • Extension lock: when the extension robot is active the page chat mic/speaker
 *    is deactivated via a custom event.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import JarvisRobot, { RobotState, AvatarStyle } from './JarvisRobot'
import OpenHumanMascot, { MascotMood } from './OpenHumanMascot'
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
const ROAM_INTERVAL_MS = 4_000  // pick a new walk target every 4s

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
  const holeRef = useRef<HTMLDivElement | null>(null)

  // Position refs (avoid re-renders in the RAF loop)
  const posRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 })
  const targetRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 })
  const dragRef = useRef<{ active: boolean; moved: boolean; offX: number; offY: number }>({
    active: false, moved: false, offX: 0, offY: 0,
  })

  // Vanish state
  const [vanished, setVanished] = useState(false)
  const vanishedRef = useRef(false)

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
  const vanishAnimRef = useRef<HTMLDivElement | null>(null)

  // Walk direction
  const walkAngleRef = useRef(Math.random() * Math.PI * 2)
  const roamTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stateRef = useRef<RobotState>(state)
  useEffect(() => { stateRef.current = state }, [state])

  const extActiveRef = useRef(extRobotActive)
  useEffect(() => {
    extActiveRef.current = extRobotActive
    emitRobotLock(extRobotActive)
  }, [extRobotActive])

  // ── Bottom-RIGHT corner positioning ────────────────────────────────────────
  // The robot used to roam the centre 80% of the viewport, which walked it
  // straight through the middle of the dashboard and parked it on top of the
  // balance cards. A floating assistant belongs in the periphery, so the roam is
  // now a short band hugging the bottom-right corner — it still wanders, just
  // not across what you are reading.
  const BOTTOM_MARGIN = 40   // clear of the viewport bottom
  const CHAT_BTN_CLEARANCE = 88  // the 52px chat button + its 20px inset + gap
  const ROAM_BAND = 200      // how far along the bottom edge it may wander

  // Right-most left-edge the robot may occupy: keeps it clear of the chat button.
  const roamMaxX = () => Math.max(
    8,
    (typeof window !== 'undefined' ? window.innerWidth : 1920) - size - CHAT_BTN_CLEARANCE,
  )
  const roamMinX = () => Math.max(8, roamMaxX() - ROAM_BAND)

  // Fixed Y position (bottom edge)
  const Y_FIXED = typeof window !== 'undefined'
    ? window.innerHeight - size - BOTTOM_MARGIN
    : 0

  // Home = bottom-right corner, just left of the floating chat button.
  const homePos = () => ({
    x: roamMaxX(),
    y: (typeof window !== 'undefined' ? window.innerHeight : 1080) - size - BOTTOM_MARGIN,
  })

  // ── Initial position (saved, clamped into the corner band, else home) ───────
  const [pos, setPos] = useState<{ x: number; y: number }>(() => {
    if (typeof window === 'undefined') return { x: 0, y: 0 }
    try {
      const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null')
      // A position saved under the old centre-roaming behaviour would drop the
      // robot back into the middle of the page on every load, so bring any
      // stale value into the current band instead of trusting it blindly.
      if (saved && typeof saved.x === 'number' && typeof saved.y === 'number') {
        return {
          x: Math.min(Math.max(roamMinX(), saved.x), roamMaxX()),
          y: Math.min(Math.max(0, saved.y), window.innerHeight - size),
        }
      }
    } catch { /* noop */ }
    return homePos()
  })

  useEffect(() => { posRef.current = pos }, [pos])

  // ── Handle window resize: keep robot in the bottom-right corner ─────────────
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onResize = () => {
      const { x: newX, y: newY } = homePos()
      posRef.current = { x: newX, y: newY }
      if (wrapRef.current) {
        wrapRef.current.style.transform = `translate(${newX}px, ${newY}px)`
      }
      setPos({ x: newX, y: newY })
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size])

  // ── Pick a new roam target inside the bottom-right corner band ─────────────
  const pickTarget = useCallback(() => {
    if (typeof window === 'undefined') return
    const minX = roamMinX()
    const maxX = roamMaxX()
    // Small vertical wobble only (±15px)
    const yVariance = 15
    targetRef.current = {
      x: Math.floor(Math.random() * Math.max(1, maxX - minX)) + minX,
      y: Y_FIXED + (Math.random() - 0.5) * yVariance * 2,
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size])

  // ── Roam timer: pick new target every ROAM_INTERVAL_MS ─────────────────────
  useEffect(() => {
    pickTarget()
    roamTimerRef.current = setInterval(pickTarget, ROAM_INTERVAL_MS)
    return () => { if (roamTimerRef.current) clearInterval(roamTimerRef.current) }
  }, [pickTarget])

  // ── Main position RAF loop ──────────────────────────────────────────────────
  useEffect(() => {
    let raf = 0
    const WALK_SPEED = 1.2 // px per frame at 60fps
    const STOP_STATES: RobotState[] = ['listening', 'thinking', 'talking']

    const tick = () => {
      raf = requestAnimationFrame(tick)
      if (dragRef.current.active) return
      if (vanishedRef.current) return

      const st = stateRef.current
      const shouldWalk = !STOP_STATES.includes(st)

      if (shouldWalk) {
        const { x: tx, y: ty } = targetRef.current
        const { x, y } = posRef.current
        const dx = tx - x
        const dy = ty - y
        const dist = Math.sqrt(dx * dx + dy * dy)
        if (dist > 2) {
          const nx = x + (dx / dist) * WALK_SPEED
          const ny = y + (dy / dist) * WALK_SPEED
          // Clamp X to the bottom-right corner band, Y to the bottom strip
          const minX = roamMinX()
          const maxX = roamMaxX()
          const minY = Y_FIXED - 15
          const maxY = Y_FIXED + 15
          posRef.current = {
            x: Math.min(Math.max(minX, nx), maxX),
            y: Math.min(Math.max(minY, ny), maxY),
          }
        }
      }

      if (wrapRef.current) {
        const { x, y } = posRef.current
        wrapRef.current.style.transform = `translate(${x}px, ${y}px)`
      }
    }

    tick()
    return () => cancelAnimationFrame(raf)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size])

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
    // Re-emerge at the bottom-right home position.
    posRef.current = homePos()
    setPos(posRef.current)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size])
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
  }, [size])

  // ── Drag handlers ───────────────────────────────────────────────────────────
  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    d.active = true; d.moved = false
    d.offX = e.clientX - posRef.current.x
    d.offY = e.clientY - posRef.current.y
    ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d.active) return
    const w = typeof window !== 'undefined' ? window.innerWidth : 1920
    const nx = Math.min(Math.max(8, e.clientX - d.offX), w - size - 8)
    // Constrain Y to bottom strip (120px from bottom ± 30px variance)
    const h = typeof window !== 'undefined' ? window.innerHeight : 1080
    const fixedY = h - size - BOTTOM_MARGIN
    const ny = Math.min(Math.max(fixedY - 30, e.clientY - d.offY), fixedY + 30)
    if (Math.abs(nx - posRef.current.x) > 2 || Math.abs(ny - posRef.current.y) > 2) d.moved = true
    posRef.current = { x: nx, y: ny }
    if (wrapRef.current) wrapRef.current.style.transform = `translate(${nx}px, ${ny}px)`
  }, [size])

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d.active) return
    d.active = false
    setPos(posRef.current)
    try { localStorage.setItem(POS_KEY, JSON.stringify(posRef.current)) } catch { /* noop */ }
    if (!d.moved) onClick?.()
    ;(e.target as HTMLElement).releasePointerCapture?.(e.pointerId)
  }, [onClick])

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

  // ── Hole portal CSS (animated with keyframes inline) ──────────────────────
  const holeStyle: React.CSSProperties = {
    position: 'fixed',
    left: posRef.current.x + size / 2 - 50,
    top:  posRef.current.y + size - 20,
    width: 100, height: 40,
    borderRadius: '50%',
    background: 'radial-gradient(ellipse at center, #000 30%, #06b6d4 70%, transparent 100%)',
    boxShadow: '0 0 24px 8px #06b6d4, 0 0 60px 20px rgba(6,182,212,0.4)',
    zIndex: 2147483599,
    animation: 'jarvis-hole-pulse 1.2s ease-in-out infinite alternate',
    opacity: vanished ? 1 : 0,
    transition: 'opacity 0.4s',
    // Clickable: the hole is the visible affordance for a robot that is hiding,
    // and clicking the thing it left behind is the obvious way to ask for it
    // back. It was pointer-events:none, which — with the wake word as the only
    // other route — is what made the disappearance permanent without a mic.
    pointerEvents: vanished ? 'auto' : 'none',
    cursor: vanished ? 'pointer' : 'default',
  }

  // Keep the summon hint on screen. Anchored under a robot that already sits a
  // fixed margin from the bottom, `y + size + 24` lands past the viewport edge
  // and the text renders clipped in half.
  const hintTop = Math.min(
    posRef.current.y + size + 24,
    (typeof window !== 'undefined' ? window.innerHeight : 1080) - 18,
  )

  return (
    <>
      {/* Keyframe styles injected once */}
      <style>{`
        @keyframes jarvis-hole-pulse {
          from { transform: scaleX(1) scaleY(1); box-shadow: 0 0 24px 8px #06b6d4, 0 0 60px 20px rgba(6,182,212,0.4); }
          to   { transform: scaleX(1.15) scaleY(1.25); box-shadow: 0 0 40px 16px #22d3ee, 0 0 80px 30px rgba(34,211,238,0.5); }
        }
        @keyframes jarvis-emerge {
          from { transform: scaleY(0) translateY(60px); opacity: 0; }
          to   { transform: scaleY(1) translateY(0); opacity: 1; }
        }
      `}</style>

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
            left: posRef.current.x + size / 2,
            top:  hintTop,
            transform: 'translateX(-50%)',
            color: '#06b6d4', fontSize: 9, whiteSpace: 'nowrap', opacity: 0.7,
            fontFamily: 'monospace', pointerEvents: 'auto', cursor: 'pointer',
            zIndex: 2147483599,
          }}
        >
          Click or say &quot;Jarvis&quot; to summon
        </div>
      )}

      {/* Robot wrapper */}
      <div
        ref={wrapRef}
        style={{
          position: 'fixed', left: 0, top: 0,
          transform: `translate(${pos.x}px, ${pos.y}px)`,
          zIndex: 2147483600,
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
          animation: !vanished && state !== 'idle' ? 'jarvis-emerge 0.5s ease-out' : undefined,
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        title="JARVIS — drag to move, click to chat"
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
