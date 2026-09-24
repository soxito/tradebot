/**
 * OblivionPrimitive — lightweight-charts v4 series primitive that draws the
 * OBLIVION AI TRADING ENGINE v4.0 overlay bundle the backend produces
 * (`app.signals.oblivion.oblivion_to_overlays`):
 *
 *   bands        session shading (Asia / London / New York), full pane height
 *   boxes        fair-value gaps and order blocks, from formation bar to +N bars
 *   labels       HH / HL / LH / LL swing tags, BOS / CHOCH, LIQ sweeps
 *   markers      BUY / SELL diamonds (v4 markers cannot draw diamonds)
 *   price_lines  fib 0.382 / 0.5 / 0.618 and the last signal's Entry / SL / TP1-3
 *   dashboard    the Pine table (bull/bear score, bias, session, news, signal…)
 *
 * Two pane views: a `bottom` one for bands + boxes (behind the candles) and a
 * `top` one for everything that must stay legible over price.
 *
 * Usage:
 *   const prim = new OblivionPrimitive()
 *   candleSeries.attachPrimitive(prim)
 *   prim.setBundle(bundle)              // from any oblivion-overlay endpoint
 *   prim.setLayers({ dashboard: false }) // optional per-layer toggles
 */
import type {
  ISeriesPrimitive,
  SeriesAttachedParameter,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  SeriesPrimitivePaneViewZOrder,
  Time,
  ISeriesApi,
  IChartApi,
} from 'lightweight-charts'
import type { CanvasRenderingTarget2D } from 'fancy-canvas'

export interface OblivionBox { time: number; end_time?: number; top: number; bottom: number; fill: string; border: string; label?: string; kind?: string }
export interface OblivionLabel { time: number; price: number; text: string; color: string; bg?: string; position: 'above' | 'below'; size?: 'tiny' | 'small' }
export interface OblivionMarker { time: number; position: 'aboveBar' | 'belowBar'; shape: 'diamond' | 'arrowUp' | 'arrowDown' | 'circle'; color: string; text?: string; detail?: string }
export interface OblivionPriceLine { price: number; color: string; style: 'solid' | 'dashed' | 'dotted'; width?: number; title: string; from_time?: number | null }
export interface OblivionBand { start: number; end: number; color: string; label?: string }
export interface OblivionDashboard { title: string; rows: [string, string, string?][]; score?: { bull?: number; bear?: number; required?: number; scalp_min?: number; sniper_min?: number } }
export interface OblivionHBand { top: number; bottom: number; color: string; label?: string }
export interface OblivionLine { name: string; color: string; width: number; points: { time: number; value: number }[] }

export interface OblivionOverlayBundle {
  boxes: OblivionBox[]
  labels: OblivionLabel[]
  markers: OblivionMarker[]
  price_lines: OblivionPriceLine[]
  bands: OblivionBand[]
  hbands: OblivionHBand[]
  lines: OblivionLine[]
  dashboard: OblivionDashboard | null
  summary?: string | null
  outcomes?: any
}

export type OblivionLayer = 'bands' | 'boxes' | 'labels' | 'markers' | 'price_lines' | 'dashboard' | 'hbands' | 'lines' | 'vwap' | 'ema' | 'pd' | 'ote' | 'outcomes'
export const OBLIVION_LAYERS: OblivionLayer[] = ['bands', 'boxes', 'labels', 'markers', 'price_lines', 'dashboard']

type Layers = Record<OblivionLayer, boolean>
const ALL_ON: Layers = { bands: true, boxes: true, labels: true, markers: true, price_lines: true, dashboard: true, hbands: true, lines: true, vwap: true, ema: true, pd: true, ote: true, outcomes: true }

const FONT = '-apple-system, system-ui, "Segoe UI", sans-serif'

function dashFor(style: OblivionPriceLine['style'], hpr: number): number[] {
  if (style === 'dashed') return [Math.round(6 * hpr), Math.round(4 * hpr)]
  if (style === 'dotted') return [Math.round(2 * hpr), Math.round(3 * hpr)]
  return []
}

/** x pixel for a unix-seconds time, extrapolating past the last bar so
 *  boxes / lines can extend to the right the way Pine draws them. */
function xFor(chart: IChartApi, t: number): number | null {
  const ts = chart.timeScale()
  const x = ts.timeToCoordinate(t as Time)
  if (x !== null) return x
  // Off the visible / loaded range — project from the logical scale using
  // the spacing of the last two bars we can resolve.
  const range = ts.getVisibleRange()
  if (!range) return null
  const from = Number(range.from), to = Number(range.to)
  const xFrom = ts.timeToCoordinate(range.from), xTo = ts.timeToCoordinate(range.to)
  if (xFrom === null || xTo === null || to === from) return null
  const pxPerSec = (xTo - xFrom) / (to - from)
  return xTo + (t - to) * pxPerSec
}

class BackRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private readonly p: OblivionPrimitive) {}
  draw(target: CanvasRenderingTarget2D): void {
    const { bundle, series, chart, layers } = this.p
    if (!bundle || !series || !chart) return
    target.useBitmapCoordinateSpace(scope => {
      const ctx = scope.context
      const hpr = scope.horizontalPixelRatio, vpr = scope.verticalPixelRatio
      const W = scope.bitmapSize.width, H = scope.bitmapSize.height

      if (layers.bands) {
        for (const b of bundle.bands ?? []) {
          const x0 = xFor(chart, b.start), x1 = xFor(chart, b.end)
          if (x0 === null || x1 === null) continue
          const bx0 = Math.max(0, Math.round(x0 * hpr)), bx1 = Math.min(W, Math.round(x1 * hpr))
          if (bx1 <= bx0) continue
          ctx.fillStyle = b.color
          ctx.fillRect(bx0, 0, bx1 - bx0, H)
          if (b.label && bx1 - bx0 >= 60 * hpr) {
            ctx.save()
            ctx.beginPath(); ctx.rect(bx0, 0, bx1 - bx0, H); ctx.clip()
            ctx.fillStyle = 'rgba(255,255,255,0.28)'
            ctx.font = `bold ${Math.round(9 * vpr)}px ${FONT}`
            ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'
            ctx.fillText(b.label, (bx0 + bx1) / 2, H - Math.round(6 * vpr))
            ctx.restore()
          }
        }
      }

      if (layers.boxes) {
        for (const box of bundle.boxes ?? []) {
          const yTop = series.priceToCoordinate(box.top), yBot = series.priceToCoordinate(box.bottom)
          const xL = xFor(chart, box.time)
          if (yTop === null || yBot === null || xL === null) continue
          const xR = box.end_time ? xFor(chart, box.end_time) : null
          const x1 = Math.round(xL * hpr)
          const x2 = xR === null ? W : Math.min(W, Math.round(xR * hpr))
          const y1 = Math.round(Math.min(yTop, yBot) * vpr), y2 = Math.round(Math.max(yTop, yBot) * vpr)
          const w = x2 - x1, h = y2 - y1
          if (w <= 0 || h <= 0) continue
          ctx.fillStyle = box.fill
          ctx.fillRect(x1, y1, w, h)
          ctx.strokeStyle = box.border
          ctx.lineWidth = Math.max(1, Math.round(hpr))
          ctx.strokeRect(x1 + 0.5, y1 + 0.5, w - 1, h - 1)
          if (box.label && w >= 24 * hpr) {
            ctx.font = `${Math.round(9 * vpr)}px ${FONT}`
            ctx.fillStyle = box.border
            ctx.textAlign = 'left'; ctx.textBaseline = 'top'
            ctx.fillText(box.label, x1 + Math.round(3 * hpr), y1 + Math.round(2 * vpr))
          }
        }
      }

      // hbands — premium/discount shading full-width between price levels
      if ((layers as any).hbands ?? (layers as any).pd ?? layers.bands) {
        for (const hb of (bundle as any).hbands ?? []) {
          const yTop = series.priceToCoordinate(hb.top), yBot = series.priceToCoordinate(hb.bottom)
          if (yTop === null || yBot === null) continue
          const y1 = Math.round(Math.min(yTop, yBot) * vpr), y2 = Math.round(Math.max(yTop, yBot) * vpr)
          const h = y2 - y1
          if (h <= 0) continue
          ctx.fillStyle = hb.color
          ctx.fillRect(0, y1, W, h)
          if (hb.label) {
            ctx.fillStyle = 'rgba(255,255,255,0.35)'
            ctx.font = `bold ${Math.round(8 * vpr)}px ${FONT}`
            ctx.textAlign = 'right'; ctx.textBaseline = 'middle'
            ctx.fillText(hb.label, W - Math.round(6 * hpr), y1 + h / 2)
          }
        }
      }

      // lines — VWAP / EMAs (and OTE when emitted as hb? no, OTE is price_lines)
      if ((layers as any).lines ?? (layers as any).vwap ?? (layers as any).ema) {
        for (const line of (bundle as any).lines ?? []) {
          const pts = line.points ?? []
          if (pts.length < 2) continue
          ctx.strokeStyle = line.color
          ctx.lineWidth = Math.max(1, Math.round((line.width ?? 1) * hpr))
          if (line.name === 'VWAP') ctx.setLineDash([])
          else if (line.name?.includes('OTE')) ctx.setLineDash(dashFor('dotted', hpr))
          else ctx.setLineDash([])
          ctx.beginPath()
          let started = false
          for (const pt of pts) {
            const x = xFor(chart, pt.time)
            const y = series.priceToCoordinate(pt.value)
            if (x === null || y === null) continue
            if (!started) { ctx.moveTo(Math.round(x * hpr), Math.round(y * vpr)); started = true } else ctx.lineTo(Math.round(x * hpr), Math.round(y * vpr))
          }
          ctx.stroke()
          ctx.setLineDash([])
        }
      }
    })
  }
}

class FrontRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private readonly p: OblivionPrimitive) {}
  draw(target: CanvasRenderingTarget2D): void {
    const { bundle, series, chart, layers } = this.p
    if (!bundle || !series || !chart) return
    target.useBitmapCoordinateSpace(scope => {
      const ctx = scope.context
      const hpr = scope.horizontalPixelRatio, vpr = scope.verticalPixelRatio
      const W = scope.bitmapSize.width, H = scope.bitmapSize.height

      // ── price lines: fib + entry / SL / TP ────────────────────────────────
      if (layers.price_lines) {
        for (const pl of bundle.price_lines ?? []) {
          const y = series.priceToCoordinate(pl.price)
          if (y === null) continue
          const py = Math.round(y * vpr) + 0.5
          let x0 = 0
          if (pl.from_time) {
            const xs = xFor(chart, pl.from_time)
            if (xs !== null) x0 = Math.max(0, Math.round(xs * hpr))
          }
          ctx.save()
          ctx.strokeStyle = pl.color
          ctx.lineWidth = Math.max(1, Math.round((pl.width ?? 1) * hpr))
          ctx.setLineDash(dashFor(pl.style, hpr))
          ctx.beginPath(); ctx.moveTo(x0, py); ctx.lineTo(W, py); ctx.stroke()
          ctx.restore()
          // pill label at the right edge
          ctx.font = `bold ${Math.round(9 * vpr)}px ${FONT}`
          const tw = ctx.measureText(pl.title).width
          const padX = Math.round(4 * hpr)
          const bw = tw + padX * 2, bh = Math.round(12 * vpr)
          const bx = W - bw - Math.round(4 * hpr), by = py - bh / 2
          ctx.fillStyle = 'rgba(15,23,42,0.85)'
          ctx.fillRect(bx, by, bw, bh)
          ctx.strokeStyle = pl.color; ctx.lineWidth = 1
          ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1)
          ctx.fillStyle = pl.color
          ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
          ctx.fillText(pl.title, bx + padX, py)
        }
      }

      // ── labels: HH/HL/LH/LL, BOS/CHOCH, LIQ ───────────────────────────────
      if (layers.labels) {
        for (const lb of bundle.labels ?? []) {
          const x = xFor(chart, lb.time), y = series.priceToCoordinate(lb.price)
          if (x === null || y === null) continue
          const px = Math.round(x * hpr), py = Math.round(y * vpr)
          if (px < -40 * hpr || px > W + 40 * hpr) continue
          const fs = Math.round((lb.size === 'small' ? 10 : 8.5) * vpr)
          ctx.font = `bold ${fs}px ${FONT}`
          const tw = ctx.measureText(lb.text).width
          const padX = Math.round(3 * hpr), bh = Math.round(fs + 4 * vpr), bw = tw + padX * 2
          const gap = Math.round(6 * vpr), tip = Math.round(3 * vpr)
          const above = lb.position === 'above'
          const by = above ? py - gap - tip - bh : py + gap + tip
          const bx = px - bw / 2
          ctx.fillStyle = lb.bg ?? 'rgba(15,23,42,0.85)'
          ctx.beginPath()
          ctx.rect(bx, by, bw, bh)
          ctx.fill()
          // pointer tip toward the bar
          ctx.beginPath()
          if (above) { ctx.moveTo(px - tip, by + bh); ctx.lineTo(px + tip, by + bh); ctx.lineTo(px, by + bh + tip) }
          else { ctx.moveTo(px - tip, by); ctx.lineTo(px + tip, by); ctx.lineTo(px, by - tip) }
          ctx.closePath(); ctx.fill()
          ctx.fillStyle = lb.color
          ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
          ctx.fillText(lb.text, px, by + bh / 2)
        }
      }

      // ── diamonds: BUY / SELL ──────────────────────────────────────────────
      if (layers.markers) {
        for (const m of bundle.markers ?? []) {
          const x = xFor(chart, m.time)
          if (x === null) continue
          const px = Math.round(x * hpr)
          if (px < -20 * hpr || px > W + 20 * hpr) continue
          // anchor to the bar's high / low (fed via setBars)
          const anchor = this.p.barExtreme(m.time, m.position === 'aboveBar')
          const y = anchor !== null ? series.priceToCoordinate(anchor) : null
          if (y === null) continue
          const r = Math.round(7 * vpr)
          const cy = Math.round(y * vpr) + (m.position === 'aboveBar' ? -(r + Math.round(4 * vpr)) : r + Math.round(4 * vpr))
          ctx.fillStyle = m.color
          ctx.beginPath()
          if (m.shape === 'diamond') {
            ctx.moveTo(px, cy - r); ctx.lineTo(px + r, cy); ctx.lineTo(px, cy + r); ctx.lineTo(px - r, cy); ctx.closePath()
          } else if (m.shape === 'circle') {
            ctx.arc(px, cy, r * 0.7, 0, Math.PI * 2)
          } else {
            const up = m.shape === 'arrowUp'
            ctx.moveTo(px, up ? cy - r : cy + r); ctx.lineTo(px + r, up ? cy + r : cy - r); ctx.lineTo(px - r, up ? cy + r : cy - r); ctx.closePath()
          }
          ctx.fill()
          ctx.strokeStyle = 'rgba(15,23,42,0.9)'; ctx.lineWidth = 1; ctx.stroke()
          if (m.text) {
            ctx.font = `bold ${Math.round(9 * vpr)}px ${FONT}`
            ctx.fillStyle = m.color
            ctx.textAlign = 'center'; ctx.textBaseline = m.position === 'aboveBar' ? 'bottom' : 'top'
            ctx.fillText(m.text, px, m.position === 'aboveBar' ? cy - r - Math.round(2 * vpr) : cy + r + Math.round(2 * vpr))
          }
        }
      }

      // ── dashboard table (top-right) ───────────────────────────────────────
      if (layers.dashboard && bundle.dashboard && bundle.dashboard.rows?.length) {
        const d = bundle.dashboard
        const fs = Math.round(9.5 * vpr)
        ctx.font = `${fs}px ${FONT}`
        const rowH = Math.round(14 * vpr), padX = Math.round(6 * hpr)
        let col0 = 0, col1 = 0
        for (const [k, v] of d.rows) { col0 = Math.max(col0, ctx.measureText(k).width); col1 = Math.max(col1, ctx.measureText(v).width) }
        ctx.font = `bold ${fs}px ${FONT}`
        col1 = Math.max(col1, ctx.measureText(d.title).width - col0 - padX)
        const bw = col0 + col1 + padX * 3, bh = rowH * (d.rows.length + 1) + Math.round(4 * vpr)
        const bx = W - bw - Math.round(64 * hpr), by = Math.round(8 * vpr)
        if (bx > 0 && by + bh < H) {
          ctx.fillStyle = 'rgba(15,23,42,0.82)'
          ctx.fillRect(bx, by, bw, bh)
          ctx.strokeStyle = 'rgba(148,163,184,0.35)'; ctx.lineWidth = 1
          ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1)
          ctx.fillStyle = '#f8fafc'
          ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
          ctx.fillText(d.title, bx + padX, by + rowH / 2 + Math.round(2 * vpr))
          ctx.font = `${fs}px ${FONT}`
          d.rows.forEach(([k, v, c], i) => {
            const y = by + rowH * (i + 1) + rowH / 2 + Math.round(2 * vpr)
            ctx.fillStyle = 'rgba(226,232,240,0.9)'
            ctx.textAlign = 'left'
            ctx.fillText(k, bx + padX, y)
            ctx.fillStyle = c ?? '#f8fafc'
            ctx.textAlign = 'right'
            ctx.fillText(v, bx + bw - padX, y)
          })
        }
      }
    })
  }
}

class BackView implements ISeriesPrimitivePaneView {
  constructor(private readonly p: OblivionPrimitive) {}
  zOrder(): SeriesPrimitivePaneViewZOrder { return 'bottom' }
  renderer(): ISeriesPrimitivePaneRenderer { return new BackRenderer(this.p) }
}
class FrontView implements ISeriesPrimitivePaneView {
  constructor(private readonly p: OblivionPrimitive) {}
  zOrder(): SeriesPrimitivePaneViewZOrder { return 'top' }
  renderer(): ISeriesPrimitivePaneRenderer { return new FrontRenderer(this.p) }
}

export class OblivionPrimitive implements ISeriesPrimitive<Time> {
  bundle: OblivionOverlayBundle | null = null
  layers: Layers = { ...ALL_ON }
  series: ISeriesApi<'Candlestick'> | null = null
  chart: IChartApi | null = null

  private _views: ISeriesPrimitivePaneView[] = [new BackView(this), new FrontView(this)]
  private _requestUpdate?: () => void
  /** time (s) → [high, low] of the drawn bar, for anchoring diamonds. */
  private _extremes = new Map<number, [number, number]>()

  attached(param: SeriesAttachedParameter<Time>): void {
    this.series = param.series as ISeriesApi<'Candlestick'>
    this.chart = param.chart
    this._requestUpdate = param.requestUpdate
  }
  detached(): void {
    this.series = null; this.chart = null; this._requestUpdate = undefined
  }

  /** Feed the candles the chart draws so markers sit on the bar's high/low. */
  setBars(bars: { time: number; high: number; low: number }[]): void {
    this._extremes = new Map(bars.map(b => [Number(b.time), [b.high, b.low]]))
    this._requestUpdate?.()
  }

  barExtreme(time: number, high: boolean): number | null {
    const e = this._extremes.get(Number(time))
    if (e) return high ? e[0] : e[1]
    // fall back to the marker's nearest label / price line so it still draws
    const lb = this.bundle?.labels?.find(l => l.time === time)
    return lb ? lb.price : null
  }

  setBundle(bundle: OblivionOverlayBundle | null): void {
    this.bundle = bundle
    this._requestUpdate?.()
  }

  setLayers(layers: Partial<Layers>): void {
    this.layers = { ...this.layers, ...layers }
    this._requestUpdate?.()
  }

  setVisible(visible: boolean): void {
    if (visible) this.layers = { ...ALL_ON }
    else { const off: any = {}; for (const k of Object.keys(ALL_ON) as (keyof Layers)[]) off[k]=false; this.layers = off }
    this._requestUpdate?.()
  }

  updateAllViews(): void { /* read live in the renderers */ }
  paneViews(): readonly ISeriesPrimitivePaneView[] { return this._views }
}

/** Attach to a candlestick series. */
export function attachOblivionPrimitive(series: ISeriesApi<'Candlestick'>): OblivionPrimitive {
  const prim = new OblivionPrimitive()
  ;(series as any).attachPrimitive(prim)
  return prim
}
