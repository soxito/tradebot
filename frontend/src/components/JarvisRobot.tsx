/**
 * JarvisRobot — the JARVIS avatar, rendered with Three.js.
 *
 * The scene itself is a *variant*: one of the looks registered in
 * `@/three/robotVariants`, built and driven by `@/three/variantScene`. The
 * selection is read from localStorage (`jarvis.robotVariant`) so it survives a
 * reload, and a `jarvis-variant-change` window event hot-swaps the live scene
 * without a rebuild or a page refresh.
 *
 * The scene runs EITHER:
 *   • inside a Web Worker via an OffscreenCanvas (preferred — keeps the
 *     always-on avatar off the UI thread so the page stays responsive), or
 *   • on the main thread as a fallback when OffscreenCanvas / worker-WebGL is
 *     unavailable (or the worker reports it couldn't init WebGL).
 *
 * Animation states: idle / walking / listening / thinking / talking. The device
 * graphics tier, devicePixelRatio, reduced-motion preference and pointer
 * position are all resolved here (workers can read none of them) and forwarded.
 */
import { useEffect, useRef } from 'react'
import { detectStaticProfile } from '@/utils/devicePerformance'
import { supportsOffscreenCanvas } from '@/utils/workerSupport'
import type { RobotState, AvatarStyle } from '@/three/robotScene'
import {
  createVariantScene,
  gfxFromProfile,
  prefersReducedMotion,
  type VariantSceneHandle,
} from '@/three/variantScene'
import {
  DEFAULT_ROBOT_VARIANT,
  ROBOT_VARIANT_KEY,
  VARIANT_EVENT,
  isVariantId,
  readVariant,
  type VariantId,
} from '@/three/robotVariants'

export type { RobotState, AvatarStyle }

interface Props {
  state: RobotState
  energy?: number
  avatarStyle?: AvatarStyle
  /** Force a look. Omit to follow the persisted `jarvis.robotVariant`. */
  variant?: VariantId
  size?: number
  className?: string
  /**
   * Hide the robot entirely. Handled inside the 3D scene rather than by fading
   * the canvas, so the glow disappears with the body instead of being left
   * behind as a smudge — and rendering pauses while hidden.
   */
  hidden?: boolean
}

export default function JarvisRobot({
  state,
  energy = 0,
  variant,
  size = 180,
  className = '',
  hidden = false,
}: Props) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const stateRef = useRef<RobotState>(state)
  const energyRef = useRef<number>(energy)
  const pointerRef = useRef({ px: 0, py: 0 })
  const workerRef = useRef<Worker | null>(null)
  const sceneRef = useRef<VariantSceneHandle | null>(null)
  const hiddenRef = useRef(hidden)
  const variantRef = useRef<VariantId>(DEFAULT_ROBOT_VARIANT)

  useEffect(() => { hiddenRef.current = hidden }, [hidden])
  useEffect(() => { stateRef.current = state }, [state])
  useEffect(() => { energyRef.current = energy }, [energy])

  // ── Build the robot: prefer an OffscreenCanvas worker, fall back to main thread
  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    // Resolve everything the worker cannot read for itself.
    const profile = detectStaticProfile()
    const gfx = gfxFromProfile(profile, 'robot')
    const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1
    const reducedMotion = prefersReducedMotion()
    const startVariant = variant ?? readVariant(ROBOT_VARIANT_KEY, DEFAULT_ROBOT_VARIANT)
    variantRef.current = startVariant

    let worker: Worker | null = null
    let sceneHandle: VariantSceneHandle | null = null
    let disposed = false

    // A visible canvas element sized to the requested box (both paths draw here).
    const makeCanvas = () => {
      const c = document.createElement('canvas')
      c.style.width = `${size}px`
      c.style.height = `${size}px`
      c.style.display = 'block'
      return c
    }

    // Main-thread fallback: render directly onto a fresh canvas.
    const runMainThread = () => {
      if (disposed) return
      const canvas = makeCanvas()
      mount.appendChild(canvas)
      sceneHandle = createVariantScene({
        canvas,
        mode: 'robot',
        variant: variantRef.current,
        width: size,
        height: size,
        dpr,
        gfx,
        reducedMotion,
        getState: () => ({
          state: stateRef.current,
          energy: energyRef.current,
          px: pointerRef.current.px,
          py: pointerRef.current.py,
        }),
      })
      if (!sceneHandle) {
        try { mount.removeChild(canvas) } catch { /* noop */ }
        return
      }
      sceneRef.current = sceneHandle
      // Apply the current visibility immediately — the scene is built visible,
      // and a robot that is meant to be hidden must not flash into view.
      sceneHandle.setHidden(hiddenRef.current)
    }

    // Preferred path: OffscreenCanvas + Web Worker (off the UI thread).
    if (supportsOffscreenCanvas()) {
      const canvas = makeCanvas()
      let offscreen: OffscreenCanvas | null = null
      try {
        offscreen = canvas.transferControlToOffscreen()
      } catch {
        offscreen = null
      }

      if (offscreen) {
        mount.appendChild(canvas)
        const fallbackFromWorker = () => {
          try { worker?.terminate() } catch { /* noop */ }
          worker = null
          workerRef.current = null
          try { mount.removeChild(canvas) } catch { /* noop */ }
          runMainThread()
        }
        try {
          worker = new Worker(new URL('../workers/jarvisRobot.worker.ts', import.meta.url))
          workerRef.current = worker
          worker.onmessage = (ev: MessageEvent) => {
            // Worker couldn't init WebGL → drop the neutered canvas and rebuild here.
            if (ev.data?.type === 'fail') {
              if (process.env.NODE_ENV !== 'production' && ev.data.error) {
                console.warn('[JarvisRobot] worker scene failed, falling back:', ev.data.error)
              }
              fallbackFromWorker()
            }
          }
          worker.onerror = () => fallbackFromWorker()
          worker.postMessage(
            {
              type: 'init',
              canvas: offscreen,
              size,
              dpr,
              gfx,
              variant: startVariant,
              reducedMotion,
              state: stateRef.current,
              energy: energyRef.current,
            },
            [offscreen],
          )
          if (hiddenRef.current) worker.postMessage({ type: 'hidden', hidden: true })
        } catch {
          fallbackFromWorker()
        }
      } else {
        // transferControlToOffscreen threw → build on the main thread instead.
        runMainThread()
      }
    } else {
      runMainThread()
    }

    return () => {
      disposed = true
      if (worker) {
        try { worker.postMessage({ type: 'stop' }) } catch { /* noop */ }
        try { worker.terminate() } catch { /* noop */ }
      }
      workerRef.current = null
      sceneHandle?.dispose()
      sceneRef.current = null
      while (mount.firstChild) mount.removeChild(mount.firstChild)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size])

  // ── Forward live state/energy to the worker (main-thread path reads refs live)
  useEffect(() => {
    const w = workerRef.current
    if (w) {
      try { w.postMessage({ type: 'state', state, energy }) } catch { /* noop */ }
    }
  }, [state, energy])

  // ── Pointer tracking for the head ──────────────────────────────────────────
  // Sampled on the page, normalised to NDC, and pushed at ~20 Hz. Posting on
  // every pointermove would flood the worker for motion the eye can't resolve.
  useEffect(() => {
    if (typeof window === 'undefined') return
    let lastSent = 0
    const onMove = (e: PointerEvent) => {
      const px = (e.clientX / window.innerWidth) * 2 - 1
      const py = (e.clientY / window.innerHeight) * 2 - 1
      pointerRef.current = { px, py }
      const now = performance.now()
      if (now - lastSent < 50) return
      lastSent = now
      const w = workerRef.current
      if (w) { try { w.postMessage({ type: 'pointer', px, py }) } catch { /* noop */ } }
    }
    window.addEventListener('pointermove', onMove, { passive: true })
    return () => window.removeEventListener('pointermove', onMove)
  }, [])

  // ── Variant selection: explicit prop, or the persisted choice + live swap ───
  useEffect(() => {
    const apply = (next: VariantId) => {
      if (next === variantRef.current) return
      variantRef.current = next
      const w = workerRef.current
      if (w) { try { w.postMessage({ type: 'variant', variant: next }) } catch { /* noop */ } }
      sceneRef.current?.setVariant(next)
    }

    if (variant) { apply(variant); return }

    apply(readVariant(ROBOT_VARIANT_KEY, DEFAULT_ROBOT_VARIANT))

    const onChange = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (detail?.kind !== 'robot') return
      if (isVariantId(detail.variant)) apply(detail.variant)
    }
    window.addEventListener(VARIANT_EVENT, onChange)
    return () => window.removeEventListener(VARIANT_EVENT, onChange)
  }, [variant])

  // ── Forward visibility to whichever render path is live ────────────────────
  useEffect(() => {
    const w = workerRef.current
    if (w) { try { w.postMessage({ type: 'hidden', hidden }) } catch { /* noop */ } }
    sceneRef.current?.setHidden(hidden)
  }, [hidden])

  return <div ref={mountRef} className={className} style={{ width: size, height: size }} />
}
