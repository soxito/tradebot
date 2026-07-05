/**
 * JarvisRobot — Realistic full-body cyborg avatar rendered with Three.js.
 *
 * The heavy scene + animation live in `@/three/robotScene` (createRobotScene),
 * which runs EITHER:
 *   • inside a Web Worker via an OffscreenCanvas (preferred — keeps the always-on
 *     avatar rendering off the UI thread so the page stays responsive), or
 *   • on the main thread as a fallback when OffscreenCanvas / worker-WebGL is
 *     unavailable (or the worker reports it couldn't init WebGL).
 *
 * Animation states: idle / walking / listening / thinking / talking. The device
 * graphics tier + devicePixelRatio are resolved here (workers can't read them)
 * and passed in; live state/energy is forwarded to the worker on change and read
 * directly from refs on the main-thread path.
 */
import { useEffect, useRef } from 'react'
import { detectStaticProfile } from '@/utils/devicePerformance'
import { supportsOffscreenCanvas } from '@/utils/workerSupport'
import {
  createRobotScene,
  type RobotSceneHandle,
  type RobotState,
  type AvatarStyle,
} from '@/three/robotScene'

export type { RobotState, AvatarStyle }

interface Props {
  state: RobotState
  energy?: number
  avatarStyle?: AvatarStyle
  size?: number
  className?: string
}

export default function JarvisRobot({
  state,
  energy = 0,
  avatarStyle = 'cyan',
  size = 180,
  className = '',
}: Props) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const stateRef = useRef<RobotState>(state)
  const energyRef = useRef<number>(energy)
  const styleRef = useRef<AvatarStyle>(avatarStyle)
  const workerRef = useRef<Worker | null>(null)

  useEffect(() => { stateRef.current = state }, [state])
  useEffect(() => { energyRef.current = energy }, [energy])
  useEffect(() => { styleRef.current = avatarStyle }, [avatarStyle])

  // ── Build the robot: prefer an OffscreenCanvas worker, fall back to main thread
  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    // Resolve the device's graphics tier + DPR here (workers can't read navigator).
    const gfx = detectStaticProfile()
    const robotGfx = {
      antialias: gfx.antialias,
      robotDprCap: gfx.robotDprCap,
      shadows: gfx.shadows,
      fpsTarget: gfx.fpsTarget,
    }
    const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1

    let worker: Worker | null = null
    let sceneHandle: RobotSceneHandle | null = null
    let disposed = false

    // A visible canvas element sized to the requested box (both paths draw here).
    const makeCanvas = () => {
      const c = document.createElement('canvas')
      c.style.width = `${size}px`
      c.style.height = `${size}px`
      c.style.display = 'block'
      return c
    }

    // Main-thread fallback: render directly onto a fresh canvas via the shared scene.
    const runMainThread = () => {
      if (disposed) return
      const canvas = makeCanvas()
      mount.appendChild(canvas)
      sceneHandle = createRobotScene({
        canvas,
        size,
        dpr,
        gfx: robotGfx,
        style: styleRef.current,
        getState: () => ({ state: stateRef.current, energy: energyRef.current }),
      })
      if (!sceneHandle) {
        try { mount.removeChild(canvas) } catch { /* noop */ }
      }
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
            if (ev.data?.type === 'fail') fallbackFromWorker()
          }
          worker.onerror = () => fallbackFromWorker()
          worker.postMessage(
            {
              type: 'init',
              canvas: offscreen,
              size,
              dpr,
              gfx: robotGfx,
              style: styleRef.current,
              state: stateRef.current,
              energy: energyRef.current,
            },
            [offscreen],
          )
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

  return <div ref={mountRef} className={className} style={{ width: size, height: size }} />
}
