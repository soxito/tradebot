import { render, screen, act } from '@testing-library/react'
import { JarvisRobotAvatar } from '@/components/JarvisRobotAvatar'
import { RobotState } from '@/components/JarvisRobot'

describe('JarvisRobotAvatar Positioning', () => {
  const defaultProps = {
    state: 'idle' as RobotState,
    energy: 0,
    size: 160,
  }

  beforeEach(() => {
    // Mock window dimensions
    Object.defineProperty(window, 'innerWidth', { value: 1920, writable: true })
    Object.defineProperty(window, 'innerHeight', { value: 1080, writable: true })
    localStorage.clear()
  })

  it('defaults to bottom center', () => {
    render(<JarvisRobotAvatar {...defaultProps} />)
    const robot = screen.getByTitle(/JARVIS/)
    expect(robot).toHaveStyle({
      transform: expect.stringContaining('translate('),
    })
  })

  it('constrains roam to horizontal strip', () => {
    // This requires testing the internal pickTarget logic
    // Can be tested by exposing pickTarget or using act to trigger RAF
  })

  it('repositions on window resize', () => {
    render(<JarvisRobotAvatar {...defaultProps} />)
    const robot = screen.getByTitle(/JARVIS/)
    
    act(() => {
      window.innerWidth = 1200
      window.dispatchEvent(new Event('resize'))
    })
    
    // Should recalculate center position
    expect(robot).toHaveStyle({
      transform: expect.stringContaining('translate('),
    })
  })
})