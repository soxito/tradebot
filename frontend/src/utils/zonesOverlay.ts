/**
 * zonesOverlay — lightweight-charts Series Primitive that draws the desk's
 * zone read directly on the price pane:
 *   • supply/demand rectangles extending right from their creation bar
 *   • fib retracement bands (premium red / equilibrium green / discount blue)
 *   • trendline channel rails
 *
 * Everything is drawn in one canvas pass per frame via a bottom-z-order pane
 * view, so it never intercepts crosshair events and costs nothing when the
 * data is empty.
 */
import type {
  ISeriesApi,
  IChartApi,
  SeriesAttachedParameter,
  ISeriesPrimitive,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  SeriesPrimitivePaneViewZOrder,
} from 'lightweight-charts';
import type { CanvasRenderingTarget2D } from 'fancy-canvas';

export interface ZoneRect {
  type: 'supply' | 'demand';
  high: number;
  low: number;
  state: string;
  strength: number;
  createdTime: number; // ms epoch
}

export interface FibBand {
  ratio: number | null;
  price: number | null;
}

export interface FibPayload {
  swing?: { direction?: string | null; high?: number | null; low?: number | null } | null;
  bands: FibBand[];
}

export interface ChannelLine {
  kind: string;
  upperStart: number; // fitted price at window start
  upperNow: number;   // fitted price at last bar
  lowerStart: number;
  lowerNow: number;
  breakout?: string | null;
}

export interface ChartPatternOverlay {
  kind: string;
  category: string;
  signal: string;
  confidence: number;
  broken?: boolean;
  breakout?: string | null;
  neckline?: number | null;
  top?: number | null;
  bottom?: number | null;
  upper_now?: number | null;
  lower_now?: number | null;
  upper?: { m: number; b: number } | null;
  lower?: { m: number; b: number } | null;
  flag_high?: number | null;
  flag_low?: number | null;
  flag_start?: number;
  flag_end?: number;
  target?: number | null;
  invalidation?: number | null;
}

export interface ZonesData {
  supply_zones: ZoneRect[];
  demand_zones: ZoneRect[];
  channels?: ChannelLine[];
  s_r_levels?: { price: number; type: string; touches: number }[];
  fib?: FibPayload;
  chart_patterns?: ChartPatternOverlay[];
  /**
   * Bitcoin 1064-day cycle boxes — full-height green (bull) / red (bear)
   * ranges across [startMs, endMs], the way the cycle chart draws them.
   * Projected boxes render dimmer so history reads apart from forecast.
   */
  cycle_windows?: CycleWindowBox[];
}

export interface CycleWindowBox {
  startMs: number; // ms epoch
  endMs: number;
  phase: 'bull' | 'bear';
  projected: boolean;
}

const COLORS = {
  demandFill: 'rgba(34, 197, 94, 0.14)',
  demandEdge: 'rgba(34, 197, 94, 0.55)',
  supplyFill: 'rgba(239, 68, 68, 0.13)',
  supplyEdge: 'rgba(239, 68, 68, 0.5)',
  premium: 'rgba(239, 68, 68, 0.07)',
  equilibrium: 'rgba(34, 197, 94, 0.08)',
  discount: 'rgba(59, 130, 246, 0.09)',
  fibLine: 'rgba(148, 163, 184, 0.45)',
  goldenLine: 'rgba(250, 204, 21, 0.65)',
  channel: 'rgba(96, 165, 250, 0.75)',
  cycleBull: 'rgba(34, 197, 94, 0.10)',
  cycleBullProjected: 'rgba(34, 197, 94, 0.05)',
  cycleBear: 'rgba(239, 68, 68, 0.10)',
  cycleBearProjected: 'rgba(239, 68, 68, 0.05)',
  cycleEdge: 'rgba(148, 163, 184, 0.5)',
  chartBull: 'rgba(34, 197, 94, 0.70)',
  chartBear: 'rgba(239, 68, 68, 0.70)',
  chartNeutral: 'rgba(148, 163, 184, 0.65)',
  chartBroken: 'rgba(250, 204, 21, 0.70)',
  chartFillBull: 'rgba(34, 197, 94, 0.06)',
  chartFillBear: 'rgba(239, 68, 68, 0.06)',
};

/** Horizontal band: filled strip between two y values across [x0, x1].
 *  `fullHeight` spans the whole pane — the cycle boxes' shape. */
interface Band { x0: number; x1: number; yTop: number; yBot: number; fill: string; fullHeight?: boolean; edge?: boolean; label?: string; labelAlpha?: number }
/** Straight segment from (x0,y0) → (x1,y1). */
interface Segment { x0: number; x1: number; y0: number; y1: number; stroke: string }

class ZonesPaneRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private bands: Band[], private segments: Segment[]) {}

  draw(target: CanvasRenderingTarget2D) {
    target.useMediaCoordinateSpace((scope) => {
      const ctx = scope.context;
      const height = scope.mediaSize.height;
      for (const b of this.bands) {
        const yTop = b.fullHeight ? 0 : Math.min(b.yTop, b.yBot);
        const yBot = b.fullHeight ? height : Math.max(b.yTop, b.yBot);
        ctx.fillStyle = b.fill;
        ctx.fillRect(b.x0, yTop, b.x1 - b.x0, yBot - yTop);
        if (b.edge) {
          ctx.strokeStyle = COLORS.cycleEdge;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(b.x0, yTop);
          ctx.lineTo(b.x0, yBot);
          ctx.moveTo(b.x1, yTop);
          ctx.lineTo(b.x1, yBot);
          ctx.stroke();
        }
        if (b.fullHeight && b.label) {
          const bandWidth = b.x1 - b.x0;
          if (bandWidth >= 50) {
            ctx.save();
            ctx.beginPath();
            ctx.rect(b.x0 + 1, yTop, bandWidth - 2, yBot - yTop);
            ctx.clip();
            ctx.fillStyle = `rgba(255,255,255,${b.labelAlpha ?? 0.35})`;
            ctx.font = 'bold 9px system-ui, -apple-system, sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(b.label, (b.x0 + b.x1) / 2, yTop + 10);
            ctx.restore();
          }
        }
      }
      for (const s of this.segments) {
        ctx.strokeStyle = s.stroke;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(s.x0, s.y0);
        ctx.lineTo(s.x1, s.y1);
        ctx.stroke();
      }
    });
  }
}

class ZonesPaneView implements ISeriesPrimitivePaneView {
  _bands: Band[] = [];
  _segments: Segment[] = [];
  zOrder(): SeriesPrimitivePaneViewZOrder { return 'bottom'; }
  renderer(): ISeriesPrimitivePaneRenderer | null {
    return new ZonesPaneRenderer(this._bands, this._segments);
  }
}

export class ZonesOverlay implements ISeriesPrimitive {
  private _view = new ZonesPaneView();
  private _data: ZonesData | null = null;
  private _params: SeriesAttachedParameter<any> | null = null;

  setData(data: ZonesData | null) {
    this._data = data;
    this._rebuild();
  }

  attached(params: SeriesAttachedParameter<any>) {
    this._params = params;
  }

  detached() {
    this._params = null;
  }

  updateAllViews() {
    this._rebuild();
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return [this._view];
  }

  private _rebuild() {
    const bands: Band[] = [];
    const segments: Segment[] = [];
    const p = this._params;
    const data = this._data;
    const hasAny =
      !!data &&
      (!!data.supply_zones?.length ||
        !!data.demand_zones?.length ||
        !!data.channels?.length ||
        !!data.cycle_windows?.length ||
        !!data.fib?.bands?.length ||
        !!(data.chart_patterns && data.chart_patterns.length));
    if (!p || !hasAny) {
      this._view._bands = bands;
      this._view._segments = segments;
      return;
    }

    const timeScale = p.chart.timeScale();
    const width = timeScale.width();
    const series = p.series;

    const yOf = (price: number): number | null =>
      series.priceToCoordinate(price) ?? null;

    const xOfMs = (ms: number): number => {
      const x = timeScale.timeToCoordinate(Math.floor(ms / 1000) as any);
      return x ?? -10;
    };

    // ── Bitcoin cycle boxes (full height, drawn first = furthest back) ──
    for (const w of data.cycle_windows ?? []) {
      const x0 = xOfMs(w.startMs);
      const x1 = w.endMs >= Date.now() ? width : xOfMs(w.endMs);
      if (x1 <= x0 && !w.projected) continue;
      const fill = w.phase === 'bull'
        ? (w.projected ? COLORS.cycleBullProjected : COLORS.cycleBull)
        : (w.projected ? COLORS.cycleBearProjected : COLORS.cycleBear);
      bands.push({ x0, x1: Math.max(x1, x0 + 1), yTop: 0, yBot: 0, fill, fullHeight: true, edge: true,
        label: w.phase === 'bull' ? 'BULLISH CYCLE' : 'BEARISH CYCLE',
        labelAlpha: w.projected ? 0.22 : 0.40,
      });
    }

    // ── Supply / Demand rectangles ─────────────────────────────
    for (const z of [...(data.demand_zones ?? []), ...(data.supply_zones ?? [])]) {
      const yTop = yOf(z.high);
      const yBot = yOf(z.low);
      if (yTop === null || yBot === null) continue;
      bands.push({
        x0: Math.max(-10, xOfMs(z.createdTime)),
        x1: width,
        yTop,
        yBot,
        fill: z.type === 'demand' ? COLORS.demandFill : COLORS.supplyFill,
      });
      // Far-edge highlight lines so tested vs fresh reads at a glance.
      const edgeStroke = z.type === 'demand' ? COLORS.demandEdge : COLORS.supplyEdge;
      segments.push({ x0: Math.max(-10, xOfMs(z.createdTime)), x1: width, y0: yTop, y1: yTop, stroke: edgeStroke });
      segments.push({ x0: Math.max(-10, xOfMs(z.createdTime)), x1: width, y0: yBot, y1: yBot, stroke: edgeStroke });
    }

    // ── Fibonacci bands (premium / equilibrium / discount) ─────
    const fib = data.fib;
    if (fib?.bands?.length) {
      const prices = fib.bands
        .filter((b) => b.price != null)
        .map((b) => ({ ratio: b.ratio ?? 0, price: b.price! }))
        .sort((a, b) => b.price - a.price);
      if (prices.length >= 2) {
        const byRatio = (r: number) => prices.find((p) => Math.abs(p.ratio - r) < 1e-6)?.price ?? null;
        const top = prices[0].price;
        const bot = prices[prices.length - 1].price;
        const p382 = byRatio(0.382);
        const p618 = byRatio(0.618);

        if (top !== p382 && p382 !== null) {
          const yT = yOf(top); const yB = yOf(p382);
          if (yT !== null && yB !== null) bands.push({ x0: 0, x1: width, yTop: yT, yBot: yB, fill: COLORS.premium });
        }
        if (p382 !== null && p618 !== null) {
          const yT = yOf(p382); const yB = yOf(p618);
          if (yT !== null && yB !== null) bands.push({ x0: 0, x1: width, yTop: yT, yBot: yB, fill: COLORS.equilibrium });
        }
        if (p618 !== null && bot !== p618) {
          const yT = yOf(p618); const yB = yOf(bot);
          if (yT !== null && yB !== null) bands.push({ x0: 0, x1: width, yTop: yT, yBot: yB, fill: COLORS.discount });
        }

        for (const pr of prices) {
          const y = yOf(pr.price);
          if (y === null) continue;
          const isGolden = pr.ratio >= 0.38 && pr.ratio <= 0.62;
          segments.push({
            x0: 0, x1: width, y0: y, y1: y,
            stroke: isGolden ? COLORS.goldenLine : COLORS.fibLine,
          });
        }
      }
    }

    // ── Channel rails (straight fits across the window) ─────────
    for (const c of data.channels ?? []) {
      for (const pair of [[c.upperStart, c.upperNow], [c.lowerStart, c.lowerNow]] as const) {
        const yA = yOf(pair[0]);
        const yB = yOf(pair[1]);
        if (yA === null || yB === null) continue;
        segments.push({ x0: 0, x1: width, y0: yA, y1: yB, stroke: COLORS.channel });
      }
    }

    // ── Chart patterns ──────────────────────────────────────────
    for (const pat of (data.chart_patterns ?? []).slice(0, 3)) {
      const stroke = pat.broken
        ? COLORS.chartBroken
        : pat.signal === 'bullish'
          ? COLORS.chartBull
          : pat.signal === 'bearish'
            ? COLORS.chartBear
            : COLORS.chartNeutral;
      const dash = pat.broken ? [] : [6, 4];
      // For now segments are solid (fancy-canvas doesn't expose setLineDash per segment easily) — width varies.
      // Neckline / rectangle edges
      const levels: Array<{ price: number | null | undefined; width: number }> = [];
      if (pat.neckline != null) levels.push({ price: pat.neckline, width: pat.broken ? 2 : 1.5 });
      if (pat.top != null && pat.bottom != null) {
        levels.push({ price: pat.top, width: 1.2 });
        levels.push({ price: pat.bottom, width: 1.2 });
        // fill rectangle faintly
        const yTop = yOf(pat.top!);
        const yBot = yOf(pat.bottom!);
        if (yTop !== null && yBot !== null) {
          const fill = pat.signal === 'bullish' ? COLORS.chartFillBull : pat.signal === 'bearish' ? COLORS.chartFillBear : 'rgba(148,163,184,0.04)';
          bands.push({ x0: 0, x1: width, yTop, yBot, fill });
        }
      } else if (pat.top != null) levels.push({ price: pat.top, width: 1.2 });
      else if (pat.bottom != null) levels.push({ price: pat.bottom, width: 1.2 });
      if (pat.flag_high != null) levels.push({ price: pat.flag_high, width: 1.2 });
      if (pat.flag_low != null) levels.push({ price: pat.flag_low, width: 1.2 });
      if (pat.upper_now != null) levels.push({ price: pat.upper_now, width: 1.2 });
      if (pat.lower_now != null) levels.push({ price: pat.lower_now, width: 1.2 });
      for (const lv of levels) {
        if (lv.price == null) continue;
        const y = yOf(lv.price as number);
        if (y === null) continue;
        segments.push({ x0: 0, x1: width, y0: y, y1: y, stroke });
      }
      // wedge rails if present as line objects
      for (const rail of [pat.upper, pat.lower]) {
        if (!rail || typeof rail.m !== 'number' || typeof rail.b !== 'number') continue;
        // draw short segment across middle of pane using x->price mapping approximation: just draw horizontal levels above already covers main use; skip precise rail projection for now
      }
      void dash; // keep until dashed rendering wired
    }

    this._view._bands = bands;
    this._view._segments = segments;
  }
}

/** Attach the overlay to a candlestick series. */
export function attachZonesOverlay(
  _series: ISeriesApi<'Candlestick'>,
  _chart: IChartApi,
): ZonesOverlay {
  const overlay = new ZonesOverlay();
  (_series as any).attachPrimitive(overlay);
  return overlay;
}
