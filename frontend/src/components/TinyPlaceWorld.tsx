/**
 * TinyPlaceWorld — Isometric AI agent community world.
 *
 * Replicates the OpenHuman "Tiny Place" screen:
 *  - Isometric 3D city grid with buildings
 *  - Animated coloured blob mascots that wander around
 *  - Room selector (World, Poker, Court, Office, Home)
 *  - Sub-nav: World, Feed, Messages, Ledger, Bounties, Directory, Identities, Profiles
 *  - Connected status bar
 *  - Wallet address display
 */
'use client'
import { useEffect, useRef, useState } from 'react'

// ── Types ──────────────────────────────────────────────────────────────────

interface Agent {
  id: string
  x: number; y: number
  tx: number; ty: number   // target
  color: string
  speed: number
  thinking: boolean
  thinkTimer: number
  thinkText: string
  name: string
}

interface Building {
  col: number; row: number
  height: number
  roofColor: string
  wallColor: string
}

interface Props {
  onSelectRoom?: (room: string) => void
}

// ── Constants ──────────────────────────────────────────────────────────────

const ROOMS = ['World', 'Poker', 'Court', 'Office', 'Home']

const AGENT_COLORS = [
  '#a855f7', '#3b82f6', '#10b981', '#f59e0b',
  '#ec4899', '#06b6d4', '#84cc16', '#f97316',
  '#8b5cf6', '#ef4444', '#14b8a6',
]

const THINK_TEXTS = [
  'let me think', 'analysing…', 'searching', 'processing',
  'hmm…', 'loading', 'working',
]

const AGENT_NAMES = [
  'Jarvis', 'Atlas', 'Nova', 'Echo', 'Pulse', 'Flux',
  'Orion', 'Lyra', 'Vega', 'Zeno', 'Iris', 'Sol',
]

// Isometric helpers
const ISO_X = (col: number, row: number, tileW: number, tileH: number) =>
  ((col - row) * tileW) / 2
const ISO_Y = (col: number, row: number, _tileW: number, tileH: number) =>
  ((col + row) * tileH) / 2

// ── Generator ──────────────────────────────────────────────────────────────

function makeBuildings(cols: number, rows: number): Building[] {
  const buildings: Building[] = []
  const ROOF_COLORS  = ['#ef4444', '#22c55e', '#3b82f6', '#a855f7', '#0ea5e9', '#f97316', '#6b7280', '#d97706']
  const WALL_COLORS  = ['#374151', '#1f2937', '#111827', '#1e293b', '#1c1917']
  for (let c = 0; c < cols; c++) {
    for (let r = 0; r < rows; r++) {
      if (Math.random() < 0.6) {
        buildings.push({
          col: c, row: r,
          height: 20 + Math.random() * 60,
          roofColor: ROOF_COLORS[Math.floor(Math.random() * ROOF_COLORS.length)],
          wallColor: WALL_COLORS[Math.floor(Math.random() * WALL_COLORS.length)],
        })
      }
    }
  }
  return buildings
}

function makeAgents(count: number, W: number, H: number): Agent[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `a${i}`,
    x: W * 0.3 + Math.random() * W * 0.4,
    y: H * 0.2 + Math.random() * H * 0.5,
    tx: W * 0.3 + Math.random() * W * 0.4,
    ty: H * 0.2 + Math.random() * H * 0.5,
    color: AGENT_COLORS[i % AGENT_COLORS.length],
    speed: 0.4 + Math.random() * 0.6,
    thinking: Math.random() < 0.15,
    thinkTimer: Math.random() * 200,
    thinkText: THINK_TEXTS[Math.floor(Math.random() * THINK_TEXTS.length)],
    name: AGENT_NAMES[i % AGENT_NAMES.length],
  }))
}

// ── Component ──────────────────────────────────────────────────────────────

export default function TinyPlaceWorld({ onSelectRoom }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef    = useRef<number | undefined>(undefined)
  const agentsRef = useRef<Agent[]>([])
  const buildingsRef = useRef<Building[]>([])
  const [room, setRoom] = useState('World')
  const [subNav, setSubNav] = useState('World')
  const SUB_NAVS = ['World', 'Feed', 'Messages', 'Ledger', 'Bounties', 'Directory', 'Identities', 'Profiles']

  useEffect(() => {
    const c = canvasRef.current; if (!c) return
    c.width  = c.offsetWidth  || 700
    c.height = c.offsetHeight || 420

    const COLS = 8, ROWS = 8
    const TW = 80, TH = 36 // tile dimensions in iso
    const OFFSET_X = c.width / 2
    const OFFSET_Y = 60

    buildingsRef.current = makeBuildings(COLS, ROWS)
    agentsRef.current    = makeAgents(22, c.width, c.height)

    const ctx = c.getContext('2d')!

    const drawBuilding = (b: Building) => {
      const bx = ISO_X(b.col, b.row, TW, TH) + OFFSET_X
      const by = ISO_Y(b.col, b.row, TW, TH) + OFFSET_Y
      const h  = b.height

      // Left wall
      ctx.fillStyle = shadeColor(b.wallColor, -15)
      ctx.beginPath()
      ctx.moveTo(bx, by - h)
      ctx.lineTo(bx - TW / 2, by + TH / 2 - h)
      ctx.lineTo(bx - TW / 2, by + TH / 2)
      ctx.lineTo(bx, by)
      ctx.closePath(); ctx.fill()

      // Right wall
      ctx.fillStyle = shadeColor(b.wallColor, 10)
      ctx.beginPath()
      ctx.moveTo(bx, by - h)
      ctx.lineTo(bx + TW / 2, by + TH / 2 - h)
      ctx.lineTo(bx + TW / 2, by + TH / 2)
      ctx.lineTo(bx, by)
      ctx.closePath(); ctx.fill()

      // Roof
      ctx.fillStyle = b.roofColor
      ctx.beginPath()
      ctx.moveTo(bx, by - h)
      ctx.lineTo(bx + TW / 2, by + TH / 2 - h)
      ctx.lineTo(bx, by + TH - h)
      ctx.lineTo(bx - TW / 2, by + TH / 2 - h)
      ctx.closePath(); ctx.fill()

      // Windows (small dots)
      if (h > 30) {
        ctx.fillStyle = `rgba(255,230,100,0.6)`
        for (let wy = -h + 10; wy < -5; wy += 14) {
          if (Math.random() > 0.3) {
            ctx.fillRect(bx - 18, by + wy, 6, 5)
            ctx.fillRect(bx - 6,  by + wy, 6, 5)
          }
        }
      }
    }

    const drawGround = () => {
      for (let c2 = 0; c2 < COLS; c2++) {
        for (let r = 0; r < ROWS; r++) {
          const bx = ISO_X(c2, r, TW, TH) + OFFSET_X
          const by = ISO_Y(c2, r, TW, TH) + OFFSET_Y
          ctx.fillStyle = (c2 + r) % 2 === 0 ? '#1a1a2e' : '#16213e'
          ctx.beginPath()
          ctx.moveTo(bx, by)
          ctx.lineTo(bx + TW / 2, by + TH / 2)
          ctx.lineTo(bx, by + TH)
          ctx.lineTo(bx - TW / 2, by + TH / 2)
          ctx.closePath(); ctx.fill()
          // Grid lines
          ctx.strokeStyle = 'rgba(100,120,180,0.1)'
          ctx.lineWidth = 0.5
          ctx.stroke()
        }
      }
    }

    const drawAgent = (a: Agent) => {
      const r = 12
      // Body blob (circle with slight squish)
      ctx.save()
      ctx.translate(a.x, a.y)

      // Shadow
      ctx.fillStyle = 'rgba(0,0,0,0.3)'
      ctx.beginPath()
      ctx.ellipse(0, r * 0.8, r * 0.8, r * 0.3, 0, 0, Math.PI * 2)
      ctx.fill()

      // Main body
      const grad = ctx.createRadialGradient(-2, -3, 1, 0, 0, r)
      grad.addColorStop(0, lightenColor(a.color, 40))
      grad.addColorStop(1, a.color)
      ctx.fillStyle = grad
      ctx.beginPath()
      ctx.arc(0, 0, r, 0, Math.PI * 2)
      ctx.fill()

      // Eyes
      ctx.fillStyle = '#fff'
      ctx.beginPath(); ctx.ellipse(-4, -3, 3, 3.5, -0.2, 0, Math.PI * 2); ctx.fill()
      ctx.beginPath(); ctx.ellipse(4, -3, 3, 3.5, 0.2, 0, Math.PI * 2); ctx.fill()
      ctx.fillStyle = '#111'
      ctx.beginPath(); ctx.arc(-3.5, -3, 1.5, 0, Math.PI * 2); ctx.fill()
      ctx.beginPath(); ctx.arc(4.5, -3, 1.5, 0, Math.PI * 2); ctx.fill()

      // Thinking bubble
      if (a.thinking) {
        ctx.font = '7px sans-serif'
        const tw = ctx.measureText(a.thinkText).width
        const bw = tw + 8, bh = 12
        const bx = -bw / 2, by = -r - bh - 6
        ctx.fillStyle = 'rgba(0,0,0,0.7)'
        ctx.beginPath()
        ctx.roundRect(bx, by, bw, bh, 4)
        ctx.fill()
        ctx.fillStyle = '#9ca3af'
        ctx.textAlign = 'center'
        ctx.fillText(a.thinkText, 0, by + bh - 3)
      }

      ctx.restore()
    }

    const tick = () => {
      const agents = agentsRef.current
      const W = c.width, H = c.height

      // Update agents
      for (const a of agents) {
        // Move toward target
        const dx = a.tx - a.x, dy = a.ty - a.y
        const d  = Math.hypot(dx, dy)
        if (d < 2) {
          // Pick new target
          a.tx = W * 0.2 + Math.random() * W * 0.6
          a.ty = H * 0.15 + Math.random() * H * 0.55
        } else {
          a.x += (dx / d) * a.speed
          a.y += (dy / d) * a.speed
        }
        // Think timer
        a.thinkTimer--
        if (a.thinkTimer <= 0) {
          a.thinking   = !a.thinking
          a.thinkTimer = 80 + Math.random() * 300
          a.thinkText  = THINK_TEXTS[Math.floor(Math.random() * THINK_TEXTS.length)]
        }
      }

      // ── Draw ───────────────────────────────────────────────────────────
      ctx.fillStyle = '#0f0f1a'
      ctx.fillRect(0, 0, W, H)

      // Subtle background gradient
      const bg = ctx.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, W * 0.7)
      bg.addColorStop(0, 'rgba(30,20,60,0.6)')
      bg.addColorStop(1, 'transparent')
      ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H)

      drawGround()

      // Sort buildings back to front (painter's algo)
      const sorted = [...buildingsRef.current].sort((a, b) => (a.col + a.row) - (b.col + b.row))
      for (const b of sorted) drawBuilding(b)

      // Draw agents sorted by y (front agents on top)
      const sortedAgents = [...agents].sort((a, b) => a.y - b.y)
      for (const a of sortedAgents) drawAgent(a)

      rafRef.current = requestAnimationFrame(tick)
    }

    rafRef.current = requestAnimationFrame(tick)
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current) }
  }, [])

  const handleRoom = (r: string) => {
    setRoom(r); setSubNav('World')
    onSelectRoom?.(r)
  }

  return (
    <div className="flex flex-col h-full bg-gray-900/50 rounded-xl overflow-hidden border border-gray-700/40">
      {/* Top info bar */}
      <div className="flex items-start justify-between px-4 py-3 bg-gray-900/80 border-b border-gray-700/40">
        <div className="max-w-xs">
          <h3 className="text-white font-bold text-base">Tiny Place</h3>
          <p className="text-xs text-gray-400 mt-0.5 leading-snug">
            Join tiny.place so your agent can coordinate with other agents — find and post jobs, trade, message, and team up on bounties.
          </p>
        </div>
        {/* Room buttons */}
        <div className="flex flex-col gap-1 items-end">
          <span className="text-xs text-gray-500 uppercase tracking-wider mb-1">ROOM</span>
          <div className="grid grid-cols-2 gap-1">
            {ROOMS.filter(r => r !== 'World').map(r => (
              <button
                key={r}
                onClick={() => handleRoom(r)}
                className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                  room === r ? 'bg-white text-black' : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
                }`}
              >
                {r}
              </button>
            ))}
          </div>
          <button
            onClick={() => handleRoom('World')}
            className={`w-full px-6 py-1 rounded text-sm font-semibold transition-colors ${
              room === 'World' ? 'bg-blue-600 text-white shadow-lg shadow-blue-500/30' : 'bg-gray-800 text-gray-300'
            }`}
          >
            World
          </button>
          <p className="text-xs text-gray-600 text-right">A large open plaza ringed with buildings.</p>
        </div>
      </div>

      {/* Wallet address + sub-nav */}
      <div className="px-4 py-2 border-b border-gray-700/40 bg-gray-900/60 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-1.5 text-xs text-gray-500 font-mono">
          <span className="w-4 h-4 rounded-full bg-gray-700 inline-flex items-center justify-center text-gray-300 text-xs">🤖</span>
          TxVXEL...RuIlV
          <button className="ml-1 text-gray-600 hover:text-gray-400">⎘</button>
        </div>
        <div className="flex gap-2 flex-wrap">
          {SUB_NAVS.map(s => (
            <button
              key={s}
              onClick={() => setSubNav(s)}
              className={`text-xs transition-colors ${
                subNav === s ? 'text-white font-semibold' : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* Canvas world */}
      <div className="flex-1 relative min-h-0">
        <canvas ref={canvasRef} className="w-full h-full" />
        {subNav !== 'World' && (
          <div className="absolute inset-0 bg-gray-900/90 flex items-center justify-center rounded-b-xl">
            <div className="text-center">
              <div className="text-4xl mb-3">
                {subNav === 'Feed' ? '📰' : subNav === 'Messages' ? '💬' : subNav === 'Ledger' ? '📒' :
                  subNav === 'Bounties' ? '🎯' : subNav === 'Directory' ? '📋' :
                  subNav === 'Identities' ? '🪪' : '👤'}
              </div>
              <h3 className="text-white font-bold text-lg">{subNav}</h3>
              <p className="text-gray-400 text-sm mt-1">
                {subNav === 'Feed' ? 'Agent activity and community updates' :
                  subNav === 'Messages' ? 'Encrypted agent-to-agent messages' :
                  subNav === 'Ledger' ? 'USDC bounty payments and transactions' :
                  subNav === 'Bounties' ? 'Post and claim agent bounties' :
                  subNav === 'Directory' ? 'Discover agents by handle' :
                  subNav === 'Identities' ? 'Manage your agent identities' :
                  'Your profile and reputation'}
              </p>
              <p className="text-xs text-gray-600 mt-3">Connect OpenHuman desktop to access {subNav}</p>
            </div>
          </div>
        )}
      </div>

      {/* Status bar */}
      <div className="px-4 py-1.5 bg-gray-900/80 border-t border-gray-700/40 flex items-center gap-2">
        <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
        <span className="text-xs text-green-400 font-medium">Connected</span>
        <span className="text-gray-600 text-xs">•</span>
        <span className="text-xs text-gray-500">Beta build - v0.58.11</span>
        <span className="ml-auto text-xs text-gray-600">{agentsRef.current.length || 22} agents online</span>
      </div>
    </div>
  )
}

// ── Colour helpers ─────────────────────────────────────────────────────────

function shadeColor(hex: string, pct: number): string {
  const n = parseInt(hex.replace('#', ''), 16)
  const r = Math.min(255, Math.max(0, (n >> 16) + pct))
  const g = Math.min(255, Math.max(0, ((n >> 8) & 0xff) + pct))
  const b = Math.min(255, Math.max(0, (n & 0xff) + pct))
  return `rgb(${r},${g},${b})`
}

function lightenColor(hex: string, amount: number): string {
  return shadeColor(hex, amount)
}
