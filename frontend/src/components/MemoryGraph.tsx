/**
 * MemoryGraph — Force-directed memory tree visualization.
 *
 * Replicates the OpenHuman Brain > Graph view:
 *  - Multiple tree clusters scattered on a dark canvas
 *  - Source (orange), L1 (blue), L2 (cyan), Document (gray) nodes
 *  - Dotted connecting lines
 *  - Physics-simulated layout that settles into tree clusters
 *  - Pan / hover interactions
 */
'use client'
import { useEffect, useRef, useState, useCallback } from 'react'

// ── Types ──────────────────────────────────────────────────────────────────

type NodeType = 'source' | 'l1' | 'l2' | 'document'

interface GNode {
  id: string
  x: number; y: number; vx: number; vy: number
  type: NodeType
  cluster: number
  label: string
}

interface GEdge { source: string; target: string }

export interface MemoryGraphData {
  nodes: { id: string; type: NodeType; label: string }[]
  edges: { source: string; target: string }[]
}

interface Props {
  data?: MemoryGraphData
  height?: number
}

// ── Constants ──────────────────────────────────────────────────────────────

const COLORS: Record<NodeType, string> = {
  source:   '#f97316',  // orange
  l1:       '#3b82f6',  // blue
  l2:       '#06b6d4',  // cyan
  document: '#6b7280',  // gray
}
const RADII: Record<NodeType, number> = { source: 7, l1: 5, l2: 4, document: 3 }

// ── Graph generator ────────────────────────────────────────────────────────

function buildCluster(cx: number, cy: number, id: number, spread: number): { nodes: GNode[]; edges: GEdge[] } {
  const nodes: GNode[] = []
  const edges: GEdge[] = []
  const rng = () => Math.random()

  const root: GNode = { id: `c${id}_root`, x: cx, y: cy, vx: 0, vy: 0, type: 'source', cluster: id, label: `Source ${id}` }
  nodes.push(root)

  const nL1 = 2 + Math.floor(rng() * 4)
  for (let i = 0; i < nL1; i++) {
    const ang = (i / nL1) * Math.PI * 2
    const d = spread * (0.5 + rng() * 0.5)
    const lx = cx + Math.cos(ang) * d, ly = cy + Math.sin(ang) * d
    const l1Id = `c${id}_l1_${i}`
    nodes.push({ id: l1Id, x: lx, y: ly, vx: 0, vy: 0, type: 'l1', cluster: id, label: `L1.${i}` })
    edges.push({ source: root.id, target: l1Id })

    const nL2 = 1 + Math.floor(rng() * 3)
    for (let j = 0; j < nL2; j++) {
      const a2 = ang + (j - nL2 / 2) * 0.6
      const l2x = lx + Math.cos(a2) * spread * 0.38
      const l2y = ly + Math.sin(a2) * spread * 0.38
      const l2Id = `c${id}_l2_${i}_${j}`
      nodes.push({ id: l2Id, x: l2x, y: l2y, vx: 0, vy: 0, type: 'l2', cluster: id, label: `L2.${j}` })
      edges.push({ source: l1Id, target: l2Id })

      if (rng() > 0.4) {
        const nDoc = 1 + Math.floor(rng() * 2)
        for (let k = 0; k < nDoc; k++) {
          const dx = l2x + (rng() - 0.5) * spread * 0.3
          const dy = l2y + (rng() - 0.5) * spread * 0.3
          const dId = `c${id}_doc_${i}_${j}_${k}`
          nodes.push({ id: dId, x: dx, y: dy, vx: 0, vy: 0, type: 'document', cluster: id, label: 'Doc' })
          edges.push({ source: l2Id, target: dId })
        }
      }
    }
  }
  return { nodes, edges }
}

function generateData(W: number, H: number) {
  const positions = [
    [0.18, 0.25], [0.55, 0.18], [0.82, 0.30],
    [0.12, 0.62], [0.42, 0.72], [0.72, 0.68],
    [0.36, 0.44], [0.65, 0.48],
  ]
  const allN: GNode[] = [], allE: GEdge[] = []
  for (let i = 0; i < positions.length; i++) {
    const [px, py] = positions[i]
    const spread = 45 + Math.random() * 30
    const { nodes, edges } = buildCluster(px * W, py * H, i, spread)
    allN.push(...nodes); allE.push(...edges)
  }
  return { nodes: allN, edges: allE }
}

// ── Component ──────────────────────────────────────────────────────────────

export default function MemoryGraph({ height = 500 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef   = useRef<number | undefined>(undefined)
  const nodesRef = useRef<GNode[]>([])
  const edgesRef = useRef<GEdge[]>([])
  const mapRef   = useRef<Map<string, GNode>>(new Map())
  const tickRef  = useRef(0)
  const [stats, setStats] = useState({ nodes: 0, edges: 0 })
  const [hovered, setHovered] = useState<string | null>(null)

  const init = useCallback(() => {
    const c = canvasRef.current; if (!c) return
    const { nodes, edges } = generateData(c.offsetWidth || 800, height)
    nodesRef.current = nodes
    edgesRef.current = edges
    mapRef.current = new Map(nodes.map(n => [n.id, n]))
    setStats({ nodes: nodes.length, edges: edges.length })
  }, [height])

  useEffect(() => { init() }, [init])

  // ── Canvas render loop ───────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return
    const ctx = canvas.getContext('2d'); if (!ctx) return

    const resize = () => {
      canvas.width  = canvas.offsetWidth  || 800
      canvas.height = canvas.offsetHeight || height
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)

    const tick = () => {
      tickRef.current++
      const nodes = nodesRef.current
      const edges = edgesRef.current
      const map   = mapRef.current
      const W = canvas.width, H = canvas.height

      // ── Physics ──────────────────────────────────────────────────────────
      // Repulsion (within same cluster only, for performance)
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j]
          if (a.cluster !== b.cluster) continue
          const dx = a.x - b.x, dy = a.y - b.y
          const d2 = dx * dx + dy * dy || 1
          if (d2 < 90 * 90) {
            const f = 25 / d2
            a.vx += dx * f; a.vy += dy * f
            b.vx -= dx * f; b.vy -= dy * f
          }
        }
      }
      // Spring attraction along edges
      for (const e of edges) {
        const s = map.get(e.source), t = map.get(e.target)
        if (!s || !t) continue
        const dx = t.x - s.x, dy = t.y - s.y
        const d = Math.sqrt(dx * dx + dy * dy) || 1
        const target = s.type === 'source' ? 60 : 38
        const f = (d - target) * 0.018 / d
        s.vx += dx * f; s.vy += dy * f
        t.vx -= dx * f; t.vy -= dy * f
      }
      // Integrate
      for (const n of nodes) {
        n.vx *= 0.88; n.vy *= 0.88
        n.x = Math.max(12, Math.min(W - 12, n.x + n.vx))
        n.y = Math.max(12, Math.min(H - 12, n.y + n.vy))
      }

      // ── Render ───────────────────────────────────────────────────────────
      ctx.clearRect(0, 0, W, H)
      ctx.fillStyle = '#06060f'
      ctx.fillRect(0, 0, W, H)

      // Edges (dotted)
      ctx.save()
      ctx.strokeStyle = 'rgba(120, 145, 200, 0.25)'
      ctx.lineWidth = 0.6
      ctx.setLineDash([2, 3])
      for (const e of edges) {
        const s = map.get(e.source), t = map.get(e.target)
        if (!s || !t) continue
        ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y); ctx.stroke()
      }
      ctx.restore()

      // Nodes
      for (const n of nodes) {
        const col = COLORS[n.type]
        const r   = RADII[n.type]
        const isH = n.id === hovered

        // Glow
        const gr = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * (isH ? 5 : 3.5))
        gr.addColorStop(0, col + (isH ? 'aa' : '50'))
        gr.addColorStop(1, 'transparent')
        ctx.fillStyle = gr
        ctx.beginPath(); ctx.arc(n.x, n.y, r * (isH ? 5 : 3.5), 0, Math.PI * 2); ctx.fill()

        // Core
        ctx.fillStyle = isH ? '#ffffff' : col
        ctx.beginPath(); ctx.arc(n.x, n.y, r * (isH ? 1.3 : 1), 0, Math.PI * 2); ctx.fill()
      }

      rafRef.current = requestAnimationFrame(tick)
    }

    rafRef.current = requestAnimationFrame(tick)
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      ro.disconnect()
    }
  }, [height, hovered])

  // ── Mouse hover ──────────────────────────────────────────────────────────
  const onMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const c = canvasRef.current; if (!c) return
    const rect = c.getBoundingClientRect()
    const mx = (e.clientX - rect.left) * (c.width / rect.width)
    const my = (e.clientY - rect.top)  * (c.height / rect.height)
    let closest: GNode | null = null
    let minD = 16
    for (const n of nodesRef.current) {
      const d = Math.hypot(n.x - mx, n.y - my)
      if (d < minD) { minD = d; closest = n }
    }
    setHovered(closest?.id ?? null)
  }

  return (
    <div className="relative w-full" style={{ height }}>
      {/* Stats */}
      <div className="absolute top-2 left-3 flex items-center gap-4 text-xs text-gray-400 z-10 pointer-events-none">
        <span>
          <span className="text-white font-semibold">{stats.nodes}</span> nodes
        </span>
        <span>
          <span className="text-white font-semibold">{stats.edges}</span> parent-child links
        </span>
      </div>
      {/* Legend */}
      <div className="absolute top-2 right-3 flex items-center gap-3 text-xs z-10 pointer-events-none">
        {(['source','l1','l2','document'] as NodeType[]).map(t => (
          <span key={t} className="flex items-center gap-1 text-gray-400">
            <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: COLORS[t] }} />
            <span className="capitalize">{t === 'l1' ? 'L1' : t === 'l2' ? 'L2' : t.charAt(0).toUpperCase() + t.slice(1)}</span>
          </span>
        ))}
        <button
          className="text-gray-500 hover:text-white transition-colors text-xs ml-1"
          onClick={() => init()}
        >
          Reset view
        </button>
      </div>
      {/* Canvas */}
      <canvas
        ref={canvasRef}
        className="w-full rounded-xl"
        style={{ height }}
        onMouseMove={onMouseMove}
        onMouseLeave={() => setHovered(null)}
      />
    </div>
  )
}
