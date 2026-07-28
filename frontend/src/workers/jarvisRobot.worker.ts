/**
 * jarvisRobot.worker — renders the JARVIS avatar off the main thread.
 *
 * The main thread (JarvisRobot.tsx) transfers an OffscreenCanvas and posts the
 * resolved device pixel ratio + graphics tier + reduced-motion preference +
 * initial variant/state. This worker owns the Three.js scene and its rAF loop,
 * so the always-on global avatar no longer competes with the UI thread. Live
 * state/energy and pointer position arrive as small messages.
 *
 * If WebGL can't be created in the worker, it posts `{ type: 'fail' }` so the
 * main thread can rebuild on the main thread instead (graceful fallback).
 */
import type { RobotState } from '../three/robotScene'
import {
  createVariantScene,
  type VariantGfx,
  type VariantSceneHandle,
} from '../three/variantScene'
import type { VariantId } from '../three/robotVariants'

type InitMsg = {
  type: 'init'
  canvas: OffscreenCanvas
  size: number
  dpr: number
  gfx: VariantGfx
  variant: VariantId
  reducedMotion: boolean
  state: RobotState
  energy: number
}
type StateMsg = { type: 'state'; state: RobotState; energy: number }
type PointerMsg = { type: 'pointer'; px: number; py: number }
type VariantMsg = { type: 'variant'; variant: VariantId }
type SizeMsg = { type: 'size'; size: number }
type HiddenMsg = { type: 'hidden'; hidden: boolean }
type StopMsg = { type: 'stop' }
type InMsg = InitMsg | StateMsg | PointerMsg | VariantMsg | SizeMsg | HiddenMsg | StopMsg

const ctx = self as unknown as Worker

let handle: VariantSceneHandle | null = null
const live = { state: 'idle' as RobotState, energy: 0, px: 0, py: 0 }

ctx.onmessage = (e: MessageEvent<InMsg>) => {
  const msg = e.data
  switch (msg.type) {
    case 'init': {
      live.state = msg.state ?? 'idle'
      live.energy = msg.energy ?? 0
      // Anything that throws in here is otherwise invisible: an uncaught worker
      // error surfaces on the main thread as a bare ErrorEvent with no message,
      // which makes a blank avatar impossible to diagnose. Report the reason.
      try {
        handle = createVariantScene({
          canvas: msg.canvas,
          mode: 'robot',
          variant: msg.variant,
          width: msg.size,
          height: msg.size,
          dpr: msg.dpr,
          gfx: msg.gfx,
          reducedMotion: msg.reducedMotion,
          getState: () => live,
        })
      } catch (err) {
        handle = null
        ctx.postMessage({ type: 'fail', error: String((err as Error)?.message ?? err) })
        break
      }
      // WebGL unavailable inside the worker → tell main thread to fall back.
      ctx.postMessage(handle
        ? { type: 'ready', drawCalls: handle.drawCalls() }
        : { type: 'fail', error: 'WebGL unavailable in worker' })
      break
    }
    case 'state':
      live.state = msg.state
      live.energy = msg.energy
      break
    case 'pointer':
      live.px = msg.px
      live.py = msg.py
      break
    case 'variant':
      handle?.setVariant(msg.variant)
      break
    case 'size':
      handle?.setSize(msg.size, msg.size)
      break
    case 'hidden':
      handle?.setHidden(msg.hidden)
      break
    case 'stop':
      handle?.dispose()
      handle = null
      break
  }
}
