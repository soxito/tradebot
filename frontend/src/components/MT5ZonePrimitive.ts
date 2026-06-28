/**
 * MT5ZonePrimitive — lightweight-charts v4 custom series primitive that draws
 * Smart-Money-Concept zones as shaded rectangle boxes which extend from the
 * zone's formation bar to the right edge of the chart (TradingView-style).
 *
 * This replaces the previous full-width horizontal price-line approach so the
 * chart matches the clean supply/demand-box aesthetic: each order block or
 * fair-value-gap is a translucent rectangle with a thin border and a small
 * right-aligned label, instead of two lines spanning the whole pane.
 *
 * Usage:
 *   const prim = new ZoneBoxPrimitive()
 *   candleSeries.attachPrimitive(prim)
 *   prim.setBoxes([{ time, top, bottom, fill, border, label, labelColor }])
 */
import type {
  ISeriesPrimitive,
  SeriesAttachedParameter,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  Time,
  ISeriesApi,
  IChartApi,
} from 'lightweight-charts'
import type { CanvasRenderingTarget2D } from 'fancy-canvas'

export interface ZoneBox {
  time: Time          // formation time (left edge of the box)
  top: number         // upper price bound
  bottom: number      // lower price bound
  fill: string        // translucent fill colour (rgba)
  border: string      // border colour (rgba)
  label?: string      // optional right-aligned label (e.g. "OB", "FVG")
  labelColor?: string // label text colour
}

class ZoneBoxRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(
    private readonly _boxes: ZoneBox[],
    private readonly _series: ISeriesApi<'Candlestick'>,
    private readonly _chart: IChartApi,
  ) {}

  draw(target: CanvasRenderingTarget2D): void {
    const series = this._series
    const timeScale = this._chart.timeScale()

    target.useBitmapCoordinateSpace(scope => {
      const ctx = scope.context
      const hpr = scope.horizontalPixelRatio
      const vpr = scope.verticalPixelRatio
      const rightEdge = scope.bitmapSize.width

      for (const box of this._boxes) {
        const yTop = series.priceToCoordinate(box.top)
        const yBot = series.priceToCoordinate(box.bottom)
        const xLeft = timeScale.timeToCoordinate(box.time)
        if (yTop === null || yBot === null || xLeft === null) continue

        const x1 = Math.round(xLeft * hpr)
        const y1 = Math.round(Math.min(yTop, yBot) * vpr)
        const y2 = Math.round(Math.max(yTop, yBot) * vpr)
        const w = rightEdge - x1
        const h = y2 - y1
        if (w <= 0 || h <= 0) continue

        // Filled rectangle
        ctx.fillStyle = box.fill
        ctx.fillRect(x1, y1, w, h)

        // Thin top + bottom border for definition
        ctx.strokeStyle = box.border
        ctx.lineWidth = Math.max(1, Math.round(hpr))
        ctx.beginPath()
        ctx.moveTo(x1, y1); ctx.lineTo(rightEdge, y1)
        ctx.moveTo(x1, y2); ctx.lineTo(rightEdge, y2)
        ctx.stroke()

        // Right-aligned label
        if (box.label) {
          const fontPx = Math.round(10 * vpr)
          ctx.font = `${fontPx}px -apple-system, system-ui, sans-serif`
          ctx.fillStyle = box.labelColor ?? box.border
          ctx.textAlign = 'right'
          ctx.textBaseline = 'bottom'
          ctx.fillText(box.label, rightEdge - Math.round(6 * hpr), y1 - Math.round(2 * vpr))
        }
      }
    })
  }
}

class ZoneBoxPaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly _source: ZoneBoxPrimitive) {}
  renderer(): ISeriesPrimitivePaneRenderer {
    return new ZoneBoxRenderer(
      this._source.boxes,
      this._source.series!,
      this._source.chart!,
    )
  }
}

export class ZoneBoxPrimitive implements ISeriesPrimitive<Time> {
  boxes: ZoneBox[] = []
  series: ISeriesApi<'Candlestick'> | null = null
  chart: IChartApi | null = null

  private _paneViews: ZoneBoxPaneView[] = [new ZoneBoxPaneView(this)]
  private _requestUpdate?: () => void

  attached(param: SeriesAttachedParameter<Time>): void {
    this.series = param.series as ISeriesApi<'Candlestick'>
    this.chart = param.chart
    this._requestUpdate = param.requestUpdate
  }

  detached(): void {
    this.series = null
    this.chart = null
    this._requestUpdate = undefined
  }

  setBoxes(boxes: ZoneBox[]): void {
    this.boxes = boxes
    this._requestUpdate?.()
  }

  updateAllViews(): void { /* boxes are read live in renderer() */ }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._paneViews
  }
}
