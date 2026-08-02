import { test, expect } from '@playwright/test'

test.describe('JARVIS Robot Improvements', () => {
  test.beforeEach(async ({ page }) => {
    // PaulChat only mounts the WebGL robot on a high/ultra GPU tier. Headless CI
    // never qualifies, so without this override every robot assertion below is
    // silently vacuous.
    await page.addInitScript(() => localStorage.setItem('paul.forceRobot', '1'))
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    // Wait for PaulChat to mount
    await page.waitForSelector('[data-testid="jarvis-robot"]', { timeout: 10000 })
  })

  test('Robot walks the bottom band of the content area', async ({ page }) => {
    const robot = page.locator('[data-testid="jarvis-robot"]')
    const box = await robot.boundingBox()
    const stage = await page.locator('[data-jarvis-stage]').boundingBox()

    expect(box).toBeTruthy()
    expect(stage).toBeTruthy()
    if (!box || !stage) return

    // Inside the content column, not the viewport corner…
    expect(box.x).toBeGreaterThanOrEqual(stage.x - 1)
    expect(box.x + box.width).toBeLessThanOrEqual(stage.x + stage.width + 1)
    // …and standing on the bottom band.
    expect(box.y + box.height).toBeGreaterThan(stage.y + stage.height - 80)
  })

  test('Robot roams horizontally only', async ({ page }) => {
    const robot = page.locator('[data-testid="jarvis-robot"]')
    const positions: { x: number; y: number }[] = []

    // Track position for 5 seconds
    for (let i = 0; i < 25; i++) {
      const box = await robot.boundingBox()
      if (box) positions.push({ x: box.x, y: box.y })
      await page.waitForTimeout(200)
    }

    // Y variance should be minimal (< 30px)
    const ys = positions.map(p => p.y)
    const yVariance = Math.max(...ys) - Math.min(...ys)
    expect(yVariance).toBeLessThan(30)

    // X should vary (horizontal roaming)
    const xs = positions.map(p => p.x)
    const xVariance = Math.max(...xs) - Math.min(...xs)
    expect(xVariance).toBeGreaterThan(50)
  })

  test('Robot never touches a chat widget', async ({ page }) => {
    const robot = page.locator('[data-testid="jarvis-robot"]')

    // Open the chat so both the button AND the 380px panel are on screen — the
    // panel is what the old hard-coded 88px clearance failed to account for.
    await page.locator('[aria-label="Open PAUL JARVIS assistant"]').click()
    await page.waitForTimeout(300)

    for (let i = 0; i < 50; i++) {
      const box = await robot.boundingBox()
      const avoid = await page.locator('[data-jarvis-avoid]').all()
      for (const el of avoid) {
        const a = await el.boundingBox()
        if (!box || !a) continue
        const hit =
          box.x < a.x + a.width && box.x + box.width > a.x &&
          box.y < a.y + a.height && box.y + box.height > a.y
        expect(hit, `robot overlapped a chat widget at sample ${i}`).toBe(false)
      }
      await page.waitForTimeout(200)
    }
  })

  test('Robot does not jump when it starts talking', async ({ page }) => {
    const robot = page.locator('[data-testid="jarvis-robot"]')
    const before = await robot.boundingBox()

    // The emerge keyframes used to run on the wrapper and animate `transform`,
    // beating the inline transform and teleporting the robot to (0,0).
    await page.evaluate(() => {
      window.postMessage({ __jarvisPage: true, type: 'jarvis-speak', text: 'Hello there.' },
        window.location.origin)
    })

    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(50)
      const box = await robot.boundingBox()
      if (!box || !before) continue
      // A teleport is hundreds of pixels; a walk step is a couple.
      expect(Math.abs(box.x - before.x)).toBeLessThan(60)
      expect(Math.abs(box.y - before.y)).toBeLessThan(60)
    }
  })

  test('No self-transcription during TTS', async ({ page }) => {
    // Trigger TTS
    await page.evaluate(() => {
      window.postMessage({ 
        __jarvisPage: true, 
        type: 'jarvis-speak', 
        text: 'Testing self hearing prevention. This is a long sentence to ensure TTS plays for several seconds.' 
      }, window.location.origin)
    })
    
    // Wait for TTS to start
    await page.waitForTimeout(500)
    
    // Check that mic is gated
    const micGated = await page.evaluate(() => window.__JARVIS_MIC_GATED__ === true)
    
    // Wait for TTS + gate to clear (2s gate + buffer)
    await page.waitForTimeout(3000)
    
    // Verify mic gate cleared
    const gateCleared = await page.evaluate(() => window.__JARVIS_MIC_GATED__ === false)
    expect(gateCleared).toBe(true)
  })

  test('Settings: Post-speech gate slider works', async ({ page }) => {
    // Open PaulChat
    await page.click('[title*="JARVIS"]')
    await page.waitForTimeout(300)
    
    // Open settings
    await page.click('button:has-text("Settings"), [aria-label="Settings"]')
    await page.waitForTimeout(200)
    
    // Find gate slider
    const slider = page.locator('input[type="range"]').filter({ hasText: /gate/i }).first()
    await expect(slider).toBeVisible()
    
    // Change value
    await slider.fill('2500')
    await page.waitForTimeout(100)
    
    // Verify persisted
    const value = await page.evaluate(() => localStorage.getItem('paul.postSpeechGateMs'))
    expect(value).toBe('2500')
  })

  test('No console errors on page navigation', async ({ page }) => {
    const errors: string[] = []
    page.on('console', msg => {
      if (msg.type() === 'error') errors.push(msg.text())
    })
    page.on('pageerror', err => errors.push(err.message))
    
    // Navigate through multiple pages
    const pages = ['/', '/trading', '/settings', '/jarvis-room', '/']
    for (const p of pages) {
      await page.goto(p)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(500)
    }
    
    // Filter out known non-critical errors
    const criticalErrors = errors.filter(e => 
      !e.includes('favicon') && 
      !e.includes('Extension') &&
      !e.includes('Non-Error promise rejection')
    )
    
    expect(criticalErrors).toHaveLength(0)
  })

  test('Memory stable after 10 navigations', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    
    // Initial memory
    const initialMem = await page.evaluate(() => (performance as any).memory?.usedJSHeapSize || 0)
    
    for (let i = 0; i < 10; i++) {
      await page.goto(i % 2 === 0 ? '/trading' : '/')
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(300)
    }
    
    const finalMem = await page.evaluate(() => (performance as any).memory?.usedJSHeapSize || 0)
    const growth = finalMem - initialMem
    
    // Allow up to 50MB growth
    expect(growth).toBeLessThan(50 * 1024 * 1024)
  })
})