/**
 * Intelligence — 2D/3D Brain Map + Graphify knowledge visualization
 *
 * Shows all 3500+ code/knowledge nodes as a live force-directed graph.
 * Nodes are coloured by community; DB entity nodes are highlighted orange.
 * Active nodes (queried by agents in last 90 s) pulse amber.
 * Click a node → details panel on the right.
 * Toggle 2D ↔ 3D with the toolbar button.
 */
import Head from 'next/head'
import Link from 'next/link'
import { useCallback, useEffect, useRef, useState, useMemo } from 'react'
import { apiClient } from '@/services/api'
import { threeDisabled, pollMultiplier } from '@/utils/devicePerformance'
import {
  Network, Sparkles, Search, Brain, Trash2, RefreshCw, Gauge,
  Layers, X, Save, Check, Maximize2, Minimize2, PanelRightClose, PanelRightOpen,
  BookOpen, ExternalLink, FileText, Zap, Activity, MessageSquareText, Monitor,
} from 'lucide-react'

// ─── Types ────────────────────────────────────────────────────────────────────
interface SignalOverlayItem {
  id: number | string
  symbol: string
  action: string
  confidence: number | null
  source?: string
  agent_role?: string
  timestamp: string
}

// ─── SignalsOverlayPanel — live signals in the brain sidebar ─────────────────
function SignalsOverlayPanel() {
  const [signals, setSignals] = useState<SignalOverlayItem[]>([])
  const [decisions, setDecisions] = useState<SignalOverlayItem[]>([])

  useEffect(() => {
    const load = () => {
      apiClient.obsidian.signalsOverlay()
        .then(r => {
          setSignals(r.data?.signals ?? [])
          setDecisions(r.data?.decisions ?? [])
        })
        .catch(() => {})
    }
    load()
    const t = setInterval(() => { if (!document.hidden) load() }, 30_000 * pollMultiplier())
    return () => clearInterval(t)
  }, [])

  const actionColor = (a: string) => {
    const l = a?.toLowerCase() ?? ''
    if (l === 'buy' || l === 'long') return 'text-emerald-400'
    if (l === 'sell' || l === 'short') return 'text-red-400'
    return 'text-gray-400'
  }

  if (!signals.length && !decisions.length) return (
    <div className="p-3 text-gray-600 text-[10px]">No recent signals</div>
  )

  return (
    <div className="p-3 space-y-2">
      <h4 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1">
        <Zap className="w-3 h-3 text-amber-400" /> Live Signals ({signals.length})
      </h4>
      {signals.slice(0, 6).map(s => (
        <div key={s.id} className="flex items-center gap-1.5 text-[10px]">
          <span className={`font-semibold ${actionColor(s.action)}`}>{s.action?.toUpperCase()}</span>
          <span className="text-gray-300 truncate flex-1">{s.symbol}</span>
          {s.confidence != null && (
            <span className="text-gray-600">{Math.round(s.confidence * 100)}%</span>
          )}
        </div>
      ))}
      {decisions.length > 0 && (
        <>
          <h4 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1 mt-2">
            <Brain className="w-3 h-3 text-violet-400" /> Agent Decisions ({decisions.length})
          </h4>
          {decisions.slice(0, 4).map(d => (
            <div key={d.id} className="flex items-center gap-1.5 text-[10px]">
              <span className={`font-semibold ${actionColor(d.action)}`}>{d.action?.toUpperCase()}</span>
              <span className="text-gray-300 truncate flex-1">{d.symbol}</span>
              <span className="text-gray-500">{d.agent_role}</span>
            </div>
          ))}
        </>
      )}
      <div className="flex gap-2 mt-2 pt-1 border-t border-gray-700/30">
        <Link href="/telegram-signals" className="flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300">
          <MessageSquareText className="w-3 h-3" /> Telegram
        </Link>
        <Link href="/mt5-live" className="flex items-center gap-1 text-[10px] text-green-400 hover:text-green-300">
          <Monitor className="w-3 h-3" /> MT5 Live
        </Link>
      </div>
    </div>
  )
}

// ─── BrainLearningConsole — live feed of every Jarvis + agent action ─────────

interface FeedItem {
  id: number
  path: string
  type: string
  symbol: string | null
  tags: string[]
  timestamp: string
  label: string
}

interface LearningActivity {
  total_learning_notes: number
  today_count: number
  yesterday_count: number
  velocity_per_hour: number
  is_actively_learning: boolean
  recent_learning: Array<{ path: string; topic: string; tags: string[]; created_at: string }>
  recent_snapshots: Array<{ path: string; topic: string; created_at: string }>
  last_learned_at: string | null
  last_snapshot_at: string | null
}

const ACTION_ICONS: Record<string, string> = {
  'jarvis-set_tp':     '🎯',
  'jarvis-set_sl':     '🛡️',
  'jarvis-close':      '❌',
  'agent-decision':    '🧠',
  'decision-outcome':  '📊',
  'jarvis-learning':   '💡',
  'insights-snapshot': '📸',
  'live-action':       '⚡',
}
const ACTION_COLORS: Record<string, string> = {
  'jarvis-set_tp':    'text-emerald-400',
  'jarvis-set_sl':    'text-amber-400',
  'jarvis-close':     'text-red-400',
  'agent-decision':   'text-cyan-400',
  'decision-outcome': 'text-violet-400',
  'jarvis-learning':  'text-blue-400',
  'insights-snapshot':'text-gray-400',
}

function BrainLearningConsole() {
  const [feed, setFeed]             = useState<FeedItem[]>([])
  const [activity, setActivity]     = useState<LearningActivity | null>(null)
  const [learningStats, setLearning] = useState<any>(null)
  const [decStats, setDecStats]     = useState<any>(null)
  const [pulse, setPulse]           = useState(false)
  const [newCount, setNewCount]     = useState(0)
  const prevFeedLen                 = useRef<number>(0)
  const prevTodayRef                = useRef<number>(0)

  const load = useCallback(async () => {
    try {
      const [feedRes, actRes, learnRes, decRes] = await Promise.allSettled([
        apiClient.obsidian.liveFeed(25),
        apiClient.obsidian.learningActivity(),
        apiClient.getLearningStats(),
        apiClient.getDecisionStats(),
      ])
      if (feedRes.status === 'fulfilled') {
        const items: FeedItem[] = feedRes.value.data?.feed ?? []
        if (items.length > prevFeedLen.current) {
          setNewCount(n => n + (items.length - prevFeedLen.current))
          setPulse(true)
          setTimeout(() => setPulse(false), 3000)
        }
        prevFeedLen.current = items.length
        setFeed(items)
      }
      if (actRes.status === 'fulfilled') {
        const data = actRes.value.data as LearningActivity
        if (data.today_count > prevTodayRef.current) {
          setPulse(true)
          setTimeout(() => setPulse(false), 3000)
        }
        prevTodayRef.current = data.today_count
        setActivity(data)
      }
      if (learnRes.status === 'fulfilled') setLearning(learnRes.value.data)
      if (decRes.status === 'fulfilled') setDecStats(decRes.value.data)
    } catch { /* silent */ }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(() => { if (!document.hidden) load() }, 10_000 * pollMultiplier())
    return () => clearInterval(t)
  }, [load])

  const timeAgo = (iso: string) => {
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60_000)
    if (mins < 1) return 'now'
    if (mins < 60) return `${mins}m`
    return `${Math.floor(mins / 60)}h`
  }

  const isLearning = activity?.is_actively_learning || feed.length > 0

  return (
    <div className="border-t border-gray-700/50 flex flex-col">
      <div className="px-3 pt-2.5 pb-1 flex items-center justify-between shrink-0">
        <h3 className="text-[10px] font-semibold text-gray-300 flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full shrink-0 ${
            pulse ? 'bg-cyan-400 animate-ping' :
            isLearning ? 'bg-emerald-400 animate-pulse' : 'bg-gray-600'
          }`} />
          Live Brain Feed
          {newCount > 0 && (
            <span className="px-1 py-0.5 bg-cyan-500/20 text-cyan-300 rounded text-[8px] font-bold">
              +{newCount}
            </span>
          )}
        </h3>
        <Link href="/insights" className="text-[9px] text-violet-400 hover:text-violet-300 flex items-center gap-0.5">
          <ExternalLink className="w-2.5 h-2.5" /> insights
        </Link>
      </div>

      {(activity || learningStats) && (
        <div className="px-3 pb-2 flex items-center gap-2">
          <span className="text-[9px] text-emerald-400 font-bold">{activity?.today_count ?? 0} today</span>
          <span className="text-gray-700 text-[9px]">·</span>
          <span className="text-[9px] text-cyan-400">{activity?.total_learning_notes ?? 0} total</span>
          {learningStats && (<>
            <span className="text-gray-700 text-[9px]">·</span>
            <span className="text-[9px] text-gray-500">{learningStats.local_pct?.toFixed(0)}% local</span>
          </>)}
        </div>
      )}

      {decStats?.top_symbols?.length > 0 && (
        <div className="px-3 pb-2 flex flex-wrap gap-1">
          {decStats.top_symbols.slice(0, 5).map((s: any) => (
            <span key={s.symbol} className="px-1.5 py-0.5 bg-gray-900/60 text-gray-500 rounded text-[8px]">
              {s.symbol.replace('/USDT', '')} <span className="text-violet-400">{s.count}</span>
            </span>
          ))}
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-3 pb-2 space-y-1 max-h-52">
        {feed.length === 0 && (
          <div className="text-[10px] text-gray-600 py-2">No actions yet. Interact with JARVIS or run a signal scan.</div>
        )}
        {feed.slice(0, 20).map((item, i) => {
          const icon  = ACTION_ICONS[item.type] || '⚡'
          const color = ACTION_COLORS[item.type] || 'text-gray-400'
          return (
            <div key={item.id ?? i} className={`flex items-start gap-1.5 text-[9px] ${i === 0 && pulse ? 'bg-cyan-900/20 rounded px-1' : ''}`}>
              <span className="shrink-0 mt-0.5">{icon}</span>
              <div className="flex-1 min-w-0">
                <span className={`${color} font-medium`}>{item.type.replace('jarvis-','').replace('-',' ')}</span>
                {item.symbol && <span className="text-gray-400 ml-1">{item.symbol}</span>}
                {item.label && <span className="text-gray-600 ml-1 truncate block">{item.label.slice(0,32)}</span>}
              </div>
              <span className="shrink-0 text-gray-700 text-[8px]">{timeAgo(item.timestamp)}</span>
            </div>
          )
        })}
      </div>

      <div className="px-3 pb-3 pt-1 border-t border-gray-800/60 shrink-0 flex flex-col gap-0.5">
        <Link href="/insights" className="flex items-center gap-1.5 text-[9px] text-emerald-400 hover:text-emerald-300"><Activity className="w-3 h-3" /> Insights (source)</Link>
        <Link href="/vault" className="flex items-center gap-1.5 text-[9px] text-violet-400 hover:text-violet-300"><BookOpen className="w-3 h-3" /> Vault (all notes)</Link>
        <Link href="/telegram-signals" className="flex items-center gap-1.5 text-[9px] text-blue-400 hover:text-blue-300"><MessageSquareText className="w-3 h-3" /> Telegram Signals</Link>
        <Link href="/mt5-live" className="flex items-center gap-1.5 text-[9px] text-green-400 hover:text-green-300"><Monitor className="w-3 h-3" /> MT5 Live</Link>
      </div>
    </div>
  )
}

// ─── JarvisLearningsPanel — replaced by BrainLearningConsole ─────────────────
function JarvisLearningsPanel() { return null }


// ─── NodeVaultPanel — shows linked Obsidian notes for the selected graph node ─

function NodeVaultPanel({ nodeId, nodeLabel }: { nodeId: string | number; nodeLabel: string }) {
  const [notes, setNotes] = useState<{ path: string; note_type: string; symbol: string | null }[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    // Search vault for notes related to this node label
    apiClient.obsidian.search({ query: nodeLabel, limit: 5 })
      .then(r => setNotes(r.data?.hits ?? []))
      .catch(() => setNotes([]))
      .finally(() => setLoading(false))
  }, [nodeLabel])

  return (
    <div className="mt-2 pt-2 border-t border-gray-700/50">
      <h4 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1 mb-1.5">
        <BookOpen className="w-3 h-3 text-violet-400" /> Vault Notes
      </h4>
      {loading ? (
        <div className="text-gray-600 text-[10px]">Loading…</div>
      ) : notes.length === 0 ? (
        <div className="text-gray-700 text-[10px]">No linked notes</div>
      ) : (
        <div className="space-y-1">
          {notes.map((n) => (
            <a
              key={n.path}
              href="/vault"
              className="flex items-center gap-1.5 text-violet-400 hover:text-violet-300 truncate text-[10px]"
              title={n.path}
            >
              <FileText className="w-2.5 h-2.5 shrink-0" />
              <span className="truncate">{n.path.split('/').pop()?.replace('.md', '')}</span>
            </a>
          ))}
          <a href="/vault" className="flex items-center gap-1 text-[10px] text-gray-600 hover:text-violet-400 mt-1">
            <ExternalLink className="w-2.5 h-2.5" /> Open Vault
          </a>
        </div>
      )}
    </div>
  )
}

// Force-graph is loaded CLIENT-SIDE INTO STATE (see IntelligencePage) rather
// than via next/dynamic. next/dynamic wraps the component in a Loadable that
// does NOT forward refs, so the graph ref pointed at the wrapper (only a
// `retry` method) and zoom()/centerAt() were silent no-ops — which is why the
// saved zoom never restored on refresh. Importing the real forwardRef component
// into state and attaching the ref directly fixes the imperative API.
// We still avoid SSR by importing inside a mount effect.

// ─── Community → colour palette ──────────────────────────────────────────────
const PALETTE = [
  '#06b6d4','#8b5cf6','#10b981','#f59e0b','#ef4444','#3b82f6',
  '#ec4899','#14b8a6','#f97316','#a855f7','#84cc16','#0ea5e9',
  '#d946ef','#22c55e','#eab308','#6366f1','#f43f5e','#2dd4bf',
  '#fb923c','#c084fc','#4ade80','#facc15',
]
const DB_COLOR = '#f97316'
const ACTIVE_COLOR = '#fbbf24'

// ─── Living-brain geometry (world units) ─────────────────────────────────────
// Key invariant: 1 SVG unit in CyberBrain's viewBox = 1 world unit in the
// force-graph. This guarantees the brain silhouette maps perfectly onto the
// node cloud at every zoom level.
//   BRAIN_IMG_W/H  = viewBox width/height (div container size in world units)
//   BRAIN_RX/RY    = ellipse that confines nodes (inside the brain silhouette)
const BRAIN_IMG_W = 900  // viewBox: -450 .. +450 in x
const BRAIN_IMG_H = 640  // viewBox: -320 .. +320 in y
// Clamp: keep nodes inside this fraction of the ellipse (< 1.0 so nodes
// free-float inside the brain, not pile up on the edge silhouette).
const BRAIN_RX = 350
const BRAIN_RY = 245
const BRAIN_CLAMP_FRAC = 0.80  // nodes live within 80% of the ellipse

// ── Saved node positions (persist across reloads) ────────────────────────────
const POSITIONS_KEY = 'brain.positions.v2'
function loadSavedPositions(): Record<string, { x: number; y: number }> {
  if (typeof window === 'undefined') return {}
  try {
    const s = localStorage.getItem(POSITIONS_KEY)
    return s ? JSON.parse(s) : {}
  } catch { return {} }
}
function saveNodePosition(id: string | number, x: number, y: number) {
  if (typeof window === 'undefined') return
  try {
    const p = loadSavedPositions()
    p[String(id)] = { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 }
    localStorage.setItem(POSITIONS_KEY, JSON.stringify(p))
  } catch { /* ignore */ }
}
function clearAllPositions() {
  if (typeof window === 'undefined') return
  try { localStorage.removeItem(POSITIONS_KEY) } catch { /* ignore */ }
}
// Persist EVERY node's current position in a single write. Used by the "Save
// map" button so the WHOLE arrangement survives a refresh — not just the nodes
// the user happened to drag. Without this, un-dragged nodes get re-laid-out by
// the force simulation on reload and the map appears to "reset".
function saveAllPositions(nodes: { id: string | number; x?: number; y?: number }[]) {
  if (typeof window === 'undefined') return
  try {
    const p: Record<string, { x: number; y: number }> = {}
    for (const n of nodes) {
      if (n.x == null || n.y == null) continue
      p[String(n.id)] = { x: Math.round(n.x * 10) / 10, y: Math.round(n.y * 10) / 10 }
    }
    localStorage.setItem(POSITIONS_KEY, JSON.stringify(p))
  } catch { /* ignore */ }
}

// ── Factory default layout ───────────────────────────────────────────────────
// A baked-in snapshot of the canonical Brain Map arrangement (every node's world
// position + a fit zoom), shipped as /brain-default-layout.json. It is applied
// when the user has NO saved layout, and is exactly what the "Reset layout"
// button restores to. Positions are absolute world coords (browser-independent);
// the zoom is fit-relative (origin-centred) so the framing is responsive on any
// screen / device.
let DEFAULT_LAYOUT: { positions: Record<string, { x: number; y: number }>; zoom: SavedZoom | null } | null = null
let defaultLayoutPromise: Promise<void> | null = null
async function loadDefaultLayout(): Promise<void> {
  if (DEFAULT_LAYOUT || typeof window === 'undefined') return
  if (!defaultLayoutPromise) {
    defaultLayoutPromise = fetch('/brain-default-layout.json')
      .then(r => (r.ok ? r.json() : null))
      .then(d => { DEFAULT_LAYOUT = d && d.positions ? d : { positions: {}, zoom: null } })
      .catch(() => { DEFAULT_LAYOUT = { positions: {}, zoom: null } })
  }
  return defaultLayoutPromise
}
function defaultPositions(): Record<string, { x: number; y: number }> {
  return DEFAULT_LAYOUT?.positions || {}
}
function defaultZoom(): SavedZoom | null {
  return DEFAULT_LAYOUT?.zoom || null
}
// The user's saved zoom, or the factory default when the user hasn't saved one.
function effectiveSavedZoom(): SavedZoom | null {
  return loadSavedZoom() || defaultZoom()
}
// ── Saved zoom / pan transform (persist across reloads) ──────────────────────
// Saves the d3-zoom {k, x, y} so the exact zoom level and pan position is
// restored on next page load. Throttled to avoid excessive localStorage writes.
const ZOOM_KEY = 'brain.zoom.v1'
let zoomSaveTimer: ReturnType<typeof setTimeout> | null = null
// World-space "fit" zoom for a canvas size: the scale at which the brain ellipse
// exactly fills the canvas. Saving the zoom RELATIVE to this fit (k / fitK) makes
// the restored view RESPONSIVE — the same fraction of the map is shown on any
// browser size / device resolution, instead of a fixed pixel scale that crops on
// small screens and floats on large ones. Node positions are stored in absolute
// world coords (browser-independent), so the layout itself is identical
// everywhere; only the framing adapts.
function fitZoomFor(w: number, h: number): number {
  const kw = (w - 30) / (BRAIN_RX * 2)
  const kh = (h - 30) / (BRAIN_RY * 2)
  return Math.max(0.1, Math.min(kw, kh))
}
interface SavedZoom { k: number; x: number; y: number; fitK?: number }
function loadSavedZoom(): SavedZoom | null {
  if (typeof window === 'undefined') return null
  try { const s = localStorage.getItem(ZOOM_KEY); return s ? JSON.parse(s) : null }
  catch { return null }
}
// The actual scale to apply on THIS canvas: scale the saved fit-ratio by the
// current canvas's fit zoom. Falls back to the absolute saved k for legacy data
// (saved before fitK was recorded).
function appliedZoomK(saved: SavedZoom, w: number, h: number): number {
  if (saved.fitK && saved.fitK > 0 && w > 0 && h > 0) {
    return (saved.k / saved.fitK) * fitZoomFor(w, h)
  }
  return saved.k
}
// The full d3-zoom transform required to restore a saved world-centre on THIS
// canvas: the responsive target scale PLUS the canvas translate that puts the
// saved world point at the canvas centre. This is the exact inverse of the
// save-time formula (worldCX = (w/2 - t.x) / t.k), so the restore loop can
// verify BOTH the scale and the pan instead of the scale alone — which is what
// stops the map "loading on the side" after a reload.
function expectedTransform(saved: SavedZoom, w: number, h: number): { k: number; tx: number; ty: number } {
  const k = appliedZoomK(saved, w, h)
  return { k, tx: w / 2 - saved.x * k, ty: h / 2 - saved.y * k }
}
function saveZoom(k: number, x: number, y: number, fitK: number) {
  if (typeof window === 'undefined') return
  if (zoomSaveTimer) clearTimeout(zoomSaveTimer)
  zoomSaveTimer = setTimeout(() => {
    try {
      localStorage.setItem(ZOOM_KEY, JSON.stringify({
        k: Math.round(k * 1000) / 1000, x: Math.round(x), y: Math.round(y),
        fitK: Math.round(fitK * 1000) / 1000,
      }))
    } catch { /* ignore */ }
  }, 400)  // debounce: only save after 400ms of no zoom events
}
function clearSavedZoom() {
  if (typeof window === 'undefined') return
  try { localStorage.removeItem(ZOOM_KEY) } catch { /* ignore */ }
}

// ── Extension-assisted zoom persistence ──────────────────────────────────────
// When the JARVIS browser extension is installed, mirror the saved zoom into the
// extension's chrome.storage (via the content-script bridge). This survives
// localStorage clears and persists across the whole browser profile. The page
// posts the value; the content script writes it to chrome.storage.local.
function mirrorZoomToExtension(k: number, x: number, y: number, fitK: number) {
  if (typeof window === 'undefined') return
  try {
    window.postMessage(
      { __jarvisPage: true, type: 'save-zoom', data: { k: Math.round(k * 1000) / 1000, x: Math.round(x), y: Math.round(y), fitK: Math.round(fitK * 1000) / 1000 } },
      window.location.origin
    )
  } catch { /* ignore */ }
}

// Brain line paths (centred on origin) — shared by the dim base + travelling light.
const BRAIN_PATHS = [
  'M -10 -200 C -120 -210 -250 -150 -270 -40 C -300 -30 -310 30 -270 60 C -290 130 -200 200 -90 195 C -60 215 30 215 60 195 C 170 205 270 130 250 55 C 300 25 290 -35 255 -45 C 240 -155 110 -210 -10 -200 Z',
  'M -10 -195 C -10 -120 -10 80 -10 190',
  'M -30 -160 C -110 -160 -150 -110 -120 -70 C -180 -70 -190 -10 -150 10 C -190 50 -150 120 -80 120',
  'M -60 -120 C -120 -110 -130 -60 -95 -45 C -140 -25 -130 25 -90 35',
  'M -50 -60 C -100 -55 -100 -10 -70 5',
  'M 30 -160 C 110 -160 150 -110 120 -70 C 180 -70 190 -10 150 10 C 190 50 150 120 80 120',
  'M 60 -120 C 120 -110 130 -60 95 -45 C 140 -25 130 25 90 35',
  'M 50 -60 C 100 -55 100 -10 70 5',
]
const BRAIN_SYNAPSES: [number, number][] = [
  [-150, -110], [-200, -10], [-130, 100], [150, -110], [200, -10], [130, 100],
  [-70, -150], [70, -150], [-90, 160], [90, 160],
]

// Inline animated living cyber-brain SVG. The viewBox is set so that
// 1 SVG unit = 1 world unit = 1 div unit. This means the brain anatomy,
// the node clamp ellipse, and the graph coordinate system are all in the
// same space, so they perfectly align through any pan/zoom.
function CyberBrain() {
  return (
    <svg
      viewBox="-450 -320 900 640"
      width="100%"
      height="100%"
      preserveAspectRatio="xMidYMid meet"
      style={{ display: 'block', overflow: 'visible' }}
    >
      <defs>
        <linearGradient id="cbTrace" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#22d3ee" /><stop offset="100%" stopColor="#8b5cf6" />
        </linearGradient>
        <radialGradient id="cbCore" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#5eead4" stopOpacity="0.95" />
          <stop offset="40%" stopColor="#22d3ee" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#0891b2" stopOpacity="0" />
        </radialGradient>
        <filter id="cbGlow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      {/* Scale 1.35× so the original ±310 path coords reach ±418 — filling
          the ±450 viewBox and wrapping the ±350 node clamp ellipse. */}
      <g transform="scale(1.35)">
        {/* Breathing ambient halo */}
        <ellipse cx="0" cy="-10" rx="310" ry="225" fill="url(#cbCore)" opacity="0.22">
          <animate attributeName="opacity" values="0.12;0.38;0.12" dur="5s" repeatCount="indefinite" />
          <animate attributeName="rx"      values="295;330;295" dur="5s" repeatCount="indefinite" />
        </ellipse>

        {/* Static brain anatomy (dim) */}
        <g fill="none" stroke="url(#cbTrace)" strokeWidth="1.8" opacity="0.40" filter="url(#cbGlow)">
          {BRAIN_PATHS.map((d, i) => <path key={`b${i}`} d={d} />)}
        </g>

        {/* Travelling light beads — give the impression of neural signals
            firing and travelling through every fold of the brain */}
        <g fill="none" strokeLinecap="round" filter="url(#cbGlow)">
          {BRAIN_PATHS.map((d, i) => (
            <path key={`p${i}`} d={d} pathLength={100}
              stroke="#a5f3fc" strokeWidth="3.6"
              strokeDasharray="5 95" strokeDashoffset={100}
            >
              {/* primary bead — travels forward */}
              <animate
                attributeName="stroke-dashoffset" from="100" to="0"
                dur={`${2.4 + (i % 4) * 0.65}s`} begin={`${i * 0.3}s`}
                repeatCount="indefinite"
              />
              {/* color shift: cyan → violet → cyan */}
              <animate attributeName="stroke"
                values="#67e8f9;#a78bfa;#22d3ee;#67e8f9"
                dur="5s" repeatCount="indefinite"
              />
            </path>
          ))}
          {/* second pass with offset for a denser firing effect */}
          {BRAIN_PATHS.map((d, i) => (
            <path key={`q${i}`} d={d} pathLength={100}
              stroke="#c4b5fd" strokeWidth="2"
              strokeDasharray="3 97" strokeDashoffset={100}
            >
              <animate
                attributeName="stroke-dashoffset" from="100" to="0"
                dur={`${3.1 + (i % 3) * 0.8}s`} begin={`${i * 0.5 + 1.2}s`}
                repeatCount="indefinite"
              />
            </path>
          ))}
        </g>

        {/* Neural core — pulsing bright centre */}
        <circle cx="0" cy="-10" r="16" fill="#5eead4">
          <animate attributeName="r"       values="12;20;12" dur="2.2s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.6;1;0.6" dur="2.2s" repeatCount="indefinite" />
        </circle>
        <circle cx="0" cy="-10" r="34" fill="none" stroke="#5eead4" strokeWidth="1.2" opacity="0.5">
          <animate attributeName="r"       values="28;50;28" dur="3.4s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.5;0;0.5" dur="3.4s" repeatCount="indefinite" />
        </circle>

        {/* Synapse nodes — flicker to simulate firing */}
        <g fill="#67e8f9" filter="url(#cbGlow)">
          {BRAIN_SYNAPSES.map(([cx, cy], i) => (
            <circle key={`s${i}`} cx={cx} cy={cy} r="3.8">
              <animate attributeName="opacity"
                values="0.2;1;0.2" dur={`${1.6 + (i % 5) * 0.45}s`}
                begin={`${i * 0.18}s`} repeatCount="indefinite"
              />
              <animate attributeName="r" values="3;5;3" dur={`${1.6 + (i % 5) * 0.45}s`} begin={`${i * 0.18}s`} repeatCount="indefinite" />
            </circle>
          ))}
        </g>
        <g fill="#a78bfa" filter="url(#cbGlow)">
          {([[-90, -50], [90, -50], [-60, 60], [60, 60]] as [number,number][]).map(([cx, cy], i) => (
            <circle key={`v${i}`} cx={cx} cy={cy} r="3.2">
              <animate attributeName="opacity" values="0.3;1;0.3" dur={`${2.0 + i * 0.35}s`} repeatCount="indefinite" />
            </circle>
          ))}
        </g>
      </g>{/* end scale(1.35) */}
    </svg>
  )
}

function communityColor(group: number, isDb: boolean, isActive: boolean): string {
  if (isActive) return ACTIVE_COLOR
  if (isDb) return DB_COLOR
  return PALETTE[Math.abs(group) % PALETTE.length]
}

function GraphSkeleton() {
  return (
    <div className="flex-1 flex items-center justify-center bg-gray-900/50 rounded-lg">
      <div className="text-center">
        <div className="w-16 h-16 border-4 border-cyan-500/30 border-t-cyan-500 rounded-full animate-spin mx-auto mb-3" />
        <p className="text-sm text-gray-400">Loading brain map…</p>
      </div>
    </div>
  )
}

interface GraphNode {
  id: string; label: string; community: string; group: number
  source_file?: string; node_type?: string; degree?: number
  db_type?: string; db_id?: string
  x?: number; y?: number; z?: number
}
interface GraphLink { source: string | GraphNode; target: string | GraphNode; relation?: string }
interface GraphData { nodes: GraphNode[]; links: GraphLink[] }

export default function IntelligencePage() {
  const [mode, setMode] = useState<'2d' | '3d'>('2d')
  const [graphData, setGraphData] = useState<GraphData | null>(null)
  const [activeNodeIds, setActiveNodeIds] = useState<Set<string>>(new Set())
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null)
  const [search, setSearch] = useState('')
  const [communityFilter, setCommunityFilter] = useState('')
  const [strategies, setStrategies] = useState<any[]>([])
  const [synthesizing, setSynthesizing] = useState(false)
  const [harvesting, setHarvesting] = useState(false)
  const [harvestCount, setHarvestCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [gpuAccelerated, setGpuAccelerated] = useState(false)
  const [nodeCount, setNodeCount] = useState(0)
  const [linkCount, setLinkCount] = useState(0)
  const [headroom, setHeadroom] = useState<any>(null)
  const [knowledge, setKnowledge] = useState<any[]>([])
  const [agentUsage, setAgentUsage] = useState<any>(null)
  const [positionsDirty, setPositionsDirty] = useState(false)  // true when dragged positions exist
  const graphRef = useRef<any>(null)
  const graphContainerRef = useRef<HTMLDivElement>(null)  // canvas container — gives accurate px dimensions
  const brainWorldRef = useRef<HTMLDivElement>(null)   // brain image, mapped into graph world-space
  const filteredDataRef = useRef<GraphData | null>(null)
  const graphDataRef = useRef<GraphData | null>(null)  // live full node set (objects mutated in-place by the sim) — used for bulk save
  const brainReadyRef = useRef(false)
  const zoomRestoredRef = useRef(false)  // true after first zoom restore attempt
  const savedRestoredRef = useRef(false) // true when a SAVED zoom was restored (locks out default fill)
  const zoomCanSaveRef = useRef(false)   // gate: only save zoom AFTER restore, so the
                                         // initial default zoom event can't clobber the saved value
  const viewInitializedRef = useRef(false)  // true once the view has been applied at least once
  const lastTransformRef = useRef<{ k: number; x: number; y: number } | null>(null)  // latest live d3-zoom transform
  const [viewSaved, setViewSaved] = useState(false)  // shows "Saved ✓" confirmation
  const [isFullscreen, setIsFullscreen] = useState(false)  // brain fills the whole screen
  const [panelOpen, setPanelOpen] = useState(true)         // right info panel visible
  const [canvasSize, setCanvasSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })  // measured graph canvas size
  const [brainReady, setBrainReady] = useState(false)  // hide brain until zoom transform is known
  // Force-graph components, imported CLIENT-SIDE into state so the graph ref
  // attaches to the real ForceGraph instance (next/dynamic swallows the ref).
  const [FG2D, setFG2D] = useState<any>(null)
  const [FG3D, setFG3D] = useState<any>(null)
  // Skip the heavy Three.js 3D graph on weak GPUs (start.py sets
  // NEXT_PUBLIC_DISABLE_3D on the 'low' tier). 2D stays fully functional.
  const [disable3D] = useState(() => threeDisabled())
  useEffect(() => {
    let alive = true
    import('react-force-graph-2d').then(m => { if (alive) setFG2D(() => m.default) }).catch(() => {})
    if (!disable3D) {
      import('react-force-graph-3d').then(m => { if (alive) setFG3D(() => m.default) }).catch(() => {})
    }
    return () => { alive = false }
  }, [disable3D])

  // ── Load full graph ─────────────────────────────────────────────────────────
  const loadGraph = useCallback(async () => {
    try {
      await loadDefaultLayout()  // ensure the factory default layout is available
      const res = await apiClient.aiAnalyst.getGraphFull()
      const data = res.data
      if (data?.available) {
        // Use the user's saved layout if they have one; otherwise fall back to
        // the baked-in FACTORY DEFAULT layout (what "Reset layout" restores to).
        // Merge so individual user drags override only those nodes while every
        // other node keeps its factory-default position.
        const userSaved = loadSavedPositions()
        const hasUserSaved = Object.keys(userSaved).length > 0
        const saved = { ...defaultPositions(), ...userSaved }

        // Pre-position nodes using the GOLDEN RATIO PHYLLOTAXIS pattern.
        // This distributes all 3000+ nodes evenly across the brain ellipse
        // from frame 1 — no warmup needed, no ring effect.
        const GOLDEN_ANGLE = 2.399963  // 2π / φ² radians
        const allNodes = data.nodes as GraphNode[]
        const totalNodes = allNodes.length
        const nodes: GraphNode[] = allNodes.map((n: GraphNode, idx: number) => {
          const pos = saved[String(n.id)]
          if (pos) return { ...n, x: pos.x, y: pos.y, fx: pos.x, fy: pos.y } as any
          // t goes 0→1, r goes 0→clamp×0.78 so there's a gap to the outer ring
          const t = (idx + 0.5) / totalNodes
          const r = Math.sqrt(t) * BRAIN_RX * 0.76
          const theta = idx * GOLDEN_ANGLE
          return {
            ...n,
            x: r * Math.cos(theta),
            y: r * Math.sin(theta) * (BRAIN_RY / BRAIN_RX),
          }
        })
        const gd = { nodes, links: data.links }
        graphDataRef.current = gd
        setGraphData(gd)
        setNodeCount(data.node_count)
        setLinkCount(data.link_count)
        setGpuAccelerated(data.gpu_accelerated)
        // Only treat the layout as "dirty" (show the Reset button) when the USER
        // has their own saved layout or zoom — the factory default is not dirty.
        setPositionsDirty(hasUserSaved || loadSavedZoom() !== null)
      }
    } catch { /* graph may be unavailable */ }
    finally { setLoading(false) }
  }, [])

  // ── Poll active nodes every 5 s (paused when tab is hidden) ────────────────
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await apiClient.aiAnalyst.getActiveNodes()
        setActiveNodeIds(new Set(res.data?.active_nodes || []))
      } catch { /* ignore */ }
    }
    poll()
    const t = setInterval(() => { if (!document.hidden) poll() }, 5000 * pollMultiplier())
    return () => clearInterval(t)
  }, [])

  // ── Sidebar data ─────────────────────────────────────────────────────────────
  // Knowledge is polled every 15 s so new nodes from JARVIS conversations appear
  // without a manual refresh (the self-learning brain map expands live).
  const [latestKnowledgeId, setLatestKnowledgeId] = useState<number | null>(null)
  const [newKnowledgeCount, setNewKnowledgeCount] = useState(0)

  useEffect(() => {
    const loadSide = async () => {
      try {
        const [h, k, au] = await Promise.all([
          apiClient.aiAnalyst.getHeadroom(30),
          apiClient.aiAnalyst.getKnowledge(),
          apiClient.aiAnalyst.getAiUsageAgents(),
        ])
        const items: any[] = k.data?.items || []
        setHeadroom(h.data); setAgentUsage(au.data)
        setKnowledge(items)
        if (items.length > 0) {
          const topId = items[0].id as number
          setLatestKnowledgeId(prev => {
            if (prev !== null && topId !== prev) {
              // New nodes arrived — flash the badge
              setNewKnowledgeCount(items.length - (prev ? 0 : 0))
              setTimeout(() => setNewKnowledgeCount(0), 5000)
            }
            return topId
          })
        }
      } catch { /* plugin unavailable */ }
    }
    loadGraph(); loadSide()
    // Live knowledge polling — new JARVIS insights arrive every ~15 s
    const knowledgePoll = setInterval(async () => {
      if (document.hidden) return   // skip when tab is not visible
      try {
        const k = await apiClient.aiAnalyst.getKnowledge()
        const items: any[] = k.data?.items || []
        setKnowledge(prev => {
          if (items.length > prev.length) {
            const added = items.length - prev.length
            setNewKnowledgeCount(added)
            setTimeout(() => setNewKnowledgeCount(0), 5000)
          }
          return items
        })
      } catch { /* ignore */ }
    }, 15000)
    return () => clearInterval(knowledgePoll)
  }, [loadGraph])

  // ── Filtered data ───────────────────────────────────────────────────────────
  const filteredData = useMemo<GraphData | null>(() => {
    if (!graphData) return null
    let nodes = graphData.nodes
    if (search) {
      const q = search.toLowerCase()
      nodes = nodes.filter(n =>
        n.label.toLowerCase().includes(q) ||
        n.community.toLowerCase().includes(q) ||
        (n.source_file || '').toLowerCase().includes(q)
      )
    }
    if (communityFilter) nodes = nodes.filter(n => n.community === communityFilter)
    const ids = new Set(nodes.map(n => n.id))
    const links = graphData.links.filter(l => {
      const s = typeof l.source === 'object' ? l.source.id : l.source
      const t = typeof l.target === 'object' ? l.target.id : l.target
      return ids.has(s) && ids.has(t)
    })
    return { nodes, links }
  }, [graphData, search, communityFilter])

  // ── Community legend ────────────────────────────────────────────────────────
  const communities = useMemo(() => {
    if (!graphData) return []
    const m = new Map<string, number>()
    for (const n of graphData.nodes) m.set(n.community, (m.get(n.community) || 0) + 1)
    return Array.from(m.entries()).sort((a, b) => b[1] - a[1]).slice(0, 25).map(([name, count]) => ({ name, count }))
  }, [graphData])

  const nodeColor = useCallback((node: GraphNode) =>
    communityColor(node.group, node.node_type === 'db_entity', activeNodeIds.has(node.id))
  , [activeNodeIds])

  const nodeVal = useCallback((node: GraphNode) =>
    (node.node_type === 'db_entity' ? 6 : 3) + Math.min((node.degree || 0) / 5, 8)
  , [])

  // Keep a ref of the current nodes so the per-tick clamp avoids stale closures.
  useEffect(() => { filteredDataRef.current = filteredData }, [filteredData])

  // After data loads, add a d3 center-force so nodes are pulled to world origin.
  // This ensures the node cluster (and the brain behind it) stays centered.
  useEffect(() => {
    if (!filteredData || mode !== '2d') return
    const timer = setTimeout(() => {
      try {
        const g = graphRef.current
        if (!g) return
        // Add a strong centering force pulling all nodes to (0,0)
        const sim = (g as any).d3Force?.('center')
        if (sim === undefined) {
          // Force not registered yet — use the graph's force accessor
          try {
            (g as any).d3Force('center', (window as any).d3?.forceCenter?.(0, 0))
          } catch { /* d3 not globally accessible */ }
        }
      } catch { /* ignore */ }
    }, 200)
    return () => clearTimeout(timer)
  }, [filteredData, mode])

  // Sync the brain layer with the d3-zoom transform so it stays pixel-perfect
  // behind the nodes. Called on every pan/zoom event.
  const handleZoom = useCallback((t: { k: number; x: number; y: number }) => {
    const el = brainWorldRef.current
    if (el) el.style.transform = `translate(${t.x}px, ${t.y}px) scale(${t.k})`
    if (!brainReadyRef.current) { brainReadyRef.current = true; setBrainReady(true) }
    // ALWAYS track the latest transform so the manual "Save view" button works
    // even if the auto-save gate is closed.
    lastTransformRef.current = t
    // GATE: don't auto-save until the saved zoom has been restored. Otherwise the
    // default zoom event d3-zoom fires on mount (k=1, centred) would overwrite
    // the user's saved position before we get a chance to restore it.
    if (!zoomCanSaveRef.current) return
    // Save as WORLD-SPACE center coordinates (resolution-independent).
    // The d3-zoom transform origin is canvas top-left. The world point at the
    // canvas centre is: worldCenter = (canvasW/2 - tx) / k
    const container = graphContainerRef.current
    if (container && t.k > 0) {
      const { width: w, height: h } = container.getBoundingClientRect()
      if (w > 0 && h > 0) {
        // IMPORTANT: react-force-graph's onZoom reports t.x / t.y as the GRAPH-
        // SPACE coordinates already at the canvas centre (it spreads
        // ...this.centerAt() over the d3 transform — see the lib source comment
        // "report x,y coordinates relative to canvas center"). They are NOT the
        // d3 canvas translate, so they ARE the world-centre we persist directly.
        // The old code wrongly re-applied (w/2 - t.x)/t.k, double-transforming
        // the value → the saved centre didn't match the view → "loads on the
        // side" after reload.
        const worldCX = t.x
        const worldCY = t.y
        const fitK = fitZoomFor(w, h)  // current fit zoom — saved so restore is responsive
        saveZoom(t.k, worldCX, worldCY, fitK)  // world coords + fit-ratio, not screen pixels
        mirrorZoomToExtension(t.k, worldCX, worldCY, fitK)
        setPositionsDirty(true)  // a user zoom is a customization → allow Reset to default
      }
    }
  }, [])

  // Push a programmatically-applied transform into the brain layer + the
  // last-transform ref so the silhouette tracks the nodes during a restore even
  // if ForceGraph's onZoom doesn't fire for the imperative zoom()/centerAt()
  // calls. It deliberately does NOT auto-save (the gate stays untouched), so it
  // can never clobber the saved zoom.
  const syncBrainTransform = useCallback((k: number, tx: number, ty: number) => {
    const el = brainWorldRef.current
    if (el) el.style.transform = `translate(${tx}px, ${ty}px) scale(${k})`
    if (!brainReadyRef.current) { brainReadyRef.current = true; setBrainReady(true) }
    lastTransformRef.current = { k, x: tx, y: ty }
  }, [])

  // Manually save the current view — 100% reliable, bypasses the auto-save gate.
  // Reads the live transform directly and writes to localStorage + extension.
  const saveCurrentView = useCallback(() => {
    // 1. Persist EVERY node's current position so the entire arrangement is
    //    restored verbatim on refresh — this is the core fix for the map
    //    "resetting" after a reload (previously only the zoom was saved, so
    //    un-dragged nodes were re-laid-out by the physics engine on load).
    const data = graphDataRef.current
    if (data && data.nodes.length) {
      saveAllPositions(data.nodes as any)
      // Pin the live node objects so the physics engine cannot drift them for
      // the rest of this session either.
      for (const n of data.nodes as any[]) {
        if (n.x != null && n.y != null) { n.fx = n.x; n.fy = n.y }
      }
      setPositionsDirty(true)
    }
    // 2. Persist the zoom / pan transform (world-space centre, resolution-
    //    independent). Written synchronously so it's guaranteed saved on click.
    //    SOURCE OF TRUTH: the live d3-zoom transform (canvas.__zoom) — NOT
    //    lastTransformRef, which the restore/onEngineStop sync can clobber with a
    //    stale saved/default transform. Reading __zoom guarantees we persist
    //    EXACTLY what is on screen (this is the fix for "saves & reloads on the
    //    side": the old code saved a stale centre that didn't match the view).
    const container = graphContainerRef.current
    const liveZoom = (container?.querySelector('canvas') as any)?.__zoom
    const hasLive = !!(liveZoom && typeof liveZoom.k === 'number')
    const t = hasLive
      ? { k: liveZoom.k, x: liveZoom.x, y: liveZoom.y }   // d3 CANVAS TRANSLATE
      : lastTransformRef.current                          // onZoom value: x,y are GRAPH centre
    if (t && container && t.k > 0) {
      const { width: w, height: h } = container.getBoundingClientRect()
      if (w > 0 && h > 0) {
        // From the live d3 transform, x/y are the canvas translate → convert to
        // world centre. From the lastTransformRef fallback, x/y are ALREADY the
        // graph-space centre (react-force-graph onZoom spreads centerAt()), so
        // use them directly. Mixing these up double-transforms the value and
        // is exactly what made the saved view reload "on the side".
        const worldCX = hasLive ? (w / 2 - t.x) / t.k : t.x
        const worldCY = hasLive ? (h / 2 - t.y) / t.k : t.y
        const fitK = fitZoomFor(w, h)  // current fit zoom → saved as a ratio so the view is responsive across browsers
        try {
          localStorage.setItem(ZOOM_KEY, JSON.stringify({
            k: Math.round(t.k * 1000) / 1000,
            x: Math.round(worldCX),
            y: Math.round(worldCY),
            fitK: Math.round(fitK * 1000) / 1000,
          }))
        } catch { /* ignore */ }
        mirrorZoomToExtension(t.k, worldCX, worldCY, fitK)
        zoomCanSaveRef.current = true  // open the auto-save gate from now on
      }
    }
    setViewSaved(true)
    setTimeout(() => setViewSaved(false), 2000)
  }, [])

  // Toggle fullscreen — the brain map covers the whole screen. After the layout
  // changes size, re-fit the brain so it fills the new dimensions.
  const toggleFullscreen = useCallback(() => {
    setIsFullscreen(f => !f)
  }, [])

  // (Fullscreen/panel resize re-zoom is handled by the authoritative
  // canvasSize-driven restore effect — no separate effect needed here.)

  // Allow Escape to exit fullscreen
  useEffect(() => {
    if (!isFullscreen) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setIsFullscreen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isFullscreen])

  // Seed the brain transform to the canvas center at mount (d3-zoom initialises
  // with translate(width/2, height/2) scale(1) so the origin is canvas-center).
  // This prevents the brain flashing at (0,0) before the first onZoom fires.
  useEffect(() => {
    const el = brainWorldRef.current
    const parent = el?.parentElement
    if (!el || !parent) return
    const { width, height } = parent.getBoundingClientRect()
    if (width > 0 && height > 0) {
      el.style.transform = `translate(${width / 2}px, ${height / 2}px) scale(1)`
      brainReadyRef.current = true
      setBrainReady(true)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // (The old imperative zoomToBrain fit helper was removed — zoom restore and
  // default-fit are now handled by the AUTHORITATIVE canvasSize-driven effect
  // below, which is responsive to any browser size.)

  // NOTE: zoom restore is handled by the AUTHORITATIVE canvasSize-driven effect
  // (further below). The old early-restore + 700ms default-fill effects were
  // removed because they raced with the ForceGraph width/height reset and caused
  // the saved zoom to be lost on refresh.

  // ── Extension-assisted zoom restore ───────────────────────────────────────
  // If localStorage has no saved zoom but the JARVIS extension does (chrome.storage),
  // request it and restore from there. Lets the extension back up the position
  // even if the browser's localStorage was cleared.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onMsg = (event: MessageEvent) => {
      if (event.source !== window) return
      const d = event.data
      if (!d || d.__jarvisExt !== true || d.type !== 'zoom-data' || !d.data) return
      // Only use the extension's value if we don't already have a local one
      if (loadSavedZoom()) return
      const { k, x, y, fitK } = d.data
      try { localStorage.setItem(ZOOM_KEY, JSON.stringify({ k, x, y, fitK })) } catch { /* noop */ }
      // Apply immediately if the graph is ready (responsive: scale by this
      // browser's fit zoom so the framing matches regardless of screen size).
      const g = graphRef.current
      if (g && typeof k === 'number') {
        zoomRestoredRef.current = true
        savedRestoredRef.current = true
        const rect = graphContainerRef.current?.getBoundingClientRect()
        // Same deterministic zoom-then-center + brain-sync as the authoritative
        // restore loop, so the extension-backed restore can't load off-center.
        if (rect && rect.width > 0 && rect.height > 0) {
          const { k: targetK, tx, ty } = expectedTransform({ k, x, y, fitK }, rect.width, rect.height)
          try { (g as any).zoom?.(targetK, 0); (g as any).centerAt?.(x, y, 0) } catch { /* noop */ }
          syncBrainTransform(targetK, tx, ty)
        } else {
          try { (g as any).zoom?.(k, 0); (g as any).centerAt?.(x, y, 0) } catch { /* noop */ }
        }
        zoomCanSaveRef.current = true
      }
    }
    window.addEventListener('message', onMsg)
    // Ask the extension for any stored zoom (no-op if extension absent)
    try { window.postMessage({ __jarvisPage: true, type: 'request-zoom' }, window.location.origin) } catch { /* noop */ }
    return () => window.removeEventListener('message', onMsg)
  }, [])

  // Confine every node inside the brain ellipse each simulation tick.
  // Nodes are clamped at BRAIN_CLAMP_FRAC (80%) of the boundary so they
  // FREE-FLOAT inside the brain rather than clustering on the outer edge.
  // Pinned nodes (fx/fy set by drag) are skipped so user drags persist.
  const clampToBrain = useCallback(() => {
    const data = filteredDataRef.current
    if (!data) return
    for (const n of data.nodes as any[]) {
      if (n.x == null || n.y == null) continue
      if (n.fx != null) continue  // skip pinned (user-dragged) nodes
      const nx = n.x / BRAIN_RX
      const ny = n.y / BRAIN_RY
      const d = Math.hypot(nx, ny)
      if (d > BRAIN_CLAMP_FRAC) {
        const s = BRAIN_CLAMP_FRAC / d
        n.x *= s; n.y *= s
        if (n.vx != null) n.vx *= 0.35
        if (n.vy != null) n.vy *= 0.35
      }
    }
  }, [])

  const deleteKnowledge = async (id: number) => {
    try { await apiClient.aiAnalyst.deleteKnowledge(id); setKnowledge(p => p.filter(k => k.id !== id)) }
    catch { /* ignore */ }
  }

  // Load any already-synthesized strategies on mount
  useEffect(() => {
    apiClient.aiAnalyst.listSynthesizedStrategies().then(r => setStrategies(r.data?.strategies || [])).catch(() => {})
  }, [])

  // Trigger a full intelligence harvest from all watched sources
  const runHarvest = useCallback(async () => {
    setHarvesting(true)
    try {
      const r = await apiClient.aiAnalyst.harvestIntelligence()
      setHarvestCount(r.data?.total_added || 0)
      // Reload knowledge to show new nodes immediately
      const k = await apiClient.aiAnalyst.getKnowledge()
      setKnowledge(k.data?.items || [])
    } catch { /* plugin unavailable */ }
    finally { setHarvesting(false) }
  }, [])

  // Synthesize Python strategies from accumulated knowledge
  const synthesize = useCallback(async () => {
    setSynthesizing(true)
    try {
      const r = await apiClient.aiAnalyst.synthesizeStrategies(3)
      const syns = r.data?.strategies || []
      setStrategies(syns)
      // Reload knowledge — synthesized strategies appear as new nodes
      const k = await apiClient.aiAnalyst.getKnowledge()
      setKnowledge(k.data?.items || [])
    } catch { /* plugin unavailable */ }
    finally { setSynthesizing(false) }
  }, [])

  // Measure the graph container with a ResizeObserver and feed EXPLICIT pixel
  // dimensions to ForceGraph. Without this, react-force-graph-2d falls back to
  // window.innerWidth — creating a canvas wider than the container that pushes
  // the right info panel off-screen. This keeps the canvas exactly container-sized.
  useEffect(() => {
    const el = graphContainerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const r = el.getBoundingClientRect()
      if (r.width > 0 && r.height > 0) {
        setCanvasSize(prev =>
          (Math.abs(prev.w - r.width) > 1 || Math.abs(prev.h - r.height) > 1)
            ? { w: Math.round(r.width), h: Math.round(r.height) }
            : prev
        )
      }
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [isFullscreen, panelOpen, mode])

  // ── AUTHORITATIVE zoom restore ────────────────────────────────────────────
  // Runs whenever the canvas size settles (initial measure, resize, fullscreen,
  // panel toggle). Re-applies the saved zoom every time — this DEFEATS the
  // ForceGraph internal reset that happens when its width/height props change
  // (which was wiping the restored zoom and causing the "resets on refresh" bug).
  useEffect(() => {
    if (mode !== '2d' || !filteredData) return
    if (canvasSize.w < 10 || canvasSize.h < 10) return  // wait for real dimensions
    const apply = () => {
      const g = graphRef.current
      if (!g) return
      const saved = effectiveSavedZoom()
      if (saved) {
        // Deterministic apply order: zoom FIRST (so centerAt computes its
        // translate against the final scale), then centerAt — both using the
        // SAME settled canvasSize so scale and pan are consistent.
        const { k: targetK, tx, ty } = expectedTransform(saved, canvasSize.w, canvasSize.h)
        try {
          ;(g as any).zoom?.(targetK, 0)
          ;(g as any).centerAt?.(saved.x, saved.y, 0)
        } catch { /* noop */ }
        // Keep the brain silhouette locked to the nodes even if onZoom doesn't
        // fire for these imperative calls.
        syncBrainTransform(targetK, tx, ty)
        savedRestoredRef.current = true
      } else if (!viewInitializedRef.current) {
        // No saved view — fit the brain ellipse, but only on first init so a
        // resize doesn't yank an unsaved pan back to centre.
        try {
          ;(g as any).zoom?.(Math.max(0.5, fitZoomFor(canvasSize.w, canvasSize.h)), 0)
          ;(g as any).centerAt?.(0, 0, 0)
        } catch { /* noop */ }
      }
      viewInitializedRef.current = true
      zoomRestoredRef.current = true
      // NOTE: the auto-save gate (zoomCanSaveRef) is intentionally NOT opened
      // here. If we open it while the restore is still settling, handleZoom
      // would persist an intermediate / default transform and CLOBBER the saved
      // zoom (the real reason the zoom "reset on refresh"). The gate is opened
      // below only once the restore is confirmed (or the user interacts).
    }
    // Re-assert the saved zoom on a VERIFYING interval until the live canvas
    // transform actually matches it. A single (or even triple) timed apply lost
    // the race: react-force-graph resets its own zoom when its width/height
    // props settle a few hundred ms after mount, AFTER the timed re-applies had
    // finished — which is exactly why the zoom "reset on refresh". This loop
    // keeps re-applying (and verifies against canvas.__zoom, the d3-zoom state)
    // until it sticks, the user interacts, or a ~3s safety cap is hit.
    const container = graphContainerRef.current
    let userInteracted = false
    // The auto-save gate opens ONLY on a genuine user gesture (or the explicit
    // "Save map" button). It is NEVER opened by this restore loop, so a
    // programmatic / settling transform can never overwrite the saved zoom —
    // this is what permanently kills the "zoom resets on refresh" clobber.
    const onUserInteract = () => { userInteracted = true; zoomCanSaveRef.current = true }
    // Capture phase: d3-zoom calls stopImmediatePropagation() on wheel/pointer
    // events, so bubble-phase listeners never fire. Capturing fires first.
    container?.addEventListener('wheel', onUserInteract, { capture: true, passive: true })
    container?.addEventListener('pointerdown', onUserInteract, { capture: true })
    let tries = 0
    const iv = setInterval(() => {
      tries += 1
      if (userInteracted) { clearInterval(iv); return }  // user took over
      apply()
      const saved = effectiveSavedZoom()
      if (!saved) { clearInterval(iv); return }  // no saved or default zoom — nothing to re-assert
      const canvas = container?.querySelector('canvas') as any
      const live = canvas?.__zoom
      // Stop ONLY once the live d3-zoom transform matches the responsive target
      // in BOTH scale AND pan/translate. Verifying scale alone let ForceGraph's
      // late width/height re-init leave the pan at its default → the map
      // "loaded on the side". The expected translate is the inverse of the
      // save-time world-centre formula (see expectedTransform).
      const { k: targetK, tx, ty } = expectedTransform(saved, canvasSize.w, canvasSize.h)
      const kTol = Math.max(0.02, targetK * 0.01)
      const pTol = Math.max(2, canvasSize.w * 0.01)  // ~1% of canvas width, ≥2px
      if (
        live && typeof live.k === 'number' &&
        Math.abs(live.k - targetK) < kTol &&
        Math.abs(live.x - tx) < pTol &&
        Math.abs(live.y - ty) < pTol
      ) { clearInterval(iv); return }
      if (tries >= 30) clearInterval(iv)  // ~3.6s safety cap — give up re-asserting
    }, 120)
    apply()  // immediate first attempt
    return () => {
      clearInterval(iv)
      container?.removeEventListener('wheel', onUserInteract, { capture: true } as any)
      container?.removeEventListener('pointerdown', onUserInteract, { capture: true } as any)
    }
  }, [canvasSize.w, canvasSize.h, mode, filteredData])

  return (
    <>
      <Head><title>Intelligence | TradeBot</title></Head>
      <div
        className={
          isFullscreen
            ? 'fixed inset-0 z-[100] flex flex-col bg-gray-950'
            // Fill the area below the 56px header, accounting for the parent
            // <main> padding (p-4 = 2rem total / md:p-6 = 3rem total) so the
            // page fits exactly without overflowing horizontally (which would
            // push the right info panel off-screen) or clipping content.
            : 'flex flex-col h-[calc(100vh-5.5rem)] md:h-[calc(100vh-6.5rem)] overflow-hidden'
        }
      >

        {/* Toolbar */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-gray-700/50 bg-gray-900/60 shrink-0 flex-wrap">
          <div className="flex items-center gap-1.5">
            <Network className="w-5 h-5 text-cyan-400" />
            <span className="font-semibold text-white text-sm">Brain Map</span>
            {gpuAccelerated && (
              <span className="text-[10px] px-1.5 py-0.5 bg-green-500/20 text-green-400 rounded font-mono">GPU</span>
            )}
          </div>
          <div className="text-[11px] text-gray-500 hidden sm:block">
            {nodeCount.toLocaleString()} nodes · {linkCount.toLocaleString()} links
          </div>

          {/* 2D/3D toggle (3D hidden on weak-GPU / disable-3D profile) */}
          <div className="flex bg-gray-800 rounded p-0.5 gap-0.5">
            {(disable3D ? (['2d'] as const) : (['2d', '3d'] as const)).map(m => (
              <button key={m} onClick={() => setMode(m)}
                className={`px-3 py-1 rounded text-xs font-semibold transition ${mode === m ? 'bg-cyan-600 text-white' : 'text-gray-400 hover:text-white'}`}>
                {m.toUpperCase()}
              </button>
            ))}
          </div>

          {/* Search */}
          <div className="flex items-center gap-1.5 flex-1 max-w-xs">
            <Search className="w-4 h-4 text-gray-400 shrink-0" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search nodes…"
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white min-w-0" />
            {search && <button onClick={() => setSearch('')}><X className="w-3.5 h-3.5 text-gray-400 hover:text-white" /></button>}
          </div>

          {/* Community filter */}
          <select value={communityFilter} onChange={e => setCommunityFilter(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white max-w-[180px]">
            <option value="">All communities</option>
            {communities.map(c => <option key={c.name} value={c.name}>{c.name} ({c.count})</option>)}
          </select>

          <button onClick={loadGraph} className="ml-auto p-1.5 bg-gray-800 rounded hover:bg-gray-700 transition" title="Reload graph">
            <RefreshCw className="w-3.5 h-3.5 text-gray-400" />
          </button>

          {/* Harvest intelligence from all watched sources */}
          <button
            onClick={runHarvest}
            disabled={harvesting}
            className="p-1.5 rounded transition flex items-center gap-1 text-[10px] font-medium bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white"
            title="Harvest intelligence from Sentiment, SMC, Telegram Signals, Insights → expands Brain Map"
          >
            <Brain className={`w-3.5 h-3.5 ${harvesting ? 'animate-pulse' : ''}`} />
            {harvesting ? 'Harvesting…' : harvestCount > 0 ? `+${harvestCount} nodes` : 'Harvest'}
          </button>

          {/* Synthesize Python strategies from knowledge */}
          <button
            onClick={synthesize}
            disabled={synthesizing}
            className="p-1.5 rounded transition flex items-center gap-1 text-[10px] font-medium bg-violet-700 hover:bg-violet-600 disabled:opacity-50 text-white"
            title="Generate Python trading strategies from accumulated Brain Map knowledge"
          >
            <Sparkles className={`w-3.5 h-3.5 ${synthesizing ? 'animate-spin' : ''}`} />
            {synthesizing ? 'Synthesizing…' : 'Synthesize'}
          </button>

          {/* Save current zoom/pan view — explicit, reliable persistence */}
          {mode === '2d' && (
            <button
              onClick={saveCurrentView}
              className={`p-1.5 rounded transition flex items-center gap-1 text-[10px] font-medium ${
                viewSaved
                  ? 'bg-green-600/80 text-white'
                  : 'bg-cyan-600 hover:bg-cyan-500 text-white'
              }`}
              title="Save the whole brain map — every node position + zoom — so it's restored exactly on refresh"
            >
              {viewSaved ? <Check className="w-3.5 h-3.5" /> : <Save className="w-3.5 h-3.5" />}
              {viewSaved ? 'Saved' : 'Save map'}
            </button>
          )}
          {positionsDirty && (
            <button
              onClick={() => { clearAllPositions(); clearSavedZoom(); zoomRestoredRef.current = false; savedRestoredRef.current = false; zoomCanSaveRef.current = false; viewInitializedRef.current = false; setPositionsDirty(false); loadGraph() }}
              className="p-1.5 bg-gray-800 rounded hover:bg-red-700/60 transition text-[10px] text-amber-400 flex items-center gap-1"
              title="Clear saved node positions and zoom, reset layout"
            >
              <Trash2 className="w-3 h-3" /> Reset layout
            </button>
          )}

          {/* Toggle right info panel */}
          <button
            onClick={() => setPanelOpen(p => !p)}
            className="p-1.5 bg-gray-800 rounded hover:bg-gray-700 transition text-gray-400"
            title={panelOpen ? 'Hide info panel' : 'Show info panel'}
          >
            {panelOpen ? <PanelRightClose className="w-3.5 h-3.5" /> : <PanelRightOpen className="w-3.5 h-3.5" />}
          </button>

          {/* Fullscreen toggle — maximise the brain to fill the screen */}
          <button
            onClick={toggleFullscreen}
            className={`p-1.5 rounded transition flex items-center gap-1 text-[10px] font-medium ${
              isFullscreen ? 'bg-violet-600 hover:bg-violet-500 text-white' : 'bg-gray-800 hover:bg-gray-700 text-gray-300'
            }`}
            title={isFullscreen ? 'Exit fullscreen (Esc)' : 'Maximise brain to fullscreen'}
          >
            {isFullscreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
            {isFullscreen ? 'Exit' : 'Fullscreen'}
          </button>
        </div>

        {/* Main content */}
        <div className="flex flex-1 min-h-0 overflow-hidden">
          {/* Canvas — ref gives accurate px dimensions for zoom calculation */}
          <div ref={graphContainerRef} className="flex-1 relative bg-gray-950 overflow-hidden min-w-0">
            {mode === '2d' ? (
              /* Fixed full-canvas brain background.
                 The brain SVG is pinned behind the node canvas and fills the
                 entire viewport. Nodes render on the transparent ForceGraph2D
                 canvas on top. The brain acts as a permanent wallpaper:
                 panning/zooming moves the nodes but the brain fills the screen.
                 This matches the reference screenshot exactly. */
              <div
                className="pointer-events-none absolute inset-0 z-0"
                style={{
                  opacity: brainReady ? 1 : 0,
                  transition: 'opacity 0.8s ease',
                }}
              >
                <CyberBrain />
              </div>
            ) : (
              /* 3D: same animated CyberBrain SVG as background (fills canvas) */
              <div className="pointer-events-none absolute inset-0 z-0" style={{ opacity: 0.7 }}>
                <CyberBrain />
              </div>
            )}
            {/* Ambient screen-fixed scan glow + vignette for a living feel */}
            <div
              className="pointer-events-none absolute inset-0 z-0"
              style={{
                background:
                  'radial-gradient(circle at 50% 44%, rgba(34,211,238,0.06), transparent 60%),' +
                  'radial-gradient(circle at 50% 50%, transparent 55%, rgba(2,6,23,0.55) 100%)',
              }}
            />
            {/* Cyber Brain badge */}
            <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 px-3 py-1 rounded-full bg-cyan-500/10 border border-cyan-400/30 backdrop-blur-sm flex items-center gap-1.5">
              <Brain className="w-3.5 h-3.5 text-cyan-300 animate-pulse" />
              <span className="text-[11px] font-semibold tracking-wide text-cyan-200">JARVIS CYBER BRAIN</span>
            </div>

            {(loading || (mode === '2d' && !FG2D) || (mode === '3d' && !FG3D)) && <GraphSkeleton />}

            {!loading && filteredData && mode === '2d' && FG2D && (
              <FG2D
                ref={graphRef}
                graphData={filteredData}
                width={canvasSize.w || undefined}
                height={canvasSize.h || undefined}
                nodeLabel={(n: any) => (n as GraphNode).label}
                nodeColor={nodeColor as any}
                nodeVal={nodeVal as any}
                linkColor={() => 'rgba(100,100,120,0.22)'}
                linkDirectionalParticles={2}
                linkDirectionalParticleSpeed={0.003}
                backgroundColor="rgba(3,7,18,0.55)"
                onNodeClick={(n: any) => setSelectedNode(n)}
                onZoom={handleZoom}
                onEngineTick={clampToBrain}
                onEngineStop={() => {
                  clampToBrain()
                  // Re-assert the saved (or factory-default) zoom after the
                  // engine settles so the physics simulation doesn't leave the
                  // view at the default fit. Same deterministic zoom-then-center
                  // order + brain-sync as the authoritative restore loop.
                  const g = graphRef.current
                  const saved = effectiveSavedZoom()
                  // CRITICAL GUARD: only re-assert the saved view while we are
                  // still in the initial restore (gate closed). Once the user
                  // has taken over (zoomCanSaveRef open via a real gesture or
                  // Save map), NEVER re-apply — otherwise a later engine stop
                  // yanks the view back AND clobbers lastTransformRef with the
                  // stale saved transform, so the next save persists the wrong
                  // centre → the map "saves & reloads on the side".
                  if (g && saved && !zoomCanSaveRef.current && canvasSize.w > 0 && canvasSize.h > 0) {
                    const { k: targetK, tx, ty } = expectedTransform(saved, canvasSize.w, canvasSize.h)
                    try { (g as any).zoom?.(targetK, 0); (g as any).centerAt?.(saved.x, saved.y, 0) } catch { /* noop */ }
                    syncBrainTransform(targetK, tx, ty)
                  }
                }}
                // Drag support — pin node to dropped position and persist it
                onNodeDragEnd={(node: any) => {
                  node.fx = node.x
                  node.fy = node.y
                  saveNodePosition((node as GraphNode).id, node.x!, node.y!)
                  setPositionsDirty(true)
                }}
                // Right-click on a node to un-pin it
                onNodeRightClick={(node: any) => {
                  node.fx = undefined
                  node.fy = undefined
                  const saved = loadSavedPositions()
                  delete saved[String((node as GraphNode).id)]
                  try { localStorage.setItem(POSITIONS_KEY, JSON.stringify(saved)) } catch { /* noop */ }
                  if (Object.keys(loadSavedPositions()).length === 0) setPositionsDirty(false)
                }}
                nodeCanvasObjectMode={() => 'replace'}
                nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
                  const n = node as GraphNode
                  const isActive = activeNodeIds.has(n.id)
                  const isDb = n.node_type === 'db_entity'
                  const color = communityColor(n.group, isDb, isActive)
                  const r = Math.max(2, 3 + Math.min((n.degree || 0) / 5, 6))
                  ctx.beginPath()
                  if (isDb) {
                    ctx.moveTo(n.x!, n.y! - r * 1.4); ctx.lineTo(n.x! + r * 1.4, n.y!)
                    ctx.lineTo(n.x!, n.y! + r * 1.4); ctx.lineTo(n.x! - r * 1.4, n.y!)
                    ctx.closePath()
                  } else { ctx.arc(n.x!, n.y!, r, 0, Math.PI * 2) }
                  ctx.fillStyle = color; ctx.fill()
                  if (isActive) { ctx.strokeStyle = '#fbbf24'; ctx.lineWidth = 1.5; ctx.stroke() }
                  if (globalScale >= 4 || n.id === selectedNode?.id) {
                    ctx.font = `${Math.min(4, 12 / globalScale)}px sans-serif`
                    ctx.textAlign = 'center'; ctx.textBaseline = 'top'
                    ctx.fillStyle = '#e2e8f0'
                    ctx.fillText(n.label.slice(0, 22), n.x!, n.y! + r + 1)
                  }
                }}
                d3AlphaDecay={0.012}
                d3VelocityDecay={0.45}
                warmupTicks={0}   // ZERO warmup — nodes start at phyllotaxis positions
                cooldownTicks={150}
              />
            )}

            {!loading && filteredData && mode === '3d' && FG3D && (
              <FG3D
                ref={graphRef}
                graphData={filteredData}
                width={canvasSize.w || undefined}
                height={canvasSize.h || undefined}
                nodeLabel={(n: any) => (n as GraphNode).label}
                nodeColor={nodeColor as any}
                nodeVal={nodeVal as any}
                linkColor={() => 'rgba(100,100,120,0.35)'}
                backgroundColor="rgba(3,7,18,0.55)"
                onNodeClick={(n: any) => setSelectedNode(n)}
                linkDirectionalParticles={1}
                linkDirectionalParticleSpeed={0.003}
                d3AlphaDecay={0.01}
                warmupTicks={60}
                cooldownTicks={200}
              />
            )}

            {!loading && !graphData && (
              <div className="absolute inset-0 flex items-center justify-center">
                <div className="text-center space-y-2">
                  <Network className="w-10 h-10 text-gray-600 mx-auto" />
                  <p className="text-gray-400 text-sm">No graph data.</p>
                  <p className="text-gray-500 text-xs">Run <code className="text-gray-300">graphify .</code> in the repo root.</p>
                </div>
              </div>
            )}

            {activeNodeIds.size > 0 && (
              <div className="absolute top-3 left-3 flex items-center gap-1.5 bg-gray-900/80 rounded px-2 py-1 text-[11px] text-amber-400 border border-amber-500/30">
                <span className="w-2 h-2 bg-amber-400 rounded-full animate-pulse" />
                {activeNodeIds.size} node{activeNodeIds.size > 1 ? 's' : ''} active
              </div>
            )}
          </div>

          {/* Right panel */}
          {panelOpen && (
          <div className="w-64 shrink-0 border-l border-gray-700/50 overflow-y-auto bg-gray-900/40 flex flex-col text-[11px]">
            {selectedNode ? (
              <div className="p-3 space-y-2">
                <div className="flex items-start justify-between gap-1">
                  <h3 className="font-semibold text-white text-xs break-all">{selectedNode.label}</h3>
                  <button onClick={() => setSelectedNode(null)}><X className="w-3.5 h-3.5 text-gray-400 hover:text-white shrink-0" /></button>
                </div>
                <InfoRow label="Community" value={selectedNode.community} />
                <InfoRow label="Type" value={selectedNode.node_type || 'code'} />
                {selectedNode.source_file && <InfoRow label="File" value={selectedNode.source_file} mono />}
                <InfoRow label="Degree" value={String(selectedNode.degree || 0)} />
                {selectedNode.db_type && <InfoRow label="DB Type" value={selectedNode.db_type} />}
                {activeNodeIds.has(selectedNode.id) && (
                  <div className="flex items-center gap-1.5 text-amber-400">
                    <span className="w-2 h-2 bg-amber-400 rounded-full animate-pulse" /> Agent-active
                  </div>
                )}
                <div className="h-1.5 rounded-full mt-1" style={{ background: communityColor(selectedNode.group, selectedNode.node_type === 'db_entity', activeNodeIds.has(selectedNode.id)) }} />

                {/* ── Vault Notes for selected node ─────────────────────────── */}
                <NodeVaultPanel nodeId={selectedNode.id} nodeLabel={selectedNode.label} />
              </div>
            ) : (
              <div className="p-3 text-gray-500">Click a node to inspect it.</div>
            )}
            <hr className="border-gray-700/50" />

            {/* Community legend */}
            <div className="p-3 space-y-1">
              <h3 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1"><Layers className="w-3 h-3" /> Communities</h3>
              {communities.slice(0, 14).map((c, i) => (
                <button key={c.name} onClick={() => setCommunityFilter(communityFilter === c.name ? '' : c.name)}
                  className={`w-full flex items-center gap-1.5 rounded px-1.5 py-0.5 transition text-left ${communityFilter === c.name ? 'bg-gray-700' : 'hover:bg-gray-800/60'}`}>
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ background: PALETTE[i % PALETTE.length] }} />
                  <span className="truncate text-gray-300 flex-1">{c.name}</span>
                  <span className="text-gray-600 text-[10px]">{c.count}</span>
                </button>
              ))}
              <button className="w-full flex items-center gap-1.5 mt-0.5" onClick={() => setCommunityFilter(communityFilter === 'DB Data' ? '' : 'DB Data')}>
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: DB_COLOR }} />
                <span className="text-orange-400">DB Data</span>
              </button>
            </div>

            <hr className="border-gray-700/50" />

            {/* Headroom mini */}
            {headroom && (
              <div className="p-3 space-y-1">
                <h3 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1"><Sparkles className="w-3 h-3 text-emerald-400" /> Headroom</h3>
                <div className="text-gray-500">{headroom.calls?.toLocaleString()} calls · {headroom.reduction_pct}% saved</div>
                <div className="h-1 bg-gray-900 rounded-full overflow-hidden">
                  <div className="h-full bg-emerald-500" style={{ width: `${Math.max(2, 100 - (headroom.reduction_pct || 0))}%` }} />
                </div>
              </div>
            )}

            {/* Agent usage mini */}
            {agentUsage?.agents?.length > 0 && (
              <div className="p-3 space-y-1">
                <h3 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1"><Gauge className="w-3 h-3 text-cyan-400" /> Token usage</h3>
                {agentUsage.agents.slice(0, 5).map((a: any) => {
                  const max = Math.max(...agentUsage.agents.map((x: any) => x.total_tokens), 1)
                  const pct = Math.round((a.total_tokens / max) * 100)
                  return (
                    <div key={a.agent_role}>
                      <div className="flex justify-between text-[10px] text-gray-600 mb-0.5">
                        <span className="truncate pr-1">{a.agent_role}</span><span>{a.total_tokens.toLocaleString()}</span>
                      </div>
                      <div className="h-1 bg-gray-900 rounded-full overflow-hidden">
                        <div className="h-full bg-cyan-500/70" style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {/* Knowledge store — expands live as JARVIS learns from conversations */}
            {knowledge.length > 0 && (
              <div className="p-3 space-y-1">
                <h3 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1">
                  <Brain className={`w-3 h-3 ${newKnowledgeCount > 0 ? 'text-cyan-300 animate-pulse' : 'text-purple-400'}`} />
                  Knowledge ({knowledge.length})
                  {newKnowledgeCount > 0 && (
                    <span className="ml-auto px-1 py-0.5 bg-cyan-500/20 text-cyan-300 rounded text-[9px] font-bold">
                      +{newKnowledgeCount} new
                    </span>
                  )}
                </h3>
                <div className="space-y-1 max-h-40 overflow-y-auto">
                  {knowledge.slice(0, 6).map(k => (
                    <div key={k.id} className="flex items-start justify-between gap-1 bg-gray-900/40 rounded p-1.5">
                      <div className="min-w-0">
                        {k.title && <div className="text-gray-200 text-[10px]">{k.title}</div>}
                        <div className="text-gray-600 text-[10px] break-words">{k.content?.slice(0, 60)}{(k.content?.length || 0) > 60 ? '…' : ''}</div>
                      </div>
                      <button onClick={() => deleteKnowledge(k.id)} className="shrink-0">
                        <Trash2 className="w-3 h-3 text-gray-600 hover:text-red-400" />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* JARVIS synthesized strategies */}
            {strategies.length > 0 && (
              <div className="p-3 space-y-1">
                <h3 className="text-[10px] font-semibold text-gray-400 flex items-center gap-1">
                  <Sparkles className="w-3 h-3 text-violet-400 animate-pulse" />
                  Strategies ({strategies.length})
                </h3>
                <div className="space-y-1.5 max-h-52 overflow-y-auto">
                  {strategies.map((s, i) => (
                    <div key={i} className="bg-gray-900/50 rounded p-2 space-y-1">
                      <div className="text-[10px] font-medium text-violet-300 truncate">{s.name || s.title}</div>
                      {(s.indicators || []).length > 0 && (
                        <div className="flex flex-wrap gap-0.5">
                          {(s.indicators || []).slice(0, 4).map((ind: string) => (
                            <span key={ind} className="px-1 py-0.5 bg-violet-900/40 text-violet-300 rounded text-[9px]">{ind}</span>
                          ))}
                        </div>
                      )}
                      <div className="text-[9px] text-gray-500">
                        {(s.symbols || []).length > 0 ? (s.symbols as string[]).join(', ') : 'All markets'}
                        {s.node_count ? ` · ${s.node_count} intel nodes` : ''}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
          )}
        </div>

          {/* ── Brain Learning Console (JARVIS learning from /insights) ─── */}
          <BrainLearningConsole />

          {/* ── Live Signals in sidebar ───────────────────────────────────── */}
          <hr className="border-gray-700/50" />
          <SignalsOverlayPanel />

          {/* ── Cross-navigation quick links ─────────────────────────────── */}
          <div className="p-3 border-t border-gray-700/50">
            <h4 className="text-[10px] font-semibold text-gray-400 mb-2">Navigate</h4>
            <div className="flex flex-col gap-1">
              <Link href="/insights" className="flex items-center gap-1.5 text-[10px] text-emerald-400 hover:text-emerald-300 py-0.5">
                <Activity className="w-3 h-3" /> Insights (learning source)
              </Link>
              <Link href="/vault" className="flex items-center gap-1.5 text-[10px] text-violet-400 hover:text-violet-300 py-0.5">
                <BookOpen className="w-3 h-3" /> Obsidian Vault
              </Link>
              <Link href="/telegram-signals" className="flex items-center gap-1.5 text-[10px] text-blue-400 hover:text-blue-300 py-0.5">
                <MessageSquareText className="w-3 h-3" /> Telegram Signals
              </Link>
              <Link href="/mt5-live" className="flex items-center gap-1.5 text-[10px] text-green-400 hover:text-green-300 py-0.5">
                <Monitor className="w-3 h-3" /> MT5 Live
              </Link>
            </div>
          </div>

      </div>
    </>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-1.5 text-[11px]">
      <span className="text-gray-500 w-16 shrink-0">{label}</span>
      <span className={`text-gray-200 break-all ${mono ? 'font-mono text-[10px]' : ''}`}>{value}</span>
    </div>
  )
}
