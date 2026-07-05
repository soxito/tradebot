/**
 * jarvisRobot.worker — renders the JARVIS cyborg avatar off the main thread.
 *
 * The main thread (JarvisRobot.tsx) transfers an OffscreenCanvas and posts the
 * resolved device pixel ratio + graphics tier + initial style/state. This worker
 * owns the Three.js scene and its rAF loop, so the always-on global avatar no
 * longer competes with the UI thread. Live state/energy updates arrive as small
 * `state` messages.
 *
 * If WebGL can't be created in the worker, it posts `{ type: 'fail' }` so the
 * main thread can rebuild on the main thread instead (graceful fallback).
 */
import {
  createRobotScene,
  type RobotSceneHandle,
  type RobotGfx,
  type RobotState,
  type AvatarStyle,
} from '../three/robotScene'

type InitMsg = {
  type: 'init'
  canvas: OffscreenCanvas
  size: number
  dpr: number
  gfx: RobotGfx
  style: AvatarStyle
  state: RobotState
  energy: number
}
type StateMsg = { type: 'state'; state: RobotState; energy: number }
type SizeMsg = { type: 'size'; size: number }
type StopMsg = { type: 'stop' }
type InMsg = InitMsg | StateMsg | SizeMsg | StopMsg

const ctx = self as unknown as Worker

let handle: RobotSceneHandle | null = null
const live: { state: RobotState; energy: number } = { state: 'idle', energy: 0 }

ctx.onmessage = (e: MessageEvent<InMsg>) => {
  const msg = e.data
  switch (msg.type) {
    case 'init': {
      live.state = msg.state ?? 'idle'
      live.energy = msg.energy ?? 0
      handle = createRobotScene({
        canvas: msg.canvas,
        size: msg.size,
        dpr: msg.dpr,
        gfx: msg.gfx,
        style: msg.style,
        getState: () => live,
      })
      // WebGL unavailable inside the worker → tell main thread to fall back.
      ctx.postMessage({ type: handle ? 'ready' : 'fail' })
      break
    }
    case 'state':
      live.state = msg.state
      live.energy = msg.energy
      break
    case 'size':
      handle?.setSize(msg.size)
      break
    case 'stop':
      handle?.dispose()
      handle = null
      break
  }
}
