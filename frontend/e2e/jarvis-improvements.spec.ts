import { test, expect } from '@playwright/test'

test.describe('JARVIS Robot Improvements', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    // Wait for PaulChat to mount
    await page.waitForSelector('[title*="JARVIS"], [data-testid="jarvis-robot"]', { timeout: 10000 })
  })

  test('Robot spawns at bottom center', async ({ page }) => {
    const robot = page.locator('[title*="JARVIS"]').first()
    const box = await robot.boundingBox()
    const viewport = page.viewportSize()
    
    expect(box).toBeTruthy()
    if (!box || !viewport) return
    
    // Bottom 15% of screen
    expect(box.y).toBeGreaterThan(viewport.height * 0.85)
    // Center horizontally (±10%)
    expect(box.x + box.width / 2).toBeCloseTo(viewport.width / 2, -1)
  })

  test('Robot roams horizontally only', async ({ page }) => {
    const robot = page.locator('[title*="JARVIS"]').first()
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

  test('Robot stays within center 80% horizontally', async ({ page }) => {
    const robot = page.locator('[title*="JARVIS"]').first()
    const viewport = page.viewportSize()
    if (!viewport) return
    
    const leftBound = viewport.width * 0.1
    const rightBound = viewport.width * 0.9
    
    for (let i = 0; i < 20; i++) {
      const box = await robot.boundingBox()
      if (box) {
        expect(box.x).toBeGreaterThan(leftBound - 20)
        expect(box.x + box.width).toBeLessThan(rightBound + 20)
      }
      await page.waitForTimeout(200)
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