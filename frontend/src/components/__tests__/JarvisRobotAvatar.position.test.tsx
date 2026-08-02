/**
 * The floating robot's geometry contract.
 *
 * These tests drive the real rAF loop under fake timers against a stubbed
 * `[data-jarvis-stage]` / `[data-jarvis-avoid]` layout, because every bug this
 * component has had was a geometry bug: walking over the chat panel, clamping
 * against a stale viewport height, or freezing after a cancelled drag.
 */
import { render, screen, act, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import JarvisRobotAvatar from '@/components/JarvisRobotAvatar'
import type { RobotState } from '@/components/JarvisRobot'

// The 3D scene is irrelevant here and drags in a worker + WebGL.
vi.mock('@/components/JarvisRobot', () => ({
  default: ({ size }: { size: number }) => (
    <div data-testid="robot-scene" style={{ width: size, height: size }} />
  ),
}))
vi.mock('@/components/OpenHumanMascot', () => ({
  default: () => <div data-testid="mascot" />,
}))

// jsdom ships no PointerEvent, so testing-library's pointer helpers would drop
// clientX/clientY and the drag maths would see NaN. A MouseEvent subclass is
// enough for everything this component reads.
if (typeof window.PointerEvent === 'undefined') {
  class FakePointerEvent extends MouseEvent {
    pointerId: number
    constructor(type: string, props: PointerEventInit = {}) {
      super(type, props)
      this.pointerId = props.pointerId ?? 1
    }
  }
  window.PointerEvent = FakePointerEvent as unknown as typeof PointerEvent
}

const SIZE = 160
const STAGE = { left: 224, top: 56, right: 1920, bottom: 1080 }
// The floating chat button: bottom-right, exactly where the robot used to live.
const CHAT = { left: 1848, top: 1008, right: 1900, bottom: 1060 }

/** Attach an element whose getBoundingClientRect is fixed, jsdom-style. */
function stubbed(tag: string, attr: string, rect: typeof STAGE) {
  const el = document.createElement(tag)
  el.setAttribute(attr, '')
  el.getBoundingClientRect = () => ({
    ...rect,
    x: rect.left,
    y: rect.top,
    width: rect.right - rect.left,
    height: rect.bottom - rect.top,
    toJSON: () => ({}),
  }) as DOMRect
  document.body.appendChild(el)
  return el
}

function robotBox() {
  const el = screen.getByTestId('jarvis-robot')
  const m = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(el.style.transform)
  if (!m) throw new Error(`no translate in "${el.style.transform}"`)
  const x = parseFloat(m[1])
  const y = parseFloat(m[2])
  return { x, y, left: x, top: y, right: x + SIZE, bottom: y + SIZE }
}

const overlaps = (a: ReturnType<typeof robotBox>, b: typeof STAGE) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top

/** Run the rAF loop for `ms` of simulated time. */
const runFrames = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

describe('JarvisRobotAvatar geometry', () => {
  const defaultProps = { state: 'idle' as RobotState, energy: 0, size: SIZE }

  beforeEach(() => {
    vi.useFakeTimers()
    Object.defineProperty(window, 'innerWidth', { value: 1920, writable: true })
    Object.defineProperty(window, 'innerHeight', { value: 1080, writable: true })
    stubbed('main', 'data-jarvis-stage', STAGE)
  })

  afterEach(() => {
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it('walks the bottom band of the stage, not the viewport corner', () => {
    render(<JarvisRobotAvatar {...defaultProps} />)
    runFrames(1000)

    const box = robotBox()
    // Inside the stage horizontally…
    expect(box.left).toBeGreaterThanOrEqual(STAGE.left)
    expect(box.right).toBeLessThanOrEqual(STAGE.right)
    // …and pinned to the bottom band (baseY = bottom - size - 24, ±10 wobble).
    expect(box.top).toBeGreaterThan(STAGE.bottom - SIZE - 40)
    expect(box.top).toBeLessThan(STAGE.bottom - SIZE)
  })

  it('never overlaps a [data-jarvis-avoid] rect', () => {
    stubbed('div', 'data-jarvis-avoid', CHAT)
    render(<JarvisRobotAvatar {...defaultProps} />)

    // Sample across many roam targets — the old build only avoided a hard-coded
    // 88px clearance and walked straight over the 380px chat panel.
    for (let i = 0; i < 40; i++) {
      runFrames(500)
      expect(overlaps(robotBox(), CHAT)).toBe(false)
    }
  })

  it('escapes a rect that appears on top of it', () => {
    render(<JarvisRobotAvatar {...defaultProps} />)
    runFrames(1000)

    // Drop an avoid rect right where the robot is standing — the chat panel
    // opening underneath it.
    const here = robotBox()
    stubbed('div', 'data-jarvis-avoid', {
      left: here.left - 20, top: here.top - 20,
      right: here.right + 20, bottom: here.bottom + 20,
    })
    runFrames(3000)

    expect(overlaps(robotBox(), {
      left: here.left - 20, top: here.top - 20,
      right: here.right + 20, bottom: here.bottom + 20,
    })).toBe(false)
  })

  it('respects a dragged placement instead of sliding straight back', () => {
    render(<JarvisRobotAvatar {...defaultProps} />)
    runFrames(1000)
    const el = screen.getByTestId('jarvis-robot')
    const start = robotBox()

    fireEvent.pointerDown(el, { pointerId: 1, clientX: start.x, clientY: start.y })
    fireEvent.pointerMove(el, { pointerId: 1, clientX: 400, clientY: start.y })
    fireEvent.pointerUp(el, { pointerId: 1, clientX: 400, clientY: start.y })

    const dropped = robotBox().x
    expect(dropped).toBeCloseTo(400, 0)

    // Parked: still there a couple of seconds later.
    runFrames(2000)
    expect(robotBox().x).toBeCloseTo(dropped, 0)
  })

  it('treats a press with no movement as a click', () => {
    const onClick = vi.fn()
    render(<JarvisRobotAvatar {...defaultProps} onClick={onClick} />)
    runFrames(500)
    const el = screen.getByTestId('jarvis-robot')
    const at = robotBox()

    fireEvent.pointerDown(el, { pointerId: 1, clientX: at.x, clientY: at.y })
    fireEvent.pointerUp(el, { pointerId: 1, clientX: at.x, clientY: at.y })

    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('keeps moving after a cancelled pointer', () => {
    render(<JarvisRobotAvatar {...defaultProps} />)
    runFrames(1000)
    const el = screen.getByTestId('jarvis-robot')

    fireEvent.pointerDown(el, { pointerId: 1, clientX: 500, clientY: 900 })
    fireEvent.pointerMove(el, { pointerId: 1, clientX: 600, clientY: 900 })
    // A browser gesture steals the pointer. This used to leave the drag flag
    // set forever and the robot frozen for the rest of the session.
    fireEvent.pointerCancel(el, { pointerId: 1 })

    const parked = robotBox().x
    runFrames(20_000)   // past PARK_MS, so roaming must have resumed
    expect(robotBox().x).not.toBeCloseTo(parked, 0)
  })

  it('does not animate transform on the wrapper', () => {
    // The emerge keyframes animate `transform`; run on the wrapper they beat the
    // inline transform the rAF loop writes and teleport the robot to (0,0).
    render(<JarvisRobotAvatar {...defaultProps} state={'talking' as RobotState} />)
    runFrames(500)
    expect(screen.getByTestId('jarvis-robot').style.animation).toBe('')
  })
})
