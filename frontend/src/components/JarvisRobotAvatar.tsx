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

interface Props {
  state: RobotState
  energy?: number
  avatarStyle?: AvatarStyle
  onClick?: () => void
  size?: number
  extRobotActive?: boolean   // true when extension robot has taken over
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
  onClick,
  size = 160,
  extRobotActive = false,
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

  // ── Initial position (saved or default) ─────────────────────────────────────
  const [pos, setPos] = useState<{ x: number; y: number }>(() => {
    if (typeof window === 'undefined') return { x: 0, y: 0 }
    try {
      const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null')
      if (saved && typeof saved.x === 'number') return saved
    } catch { /* noop */ }
    return { x: window.innerWidth - size - 36, y: window.innerHeight - size - 120 }
  })

  useEffect(() => { posRef.current = pos }, [pos])

  // ── Pick a new random roam target ──────────────────────────────────────────
  const pickTarget = useCallback(() => {
    if (typeof window === 'undefined') return
    targetRef.current = {
      x: Math.floor(Math.random() * (window.innerWidth  - size - 16)) + 8,
      y: Math.floor(Math.random() * (window.innerHeight - size - 16)) + 8,
    }
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
          posRef.current = {
            x: Math.min(Math.max(8, nx), window.innerWidth  - size - 8),
            y: Math.min(Math.max(8, ny), window.innerHeight - size - 8),
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
  }, [size])

  // ── Vanish idle timer: reset on any non-idle state ──────────────────────────
  useEffect(() => {
    if (state !== 'idle' && state !== 'walking') {
      // Active — reset vanish timer
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      // If we were vanished, come back immediately
      if (vanishedRef.current) {
        vanishedRef.current = false
        setVanished(false)
      }
    } else {
      // Idle/walking — start vanish countdown
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      idleTimerRef.current = setTimeout(() => {
        if (stateRef.current === 'idle' || stateRef.current === 'walking') {
          triggerVanish()
        }
      }, VANISH_IDLE_MS)
    }
    return () => { if (idleTimerRef.current) clearTimeout(idleTimerRef.current) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state])

  // ── Vanish: robot digs a glowing hole and disappears ───────────────────────
  function triggerVanish() {
    vanishedRef.current = true
    setVanished(true)
  }

  // ── Listen for "Jarvis" shout to re-emerge ──────────────────────────────────
  // The extension/page will fire a window event when the wake word is detected
  useEffect(() => {
    const handleWake = (e: Event) => {
      const ev = e as CustomEvent
      if (ev.detail?.type === 'wake' || ev.detail?.wakeWord === 'jarvis') {
        if (vanishedRef.current) {
          vanishedRef.current = false
          setVanished(false)
          // Walk back from a random edge
          posRef.current = {
            x: Math.random() < 0.5 ? 8 : window.innerWidth - size - 8,
            y: Math.random() * (window.innerHeight - size - 16) + 8,
          }
        }
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
    const nx = Math.min(Math.max(8, e.clientX - d.offX), window.innerWidth  - size - 8)
    const ny = Math.min(Math.max(8, e.clientY - d.offY), window.innerHeight - size - 8)
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
    pointerEvents: 'none',
  }

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

      {/* Glowing hole portal */}
      {vanished && <div style={holeStyle} />}

      {/* Robot wrapper */}
      <div
        ref={wrapRef}
        style={{
          position: 'fixed', left: 0, top: 0,
          transform: `translate(${pos.x}px, ${pos.y}px)`,
          zIndex: 2147483600,
          width: size, height: size,
          cursor: 'grab', touchAction: 'none',
          filter: 'drop-shadow(0 12px 32px rgba(0,0,0,.55))',
          opacity: vanished ? 0 : 1,
          transition: 'opacity 0.6s',
          animation: !vanished && state !== 'idle' ? 'jarvis-emerge 0.5s ease-out' : undefined,
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        title="JARVIS — drag to move, click to chat"
      >
        <JarvisRobot
          state={displayState}
          energy={energy}
          avatarStyle={avatarStyle}
          size={size}
        />

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

        {/* Vanish hint while robot is active */}
        {vanished && (
          <div style={{
            position: 'absolute', bottom: -28, left: '50%', transform: 'translateX(-50%)',
            color: '#06b6d4', fontSize: 9, whiteSpace: 'nowrap', opacity: 0.7,
            fontFamily: 'monospace', pointerEvents: 'none',
          }}>
            Say "Jarvis" to summon
          </div>
        )}
      </div>
    </>
  )
}
